"""从 9 万条真实帖子里按系统时间抽一个话题，再写「同一类、另一件事」。

为什么不再用写死的 angle 列表
------------------------------
`persona.PERSONAS[*]["angles"]` 是十几条手写场景，7 个号每天各发一条，
十几天就把池子走完一遍，然后开始「图书馆闭馆 + 旧书 + 霉味」连着出现。
手写场景的另一个毛病是它们共享同一套意象（图书馆、食堂、猫、晚霞），
听起来像同一个人换了七种语气。

这层的做法：
    系统时间 → 种子 → 在向量库的 7.8 万条真实帖里抽一条 → 抽象成 12-20 字话题
再让模型「按这个话题写你自己遇到的另一件事」。

两条硬约束
----------
1. **不复述**：只把抽象后的话题给模型，不给原文。原文里的细节（三楼、豆浆、
   《民法总论》）不会被搬到生成结果里。
2. **语义查重**：生成后用 bge-m3 编码，和「近期所有号发过的帖子」比余弦。
   实测同一主题的两条帖子余弦在 0.66-0.77，不同主题 0.47-0.64，所以阈值取
   0.70 就能挡住「又是旧书」而不误伤正常内容。3-gram 查重挡不住这个——
   「旧纸霉味」和「霉味冲鼻」的 3-gram 几乎不重合。
"""
from __future__ import annotations

import base64
import json
import random
import re
import sqlite3
import time
import zlib
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parent.parent
RAW = ROOT / "kb" / "raw.db"
VECMAP = ROOT / "kb" / "dense.db"
STATE = ROOT / "state.db"
DRAWS = ROOT / "logs" / "topic_draws.jsonl"
VECS = ROOT / "logs" / "post_vecs.jsonl"

# 交易/招募类帖子本质是广告，照着写出来就是软广，直接不进候选
SKIP_BOARDS = {"二手交易", "失物招领", "拼团拼单"}

# 带联系方式、招募、代拿的一律不要：模型照着写会把「加v」「100r」也搬进去
_AD = re.compile(
    r"(招募|被测|被试|有偿|代订|代取|代拿|代寄|代跑|代课|代签|转让|出售|求购|收购|出租|"
    r"租房|转租|拼车|拼单|价格可议|联系我|私信|加\s*[vV微]|微信|qq|QQ|手机号|"
    r"1[3-9]\d{9}|有意者|扫码|接单|兼职|家教|代理|包邮|自提|九成新|带价|"
    r"招[同人个]|报酬|日结|时薪|按人头|元/小时|蹲个|求个|帮跑|出一|收一)"
)

KINDS = ("见闻", "吐槽", "堵", "吃", "问")

# 类型配比。不按语料比例走，是因为语料里 51% 是求助提问，照着抽就变成七个问答机。
# 见闻占四成、吐槽两成半，剩下三种各占一成上下——真人在社区里大概就是这个比例。
KIND_W = {"见闻": 0.40, "吐槽": 0.20, "堵": 0.14, "吃": 0.07, "问": 0.09, "恋爱": 0.10}

# 时效性词：带这些的帖子只能当「当天的事」，拿来改编就是造谣，直接不进候选
_TIME_SENSITIVE = re.compile(
    r"(今天|明天|昨天|今晚|明晚|本周|这周|下周|上周|本月|周末|几号|截止|报名|开抢|"
    r"领取|活动|讲座|招聘|宣讲|考试周|期末|放假|开学|春运|抢票|投票|评选|公示)")

# 老帖更安全：放了两个月以上还没过期的内容，说明它本身不依赖时间
OLD_DAYS = 45

# 每种类型去哪几个板块取材。类型定板块，板块定语感，比「人设固定三块板」散得开。
KIND_BOARDS = {
    "见闻": ["抛抛帖子", "新生专栏", "苏大神奇动物", "苏大恋爱日常", "女孩，女孩"],
    # 打工电波整版都是招人/接单的广告，抽出来只能写成软广，不进候选
    "吐槽": ["苏大迷惑大赏", "抛抛帖子"],
    "堵": ["今天不开心"],
    "吃": ["今天吃什么"],
    "问": ["求助提问箱", "新生专栏"],
    "恋爱": ["苏大恋爱日常", "女孩，女孩", "抛抛帖子"],
}


# 具体性打分用的词。真人写事一定带这些东西，抽象感慨则一条都不带。
_PLACE = ("食堂", "图书馆", "宿舍", "教室", "操场", "超市", "快递", "校车", "澡堂",
          "教学楼", "实验", "校门", "球场", "自习", "摊", "窗口", "驿站", "便利店")
_TIME = ("早上", "中午", "下午", "晚上", "凌晨", "半夜", "早八", "六点", "七点", "八点",
         "九点", "十点", "十一点", "分钟", "小时")
_DIGIT = re.compile(r"\d")
_ACT = ("了", "过", "看到", "看见", "听见", "发现", "碰到", "遇到", "排队", "坐", "走",
        "吃", "买", "拿", "跑", "睡", "聊")
# 求人办事类帖子没有「事」，抽到也没得写，打分时压下去
_REQ = re.compile(r"(想问|请问|有没有|有人知道|求推荐|求问|找一|寻找|捞一|蹲一个|谁能|"
                  r"怎么|吗[？?]|嘛[？?]|推荐|带价)")

# 按小时/按次报价的帖子（「100一小时教字母」）在评论里会被当成招工贴去应聘，
# 单独列一条，因为「18元」这种正常价格不能算广告
_RATE = re.compile(r"\d+\s*(?:r|元|块|块钱)?\s*(?:/|一|每)\s*(?:个)?\s*(?:小时|h|次|天|课时)")

_cache: dict = {"t": 0.0, "pool": []}


def is_ad(text: str) -> bool:
    """评论/点赞前用来跳过广告贴：招募、代拿、按小时报价都算。"""
    t = text or ""
    return bool(_AD.search(t) or _RATE.search(t))


def _own_post_ids() -> set[str]:
    """自己发过的帖子。语料是今天抓的，里面已经有我们 7 个号发的东西，
    抽到自己的帖子当话题就等于让模型照抄自己上一轮。"""
    try:
        c = sqlite3.connect(STATE)
        rows = c.execute("SELECT target FROM actions WHERE kind LIKE '%post'").fetchall()
        c.close()
        return {r[0] for r in rows if r[0] and r[0].startswith("UG9zdD")}
    except Exception:
        return set()


def pool(ttl: float = 600) -> list[tuple[str, str, str]]:
    """[(doc_id, board, content)]，过滤掉广告板和过短过长的。

    7.8 万条一次读进来约 40MB，缓存 10 分钟——每次发帖重读一遍是白烧 IO。
    """
    if _cache["pool"] and time.time() - _cache["t"] < ttl:
        return _cache["pool"]
    own = _own_post_ids()
    rows: list[tuple[str, str, str]] = []
    c = sqlite3.connect(f"file:{RAW}?mode=ro", uri=True)
    # vecmap 在另一个库里，分开查再拼，省得 ATTACH 路径写死
    vm = sqlite3.connect(f"file:{VECMAP}?mode=ro", uri=True)
    meta = {doc: (board or "", day or "") for doc, board, day in
            vm.execute("SELECT doc, board, day FROM vecmap WHERE kind='post'")}
    vm.close()
    for pid, board, content in c.execute(
            "SELECT id, sub_name, content FROM posts WHERE length(content) BETWEEN 20 AND 180"):
        if pid not in meta or not content:
            continue
        board_meta, day = meta[pid]
        if pid in own or (board or "") in SKIP_BOARDS:
            continue
        if _AD.search(content):
            continue
        if _TIME_SENSITIVE.search(content):
            continue
        rows.append((pid, board or board_meta, content.strip(), day))
    c.close()
    _cache["t"], _cache["pool"] = time.time(), rows
    return rows


def _age_days(day: str) -> int:
    try:
        d = time.mktime(time.strptime(str(day)[:10], "%Y-%m-%d"))
        return int((time.time() - d) / 86400)
    except Exception:
        return 0


# 到处都是的地点。写「宿舍」「食堂」不算提供了具体信息，因为人人都在写；
# 只有「澡堂」「校车」「快递点」这种才把场景钉死。两者给的分要不一样，
# 否则具体性排序会把宿舍帖顶到前面——实测生成 29% 在写宿舍，真人只有 5%。
_COMMON_PLACE = ("食堂", "宿舍", "图书馆", "教室")


def _concrete(text: str) -> int:
    """具体性打分：有数字、有地点、有钟点、有过去动作，才是能写成一条帖子的素材。"""
    s = 0
    if _DIGIT.search(text):
        s += 2
    places = [w for w in _PLACE if w in text]
    rare = [w for w in places if w not in _COMMON_PLACE]
    common = [w for w in places if w in _COMMON_PLACE]
    s += min(4, len(rare) * 2) + min(1, len(common))
    s += min(2, sum(1 for w in _TIME if w in text))
    s += min(3, sum(1 for w in _ACT if w in text))
    if any(w in text for w in ("我", "本人", "咱")):
        s += 2
    if 30 <= len(text) <= 140:
        s += 1
    if _REQ.search(text):
        s -= 4
    if "【" in text:
        s -= 5
    return s


# ------------------------------------------------------------------ 题材配额
# 照抄 eval_run.py 的 POST_TOPICS：只看正文前 30 字命中哪些词。
# 一个真实帖可能同时命中多类（「在图书馆吃面包」），无标签 = 7 类一个都不命中。
TOPIC_KW = {
    "食堂/吃": ("食堂", "饭", "菜", "鸡", "肉", "汤", "奶茶", "咖啡", "外卖", "煎", "烤", "串", "粉", "面包", "窗口", "吃"),
    "图书馆/自习": ("图书馆", "自习", "书", "闭馆", "座位", "占座", "借"),
    "雨/伞": ("雨", "伞", "淋", "湿"),
    "考试/作业/绩点": ("考试", "考", "作业", "期中", "期末", "复习", "错题", "刷题", "绩点", "保研", "挂科", "论文", "实验", "报告"),
    "宿舍/室友": ("宿舍", "室友", "寝室", "舍"),
    "车/充电": ("车", "充电", "桩", "插头", "电动车"),
    "恋爱/情感": ("喜欢", "分手", "男", "女", "朋友", "暗恋", "约", "对象"),
}
NO_LABEL = "无标签"

_topic_cache: dict = {"dist": None, "buckets": None}
_topic_draw_counts: Counter = Counter()      # 模块级计数：各题材抽中次数（跨账号/跨次）
_topic_last: list = ["", 0, 0.0]             # [上一题材, 连续次数, 上次时间]，带时间衰减


def _topic_match(head: str) -> list[str]:
    return [k for k, kws in TOPIC_KW.items() if any(w in head for w in kws)]


def real_topic_distribution() -> dict:
    """从 kb/raw.db 的 posts 表统计前 30 字命中 7 类的占比 + 无标签占比，模块内缓存。

    口径与 tools/eval_run.py::topic_dist_score 完全一致：每类独立按「前 30 字
    命中任一关键词」计占比，另算 7 类都不命中的「无标签」占比。
    """
    if _topic_cache["dist"] is not None:
        return _topic_cache["dist"]
    c = sqlite3.connect(f"file:{RAW}?mode=ro", uri=True)
    rows = [r[0] for r in c.execute(
        "SELECT content FROM posts WHERE length(content) BETWEEN 10 AND 300")]
    c.close()
    n = max(1, len(rows))
    cnt = {k: 0 for k in TOPIC_KW}
    none = 0
    for text in rows:
        m = _topic_match((text or "")[:30])
        for k in m:
            cnt[k] += 1
        if not m:
            none += 1
    dist = {k: v / n for k, v in cnt.items()}
    dist[NO_LABEL] = none / n
    _topic_cache["dist"] = dist
    return dist


def _label_bucket(label: str) -> list:
    """把 pool() 的安全候选帖按前 30 字归入 8 个题材桶，模块内缓存。

    桶之间可重叠（一帖可进多类）；无标签桶是 7 类都不命中的帖。
    """
    if _topic_cache["buckets"] is None:
        b = {k: [] for k in TOPIC_KW}
        b[NO_LABEL] = []
        for row in pool():
            m = _topic_match((row[2] or "")[:30])
            if not m:
                b[NO_LABEL].append(row)
            else:
                for k in m:
                    b[k].append(row)
        _topic_cache["buckets"] = b
    return _topic_cache["buckets"].get(label) or []


def _draw_topic_label(r: random.Random) -> str:
    """按真实题材分布抽一个标签（含「无标签」）；模块级计数+时间衰减做轻量去重。

    只挡「同一题材连发 3 条以上」。无标签是题材缺失、连发本来就是不同的事，
    不参与去重——否则会人为压掉占比最高的无标签档，反而破坏真实分布。
    """
    dist = real_topic_distribution()
    labels = list(dist)
    weights = [dist[l] for l in labels]
    now = time.time()
    last, streak, t = _topic_last
    if now - t > 300:                        # 时间衰减：隔太久不算「连着」
        last, streak = "", 0
    label = ""
    for _ in range(4):
        label = r.choices(labels, weights=weights)[0]
        if label == NO_LABEL or not (label == last and streak >= 2):
            break
    if label == last:
        streak += 1
    else:
        last, streak = label, 1
    _topic_last[:] = [last, streak, now]
    _topic_draw_counts[label] += 1
    return label


def draw(account: str = "", seed: int | None = None, board: str | None = None,
         avoid_boards: set[str] | None = None,
         avoid_kw: tuple[str, ...] | None = None,
         label: str | None = None) -> dict | None:
    """在指定板块里按系统时间种子抽一条真实帖。

    label 非空时改从对应题材桶抽（配额模式）：正文前 30 字自然带该题材，
    不硬拼关键词；题材桶为空则退回按板块抽。
    """
    rows = pool()
    if not rows:
        return None
    sd = seed if seed is not None else (zlib.crc32(f"{time.time_ns()}:{account}:topic".encode()) & 0xFFFFFFFF)
    r = random.Random(sd)
    if label is not None:
        bucket = _label_bucket(label)
        if bucket:
            cands = r.sample(bucket, min(16, len(bucket)))
            avoid_kw = None                 # 配额要的就是这个题材，不再压场景
        else:
            label = None                    # 桶空，退回按板块抽
    if label is None:
        by: dict[str, list] = {}
        for row in rows:
            by.setdefault(row[1], []).append(row)
        avoid = avoid_boards or set()
        pick = board if (board and board in by) else ""
        if not pick:
            boards = [b for b in by if b not in avoid] or list(by)
            pick = r.choice(boards)
        cands = r.sample(by[pick], min(16, len(by[pick])))
    if avoid_kw:
        filtered = [c for c in cands if not any(k in c[2] for k in avoid_kw)]
        if len(filtered) >= 1:      # 配额一旦触发，剩一条也要用
            cands = filtered
    # 优先老帖：放了一个半月还没过期的内容，说明它本身不依赖时间点
    old = [c for c in cands if _age_days(c[3]) >= OLD_DAYS]
    if len(old) >= 3:
        cands = old
    cands.sort(key=lambda row: _concrete(row[2]), reverse=True)
    doc, bd, text, day = r.choice(cands[:4])
    return {"doc": doc, "board": bd, "text": text, "seed": sd,
            "day": day, "age_days": _age_days(day)}


_ABS_SYS = (
    "把下面这条校园论坛帖子，抽象成一个可以换个人重写的「话题」和一个「帖子类型」。\n"
    "只输出一行 JSON，不要解释：{\"topic\":\"12-22字，写成「场合｜发生的事」，"
    "必须有动词和具体名词\",\"kind\":\"见闻|吐槽|堵|吃|问\"}\n"
    "例子：图书馆三楼｜闭馆广播响了还差一页没看完；莘园食堂｜半只烤鸭要50块\n"
    "不要用「经历」「现象」「情况」「问题」「咨询」「体验」这类总结词，"
    "不要评价，不要句号，不要引号。\n"
    "kind：见闻=看到遇到的一件事；吐槽=对某个现象的抱怨；堵=情绪低落/难受；"
    "吃=吃喝；问=想求个答案。\n"
    "如果这条帖子是广告、招募、交易、代拿、纯求推荐，输出 {\"topic\":\"NONE\",\"kind\":\"\"}。"
)


def abstract(brain, seed_text: str, board: str) -> tuple[str, str]:
    """把真实帖压成（话题, 类型）。只留「场合 + 具体物/事」，原文措辞一律不进后续提示词。

    类型这一维是被同质化最狠的地方：7 个号如果都写「今天看到一件小事」，
    就算话题不同，读起来还是同一种东西。有了类型，吐槽/难受/吃喝/提问
    会自然长成不同形状的帖子。
    """
    try:
        out = brain.chat(_ABS_SYS, f"【板块】{board}\n【帖子】{seed_text[:200]}",
                         max_tokens=80, temperature=0.7) or ""
        m = re.search(r"\{.*\}", out, re.S)
        if m:
            d = json.loads(m.group(0))
            t = re.sub(r"[\s\"“”「」。]+", "", str(d.get("topic") or ""))
            k = str(d.get("kind") or "").strip()
            if t.upper() == "NONE":
                return "", ""
            if 6 <= len(t) <= 30:
                return t, (k if k in KINDS else "见闻")
    except Exception:
        pass
    return seed_text[:20].replace("\n", "").strip(), "见闻"


def recent(n: int = 40) -> list[dict]:
    try:
        lines = DRAWS.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except Exception:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


def _grams(text: str, n: int = 2) -> set[str]:
    t = re.sub(r"[\s，,。、！？!?～~]+", "", text or "")
    return {t[i:i + n] for i in range(max(0, len(t) - n + 1))}


def too_similar_topic(label: str, recent_labels: list[str], thr: float = 0.34) -> str | None:
    """2-gram Jaccard 撞了就算重复。话题短，字符级比词级稳，不用调分词。"""
    g = _grams(label)
    if not g:
        return None
    for old in recent_labels:
        h = _grams(old)
        if not h:
            continue
        inter = len(g & h)
        if inter / max(1, len(g | h)) >= thr:
            return old
    return None


def plan(brain, account: str = "", persona: str = "", tries: int = 2,
         budget: float = 14.0, quota: bool = True) -> dict:
    """抽类型 → 抽题材配额 → 抽真实帖 → 抽象成话题。返回话题/类型/板块/种子帖 id。

    budget 是总耗时上限：模型偶尔单次要 25s，重抽多轮能拖到一分半，发帖链路等不起。
    quota=True（默认）时按 raw.db 的真实题材分布抽一个标签（含「无标签」），
    从对应真实帖里取种子，让正文前 30 字自然带上该题材；传 quota=False 关掉做 A/B。
    函数签名与返回字段结构保持不变。
    """
    t0 = time.time()
    hist = recent(40)
    labels = [h.get("topic") or "" for h in hist if h.get("topic")]
    # 最近 3 次用过的板块先避开，7 个号不会连着同一板块刷屏
    avoid = {h.get("board") for h in hist[:3] if h.get("board")}
    sd = zlib.crc32(f"{time.time_ns()}:{account}:post".encode()) & 0xFFFFFFFF
    r = random.Random(sd)
    best = None
    first = None
    for i in range(tries):
        if i and time.time() - t0 > budget and best:
            break
        # 最近六条里已经有两顿吃了，这一轮强制别再写吃的
        recent_kinds = [h.get("kind") for h in hist[:6]]
        weights = dict(KIND_W)
        if recent_kinds.count("吃") >= 2:
            weights.pop("吃", None)
        # 全局 24 小时配额：吃类不超过 15%
        day_kinds = [h.get("kind") for h in hist if time.time() - float(h.get("t") or 0) < 86400]
        if day_kinds and day_kinds.count("吃") / len(day_kinds) >= 0.15:
            weights.pop("吃", None)
        # 按权重抽类型，再在类型对应的板块里挑一个最近没用的
        x, acc, kind = r.random() * sum(weights.values()), 0.0, "见闻"
        for k, w in weights.items():
            acc += w
            if x <= acc:
                kind = k
                break
        pool_boards = [b for b in KIND_BOARDS.get(kind, ["抛抛帖子"]) if b not in avoid]
        board = r.choice(pool_boards or KIND_BOARDS.get(kind, ["抛抛帖子"]))
        # 高频场景配额。种子池里「食堂 / 宿舍 / 图书馆」这些场景占比远高于真实语料，
        # 不压的话实测生成：食堂 35%、图书馆 20%、宿舍 20%，
        # 而真实帖子分别是 7.8% / 6.5% / 5.8%——读十条有五条在这三个地方。
        _SCENE_QUOTA = (
            ("宿舍", ("宿舍", "寝室", "舍友", "室友", "舍楼下"), 0.04),
            ("食堂", ("食堂", "打饭", "窗口", "饭菜", "外卖", "米线", "香锅", "炒菜"), 0.05),
            ("图书馆", ("图书馆", "自习", "闭馆", "占座"), 0.06),
        )
        blocked: list[str] = []
        for _name, kws, cap in _SCENE_QUOTA:
            n_hit = sum(1 for h in hist
                        if any(k in (h.get("topic") or "") for k in kws))
            if hist and n_hit / len(hist) >= cap:
                blocked.extend(kws)
        avoid_kw = tuple(blocked) or None
        # 配额采样：按真实题材分布抽一个标签（含「无标签」），从对应真实帖取种子，
        # 配额模式下不再用 _SCENE_QUOTA 压场景（要的就是这个题材）。
        qlabel = _draw_topic_label(r) if quota else None
        d = draw(account, seed=None if i == 0 else
                 (zlib.crc32(f"{time.time_ns()}:{account}:topic:{i}".encode()) & 0xFFFFFFFF),
                 board=board, avoid_kw=(None if qlabel else avoid_kw), label=qlabel)
        if not d:
            break
        d.update(kind=kind)
        if first is None:
            first = dict(d)
        label, seed_kind = abstract(brain, d["text"], d["board"])
        if not label:                       # NONE：广告/招募/求推荐，换一条
            avoid = avoid | {d["board"]}
            continue
        dup = too_similar_topic(label, labels)
        d.update(topic=label, seed_kind=seed_kind, dup_of=dup or "")
        if best is None:
            best = d
        if not dup:
            best = d
            break
        avoid = avoid | {d["board"]}
    if not best and first:
        # 模型抽不出来（超时/全是广告）也别让发帖失败：退回种子帖开头当话题
        first.update(topic=first["text"][:18].replace("\n", " ").strip(),
                     seed_kind="", dup_of="fallback")
        best = first
    if not best:
        return {"topic": "", "kind": "", "board": "", "doc": "", "text": "", "dup_of": ""}
    remember(best, account)
    return best


def remember(rec: dict, account: str = "") -> None:
    try:
        DRAWS.parent.mkdir(parents=True, exist_ok=True)
        with DRAWS.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"t": round(time.time()), "a": account,
                                "doc": rec.get("doc", ""), "board": rec.get("board", ""),
                                "kind": rec.get("kind", ""),
                                "topic": rec.get("topic", "")}, ensure_ascii=False) + "\n")
        if DRAWS.stat().st_size > 400_000:
            keep = DRAWS.read_text(encoding="utf-8", errors="replace").splitlines()[-600:]
            DRAWS.write_text("\n".join(keep) + "\n", encoding="utf-8")
    except Exception:
        pass


# ------------------------------------------------------------------ 语义查重

def remember_vec(text: str, vec, account: str = "") -> None:
    """把发出去的帖子连同向量存一份，下次比对用。float16 + base64 一条 2.7KB。"""
    try:
        v = (vec.astype("float16")).tobytes()
        VECS.parent.mkdir(parents=True, exist_ok=True)
        with VECS.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"t": round(time.time()), "a": account, "x": text[:200],
                                "v": base64.b64encode(v).decode()}, ensure_ascii=False) + "\n")
        if VECS.stat().st_size > 2_000_000:
            keep = VECS.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
            VECS.write_text("\n".join(keep) + "\n", encoding="utf-8")
    except Exception:
        pass


def recent_vecs(n: int = 60) -> list[tuple[str, object]]:
    try:
        import numpy as np
        lines = VECS.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except Exception:
        return []
    out = []
    for ln in lines:
        try:
            d = json.loads(ln)
            v = np.frombuffer(base64.b64decode(d["v"]), dtype=np.float16).astype(np.float32)
            out.append((d.get("x", ""), v))
        except Exception:
            continue
    return out


def closest(vec, n: int = 60) -> tuple[str, float]:
    """和最近发过的帖子比余弦。返回最像的那条及其分数。"""
    best, score = "", 0.0
    for text, v in recent_vecs(n):
        if v.shape != vec.shape:
            continue
        s = float((v * vec).sum())
        if s > score:
            best, score = text, s
    return best, score


def seed_overlap(vec, seed_doc: str, k: int = 8) -> float:
    """生成结果和「抽到的那条真实帖」有多像。>0.85 就是换词复述。"""
    try:
        from . import dense
    except Exception:
        return 0.0
    if not seed_doc:
        return 0.0
    try:
        for doc, cos in dense.search(vec, k=k, kind="post"):
            if doc == seed_doc:
                return float(cos)
    except Exception:
        pass
    return 0.0
