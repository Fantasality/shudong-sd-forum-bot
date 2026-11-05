"""拟人层：把「像人」从形容词变成一组可测的分布约束。

为什么需要它
------------
模型不是不会说人话，是它说的「人话」是**平均值**：长度永远 20 字上下、结尾永远有句号、
情绪永远给一句建议。而真实语料是这样分布的——

    评论中位 14 字，**31% 不到 9 个字**，只有 3.8% 以句号结尾，22.7% 带 [表情]。
    「总之」「综上」「需要注意的是」在 1.8 万条评论里出现 0~2 次。

所以判定「像不像人」不需要玄学，比分布就行。这个模块做三件事：

1. `plan()`  每次生成前按**真实长度分布抽样**一个目标长度，而不是写死 20 字。
              账号的句子长度因此有方差，跟真人一样忽长忽短。
2. `tells()` 用真实语料里近乎为零的词表给输出打「客服味」分，超标就重写一次。
3. `shape()` 结尾去句号、按情绪偶尔补一个 [表情]——这两步的真实占比是从语料里数出来的。
"""
from __future__ import annotations

import json
import random
import re
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
RAW = ROOT / "kb" / "raw.db"
STYLE = ROOT / "kb" / "style.json"

_lock = {"t": 0.0, "d": {}}

# 客服/AI 味词表。右边权重来自 18,276 条真实评论里的出现次数：
# 0 次 = 3 分（出现即判死），个位数 = 2 分，几十次 = 1 分（不是不能出现，是别扎堆）。
TELLS: list[tuple[str, int]] = [
    ("总的来说", 3), ("综上所述", 3), ("综上", 3), ("需要注意的是", 3),
    ("值得一提的是", 3), ("希望对你有所帮助", 3), ("希望这些建议", 3),
    ("首先", 2), ("其次", 2), ("最后，", 1), ("总之", 3),
    ("我理解你的感受", 3), ("感受到你的", 3), ("听起来你", 2),
    ("作为一个AI", 3), ("作为AI", 3), ("情绪价值", 2), ("共情", 2),
    ("建议你可以尝试", 3), ("你可以试试", 2), ("不妨试试", 2), ("建议你", 2),
    ("保持积极", 3), ("心态很重要", 3), ("会好起来的", 2), ("相信你一定", 3),
    ("祝你", 1), ("祝你好运", 2), ("加油哦", 2), ("抱抱你", 2),
    ("别担心", 1), ("注意身体", 2), ("一定要好好", 2),
    ("以下几个方面", 3), ("一一", 1), ("希望对你有帮助", 3),
    # 文案腔：这些词在 78,511 条真实帖里的出现率都低于 0.1%，模型却三天两头写。
    # 括号里是真实出现次数，权重按次数分三档。
    ("氤氲", 3), ("缱绻", 3), ("斑驳", 3), ("岁月静好", 3), ("人间烟火", 3),
    ("小确幸", 3), ("不由得", 3), ("顿了顿", 3), ("说不上来的", 3), ("说不出的", 3),
    ("岁月", 2), ("喧嚣", 2), ("慰藉", 2), ("怅然", 2), ("恍惚", 2), ("憧憬", 2),
    ("泛起", 2), ("某种", 2), ("一丝", 2), ("一抹", 2), ("涌上", 2), ("仿佛", 2),
    ("缓缓", 2), ("微微", 2), ("柔软", 2), ("——", 2), ("仪式感", 2),
    ("治愈", 1), ("时光", 1), ("心事", 1), ("碎碎念", 1), ("轻轻", 1), ("瞬间", 1),
]

# 平台真实存在的表情白名单（取自语料，流泪 22,918 次居首）。模型偶尔会自创
# [微笑] [捂脸] 这种平台没有的名字，发出去就是一串裸文本，必须挡掉。
STICKERS: tuple[str, ...] = ("流泪", "期待", "wink", "疑问", "爱心", "晕了", "生气",
                             "吐血", "摸头", "滑稽", "无语", "亲亲")
STICKER_RE = re.compile(r"\[([^\[\]]{1,6})\]")

# 极性别扭的表情：给「太对了！」配一个 [生气]，一眼就是机器拼的
NEG_STICKERS = {"流泪", "生气", "吐血", "无语", "晕了"}
POS_STICKERS = {"期待", "爱心", "亲亲", "摸头", "滑稽", "wink", "疑问"}

_POS = ("！", "!","哈哈", "太对", "真好", "好吃", "好看", "喜欢", "可爱", "开心", "期待",
        "谢谢", "可以", "冲", "棒", "赞", "笑死", "救命")
_NEG = ("难受", "崩溃", "崩", "哭", "烦", "累", "疼", "挂", "寄", "无语", "生气", "吐",
        "焦虑", "孤独", "失", "惨", "痛", "骂", "恶心", "离谱", "会谢", "麻了", "压力")


def polarity(text: str) -> int:
    """粗极性：+1 正向 / -1 负向 / 0 中性。只用来挡住明显别扭的表情组合。"""
    t = text or ""
    pos = sum(1 for w in _POS if w in t)
    neg = sum(1 for w in _NEG if w in t)
    if pos == neg:
        return 0
    return 1 if pos > neg else -1


SCENE_STICKER: dict[str, list[str]] = {
    "情绪低落": ["流泪", "摸头", "晕了"],
    "人际孤独": ["流泪", "摸头", "亲亲"],
    "家庭矛盾": ["流泪", "晕了"],
    "考试受挫": ["流泪", "吐血", "晕了"],
    "学业焦虑": ["流泪", "吐血", "期待"],
    "被冒犯": ["生气", "无语", "吐血", "滑稽"],
    "对校园管理不满": ["无语", "生气", "滑稽"],
    "恋爱困惑": ["期待", "爱心", "亲亲"],
    "被善意治愈": ["爱心", "摸头", "期待"],
    "身体不适": ["流泪", "摸头", "晕了"],
    "失物": ["期待", "疑问", "流泪"],
    "兼职打工": ["吐血", "流泪", "生气"],
    "": ["期待", "滑稽", "wink", "疑问"],
}


# ------------------------------------------------------------------ 方言统计
def _q(sorted_vals: list[int], p: float) -> int:
    if not sorted_vals:
        return 0
    i = min(len(sorted_vals) - 1, int(len(sorted_vals) * p / 100))
    return sorted_vals[i]


def build_style(path: Path = STYLE) -> dict:
    """从语料里数出真实分布，落成 kb/style.json。语料变了重跑即可。"""
    c = sqlite3.connect(RAW)
    out: dict = {"built_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    for kind, sql in (
        # 评论表没有板块字段，得回连帖子才能按板块统计长度
        ("comment", "SELECT c.content, p.sub_name FROM comments c "
                    "LEFT JOIN posts p ON p.id = c.post_id WHERE c.content IS NOT NULL"),
        ("post", "SELECT content, sub_name FROM posts WHERE content IS NOT NULL"),
    ):
        rows = [(t, b or "") for t, b in c.execute(sql)]
        lens = sorted(len(t) for t, _ in rows if t.strip())
        n = len(lens) or 1
        # 用分位点切 5 段，每段 20%——这样抽样出来的长度分布天然等于真人分布
        cuts = [0, _q(lens, 20), _q(lens, 45), _q(lens, 70), _q(lens, 88), _q(lens, 97)]
        bands = []
        for i in range(5):
            lo, hi = cuts[i], cuts[i + 1]
            if hi <= lo:
                continue
            seg = [t for t in (r[0] for r in rows) if max(1, lo) <= len(t) <= hi]
            srate = (sum(1 for t in seg if re.search(r"\[[^\]]{1,6}\]", t)) / len(seg)
                     if seg else 0.0)
            bands.append({"lo": max(1, lo), "hi": hi, "p": 0.2,
                          "sticker": round(srate, 4)})
        tot = sum(b["p"] for b in bands) or 1
        for b in bands:
            b["p"] = round(b["p"] / tot, 4)
        stick = sum(1 for t, _ in rows if re.search(r"\[[^\]]{1,6}\]", t))
        only = sum(1 for t, _ in rows if re.fullmatch(r"[\s\[][^\[\]]{1,6}\]", t.strip()))
        dot = sum(1 for t, _ in rows if t.rstrip().endswith("。"))
        emo = sum(1 for t, _ in rows if re.search(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", t))
        per_board: dict[str, list[int]] = {}
        for t, b in rows:
            if b and t.strip():
                per_board.setdefault(b, []).append(len(t))
        out[kind] = {
            "n": len(rows),
            "bands": bands,
            "p50": _q(lens, 50),
            "p90": _q(lens, 90),
            "sticker_rate": round(stick / n, 4),
            "sticker_only_rate": round(only / n, 4),
            "dot_rate": round(dot / n, 4),
            "emoji_rate": round(emo / n, 4),
            "board_p50": {b: _q(sorted(v), 50) for b, v in per_board.items() if len(v) >= 30},
        }
    c.close()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    _lock["t"] = 0.0
    return out


def style() -> dict:
    """带 mtime 缓存的读取；文件不存在就地生成一次。"""
    try:
        mt = STYLE.stat().st_mtime
    except FileNotFoundError:
        return build_style()
    if _lock["d"] and _lock["t"] == mt:
        return _lock["d"]
    try:
        d = json.loads(STYLE.read_text(encoding="utf-8"))
    except Exception:
        return build_style()
    _lock["t"], _lock["d"] = mt, d
    return d


# ------------------------------------------------------------------ 生成前
# 帖子形态。之前只有「长度」和「类型」，所以每条都长成同一副样子：逗号串、三句话、
# 一堆细节。真实帖子一半是碎片和一句话，只有一成是完整小段。按真实比例抽形态。
FORMS = [
    ("frag", 0.14, 6, 14, "只写一个片段，6-14 字，不交代前因后果，可以半句话"),
    ("one", 0.18, 10, 26, "一句话说完，10-26 字，不要第二句"),
    ("two", 0.14, 18, 40, "写两行，第二行单独一句，可以很短，中间换行"),
    ("ask", 0.12, 12, 30, "写成一个真问题，12-30 字，必须带疑问词（吗/哪/几/多少/怎么/能不能），像在问同学，不要自问自答"),
    ("rant", 0.14, 15, 35, "短吐槽，15-35 字，带一点情绪，但别喊口号"),
    ("list", 0.10, 20, 45, "写两三件相关的事各一句（都是同一件事的不同方面），中间换行，像随手记"),
    ("full", 0.18, 40, 90, "正常一段，40-90 字，最多两个具体细节"),
]
def plan(kind: str, board: str | None = None, scene: str | None = None,
         rng: random.Random | None = None) -> dict:
    """抽一个「这一次该怎么写」的形态：长度段、要不要表情、结尾怎么收。"""
    rng = rng or random.Random()
    st = (style().get(kind) or {})
    bands = st.get("bands") or [{"lo": 8, "hi": 20, "p": 1.0}]
    r, acc = rng.random(), 0.0
    band = bands[-1]
    for b in bands:
        acc += b["p"]
        if r <= acc:
            band = b
            break
    # 表情率按长度段取真值：语料里 1-6 字只有 3.0% 带表情，13 字以上才 31-34%。
    # 直觉上「短句更爱用表情」是错的，照直觉做会把短评写成一排表情。
    rate = float(band.get("sticker") if band.get("sticker") is not None
                 else st.get("sticker_rate") or 0.2)
    form = None
    if kind == "post":
        x, acc2 = rng.random(), 0.0
        for name, p, lo, hi, _txt in FORMS:
            acc2 += p
            if x <= acc2:
                form = (name, lo, hi)
                break
        form = form or ("one", 10, 26)
    return {
        "kind": kind,
        "form": form[0] if form else "",
        "lo": int(form[1]) if form else int(band["lo"]),
        "hi": int(form[2]) if form else int(band["hi"]),
        "sticker": rng.random() < min(0.5, rate),
        # 多行：两行/清单形态固定分段，其余按 14.9% 的真实比例
        "newline": kind == "post" and (form[0] in ("two", "list") if form else rng.random() < 0.15),
        "scene": scene or "",
        "board": board or "",
    }


def brief(pl: dict) -> str:
    """给模型的长度指令。帖子按形态给「最多多少字」，评论仍用区间。"""
    if pl.get("kind") == "post":
        form = pl.get("form") or "one"
        txt = next((f[4] for f in FORMS if f[0] == form), "")
        return (f"【形态】{txt}。长度大概 {int(pl['hi']) + 6} 字上下。"
                "先把这句话说完，不要数着字数写、不要断在半句。"
                "只保留跟这件事有关的事实（时间、地点、人物、结果），没有就不编。")
    lo, hi = pl["lo"], pl["hi"]
    if hi <= 8:
        tone = "极短，就几个字，像随手打的"
    elif hi <= 15:
        tone = "短句，一口气说完"
    elif hi <= 30:
        tone = "正常长度，别铺陈"
    else:
        tone = "可以多说两句，但别写成小作文"
    return f"【这次必须写 {lo}-{hi} 字】{tone}，不要超出这个长度太多。"


def sticker_hint(pl: dict) -> str:
    """表情提示要放在 user 里、并且比长度弱：放 system 末尾时模型会 60% 都加表情。

    只给白名单让它自己按语境挑——指定 [生气] 的话，它会在「太对了」后面也贴上。
    """
    if not pl.get("sticker"):
        return ""
    return ("（结尾合适的话可以带一个表情，只能用这些："
            + "".join(f"[{x}]" for x in STICKERS) + "；不合适就不加）")


NEG_SCENES = {"情绪低落", "人际孤独", "家庭矛盾", "考试受挫", "学业焦虑", "身体不适",
              "被冒犯", "对校园管理不满", "兼职打工"}
POS_SCENES = {"被善意治愈", "恋爱困惑"}


def _pick_sticker(scene: str) -> str:
    pool = SCENE_STICKER.get(scene or "", SCENE_STICKER[""])
    return random.choice(pool)


# ------------------------------------------------------------------ 生成后
def tells(text: str) -> list[tuple[str, int]]:
    return [(w, s) for w, s in TELLS if w in (text or "")]


def ai_score(text: str) -> int:
    return sum(s for _, s in tells(text))


def needs_rewrite(text: str) -> tuple[bool, str]:
    """够不够「客服」？够就返回一句重写指令。"""
    hit = tells(text)
    score = sum(s for _, s in hit)
    # 长文（建议/拆解档 100-260 字）以句号收尾是正常的，不能一律判成「写作文」，
    # 否则每条长评都会被要求「短一点」，跟长度指令打架。
    # 只拦 45-90 字这一段：不上不下、还收得很整齐，才是作文腔。
    long_tail = 45 < len(text or "") <= 90 and (text or "").rstrip().endswith("。")
    if score >= 3:
        bad = "、".join(f"「{w}」" for w, _ in hit[:4])
        return True, (f"刚才那句太像文案了（用了 {bad}）。重写：说人话，"
                      "别用书面词和抒情词，别编自己的画面和经历，"
                      "直接回对方那件事本身。")
    if long_tail:
        return True, "刚才那句太完整了，像写作文。重写：短一点，收得随意一点。"
    return False, ""


def strip_dots(text: str, kind: str = "comment", newline: bool = False,
               keep_dots: bool = False) -> str:
    """句号处理。

    短内容一个句号不留：语料里 74,558 条帖子只有 9.2% 出现句号，40 字以下的评论
    只有 4.0% 有句号。模型几乎 100% 每句都收句号，这是最容易被一眼看出来的机器痕迹。

    但**长文必须留**：≥100 字的评论 58% 有句号，≥200 字的 81% 有、46% 还分段。
    两百字全用逗号连成一句，人根本读不下去——这条规则原来一刀切，长评论没法看。
    """
    t = (text or "").replace("．", "。")
    if keep_dots:
        return re.sub(r"\n{3,}", "\n\n", t).strip()
    if "。" not in t and "." not in t:
        return t
    parts = [p.strip() for p in re.split(r"[。]+|(?<=[\u4e00-\u9fff])\.", t)]
    parts = [p for p in parts if p]
    if not parts:
        return ""
    if kind == "post" and newline and 2 <= len(parts) <= 4:
        return "\n".join(parts)
    return "，".join(parts)


def _add_dots(t: str, n: int) -> str:
    """把 n 个逗号换成句号。长文兜底用。

    模型偶尔会无视「长文要用句号」，写出三百字一整串逗号。语料里 ≥300 字的帖子
    76% 有句号、p50 是 3 个，一个都没有的读起来喘不过气。均匀挑几个逗号换掉即可。
    """
    if n <= 0:
        return t
    pos = [m.start() for m in re.finditer("，", t)]
    if not pos:
        return t
    step = max(1, len(pos) // (n + 1))
    pick = pos[step::step][:n]
    chars = list(t)
    for i in pick:
        chars[i] = "。"
    return "".join(chars)


def shape(text: str, pl: dict | None = None) -> str:
    """最后的形态修正：去句号、去多余引号、按计划补表情、长帖换行。"""
    pl = pl or {}
    kind = pl.get("kind") or "comment"
    longform = bool(pl.get("longform"))
    t = (text or "").strip().strip('"“”「」')
    # 模型偶尔从半句对话开始，留下一个孤零零的收尾引号（「又在吹笛子了”，」）；
    # 中文帖子几乎不用引号，全部去掉最省事
    t = re.sub(r'["“”「」]', "", t)
    if (kind == "post" or longform) and "\n" in t:
        # 帖子保留换行：14.9% 的真实帖子是多行的，全压成一行反而假。
        # 长评论同理，46.4% 的超长评论（≥200 字）是分段的。
        lines = [re.sub(r"[ \t]+", " ", x).strip() for x in t.split("\n")]
        t = "\n".join([x for x in lines if x][:6])
    else:
        t = re.sub(r"\s+", " ", t)
    t = t.lstrip("，,、；;：: ")
    if not t:
        return t
    # 长文保留句号（语料 ≥100 字评论 58% 有句号，≥200 字 81% 有）
    t = strip_dots(t, kind, bool(pl.get("newline")), keep_dots=longform)
    t = t.strip()
    # 模型被要求「不要句号」之后，常用空格代替断句（「南区过去要骑车十分钟吧 到了奶茶都凉了」）。
    # 真人不会这么写，把中文之间的空格换成逗号
    t = re.sub(r"(?<=[一-鿿])\s+(?=[一-鿿])", "，", t)
    # 每一行结尾的逗号/顿号也清掉，保留问号感叹号
    t = "\n".join(re.sub(r"[，,、；;]+$", "", ln) for ln in t.split("\n"))
    if not t:
        return t

    # token 截断会把表情切成半截「[流泪」，留下一个孤立的中括号，很难看
    t = re.sub(r"\[[^\]\[]*$", "", t).rstrip()
    # 表情前面常常多一个空格（「不容易 [摸头]」），真人不会这么打
    t = re.sub(r"\s+(?=\[)", "", t)
    # 长文兜底：人设 prompt 里的「不用句号」有时还是会漏过去，
    # 三百字一个句号都没有读起来喘不过气。按语料水平补到每 90 字一个。
    if longform and len(t) > 160:
        need = max(2, len(t) // 90)
        have = t.count("。")
        if have < need:
            t = _add_dots(t, need - have)

    # 表情名必须在白名单里，模型自创的 [微笑] 之类一律去掉。
    # 长评额外一条：语料里 ≥100 字评论 37% 带表情，但几乎都收在整条末尾；
    # 模型爱在段落中间插一个 [摸头]（「…隔多少公里。[摸头] 反正我这人…」），
    # 夹在两句正经话之间最扎眼。长文只保留末尾那个，句中的全部删掉。
    if longform:
        _end = len(t.rstrip())
        t = STICKER_RE.sub(
            lambda m: m.group(0) if (m.group(1) in STICKERS and m.end() >= _end) else "", t)
        t = re.sub(r"[ \t]{2,}", " ", t).strip()
    else:
        t = STICKER_RE.sub(lambda m: m.group(0) if m.group(1) in STICKERS else "", t)
    # 补表情/补问号之前先清掉紧挨着的逗号：「…自荐恋爱，[晕了]」「…快3公里，？」
    # 都是「原句有句号 → 去句号 → 又补了符号」留下的疤，真人不会这么打
    t = re.sub(r"[，,、；;]+(?=\[)", "", t)
    if not longform and pl.get("sticker") and not STICKER_RE.search(t):
        t = t.rstrip("，,、；; ")
        # 模型没加就补一个。帖子没有「场景」标签，按句子本身的极性选，不再依赖 scene
        pol = polarity(t)
        scene_neg = pl.get("scene") in NEG_SCENES
        scene_pos = pl.get("scene") in POS_SCENES
        known = scene_neg or scene_pos
        ok = ((pol <= 0 and scene_neg) or (pol >= 0 and scene_pos)) if known else True
        if ok:
            if known:
                pick = _pick_sticker(pl.get("scene", ""))
            else:
                pool = SCENE_STICKER[""] if pol >= 0 else ["流泪", "无语", "晕了"]
                pick = random.choice(pool)
            if (pick in NEG_STICKERS and pol > 0) or (pick in POS_STICKERS and pol < 0):
                pick = ""
            t = t + (f"[{pick}]" if pick else "")

    # 问句形态：模型经常写成陈述，补一个问号，保证这一档真的是提问
    if pl.get("form") == "ask" and "？" not in t and "?" not in t:
        # 只有真的在问才补问号，陈述句补问号会读起来很怪
        if re.search(r"(吗|么|哪|几|多少|怎么|为什么|能不能|要不要|是不是|有没有|谁)", t):
            m = re.search(r"(\[[^\]]{1,6}\])$", t)
            if m:                              # 问号要放在表情前面
                t = t[: m.start()].rstrip("，,、；; ") + "？" + m.group(1)
            else:
                t = t.rstrip("，,。.、；; ") + "？"

    # 长评论自动分段：模型经常一口气写完，读起来像论文。
    # 语料里 46% 的超长评论是分段的，按句号切两到三段，段间空一行。
    if longform and "\n" not in t and len(t) > 110:
        segs = [s for s in re.split(r"(?<=[。！？])", t) if s.strip()]
        if len(segs) >= 3:
            per = max(1, (len(segs) + 1) // 2) if len(segs) <= 6 else max(1, (len(segs) + 2) // 3)
            out, buf = [], []
            for s in segs:
                buf.append(s)
                if len(buf) >= per:
                    out.append("".join(buf))
                    buf = []
            if buf:
                out.append("".join(buf))
            t = "\n\n".join(out)

    # 两行/清单形态：模型常常还是一行写完，按中间的逗号补一个换行
    if pl.get("newline") and not longform and "\n" not in t and len(t) > 14:
        pos = [m.start() for m in re.finditer(r"[，,]", t)]
        if pos:
            cut = min(pos, key=lambda i: abs(i - len(t) // 2))
            t = t[:cut] + "\n" + t[cut + 1:]
    return t


# 真实评论里有一类机器人永远做不出来的东西：整条只有一个「？」「啊？」「啥？」。
# 4.7% 的评论带问号，其中相当一部分就是这个长度。只在「离谱/被冒犯」类帖子下启用，
# 因为那是唯一不会误伤的场景——在别人诉苦的帖子下回一个「？」就是冒犯。
REACTIONS: tuple[str, ...] = ("？", "啊？", "啥？", "？？？", "这啥啊", "离谱", "真的假的", "没看懂")


def reaction(scene: str | None, rate: float = 0.05, rng: random.Random | None = None) -> str | None:
    rng = rng or random.Random()
    if scene != "被冒犯":
        return None
    if rng.random() >= rate:
        return None
    return rng.choice(REACTIONS)


# 所有号共用一份「最近说过的话」。三个号各自防复读不够——同一批帖子被两个号
# 各回一句「蹲蹲」，比一个号复读更容易被看出来。
SHARED = ROOT / "logs" / "recent_shared.jsonl"


def remember(text: str, acc: str = "") -> None:
    if not (text or "").strip():
        return
    try:
        SHARED.parent.mkdir(parents=True, exist_ok=True)
        with SHARED.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"t": round(time.time()), "a": acc, "x": text[:120]},
                               ensure_ascii=False) + "\n")
        if SHARED.stat().st_size > 300_000:        # 只留最近 800 条，文件不会无限长
            keep = SHARED.read_text(encoding="utf-8", errors="replace").splitlines()[-800:]
            SHARED.write_text("\n".join(keep) + "\n", encoding="utf-8")
    except Exception:
        pass


def shared_recent(n: int = 120) -> list[str]:
    try:
        lines = SHARED.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln)["x"])
            except Exception:
                continue
        return out
    except Exception:
        return []


# 七个号轮流刷同一条新帖是最容易露馅的协同痕迹：帖子下面一分钟出现七条评论，
# 每条口吻还不一样。这里记「谁在什么时候评论过哪条帖」，别的号一小时内避开。
TARGETS = ROOT / "logs" / "comment_targets.jsonl"


def remember_target(post_id: str, acc: str = "") -> None:
    if not post_id:
        return
    try:
        TARGETS.parent.mkdir(parents=True, exist_ok=True)
        with TARGETS.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"t": round(time.time()), "id": post_id, "a": acc}) + "\n")
        if TARGETS.stat().st_size > 200_000:
            keep = TARGETS.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]
            TARGETS.write_text("\n".join(keep) + "\n", encoding="utf-8")
    except Exception:
        pass


def recent_targets(hours: float = 1.5) -> set[str]:
    try:
        lines = TARGETS.read_text(encoding="utf-8", errors="replace").splitlines()[-300:]
    except Exception:
        return set()
    cutoff = time.time() - hours * 3600
    out = set()
    for ln in lines:
        try:
            d = json.loads(ln)
            if float(d.get("t") or 0) >= cutoff:
                out.add(d.get("id") or "")
        except Exception:
            continue
    return {x for x in out if x}


def grams(text: str, n: int = 3) -> set[str]:
    s = re.sub(r"[\s\[\]（）()，,。.！!？?~～…]+", "", text or "")
    return {s[i:i + n] for i in range(max(0, len(s) - n + 1))}


_TIME_HEAD = re.compile(
    r"^(?:今天|昨天|昨晚|今早|早上|上午|中午|下午|傍晚|晚上|凌晨|深夜|"
    r"\d{1,2}\s*点(?:\d{1,2}\s*分?)?)\s*(?:的)?")


# 以这些字结尾，基本可以断定句子没说完（「我过去看了一眼，就在里面躺着，下雨」）
# 真正不能结尾的字：单字介词、连词、体标记、程度副词、助动词。
# 「了」不在表里——「雨停了」「人挺多的」都是完整句，把它列进来会误伤一大半正常帖子。
# 「过」「会」「可」也不在——「我没去过」「今天开会」结尾就是完整的。
# 「没/要/能/想/做/用/成/为」加进来是因为实测模型会在这些字上停住
# （「我攥着伞柄站了半分钟没」「当时没」），这类残句读起来最像断线。
_BAD_END = set("的着不在和跟把被就还也都太很从向往对给与及或而并但却使让"
               "没要能想做用成为"
               "，,、；;：:（(【[")


def ends_ok(text: str) -> bool:
    t = (text or "").rstrip()
    if not t:
        return False
    if re.search(r"\[[^\]]*$", t):        # 表情被截断
        return False
    # 「用」在黑名单里是因为「没…用」「想用」常是半截，但「管用/有用/没用/耐用」
    # 是完整词：「找宿管记一次，比你说三次管用」就是一句说完的话，不能误杀。
    if t[-1] == "用" and len(t) >= 2 and t[-2] in "管有没好耐顶":
        return True
    return t[-1] not in _BAD_END


def strip_time_opener(text: str) -> str:
    """把开头的时间词剥掉。模型对「不要用时间开头」的服从率只有三成，直接后处理更稳。"""
    t = (text or "").strip()
    for _ in range(2):          # 「今天中午」要剥两次
        m = _TIME_HEAD.match(t)
        if not m:
            break
        rest = t[m.end():].lstrip("，, ")
        if len(rest) < 4:
            break
        t = rest
    return t


def opener(text: str, n: int = 4) -> str:
    """开头四个字。七个号连续用「今天下午」开头，比内容重复更显眼。"""
    s = re.sub(r"[\s\[\]（）()，,。.！!？?~～…0-9]+", "", text or "")
    return s[:n]


def repeats(recent: list[str], text: str, thr: float = 0.4) -> bool:
    """3-gram 重合度。比字符集 Jaccard 准：同义改写不会被误杀，模板套话跑不掉。"""
    a = grams(text)
    if not a:
        return False
    for old in recent or []:
        b = grams(old)
        if not b:
            continue
        inter = len(a & b)
        if inter / min(len(a), len(b)) >= thr:
            return True
    return False
