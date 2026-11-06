"""冒烟模拟测试：让 AI 对着真实帖子生成评论/帖子，和真人写的对比打分。

为什么要有这个
--------------
改 prompt 靠感觉，改完不知道是变好还是变坏。这套脚本给一个 0-100 的分数，
每次改完跑一遍，数字涨了才算改对。

评测怎么做
----------
评论（--mode comment）：
    1. 图灵测试 40 分：AI 评论混进 4 条真实评论，让裁判挑「最不像这个论坛的人写的」。
       裁判没挑中 = 没露馅。38 个样本统计被抓率。
    2. 分布匹配 25 分：长度中位、表情率、句号率、换行率，跟真实评论的差。
    3. 同质化 20 分：38 条 AI 评论自己之间的开头重复、3-gram 重复。
    4. AI 味 15 分：书面语连接词、金句升华、客服腔。

帖子（--mode post）：
    1. 图灵测试 40 分（同上，混进真实帖子）
    2. 题材分布 25 分（对 raw.db 全量分布）
    3. 形式 20 分（长度、时间开头率、数字堆砌）
    4. AI 味 15 分

用法
----
    python eval_run.py --mode comment          # 跑评论评估
    python eval_run.py --mode post             # 跑帖子评估
    python eval_run.py --mode both             # 两个都跑
    python eval_run.py --mode comment --n 12   # 小样本快跑（调参时用）
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sqlite3
import statistics
import sys
import threading
import time
import zlib
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
BOT = HERE.parent                      # szlike_bot/ 或 bot/
sys.path.insert(0, str(BOT))

from szlike import brain as brain_mod          # noqa: E402
from szlike import quality                     # noqa: E402
from szlike import tasks as tasks_mod          # noqa: E402

TESTSET = HERE / "testset.json"
RAW = BOT / "kb" / "raw.db"
CONFIG = BOT / "config.json"
OUT = HERE / "eval_last.json"

# 固定考卷种子。只影响「抽哪 400 条参照帖」「每条 AI 文本配哪 4 条真人」，
# 不碰任何评分公式、权重、门槛。改这个数 = 换一套等难度考卷。
EVAL_SEED = 20261105
_SEED = EVAL_SEED

# 候选重排开关：默认 1（关闭，与旧版行为完全一致）。
# 设 2/3 时同一条生成多个候选，交给 szlike/rerank.py 的确定性人味打分挑选。
CANDIDATES = max(1, int(os.environ.get("EVAL_CANDIDATES", "1")))

# 第三方网关常撑不住 8 并发 / 大 max_tokens / 25s×2 重试。
# 默认仍是 8（不改变现有行为），临时模型考试时可用 SZLIKE_LLM_WORKERS=2 降并发。
WORKERS = max(1, int(os.environ.get("SZLIKE_LLM_WORKERS", "8")))

# 逐题答卷：eval_last.json 只存 samples 前 10-12 条，导不出完整答卷。
# 这里按题记录「试卷题目 / 人类标准答案 / AI 答案 / 图灵判定」，
# main() 结束时写 tools/exam_answers.json，供 WebUI 导出。
# 绝不含 api_key 或账号凭据。
_ANSWERS: list[dict] = []
ANSWERS_OUT = HERE / "exam_answers.json"

# 旧版把「图灵裁判调用失败」当成「没被抓」：b.chat() 返回 None（限流/超时/
# 网关故障）或抛异常时 return False，生成失败率守卫又只看生成、不看裁判。
# 实测 62 次 429 曾把 reply 图灵分推到 37.4/40、总分 90.3，全是假分。
# 用独立哨兵区分：None=无效样本（参照不足/裁判没给有效序号），
# _JUDGE_FAIL=裁判调用失败（必须剔除并计入失败率守卫）。
_JUDGE_FAIL = "JUDGE_FAIL"

# 以后的答卷要显示「发给模型的原输入」，便于调试向量检索。
# 默认关闭；SZLIKE_EXAM_RECORD_INPUT=1 开启后写进逐题答卷的 model_input。
# 本次入仓的两份答卷不含该字段（用户明确说「这次就算了」）。
RECORD_INPUT = os.environ.get("SZLIKE_EXAM_RECORD_INPUT") == "1"
_INPUT_LOG: dict[str, dict] = {}
_INPUT_LOCK = threading.Lock()


def _note_input(system, user, raw, cleaned) -> None:
    """生成结束时登记本次 system/user；key 同时挂原始输出与清洗后文本。"""
    if not RECORD_INPUT:
        return
    p = {"system": system or "", "user": user or ""}
    with _INPUT_LOCK:
        if raw:
            _INPUT_LOG[raw] = p
        if cleaned:
            _INPUT_LOG[cleaned] = p


def _model_input_field(text) -> dict:
    if not RECORD_INPUT or not text:
        return {}
    with _INPUT_LOCK:
        p = _INPUT_LOG.get(text)
    return {"model_input": p} if p else {}


def _safe_base_url(url: str) -> str:
    """答卷不暴露临时接入的第三方网关地址。

    用户要求：只展示 DeepSeek 与 Gemini 两套模型的答卷，README 给引用，
    但不得暴露临时网关地址。模型名照常保留，便于两套答卷对照。
    """
    u = url or ""
    if "api.deepseek.com" in u:
        return "https://api.deepseek.com/v1"
    if not u:
        return ""
    return "third-party OpenAI-compatible gateway (redacted)"

# 按类型选择性重排，依据 2026-11-06 固定考卷 A/B（c1 vs c3）：
#   post   80.0 -> 88.4（被抓 21.7% -> 15.0%）   收益 +8.4
#   reply  79.3 -> 86.2（被抓 32.6% -> 17.4%）   收益 +6.9
#   comment 83.6 -> 80.3（被抓 30.9% 不变），3gram 重复 0.041->0.066、
#           句号率 0.22->0.10，重排让短评同质化，是净负收益 -> 不启用。
# 不是拍脑袋挑好看的数：只在实测有收益的 kind 上开。
_RERANK_KINDS = {"post": 3, "reply": 3}


# ---------------------------------------------------------------- 统一生成管线
def _postprocess(txt: str, kind: str, plan: dict, scene: str = "") -> str:
    """线上与评估共用同一条后处理链。

    历史问题：评估只调三步（erase_hedges / trim_periods / ensure_sticker），
    线上是五步（多 erase_ai_flourish 与 fix_period_rate），考的跟跑的不是同一份
    代码。tasks.postprocess_text 存在就优先走它，不存在则回退旧链，
    保证并行改造期间评估仍能跑。
    """
    # A/B 开关：EVAL_LEGACY_POSTPROCESS=1 时强制走旧三步链，
    # 用来隔离「统一后处理」与「候选重排」各自的贡献。
    legacy = os.environ.get("EVAL_LEGACY_POSTPROCESS") == "1"
    fn = None if legacy else getattr(tasks_mod, "postprocess_text", None)
    if callable(fn):
        try:
            out = fn(txt, kind, plan, scene)
            if isinstance(out, str) and out.strip():
                return out
        except Exception:
            pass
    t = tasks_mod.erase_hedges(txt)
    t = tasks_mod.trim_periods(t)
    return tasks_mod.ensure_sticker(t, plan, scene)


def _best_of(cands: list[str], kind: str, seed: str) -> str:
    """候选重排；rerank 模块未就绪时取第一条，绝不静默丢样本。"""
    cands = [c for c in cands if c and c.strip()]
    if not cands:
        return ""
    if len(cands) == 1:
        return cands[0]
    try:
        from szlike import rerank as _rr
        return _rr.pick_best(cands, kind, seed) or cands[0]
    except Exception:
        return cands[0]


def _chat_multi(ctx, system: str, user: str, mt: int, kind: str,
                seed: str, n: int = 0) -> str:
    """生成 n 个候选并挑最像真人的一条；n<=1 时行为与原来完全一致。"""
    k = CANDIDATES if n <= 0 else n
    if k > 1:
        k = _RERANK_KINDS.get(kind, 1)
    if k <= 1:
        return ctx.brain.chat(system, user, max_tokens=mt)
    raws = []
    for _ in range(k):
        try:
            raws.append(ctx.brain.chat(system, user, max_tokens=mt))
        except Exception:
            continue
    return _best_of(raws, kind, f"{seed}|{k}")

# ---------------------------------------------------------------- AI 味

# 书面语/客服腔。真人在论坛里不说这些。
_AI_WORDS = (
    "其实", "总的来说", "总而言之", "值得注意的是", "某种程度上", "不得不说",
    "确实", "建议你", "首先", "其次", "最后", "综上", "另一方面", "与此同时",
    "可以看出", "由此可见", "希望对你有所帮助", "希望对你有帮助", "加油哦",
    "你可以试试", "不妨", "值得一提的是", "换句话说", "也就是说",
)
# 金句/升华：结尾突然拔高，是最容易被认出的一条
_AI_PUNCH = re.compile(
    r"(不是.{1,12}而是|本质上|归根到底|说到底|真正重要的是|这才是|"
    r"愿你|希望我们|生活就是|人生|成长就是|规则|逻辑|底层)")

# 客服/助手腔：给建议的结构化表达
_AI_SERVICE = re.compile(
    r"(建议你|你可以考虑|步骤如下|第一[，、]|第二[，、]|首先要|其次要|"
    r"另外你|以及|综上)")


def ai_flavor(texts: list[str]) -> tuple[float, list[str]]:
    """返回 (0-1 的干净度, 命中的问题列表)。1 = 没有 AI 味。"""
    if not texts:
        return 1.0, []
    hits: list[str] = []
    bad = 0
    for t in texts:
        n = sum(1 for w in _AI_WORDS if w in t)
        if n:
            bad += 1
            hits.append(f"书面词×{n}: {t[:26]}")
        elif _AI_PUNCH.search(t):
            bad += 1
            hits.append(f"金句: {t[:26]}")
        elif _AI_SERVICE.search(t) and len(t) > 60:
            bad += 1
            hits.append(f"客服腔: {t[:26]}")
    return max(0.0, 1 - bad / len(texts)), hits[:6]


# ---------------------------------------------------------------- 图灵测试

_JUDGE_SYS = (
    "你是苏州大学校园论坛的资深用户。下面给你一条帖子和几条评论，"
    "其中有一条是机器写的，其余是真人写的。"
    "请你只输出机器写的那条的序号（一个数字），不要解释。"
)


def turing_case(b: brain_mod.Brain, post: str, real: list[str],
                fake: str, rng: random.Random) -> bool | None | str:
    """True=被抓；False=没被抓；None=无效样本须剔除；_JUDGE_FAIL=裁判调用失败。

    旧版在 b.chat() 返回 None 或抛异常时 return False，等于把「裁判挂了」
    计成「没被抓」，限流时凭空推高图灵分。现在三种失败各归各位。
    """
    pool = [r for r in real if r.strip() and r.strip() != fake.strip()]
    rng.shuffle(pool)
    picks = pool[:4]
    if len(picks) < 3:
        return None          # 参照不足，这个样本无效，统计时要剔除
    items = picks + [fake]
    rng.shuffle(items)
    fake_idx = items.index(fake) + 1
    body = "\n".join(f"{i+1}. {t[:120]}" for i, t in enumerate(items))
    user = f"【帖子】{post[:300]}\n\n【评论】\n{body}\n\n哪一条是机器写的？只回答序号。"
    try:
        raw = b.chat(_JUDGE_SYS, user, max_tokens=8)
    except Exception:
        return _JUDGE_FAIL
    if raw is None:
        return _JUDGE_FAIL
    m = re.search(r"[1-5]", (raw or "").strip())
    if not m:
        return None          # 裁判没给有效序号，同样按无效样本剔除
    return int(m.group()) == fake_idx


# ---------------------------------------------------------------- 生成

def make_ctx(cfg: dict, acc: dict, b: brain_mod.Brain):
    """构造一个只用来构建 prompt 的 Ctx（client/store/guard 都不需要）。"""
    cfg2 = dict(cfg)
    cfg2["_account"] = acc
    return tasks_mod.Ctx(client=None, store=None, brain=b, guard=None,
                         cfg=cfg2, manual=True)


_FACT = {"bad": 0, "total": 0, "issues": []}


def _fact_check(txt: str, campus: str) -> None:
    """记一笔事实校验：食堂/地标是否属于这个校区、名字在不在真实清单里。"""
    if not txt:
        return
    _FACT["total"] += 1
    try:
        v = tasks_mod.facts.verify(txt, campus or "")
    except Exception:
        return
    if v:
        _FACT["bad"] += 1
        if len(_FACT["issues"]) < 8:
            _FACT["issues"].append((txt[:44], v))


def gen_comment(ctx, post: str, board: str, prev: list[str]) -> str | None:
    r = tasks_mod.build_comment_prompt(ctx, post, board, prev, "")
    if r["early"]:                      # 固定反应（「？」这种），不调模型
        return r["early"]
    if not r["enabled"]:
        return None
    try:
        raw = _chat_multi(ctx, r["system"], r["user"],
                          max(30, min(700, int(r["plan"]["hi"] * 2.4))),
                          "comment", post)
    except Exception:
        return None
    txt = tasks_mod._clean_content(raw or "", lo=2, hi=max(60, r["plan"]["hi"] * 2))
    if txt:
        txt = _postprocess(txt, "comment", r["plan"], r.get("scene") or "")
        # 线上发布后会 human.remember()，短回复池靠它去重。评估不写这条记录的话，
        # 同一批里会冒出好几个「蹲」，开头重复率 0.11，而真人基数是 0.012。
        try:
            tasks_mod.human.remember(txt, getattr(ctx, "account", ""))
        except Exception:
            pass
        _fact_check(txt, getattr(ctx, "campus", "") or "")
    _note_input(r["system"], r["user"], raw, txt)
    return txt


def gen_post(ctx, board: str = "", topic_hint: str = "") -> str | None:
    """生成一条帖子。走跟线上 scheduled_post 一样的路径：先抽真实话题，再写。

    早先直接拿板块名当话题，结果 24 条里 16 条在写食堂——因为板块里就有
    「今天吃什么」。线上其实是从 7.8 万条真实帖里随机取一条抽象成「场合｜事」。
    """
    from szlike import human, longform, topic
    tp = {}
    try:
        tp = topic.plan(ctx.brain, ctx.account, ctx.persona) or {}
    except Exception:
        tp = {}
    bd = tp.get("board") or board or "抛抛帖子"
    plan = {"subtopic_name": bd,
            "angle": tp.get("topic") or topic_hint or f"{bd}里常见的一件小事",
            "style": "", "kind": tp.get("kind") or "见闻", "seed": "",
            "seed_doc": tp.get("doc") or ""}
    pl = human.plan("post", bd, plan["angle"], ctx.rng("shape"))
    lf = None
    if plan["kind"] in ("堵", "恋爱"):
        lf = longform.plan_post(plan["angle"], {"mood": "孤独"}, ctx.rng("long"))
        if lf:
            pl = dict(pl, lo=lf["lo"], hi=lf["hi"], newline=True, longform="post")
    length_txt = longform.brief_post(lf) if lf else human.brief(pl)
    try:
        system, user, enabled = ctx.prompt(
            "post", topic=plan["subtopic_name"], board=plan["subtopic_name"],
            sample=f"{plan['subtopic_name']} {plan['angle']} {plan['style']}",
            length=length_txt, hint=human.sticker_hint(pl),
            long_text=bool(lf), **plan)
    except Exception:
        return None
    if not enabled:
        return None
    mt = 320 if not lf else min(2400, int(lf["hi"] * 2.6))
    try:
        raw = _chat_multi(ctx, system, user, mt, "post", plan["angle"])
    except Exception:
        return None
    txt = tasks_mod._clean_content(raw or "", lo=8, hi=400)
    # 线上发帖会剥掉开头的时间词，评估也要走这一步，否则测出来的是没剥的半成品
    txt = human.strip_time_opener(txt) if txt else None
    # 帖子旧链没有 ensure_sticker；只有统一入口就位时才走它，
    # 否则保持原样，避免回退链给帖子硬补表情造成回归。
    legacy = os.environ.get("EVAL_LEGACY_POSTPROCESS") == "1"
    fn = None if legacy else getattr(tasks_mod, "postprocess_text", None)
    if callable(fn) and txt:
        try:
            out = fn(txt, "post", pl, "")
            if isinstance(out, str) and out.strip():
                _note_input(system, user, raw, out)
                return out
        except Exception:
            pass
    _note_input(system, user, raw, txt)
    return txt


# ---------------------------------------------------------------- 打分

def _pct(x: float) -> float:
    return max(0.0, min(1.0, x))


def dist_score(gen: list[str], ref: list[str]) -> tuple[float, dict]:
    """长度/表情/句号/换行 四项分布接近度，各占 1/4。"""
    if not gen or not ref:
        return 0.0, {}
    detail = {}
    scores = []

    gm, rm = statistics.median(len(t) for t in gen), statistics.median(len(t) for t in ref)
    s = _pct(1 - abs(gm - rm) / max(rm, 1))
    scores.append(s); detail["长度中位"] = (round(gm, 1), round(rm, 1), round(s, 2))

    def rate(arr, pat):
        return sum(1 for t in arr if re.search(pat, t)) / len(arr)

    for name, pat in (("表情率", r"\[[^\]]{1,6}\]"), ("换行率", r"\n")):
        g, r = rate(gen, pat), rate(ref, pat)
        s = _pct(1 - abs(g - r) / max(r, 0.08))
        scores.append(s); detail[name] = (round(g, 2), round(r, 2), round(s, 2))

    # 句号只在长句里出现。生成的中位 18 字、真实中位 14 字，拿整体比永远比不过——
    # 18 字的真人也基本不收句号。只在 >25 字的段里比才公平。
    g_long = [t for t in gen if len(t) > 25]
    r_long = [t for t in ref if len(t) > 25]
    if g_long and r_long:
        g, r = rate(g_long, "。"), rate(r_long, "。")
        s = _pct(1 - abs(g - r) / max(r, 0.15))
        scores.append(s); detail["句号率(>25字)"] = (round(g, 2), round(r, 2), round(s, 2))
    else:
        scores.append(1.0); detail["句号率(>25字)"] = ("样本不足",)

    return sum(scores) / len(scores), detail


def dup_score(texts: list[str]) -> tuple[float, dict]:
    """内部重复度：开头 4 字重复 + 3-gram 重复。1 = 完全多样。"""
    if len(texts) < 2:
        return 1.0, {}
    opens = Counter(t[:4] for t in texts if t)
    open_dup = sum(n - 1 for n in opens.values() if n > 1) / len(texts)

    grams = Counter()
    for t in texts:
        seen = {t[i:i+3] for i in range(max(0, len(t) - 2))}
        grams.update(seen)
    total = sum(grams.values()) or 1
    gram_dup = sum(n - 1 for n in grams.values() if n > 1) / total

    s = _pct(1 - open_dup * 1.2 - gram_dup * 0.8)
    return s, {"开头重复率": round(open_dup, 3), "3gram重复率": round(gram_dup, 3)}


POST_TOPICS = {
    "食堂/吃": ("食堂", "饭", "菜", "鸡", "肉", "汤", "奶茶", "咖啡", "外卖", "煎", "烤", "串", "粉", "面包", "窗口", "吃"),
    "图书馆/自习": ("图书馆", "自习", "书", "闭馆", "座位", "占座", "借"),
    "雨/伞": ("雨", "伞", "淋", "湿"),
    "考试/作业/绩点": ("考试", "考", "作业", "期中", "期末", "复习", "错题", "刷题", "绩点", "保研", "挂科", "论文", "实验", "报告"),
    "宿舍/室友": ("宿舍", "室友", "寝室", "舍"),
    "车/充电": ("车", "充电", "桩", "插头", "电动车"),
    "恋爱/情感": ("喜欢", "分手", "男", "女", "朋友", "暗恋", "约", "对象"),
}


def topic_dist_score(gen: list[str], ref_posts: list[str]) -> tuple[float, dict]:
    """题材分布偏差。1 = 跟真实分布一致。

    只看正文前 30 字：真人在帖子后半段顺带提一句「后来在食堂吃饭」，
    不该算成食堂帖。早先按全文匹配，把「顺带提到」全算进去，
    生成长度更长就天然吃亏，比出来的差距不真实。
    """
    detail = {}
    penalties = 0.0
    for name, kws in POST_TOPICS.items():
        r = sum(1 for x in ref_posts if any(k in x[:30] for k in kws)) / max(1, len(ref_posts))
        g = sum(1 for x in gen if any(k in x[:30] for k in kws)) / max(1, len(gen))
        d = abs(g - r)
        penalties += d
        detail[name] = (round(r * 100, 1), round(g * 100, 1), round(d * 100, 1))
    # 平均偏差 10 个百分点算及格线
    return _pct(1 - (penalties / len(POST_TOPICS)) / 0.20), detail


# ---------------------------------------------------------------- 主流程

def load_ref(bot: Path, limit: int = 400) -> list[str]:
    """从 raw.db 抽真实帖子作分布参考（固定 seed，可复现）。

    原来用 SQL `ORDER BY RANDOM()`，每次考试换一批参照帖，题材分布分随参照系
    漂移 —— 实测同一版代码 post 在 76~86 之间跳，主要就是这里。现在读全量候选
    后用 random.Random(_SEED).sample 固定考卷；评分公式/权重/阈值一律不变。
    """
    conn = sqlite3.connect(RAW)
    rows = [r[0] for r in conn.execute(
        "SELECT content FROM posts WHERE length(content) BETWEEN 10 AND 300")]
    conn.close()
    return random.Random(_SEED).sample(rows, min(limit, len(rows)))


def _stable_rng(text: str, k: int) -> random.Random:
    """裁判配对的稳定随机源。

    Python 的 hash(str) 带进程随机盐，跨进程不可复现：同一版代码两轮会给同一条
    AI 文本配上不同的 4 条真人对照，图灵分跟着抖。改用 crc32 固定抽样。
    只影响抽样，不改评分公式或阈值。
    """
    return random.Random(zlib.crc32(
        f"{_SEED}|{k}|{text}".encode("utf-8")))


def run_comment(cfg: dict, cases: list[dict], b, n: int, rng) -> dict:
    accs = [(k, v) for k, v in (cfg.get("accounts") or {}).items()
            if (v or {}).get("enabled")]
    rng.shuffle(accs)
    if not accs:
        accs = [("eval", {"name": "eval", "persona": "warm", "label": "评测",
                          "campus": "独墅湖", "profile": "苏州大学学生"})]

    subset = cases[:n]
    jobs = []
    for i, c in enumerate(subset):
        name, acc = accs[i % len(accs)]
        acc = dict(acc, name=name)
        ctx = make_ctx(cfg, acc, b)
        prev = [x["text"] for x in c["real_comments"]][:5]
        jobs.append((c, ctx, prev))

    def work(j):
        c, ctx, prev = j
        return gen_comment(ctx, c["post"], c["board"], prev)

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        gens = list(ex.map(work, jobs))
    gen_time = time.time() - t0

    pairs = [(c, g) for (c, _, _), g in zip(jobs, gens) if g]
    texts = [g for _, g in pairs]
    # 生成失败率过高时直接停：API 余额不足/限流会让「剩下的样本」继续算分，
    # 算出来的 0% 是假的（实测过：余额耗尽后三轮都显示 0%，全是生成失败）。
    if len(texts) < len(subset) * 0.7:
        raise SystemExit(
            f"生成失败率过高：{len(subset)-len(texts)}/{len(subset)} 条没生成出来。"
            "最可能是 API 余额不足或限流，评估结果不可信，先修好再跑。")

    ref = [x["text"] for c in subset for x in c["real_comments"]]
    print(f"  生成 {len(texts)}/{len(subset)} 条，用时 {gen_time:.0f}s")

    # 1. 图灵
    # 每条约判两次再取平均：只判一次的话，同一版代码两轮能差 10-30 分，
    # 全是裁判的抽签，看不出改动到底有没有用。
    t0 = time.time()
    def _judge_twice(cg):
        post, real_txt, fake = cg[0]["post"], [x["text"] for x in cg[0]["real_comments"]], cg[1]
        return [turing_case(b, post, real_txt, fake, _stable_rng(fake, k))
                for k in range(2)]

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        verdicts = list(ex.map(_judge_twice, pairs))
    all_v = [x for v in verdicts for x in v]
    jf = sum(1 for x in all_v if x == _JUDGE_FAIL)
    if all_v and jf / len(all_v) > 0.30:
        raise SystemExit(
            f"图灵裁判失败率过高 {jf}/{len(all_v)}（>30%）：可能是限流/超时/网关故障。"
            "裁判失败不能算「没被抓」，拒绝输出虚高的图灵分。")
    if jf:
        print(f"  [!] 图灵裁判失败 {jf} 次，已剔除且不计入分母", flush=True)
    flat = [x for x in all_v if isinstance(x, bool)]
    caught_n = sum(1 for x in flat if x)
    total_n = len(flat)
    catch_rate = caught_n / max(1, total_n)
    turing = _pct(1 - catch_rate) * 40
    print(f"  图灵：{caught_n}/{total_n} 次被判（{catch_rate*100:.0f}%），用时 {time.time()-t0:.0f}s")

    for (c, ai), vs in zip(pairs, verdicts):
        ok = [x for x in vs if x is not None]
        _ANSWERS.append({
            "mode": "comment", "board": c.get("board") or "",
            "question": c.get("post") or "",
            "human_answers": [x.get("text", "") for x in (c.get("real_comments") or [])],
            "ai_answer": ai,
            **_model_input_field(ai),
            "turing_votes": len(ok), "turing_caught": sum(1 for x in ok if x),
        })

    ds, ddet = dist_score(texts, ref)
    dup, dupdet = dup_score(texts)
    clean, flavor_hits = ai_flavor(texts)

    dist = ds * 25
    dup_s = dup * 20
    flav = clean * 15
    total = turing + dist + dup_s + flav

    return {
        "mode": "comment", "n": len(texts), "total": round(total, 1),
        "turing": round(turing, 1), "catch_rate": round(catch_rate, 3),
        "dist": round(dist, 1), "dist_detail": ddet,
        "dup": round(dup_s, 1), "dup_detail": dupdet,
        "flavor": round(flav, 1), "flavor_hits": flavor_hits,
        "fact_total": _FACT["total"], "fact_bad": _FACT["bad"],
        "fact_rate": round(1 - _FACT["bad"] / max(1, _FACT["total"]), 3),
        "fact_issues": _FACT["issues"],
        "samples": texts[:12],
    }


def run_post(cfg: dict, b, n: int, rng) -> dict:
    accs = [(k, v) for k, v in (cfg.get("accounts") or {}).items()
            if (v or {}).get("enabled")] or [("eval", {"name": "eval", "persona": "warm"})]
    rng.shuffle(accs)
    boards = ["抛抛帖子", "今天不开心", "求助提问箱", "今天吃什么", "苏大迷惑大赏",
              "苏大恋爱日常", "打工电波", "新生专栏", "二手交易", "失物招领"]

    jobs = []
    for i in range(n):
        name, acc = accs[i % len(accs)]
        ctx = make_ctx(cfg, dict(acc, name=name), b)
        jobs.append((ctx, boards[i % len(boards)]))

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        gens = list(ex.map(lambda j: gen_post(j[0], j[1]), jobs))
    texts = [g for g in gens if g]
    print(f"  生成 {len(texts)}/{n} 条帖子，用时 {time.time()-t0:.0f}s")
    if len(texts) < n * 0.7:
        raise SystemExit(f"生成失败率过高：{n-len(texts)}/{n} 条没生成出来，先查 API 余额。")

    ref_posts = load_ref(BOT, 400)
    t0 = time.time()
    def _judge_twice(t):
        return [turing_case(b, "（下面这几条是同一条帖子标题下的正文）",
                            rng.sample(ref_posts, 4), t,
                            _stable_rng(t, k))
                for k in range(2)]

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        verdicts = list(ex.map(_judge_twice, texts))
    all_v = [x for v in verdicts for x in v]
    jf = sum(1 for x in all_v if x == _JUDGE_FAIL)
    if all_v and jf / len(all_v) > 0.30:
        raise SystemExit(
            f"图灵裁判失败率过高 {jf}/{len(all_v)}（>30%）：可能是限流/超时/网关故障。"
            "裁判失败不能算「没被抓」，拒绝输出虚高的图灵分。")
    if jf:
        print(f"  [!] 图灵裁判失败 {jf} 次，已剔除且不计入分母", flush=True)
    flat = [x for x in all_v if isinstance(x, bool)]
    caught_n = sum(1 for x in flat if x)
    total_n = len(flat)
    catch_rate = caught_n / max(1, total_n)
    turing = _pct(1 - catch_rate) * 40
    print(f"  图灵：{caught_n}/{total_n} 次被判（{catch_rate*100:.0f}%）")

    for ai, vs in zip(texts, verdicts):
        ok = [x for x in vs if x is not None]
        _ANSWERS.append({
            "mode": "post", "board": "",
            "question": "（开放式发帖：按抽到的真实话题写一条帖子）",
            "human_answers": ref_posts[:4],
            "human_answers_kind": "reference_posts（真实帖子，作为风格参照而非唯一标准答案）",
            "ai_answer": ai,
            **_model_input_field(ai),
            "turing_votes": len(ok), "turing_caught": sum(1 for x in ok if x),
        })

    td, tdet = topic_dist_score(texts, ref_posts)

    # 形式：长度、时间开头、数字堆砌
    lens = [len(t) for t in texts] or [0]
    ref_lens = [len(t) for t in ref_posts]
    lm = statistics.median(lens)
    rm = statistics.median(ref_lens)
    form_len = _pct(1 - abs(lm - rm) / max(rm, 1))
    time_open = sum(1 for t in texts if re.match(
        r"^(今天|昨天|昨晚|早上|上午|中午|下午|傍晚|晚上|凌晨)", t)) / max(1, len(texts))
    ref_time_open = sum(1 for t in ref_posts if re.match(
        r"^(今天|昨天|昨晚|早上|上午|中午|下午|傍晚|晚上|凌晨)", t)) / max(1, len(ref_posts))
    form_open = _pct(1 - abs(time_open - ref_time_open) / max(ref_time_open, 0.08))
    digit = sum(1 for t in texts if len(re.findall(r"\d", t)) >= 5) / max(1, len(texts))
    form_digit = _pct(1 - digit / 0.25)
    form = (form_len + form_open + form_digit) / 3 * 20

    dup, dupdet = dup_score(texts)
    dup_s = dup * 20 if False else 20 * dup   # 帖子不含同质化项，先并入形式
    clean, hits = ai_flavor(texts)

    total = turing + td * 25 + form + clean * 15
    return {
        "mode": "post", "n": len(texts), "total": round(total, 1),
        "turing": round(turing, 1), "catch_rate": round(catch_rate, 3),
        "topic": round(td * 25, 1), "topic_detail": tdet,
        "form": round(form, 1),
        "form_detail": {"长度中位": (round(lm, 1), round(rm, 1), round(form_len, 2)),
                        "时间开头率": (round(time_open, 2), round(ref_time_open, 2), round(form_open, 2)),
                        "数字堆砌率": (round(digit, 2), round(form_digit, 2))},
        "dup_detail": dupdet,
        "flavor": round(clean * 15, 1), "flavor_hits": hits,
        "samples": texts[:10],
    }


def run_reply(cfg: dict, b, n: int, rng) -> dict:
    """楼中楼回复评估。样本从 eval_replies.py 抓的真实楼中楼来。"""
    rpath = HERE / "replyset.json"
    if not rpath.exists():
        return {"mode": "reply", "total": 0, "err": "缺 replyset.json，先跑 eval_replies.py"}
    cases = json.loads(rpath.read_text(encoding="utf-8"))["cases"][:n or 999]

    accs = [(k, v) for k, v in (cfg.get("accounts") or {}).items()
            if (v or {}).get("enabled")] or [("eval", {"name": "eval", "persona": "warm"})]
    rng.shuffle(accs)

    jobs = []
    for i, c in enumerate(cases):
        name, acc = accs[i % len(accs)]
        ctx = make_ctx(cfg, dict(acc, name=name), b)
        jobs.append((c, ctx))

    def work(j):
        c, ctx = j
        thread = "\n".join(f"- {x}" for x in c.get("thread") or [])
        thread_txt = (f"\n\n【楼里已经说过的】\n{thread}" if thread else "")
        r = tasks_mod.build_reply_prompt(ctx, c["post"], c["their"], thread_txt,
                                         c.get("comment_id") or "")
        if not r["enabled"]:
            return None
        try:
            raw = _chat_multi(ctx, r["system"], r["user"],
                              max(20, min(160, int(r["plan"]["hi"] * 2))),
                              "reply", c["their"])
        except Exception:
            return None
        txt = tasks_mod._clean_content(raw or "", lo=2, hi=max(40, r["plan"]["hi"] * 2))
        if txt:
            txt = _postprocess(txt, "reply", r["plan"], r.get("scene") or "")
        _note_input(r["system"], r["user"], raw, txt)
        return txt

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        gens = list(ex.map(work, jobs))
    pairs = [(c, g) for (c, _), g in zip(jobs, gens) if g]
    texts = [g for _, g in pairs]
    print(f"  生成 {len(texts)}/{len(cases)} 条楼中楼，用时 {time.time()-t0:.0f}s")
    if len(texts) < len(cases) * 0.7:
        raise SystemExit(f"生成失败率过高：{len(cases)-len(texts)}/{len(cases)} 条没生成出来，先查 API 余额。")
    if not texts:
        return {"mode": "reply", "total": 0, "err": "生成全失败"}

    def _judge_twice(cg):
        c, fake = cg
        pool = [x for x in (c.get("real_replies") or []) if x.strip() and x.strip() != fake]
        if len(pool) < 3:
            pool = (c.get("real_replies") or [])[:4]
        return [turing_case(b, c["post"], pool, fake, _stable_rng(fake, k))
                for k in range(2)]

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        verdicts = list(ex.map(_judge_twice, pairs))
    all_v = [x for v in verdicts for x in v]
    jf = sum(1 for x in all_v if x == _JUDGE_FAIL)
    if all_v and jf / len(all_v) > 0.30:
        raise SystemExit(
            f"图灵裁判失败率过高 {jf}/{len(all_v)}（>30%）：可能是限流/超时/网关故障。"
            "裁判失败不能算「没被抓」，拒绝输出虚高的图灵分。")
    if jf:
        print(f"  [!] 图灵裁判失败 {jf} 次，已剔除且不计入分母", flush=True)
    flat = [x for x in all_v if isinstance(x, bool)]
    caught_n = sum(1 for x in flat if x)
    total_n = len(flat)
    catch_rate = caught_n / max(1, total_n)
    turing = _pct(1 - catch_rate) * 40
    print(f"  图灵：{caught_n}/{total_n} 次被判（{catch_rate*100:.0f}%）")

    for (c, ai), vs in zip(pairs, verdicts):
        ok = [x for x in vs if x is not None]
        _ANSWERS.append({
            "mode": "reply", "board": c.get("board") or "",
            "question": c.get("post") or "",
            "their": c.get("their") or "",
            "human_answers": list(c.get("real_replies") or []),
            "ai_answer": ai,
            **_model_input_field(ai),
            "turing_votes": len(ok), "turing_caught": sum(1 for x in ok if x),
        })

    ref = [x for c in cases for x in (c.get("real_replies") or [])]
    ds, ddet = dist_score(texts, ref)
    dup, dupdet = dup_score(texts)
    clean, hits = ai_flavor(texts)

    total = turing + ds * 25 + dup * 20 + clean * 15
    return {
        "mode": "reply", "n": len(texts), "total": round(total, 1),
        "turing": round(turing, 1), "catch_rate": round(catch_rate, 3),
        "dist": round(ds * 25, 1), "dist_detail": ddet,
        "dup": round(dup * 20, 1), "dup_detail": dupdet,
        "flavor": round(clean * 15, 1), "flavor_hits": hits,
        "samples": texts[:12],
    }


def main() -> None:
    global _SEED, CANDIDATES
    _ANSWERS.clear()
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["comment", "post", "reply", "both", "all"], default="all")
    ap.add_argument("--n", type=int, default=0, help="样本数，0=用测试集全部")
    ap.add_argument("--seed", type=int, default=EVAL_SEED,
                    help="考卷种子；同种子可复现，换种子=换一套等难度题")
    ap.add_argument("--candidates", type=int, default=CANDIDATES,
                    help="每条生成几个候选再确定性重排（1=关闭，与旧版一致）")
    args = ap.parse_args()
    _SEED = args.seed
    CANDIDATES = max(1, args.candidates)

    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    # 临时换模型考试：三个环境变量覆盖当前 llm 配置，绝不写回 config.json。
    # key 只存在于进程内存，不落盘、不打印、不进结果 JSON。
    # 供 WebUI「一键测试当前模型」和命令行临时 A/B 共用。
    llm = dict(cfg.get("llm") or {})
    for _env, _key in (("SZLIKE_LLM_BASE", "base_url"),
                       ("SZLIKE_LLM_KEY", "api_key"),
                       ("SZLIKE_LLM_MODEL", "model"),
                       ("SZLIKE_LLM_TIMEOUT", "timeout"),
                       ("SZLIKE_LLM_RETRIES", "retries")):
        _val = os.environ.get(_env)
        if _val:
            llm[_key] = _val
    if os.environ.get("SZLIKE_LLM_BASE") or os.environ.get("SZLIKE_LLM_KEY"):
        print(f"  [临时模型] {llm.get('model')} @ {llm.get('base_url')}（key 不显示）",
              flush=True)
    b = brain_mod.Brain(llm)
    data = json.loads(TESTSET.read_text(encoding="utf-8"))
    cases = data["cases"]
    rng = random.Random(args.seed)

    results = {}
    if args.mode in ("comment", "both", "all"):
        n = args.n or len(cases)
        print(f"\n===== 评论评估（{n} 条）=====")
        results["comment"] = run_comment(cfg, cases, b, n, rng)
    if args.mode in ("post", "both", "all"):
        # 40 条在一轮之间还能差 5 分，全是抽签。60 条才稳。
        n = args.n or 60
        print(f"\n===== 帖子评估（{n} 条）=====")
        results["post"] = run_post(cfg, b, n, rng)
    if args.mode in ("reply", "all"):
        n = args.n or 40
        print(f"\n===== 楼中楼评估（{n} 条）=====")
        results["reply"] = run_reply(cfg, b, n, rng)

    print("\n" + "=" * 56)
    for mode, r in results.items():
        print(f"\n【{mode}】总分 {r['total']}/100")
        print(f"  图灵 {r['turing']}/40（被抓 {r['catch_rate']*100:.0f}%）")
        if mode in ("comment", "reply"):
            print(f"  分布 {r['dist']}/25  {r['dist_detail']}")
            print(f"  同质 {r['dup']}/20  {r['dup_detail']}")
        else:
            print(f"  题材 {r['topic']}/25")
            for k, v in r["topic_detail"].items():
                print(f"      {k:14s} 真实{v[0]:5.1f}% 生成{v[1]:5.1f}% 差{v[2]:5.1f}")
            print(f"  形式 {r['form']}/20  {r['form_detail']}")
        if "fact_rate" in r:
            print(f"  事实 {r['fact_rate']*100:.0f}% 准确（{r['fact_total']-r['fact_bad']}/{r['fact_total']}）")
            for t, v in (r.get("fact_issues") or [])[:3]:
                print(f"      ⚠ {t}  ->  {v}")
        print(f"  AI味 {r['flavor']}/15")
        if r["flavor_hits"]:
            for h in r["flavor_hits"][:4]:
                print(f"      · {h}")
        print("  样本：")
        for s in r["samples"][:6]:
            print(f"      {s[:70]}")

    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    # 逐题答卷单独落盘：只记模型名与 base_url，不记 api_key
    ANSWERS_OUT.write_text(json.dumps({
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "seed": _SEED, "candidates": CANDIDATES,
        "model": llm.get("model", ""),
        "base_url": _safe_base_url(llm.get("base_url", "")),
        "n": len(_ANSWERS), "answers": _ANSWERS,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n结果已写入 {OUT}")
    print(f"逐题答卷已写入 {ANSWERS_OUT}（{len(_ANSWERS)} 题）")


if __name__ == "__main__":
    main()
