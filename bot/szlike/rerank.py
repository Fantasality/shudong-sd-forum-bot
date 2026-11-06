"""确定性候选重排器（Best-of-N 的轻量版）。

同一条帖子生成 2-3 个候选评论/回复/帖子后，用「从真实语料统计出来的人味特征」
挑最像真人的那条，把 L1 图灵被抓率从 ~30% 往 5 选 1 随机基线 20% 压。

为什么不用 LLM 自评当裁判
-------------------------
LLM 自评会把文本推向「平均、安全、书面、句句完整」，反而更像 AI。
本模块是纯统计 / 确定性：
    * 零 LLM 调用
    * 零第三方依赖（仅标准库 + sqlite3 读 kb/raw.db）
    * 不奖励「安全、通用、正确」，只奖励真实语料里常见的
      「具体、随意、不完整、带社区黑话」的表达。

主代理接线只需四个函数：
    fit_profile(kind) -> dict          统计该 kind 的人味特征分布（结果模块内缓存）
    score(text, kind, profile=None)    给单条文本打 0-1 人味分
    pick_best(candidates, kind, seed) 挑分最高者；同分按 seed 稳定 hash 打破平局
    rank(candidates, kind)            返回 [(text, score), ...] 便于调试/验收

离线验收（不调 LLM）：直接 `PYTHONIOENCODING=utf-8 python szlike/rerank.py`。
"""
from __future__ import annotations

import hashlib
import math
import re
import sqlite3
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "kb" / "raw.db"

# ---------------------------------------------------------------- 语料切分
# 与 kb_analyze.py 保持同一口径，保证统计可与体检报告对齐。
CJK = re.compile(r"[\u4e00-\u9fff]+")
BRACKET = re.compile(r"\[[^\[\]]{1,8}\]")                  # 平台贴纸 [流泪][摸头]
EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2764]")
LONGNUM = re.compile(r"\d{5,}")                             # 联系方式，不算「具体名词」

# 具体名词锚点：楼 / 课 / 食堂 / 价格 / 时间。命中即「真实、有细节」。
_CONCRETE_RES = [re.compile(p) for p in (
    r"[A-Za-z]?\d{1,2}\s*(?:号|栋|楼|舍|幢|教|食堂)",
    r"\d+\s*(?:r|R|块|元|毛|币|米|分钟|小时|点|周|号|人|次|份|页|题)",
    r"[东西南北中]?\d{0,2}(?:食堂|教学楼|宿舍楼|图书馆|快递站|菜鸟|公交站|地铁站|自习室)",
    r"周[一二三四五六日天末]|早八|晚课|期中|期末|开学|放假|四六级|考研|体测|选课|抢课|"
    r"绩点|学分|专业课|通识课|选修|必修|作业|ddl|DDL|小组|答辩|实习|社团",
    r"\d{1,2}\s*[点时:：]\s*\d{0,2}|\d+月\d+|\d+号|大[一二三四]|研[一二三]",
    r"(?:食堂|外卖|快递|奶茶|咖啡|烧烤|火锅|窗口|美团|饿了么|教材|资料|二手|转让|拼单|代拿|代课)",
)]

# 书面 / AI 腔词与套话。命中越多越不像真人。
_FORMAL = (
    "确实", "建议", "希望", "值得", "首先", "其次", "再者", "综上", "综上所述",
    "总而言之", "总的来说", "一方面", "另一方面", "不仅", "而且", "此外", "因此",
    "由此可见", "值得注意的是", "需要注意的是", "事实上", "客观上", "某种程度上",
    "某种意义上", "换句话说", "换言之", "也就是说", "归根结底", "说到底", "本质上",
    "希望对你有帮助", "仅供参考", "不妨", "可以尝试", "祝你", "愿你", "加油哦",
    "加油呀", "一起加油", "治愈", "温柔", "力量", "成长", "接纳", "拥抱", "和解",
    "不失为", "何尝不是", "无可厚非", "理性看待", "结合自身", "多角度", "各个方面",
    "保持心态", "属于自己的答案", "更是一种", "人生态度",
)
_FORMAL_RE = re.compile("|".join(map(re.escape, _FORMAL)))
# 最典型的客服 / 鸡汤 / 升华套话，额外重扣。
_HARD = (
    "希望对你有帮助", "综上所述", "总而言之", "总的来说", "值得注意的是", "需要注意的是",
    "归根结底", "本质上", "不失为", "何尝不是", "无可厚非", "祝你", "愿你", "加油哦",
    "加油呀", "一起加油", "仅供参考", "人生态度", "属于自己的答案", "更是一种",
)
_HARD_RE = re.compile("|".join(map(re.escape, _HARD)))
# 破折号后面的「升华金句」判定词（只在 —— 之后检测，避免误伤真人的列举式破折号）。
_SUBLIME = (
    "成长", "意义", "人生", "本质", "真正的", "才是", "治愈", "力量", "活成", "和解",
    "拥抱", "答案", "最好的自己", "修行", "温柔地", "学会接纳",
)
# 典型 AI / 书面开头（命中即判低分，且常与 FORMAL 共现）。
_AI_OPENERS = (
    "确实", "建议", "希望", "作为", "对于", "关于", "首先", "总之", "综上", "总的",
    "值得", "不妨", "其实", "客观", "从某", "某种", "欢迎", "如有", "仅供", "祝", "愿",
    "综上", "总的", "无疑", "不妨说",
)

# 句末标点类别。真人大量「无标点 / 省略号 / 波浪号 / 贴纸收尾」，AI 几乎必带句号。
_NONE = 0
_PERIOD = 1
_EXCL = 2
_QUEST = 3
_ELLIPSIS = 4
_WAVE = 5
_BRACKET = 6
_OTHER = 7
_END_LABELS = ("none", "period", "excl", "quest", "ellipsis", "wave", "bracket", "other")

_WEIGHTS = {
    "length": 0.18,
    "emoji": 0.07,
    "end_punct": 0.10,
    "concrete": 0.12,
    "opener": 0.08,
    "trigram": 0.25,
    "formal": 0.20,
}

# 模块内缓存：语料只读一次库；profile 每种 kind 只算一次。
_CORPUS_CACHE: dict[str, list[str]] = {}
_PROFILE_CACHE: dict[str, dict] = {}
_TRIGRAM_TOP = 400


# ---------------------------------------------------------------- 语料加载
def _load_all() -> None:
    """一次查库，把三路语料读进内存并按 kind 分组缓存。"""
    posts: list[str] = []
    comments: list[str] = []
    if DB.exists():
        con = sqlite3.connect(str(DB))
        try:
            for (c,) in con.execute(
                    "SELECT content FROM posts WHERE content IS NOT NULL AND content != '' ORDER BY id"):
                posts.append(c)
            for (c,) in con.execute(
                    "SELECT content FROM comments WHERE content IS NOT NULL AND content != '' ORDER BY id"):
                comments.append(c)
        finally:
            con.close()
    # reply：raw.db 的 comments.is_sub 全为 0，没有独立楼中楼语料。
    # 用「评论中长度 <= 全量 p75 的短文本子集」作近似：保留「回复比评论短」的倾向，
    # 又不至于把 15-25 字的正常回复误判成超长。仍 100% 来自真实语料、确定性。
    # report 里如实说明这是 proxy。
    if comments:
        lens = sorted(len(c) for c in comments)
        p75 = lens[int(len(lens) * .75)]
        reply = [c for c in comments if len(c) <= p75]
    else:
        reply = []
    _CORPUS_CACHE["post"] = posts
    _CORPUS_CACHE["comment"] = comments
    _CORPUS_CACHE["reply"] = reply


def _corpus(kind: str) -> list[str]:
    if not _CORPUS_CACHE:
        _load_all()
    return _CORPUS_CACHE.get(kind, [])


def _quantile(sorted_vals: list[int], p: float) -> int:
    if not sorted_vals:
        return 0
    return sorted_vals[min(len(sorted_vals) - 1, int(len(sorted_vals) * p))]


def _end_cat(t: str) -> int:
    if not t:
        return _NONE
    if t.endswith("]"):
        return _BRACKET
    ch = t[-1]
    if ch in "。.":
        return _PERIOD
    if ch in "！!":
        return _EXCL
    if ch in "？?":
        return _QUEST
    if ch in "…~～" or t.endswith("...") or t.endswith("……"):
        return _ELLIPSIS if ch == "…" or t.endswith("...") or t.endswith("……") else _WAVE
    if ch.isalnum() or "\u4e00" <= ch <= "\u9fff":
        return _NONE
    return _OTHER


def _sticker_count(t: str) -> int:
    return len(BRACKET.findall(t)) + len(EMOJI.findall(t))


def _ngrams(texts: list[str], n: int, top: int) -> dict[str, int]:
    c: Counter[str] = Counter()
    for t in texts:
        for seg in CJK.findall(t):
            for i in range(len(seg) - n + 1):
                c[seg[i:i + n]] += 1
    items = [(g, k) for g, k in c.items() if k >= 3]
    items.sort(key=lambda x: (-x[1], x[0]))          # 频次降序、字典序兜底，确定性
    return {g: k for g, k in items[:top]}


def _neutral_profile(kind: str) -> dict:
    """DB 不存在时的确定性兜底（不查库、不崩 import）。"""
    p50 = {"post": 29, "comment": 12, "reply": 9}.get(kind, 12)
    q = {"p10": max(2, p50 // 2), "p25": max(3, int(p50 * .7)), "p50": p50,
         "p75": int(p50 * 1.8), "p90": int(p50 * 2.6), "mean": p50, "max": p50 * 4}
    end = {k: 0.0 for k in _END_LABELS}
    end["none"] = 1.0
    return {
        "kind": kind, "source": "neutral-fallback", "n": 0, "length": q,
        "sticker": {"rate": 0.0, "per_text": 0.0, "density": 0.0, "heavy_rate": 0.0},
        "end_punct": end,
        "concrete": {"rate": 0.0},
        "opener": {"unique_ratio": 0.0, "entropy_norm": 0.0, "top1_share": 0.0,
                   "top": [], "freq": {}, "max": 1},
        "trigram": {"n_types": 0, "grams": {}},
        "formal": {"hit_rate": 0.0, "top_markers": []},
    }


# ---------------------------------------------------------------- 主接口 1
def fit_profile(kind: str = "comment") -> dict:
    """从 kb/raw.db 真实语料统计该 kind 的人味特征分布，结果模块内缓存。

    kind: "post" | "comment" | "reply"（reply 用短评论子集近似楼中楼）。
    返回 dict 含：长度分位 / 表情率 / 句末标点分布 / 具体名词率 /
    开头词性多样性 / 3gram 词表 / 书面连接词命中率。
    """
    if kind in _PROFILE_CACHE:
        return _PROFILE_CACHE[kind]
    texts = _corpus(kind)
    if not texts:
        prof = _neutral_profile(kind)
        _PROFILE_CACHE[kind] = prof
        return prof

    n = len(texts)
    # --- 长度分位 ---
    lens = sorted(len(t) for t in texts)
    length = {"p10": _quantile(lens, .10), "p25": _quantile(lens, .25),
              "p50": _quantile(lens, .50), "p75": _quantile(lens, .75),
              "p90": _quantile(lens, .90), "mean": round(sum(lens) / n, 2), "max": lens[-1]}

    # --- 表情率 ---
    sticker_counts = [_sticker_count(t) for t in texts]
    total_sticker = sum(sticker_counts)
    total_char = sum(lens)
    sticker = {
        "rate": round(sum(1 for k in sticker_counts if k > 0) / n, 4),
        "per_text": round(total_sticker / n, 4),
        "density": round(total_sticker / max(1, total_char), 5),
        "heavy_rate": round(sum(1 for k in sticker_counts if k >= 3) / n, 4),
    }

    # --- 句末标点分布 ---
    end_cnt = Counter(_end_cat(t.strip()) for t in texts)
    end_punct = {_END_LABELS[i]: round(end_cnt.get(i, 0) / n, 4) for i in range(len(_END_LABELS))}

    # --- 具体名词（楼/课/食堂/价格/时间）命中率 ---
    def has_concrete(t: str) -> bool:
        clean = LONGNUM.sub("", t)          # 长数字是联系方式，不算「具体」
        return any(r.search(clean) for r in _CONCRETE_RES)
    concrete = {"rate": round(sum(1 for t in texts if has_concrete(t)) / n, 4)}

    # --- 开头词性多样性（开头 2 字） ---
    op_freq = Counter(t.strip()[:2] for t in texts if len(t.strip()) > 1)
    op_total = sum(op_freq.values()) or 1
    probs = [v / op_total for v in op_freq.values()]
    entropy = -sum(p * math.log(p) for p in probs if p > 0)
    max_entropy = math.log(len(op_freq)) if len(op_freq) > 1 else 1.0
    top_op = op_freq.most_common(20)
    opener = {
        "unique_ratio": round(len(op_freq) / n, 4),
        "entropy_norm": round(entropy / max_entropy, 4),
        "top1_share": round((top_op[0][1] / op_total) if top_op else 0.0, 4),
        "top": top_op,
        "freq": dict(op_freq),
        "max": max(op_freq.values()) if op_freq else 1,
    }

    # --- 3gram 与真实语料的重合词表 ---
    grams = _ngrams(texts, 3, _TRIGRAM_TOP)
    trigram = {"n_types": len(grams), "grams": grams}

    # --- 书面连接词命中率 ---
    hit_texts = 0
    marker_cnt: Counter[str] = Counter()
    for t in texts:
        found = _FORMAL_RE.findall(t)
        if found:
            hit_texts += 1
        marker_cnt.update(found)
    formal = {"hit_rate": round(hit_texts / n, 4),
              "top_markers": marker_cnt.most_common(15)}

    src = {"post": "posts", "comment": "comments",
           "reply": "comments(<=p75 short subset; is_sub all 0)"}.get(kind, kind)
    prof = {"kind": kind, "source": src, "n": n, "length": length, "sticker": sticker,
            "end_punct": end_punct, "concrete": concrete, "opener": opener,
            "trigram": trigram, "formal": formal}
    _PROFILE_CACHE[kind] = prof
    return prof


# ---------------------------------------------------------------- 打分部件
def _length_score(t: str, p: dict) -> float:
    L = len(t)
    q = p["length"]
    if L < 2:
        return 0.15                                   # 过短（<2 字）：信息量不足
    p50, p25, p75, p90 = q["p50"], q["p25"], q["p75"], q["p90"]
    sigma = max(1.5, (p75 - p25) / 1.349)            # IQR -> 近似标准差
    if L > p90 * 3:
        return 0.02                                   # 过长：几乎不可能是真人
    return math.exp(-0.5 * ((L - p50) / sigma) ** 2)


def _emoji_score(t: str, p: dict) -> float:
    k = _sticker_count(t)
    if k == 0:
        return 0.65                                   # 无表情：真人常见，轻微低于「贴合」
    if k >= 3 or (len(t) > 0 and k * 2 / len(t) > 0.3):
        return 0.25                                   # 刷表情 / 表情密度过高：重扣
    return 1.0                                        # 1-2 个表情：真实语料强人味信号


def _end_score(t: str, p: dict) -> float:
    dist = p["end_punct"]
    mx = max(dist.values()) or 1.0
    cat = _END_LABELS[_end_cat(t.strip())]
    return 0.30 + 0.70 * (dist.get(cat, 0.0) / mx)


def _concrete_score(t: str, p: dict) -> float:
    clean = LONGNUM.sub("", t)
    hits = sum(1 for r in _CONCRETE_RES if r.search(clean))
    if hits == 0:
        return 0.15                                   # 不含具体名词：不奖励（但不清零）
    return min(1.0, 0.45 + 0.28 * hits)


def _opener_score(t: str, p: dict) -> float:
    op = p["opener"]
    if any(t.startswith(w) for w in _AI_OPENERS):
        return 0.08                                   # AI / 书面开头：判低分
    freq = op.get("freq", {})
    mx = op.get("max", 1) or 1
    f = freq.get(t.strip()[:2], 0)
    return 0.35 + 0.65 * (math.log1p(f) / math.log1p(mx))


def _trigram_score(t: str, p: dict) -> float:
    grams = [seg[i:i + 3] for seg in CJK.findall(t) for i in range(len(seg) - 2)]
    if not grams:
        return 0.12                                   # 纯数字/字母/贴纸：只是「没 AI 味」，不算人味证据
    table = p["trigram"]["grams"]
    hits = sum(1 for g in grams if g in table)
    if hits == 0:
        return 0.02
    return min(1.0, 1.8 * hits / len(grams))


def _formal_score(t: str, p: dict) -> float:
    hits = len(_FORMAL_RE.findall(t))
    return 1.0 / (1.0 + 0.8 * hits)                  # 命中越多越不像真人


def _ai_penalty(t: str) -> float:
    """书面腔 / 升华句 / 破折号金句 / 排比 / 面面俱到的额外扣分（0-0.5）。"""
    pen = 0.0
    # 破折号升华金句：仅当 —— 之后出现抽象升华词时重扣；真人列举式破折号只轻扣。
    if "——" in t:
        tail = t.split("——")[-1]
        pen += 0.18 if any(w in tail for w in _SUBLIME) else 0.05
    # 排比 / 分点 / 面面俱到的结构
    if re.search(r"首先.*?其次|一方面.*?另一方面|不仅.*?而且|既.*?又|与其.*?不如", t, re.S):
        pen += 0.08
    n_period = t.count("。")
    n_comma = t.count("，")
    if n_period >= 2 and len(t) > 40:
        pen += 0.06                                   # 句句完整、多句铺陈
    if n_comma >= 3 and len(t) > 40:
        pen += 0.04
    # 客服 / 鸡汤套话
    pen += 0.05 * min(3, len(_HARD_RE.findall(t)))
    return min(0.5, pen)


# ---------------------------------------------------------------- 主接口 2
def score(text: str, kind: str = "comment", profile: dict | None = None) -> float:
    """给单条文本打 0-1 的人味分：越贴近真实语料分布越高。

    长度偏离中位、表情率失衡、书面腔 / 升华句 / 破折号金句明显扣分；
    奖励真实语料里常见的具体、随意、不完整、带社区黑话的表达。
    不奖励「安全、通用、正确」。
    """
    if profile is None:
        profile = fit_profile(kind)
    t = (text or "").strip()
    if len(t) < 2:
        # 过短（空 / 1 字）：信息量不足，不应靠「没有任何书面词」拿到中性分，直接判低分。
        return 0.1
    comps = {
        "length": _length_score(t, profile),
        "emoji": _emoji_score(t, profile),
        "end_punct": _end_score(t, profile),
        "concrete": _concrete_score(t, profile),
        "opener": _opener_score(t, profile),
        "trigram": _trigram_score(t, profile),
        "formal": _formal_score(t, profile),
    }
    base = sum(_WEIGHTS[k] * comps[k] for k in _WEIGHTS)
    s = base - _ai_penalty(t)
    return round(max(0.0, min(1.0, s)), 6)


# ---------------------------------------------------------------- 主接口 3
def _stable_key(text: str, seed: str) -> str:
    return hashlib.sha1((seed + "\x00" + text).encode("utf-8")).hexdigest()


def pick_best(candidates: list[str], kind: str = "comment", seed: str = "") -> str:
    """从候选里挑人味分最高者；同分按 seed 稳定 hash 打破平局，可复现。"""
    if not candidates:
        return ""
    if len(candidates) == 1:
        return candidates[0]
    profile = fit_profile(kind)
    scored = [(t, score(t, kind, profile)) for t in candidates]
    best = max(s for _, s in scored)
    tied = [t for t, s in scored if abs(s - best) < 1e-9]
    if len(tied) == 1:
        return tied[0]
    return min(tied, key=lambda t: _stable_key(t, seed))


# ---------------------------------------------------------------- 主接口 4
def rank(candidates: list[str], kind: str = "comment") -> list[tuple[str, float]]:
    """返回 [(text, score), ...] 按分数降序；同分保持输入顺序（稳定、确定性）。"""
    if not candidates:
        return []
    profile = fit_profile(kind)
    scored = [(t, score(t, kind, profile)) for t in candidates]
    return sorted(scored, key=lambda x: -x[1])


# ================================================================ 离线验收
def _dist(v: list[float]) -> dict:
    s = sorted(v)
    return {"n": len(s), "min": round(s[0], 3), "p10": round(_quantile(s, .10), 3),
            "p25": round(_quantile(s, .25), 3), "p50": round(_quantile(s, .50), 3),
            "p75": round(_quantile(s, .75), 3), "p90": round(_quantile(s, .90), 3),
            "max": round(s[-1], 3), "mean": round(sum(s) / len(s), 3)}


def _auc(real: list[float], ai: list[float]) -> tuple[float, float]:
    """P(真人分 > AI分) + 0.5*P(并列)，以及严格的「真人分 > AI分」占比。"""
    win = tie = 0
    total = len(real) * len(ai)
    for r in real:
        for a in ai:
            if r > a:
                win += 1
            elif r == a:
                tie += 1
    return (win + .5 * tie) / total, win / total


def _ai_texts(n: int = 200) -> list[str]:
    """确定性造 n 条典型 AI 腔文本：长、书面、排比、破折号升华、面面俱到、
    句句完整、带「确实/建议/希望对你有帮助」。故意混入少量具体词，避免只靠「有无数字」
    就能区分，让验收更硬。"""
    topics = ["选课", "期末复习", "食堂排队", "宿舍关系", "考研还是就业", "社团招新",
              "体测", "快递站", "兼职", "恋爱"]
    clauses = [
        "要先梳理清楚自己的优先级", "多和同学交流一下经验", "留意学校通知的具体时间",
        "别把压力都攒到最后", "给自己留一点缓冲的余地", "结合实际情况做判断",
        "周一早八先去教务系统蹲一下", "东17楼下的窗口可以问问", "25块以内的资料先看看",
    ]
    tmpl = [
        "确实，关于{topic}这件事，{a}；同时{b}。总的来说，{c}——这本身就是一种成长，希望对你有帮助。",
        "关于{topic}，我的建议是：一方面{a}，另一方面{b}。值得注意的是，{c}。希望你结合自身情况理性看待，祝好。",
        "首先，{topic}需要{a}；其次，{b}；最后，{c}。综上所述，保持平和的心态、学会接纳自己才是最重要的，希望对你有帮助。",
        "对于{topic}，不妨{a}，也可以尝试{b}。从某种意义上说，{c}。愿你在这个过程中找到属于自己的答案。",
        "确实，{a}。此外，{b}。更重要的是，{c}——这不仅关乎{topic}，更是一种人生态度，值得认真对待。",
    ]
    out = []
    for i in range(n):
        out.append(tmpl[i % len(tmpl)].format(
            topic=topics[i % len(topics)],
            a=clauses[(i * 3) % len(clauses)],
            b=clauses[(i * 5 + 1) % len(clauses)],
            c=clauses[(i * 7 + 2) % len(clauses)],
        ))
    return out


def _human_texts(n: int = 200) -> list[str]:
    """从 kb/raw.db 等距确定性抽 n 条真实评论（不 RANDOM，保证两次运行完全一致）。"""
    all_c = _corpus("comment")
    if not all_c:
        return []
    step = max(1, len(all_c) // n)
    return all_c[::step][:n]


def _demo() -> None:
    print("=" * 78)
    print("rerank.py 离线验收（零 LLM 调用 / 零第三方依赖 / 纯确定性）")
    print("=" * 78)

    # ---- profile 摘要 ----
    print("\n[1] 各 kind 人味画像（来自 kb/raw.db 真实语料）")
    for kind in ("post", "comment", "reply"):
        p = fit_profile(kind)
        q = p["length"]
        print(f"  {kind:7s} n={p['n']:<6d} src={p['source']}")
        print(f"          长度 p10/25/50/75/90 = {q['p10']}/{q['p25']}/{q['p50']}/{q['p75']}/{q['p90']} 字")
        print(f"          表情率={p['sticker']['rate']} 具体名词率={p['concrete']['rate']} "
              f"书面词命中率={p['formal']['hit_rate']} 3gram类型={p['trigram']['n_types']}")
        print(f"          句末分布 none={p['end_punct']['none']} period={p['end_punct']['period']} "
              f"bracket={p['end_punct']['bracket']} | 开头多样性(熵)={p['opener']['entropy_norm']} "
              f"top1={p['opener']['top1_share']}")

    # ---- 真人组 vs AI 组 ----
    print("\n[2] 区分度验收：真人组(200 条真实评论) vs AI 组(200 条典型 AI 腔)")
    humans = _human_texts(200)
    ais = _ai_texts(200)
    hs = [score(t, "comment") for t in humans]
    as_ = [score(t, "comment") for t in ais]
    dh, da = _dist(hs), _dist(as_)
    auc, strict = _auc(hs, as_)
    print(f"  真人组分布 {dh}")
    print(f"  AI  组分布 {da}")
    print(f"  真人均值 {dh['mean']} 中位 {dh['p50']}   |   AI 均值 {da['mean']} 中位 {da['p50']}")
    print(f"  AUC(P(真人>AI)+0.5P(并列)) = {auc:.4f}   |   「真人分 > AI 分」占比 = {strict:.4f}")
    print(f"  真人>AI 的绝对差(均值) = {round(dh['mean'] - da['mean'], 3)}")

    # ---- 10 组三候选样例 ----
    print("\n[3] 10 组三候选样例（打印三分 + pick_best，★ 为被选中那条的判断）")
    cases = [
        ("comment", ["有没有选过这门课的uu，期末作业啥时候交啊[疑问]",
                     "关于这门课程的期末作业提交时间，建议你留意教务系统的正式通知，"
                     "也可以咨询任课老师，希望对你有帮助。",
                     "蹲一个，我也想知道"]),
        ("comment", ["确实，选课这件事需要结合自身情况理性看待，首先明确优先级，"
                     "其次多参考培养方案，最后保持平和心态——这本身就是一种成长。",
                     "别选早八，真的会谢[流泪]",
                     "我也蹲，上次抢课系统直接卡死"]),
        ("post", ["想问问东17楼下外卖一般放哪，之前那次找了二十分钟[晕了]",
                  "关于校园外卖配送地址的说明：建议同学们在下单前与骑手充分沟通，"
                  "确认具体的存放位置，同时留意取餐时间，避免不必要的等待，希望对你有帮助。",
                  "本部北区快递站是不是又搬了"]),
        ("comment", ["哈哈哈哈这个我懂，上次我也以为TXT是颜文字[滑稽]",
                     "确实，这种缩写容易产生歧义，建议在正式沟通中尽量使用完整表述，"
                     "以免造成误解，希望对你有帮助。",
                     "111"]),
        ("reply", ["啊这，我上周刚考完，重点全在最后两章",
                   "关于复习范围，我的建议是：一方面梳理老师课上强调的重点，"
                   "另一方面结合往年习题巩固，总的来说合理规划时间最重要。",
                   "同问+1"]),
        ("comment", ["食堂二楼那个窗口 12 块管饱，冲[期待]",
                     "综合考虑价格、口味与营养搭配，建议选择性价比更高的窗口，"
                     "同时注意膳食均衡，愿你吃得开心。",
                     "哪家啊，求个位置"]),
        ("post", ["周一早八两节找个小代，本部，25r，男女不限",
                  "因本周一上午课程时间冲突，现诚征一位同学代为上课，地点位于本部校区，"
                  "报酬面议，欢迎有意愿的同学与我联系，希望合作愉快。",
                  "有人代周三的高数吗 有偿"]),
        ("comment", ["分手。",
                     "面对感情的结束，不妨给自己一点时间慢慢消化，学会接纳当下的情绪，"
                     "从某种意义上说，这也是一种成长，愿你早日走出来。",
                     "摸摸[摸头]"]),
        ("reply", ["不是…老师你划的重点一个没考是什么意思…",
                   "确实，划重点与考试范围出现偏差时，建议先与任课老师确认，"
                   "再调整复习策略，避免做无用功，希望对你有帮助。",
                   "笑死，我这门也是"]),
        ("comment", ["太好了是密码学家我们有救了",
                     "综合来看，该现象值得从多个角度分析，不仅涉及技术层面，"
                     "也关乎 community 的沟通方式，综上所述保持开放心态尤为重要。",
                     "啊？"]),
    ]
    ok = 0
    for i, (kind, cands) in enumerate(cases, 1):
        sc = [score(t, kind) for t in cands]
        best = pick_best(cands, kind, seed=f"case{i}")
        bi = cands.index(best)
        # 最书面那条基本是最长的、含「建议/希望」的那条
        written = max(range(3), key=lambda j: len(cands[j]))
        reasonable = (bi != written)
        ok += reasonable
        print(f"\n  组{i:02d} kind={kind}  " + ("✓ 未选最书面" if reasonable else "✗ 选中了最书面条"))
        for j, (t, s) in enumerate(zip(cands, sc)):
            star = " <<< pick_best" if j == bi else ""
            print(f"    {'ABC'[j]} {s:.3f} {t[:34]}{'…' if len(t) > 34 else ''}{star}")
    print(f"\n  10 组中「未选最书面条」= {ok}/10")

    # ---- 两次一致性 ----
    print("\n[4] 确定性 / 可复现性（同一输入连跑两次）")
    run1 = rank(cases[0][1], "comment")
    run2 = rank(cases[0][1], "comment")
    p1 = pick_best(cases[0][1], "comment", seed="same-seed")
    p2 = pick_best(cases[0][1], "comment", seed="same-seed")
    print(f"  rank 两次完全一致: {run1 == run2}  ({[round(s, 3) for _, s in run1]})")
    print(f"  pick_best 两次完全一致: {p1 == p2}  -> {p1[:30]}")
    # 同分打破平局：三条「超长纯 ASCII」文本，开头 2 字相同、句末类别相同、
    # 长度相同、仅中段字母不同 -> 所有分量完全相同，严格同分。
    # 以此验证 seed 的 hash 确实参与平局裁决、且同 seed 可复现。
    tie = ["aa" + "x" * 198, "aa" + "y" * 198, "aa" + "z" * 198]
    ts = [round(score(t, "comment"), 3) for t in tie]
    a = pick_best(tie, "comment", seed="alpha")
    b = pick_best(tie, "comment", seed="alpha")
    c = pick_best(tie, "comment", seed="gamma")
    d = pick_best(tie, "comment", seed="s1")
    tag = lambda t: t[2]                                  # 用中段字母标识被选中者
    print(f"  三条同分候选分数: {ts}（严格相等: {ts[0] == ts[1] == ts[2]}）")
    print(f"  seed=alpha 复现: {a == b}  -> 选中中段字母 '{tag(a)}'")
    print(f"  不同 seed 打破平局: alpha='{tag(a)}' gamma='{tag(c)}' s1='{tag(d)}' "
          f"(三者互不相同: {len({tag(a), tag(c), tag(d)}) == 3})")

    print("\n" + "=" * 78)
    print("确认：零 LLM 调用、零第三方依赖（仅标准库 + sqlite3 读 kb/raw.db）。")
    print("=" * 78)


if __name__ == "__main__":
    _demo()
