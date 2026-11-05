"""反复读：治「几个号在同一帖下面说同一件事、用同一个句式」。

用户看到的问题不是措辞像，而是**同一批号对同一条帖给出同一个建议**：
「满音量闹钟」那条帖下，三个号分别写
    「直接找宿管登记换寝，比讲道理管用」
    「先跟宿管报备留个记录，再拉上室友一起去学院找辅导员当面说」
    「你直接找宿管阿姨登记投诉，让她出面谈一次比你磨破嘴管用」
三条 3-gram 重合度都不到 0.4，`human.repeats()` 抓不住，因为它是**换词不换事**。

所以这里按三层判：
1. 动作层——这条评论在建议对方「做什么」（找宿管 / 找辅导员 / 换寝 / 取证…）。
   同一帖下面同一个动作只允许出现一次，后来的人必须换一条路。
2. 骨架层——「X，比Y管用」「不是X，是Y」「先X再Y」这种模板句。
   把它们抽象成骨架名再比，换词也跑不掉。
3. 词频层——「蹲」「我也」「确实」这类口水词，最近若干条里用超了就本次禁用。
"""
from __future__ import annotations

import re

# ---------- 动作层 ----------

# key 是动作名（给重写指令用），value 是命中它的说法。
# 只收「能落到一个具体做法」的动作，纯情绪词不算。
ACTIONS: dict[str, tuple[str, ...]] = {
    "找宿管": ("宿管",),
    "找辅导员": ("辅导员",),
    "找教务": ("教务",),
    "找助教": ("助教",),
    "找任课老师": ("任课老师", "问老师", "找老师", "老师办公室"),
    "找学院": ("学院办", "学院找", "学院那边", "院办"),
    "换寝室": ("换寝", "换宿舍", "调寝", "搬出去"),
    "换座位": ("换座", "坐远", "坐前面", "坐后排", "换到"),
    "取证留证": ("录音", "截图", "留证", "留个记录", "报备", "留证据", "拍下来"),
    "投诉举报": ("投诉", "举报", "报警", "12345", "校长信箱", "反映给"),
    "当面说开": ("当面说", "当面聊", "当面问", "直接问", "问清楚", "摊开说", "摊牌",
                 "说清楚", "讲清楚", "跟他确认", "找他谈"),
    "先自问": ("先想清楚", "先确认", "先问自己", "先搞清", "先弄明白", "想清楚你",
               "先算算", "先掂量"),
    "问群问同学": ("群里问", "课程群", "问同学", "问学长", "问学姐", "群文件", "问一下同学"),
    "查文件规定": ("学院文件", "看文件", "规定", "通知里", "官网上", "手册上", "培养方案"),
    "加联系方式": ("加微信", "加个微信", "加QQ", "加你", "微信聊", "拉我进群", "加个好友"),
    "蹲后续": ("蹲", "码住", "等后续", "等个后续"),
    "自己先试": ("先自己", "先试", "先翻", "先看", "自己查", "自己先"),
    "算了不值": ("算了", "不值得", "别管", "放弃", "别理"),
    "换个方法": ("换个", "换一种", "改成", "别硬"),
    "约时间见面": ("约出来", "约个", "见一面", "碰一下", "当面见"),
    "定个期限": ("定个期限", "给个准话", "说死", "限期", "拖到什么时候"),
}

_ACTION_RE = {k: re.compile("|".join(map(re.escape, v))) for k, v in ACTIONS.items()}

# 核心解法：一条帖下面只该出现一次。两个号都说「找宿管」，用户一眼就看出来。
# 通用建议（当面说开、自己先查查、算了）不算：不同的事都可以这么说，
# 拦它们会把正常评论也逼成重写。
CORE_ACTIONS = frozenset({
    "找宿管", "找辅导员", "找教务", "找助教", "找任课老师", "找学院",
    "换寝室", "换座位", "取证留证", "投诉举报", "加联系方式", "蹲后续",
    "问群问同学", "约时间见面", "先自问",
})

# 「别绕宿管了」「不用找辅导员」不是建议他去，是否定这一条。
# 不做这层过滤会把「换个角度」的评论误判成撞车，反而逼它退回老路上。
_NEG_PREFIX = ("别", "不用", "不要", "不必", "无需", "没必要", "绕开", "绕过", "别绕",
               "省得", "别再")


def _negated(t: str, pos: int) -> bool:
    pre = t[max(0, pos - 5):pos]
    return any(n in pre for n in _NEG_PREFIX)


def actions_in(text: str) -> set[str]:
    """这条文本在建议对方做什么动作（否定语境不算）。"""
    t = text or ""
    out: set[str] = set()
    for k, words in ACTIONS.items():
        for w in words:
            hit = False
            for m in re.finditer(re.escape(w), t):
                if not _negated(t, m.start()):
                    hit = True
                    break
            if hit:
                out.add(k)
                break
    return out


# ---------- 骨架层 ----------

# (骨架名, 正则)。骨架名要粗到能把换词版本归到一起，又不能粗到误伤正常句子。
SKELETONS: list[tuple[str, re.Pattern]] = [
    ("比Y管用", re.compile(r"比[^。！？\n]{1,16}?(?:管用|好使|实在|靠谱|强|省事|划算|有用)")),
    ("直接X", re.compile(r"直接[^。！？\n]{1,16}?(?:就行|就好|得了|管用|省事|吧|呗)")),
    ("不是X是Y", re.compile(r"不是[^。！？\n]{1,14}[，,]\s*(?:而)?是[^。！？\n]{1,18}")),
    ("先X再Y", re.compile(r"先[^。！？\n]{1,14}[，,]\s*再[^。！？\n]{1,16}")),
    ("X就行了", re.compile(r"[^。！？\n]{1,10}(?:就行|就好了|就对了|准没错)[了。！？]?$")),
    ("要不X", re.compile(r"要不[^。！？\n]{1,20}")),
    ("还是X吧", re.compile(r"还是[^。！？\n]{1,16}?(?:吧|比较好|算了)")),
    ("你以为X其实Y", re.compile(r"你以为[^。！？\n]{1,14}[，,]\s*其实")),
    ("这哪是X这是Y", re.compile(r"这哪是[^。！？\n]{1,12}[，,]\s*这?是")),
]


def skeletons_in(text: str) -> set[str]:
    t = text or ""
    return {name for name, rx in SKELETONS if rx.search(t)}


# ---------- 短语层 ----------

_CJK = re.compile(r"[^\u4e00-\u9fa5A-Za-z0-9]+")


def ngrams(text: str, n: int = 4) -> set[str]:
    s = _CJK.sub("", text or "")
    return {s[i:i + n] for i in range(max(0, len(s) - n + 1))}


def shared_phrases(a: str, b: str, n: int = 4) -> int:
    """两条一共有几个「连续 n 字」是相同的。

    「先想清楚你能不能接受异地」和「先确认你能不能接受异地」：
    3-gram 重合率只有 0.2，骨架也归不到一起，但人一眼看出来是同一句话。
    共同 4-gram 有 4 个（你能接受/能接受异/接受异地/受异地不），这个量能抓住它。
    """
    return len(ngrams(a, n) & ngrams(b, n))


# ---------- 开头 ----------

_PUNCT = re.compile(r"[\s，,。.！!？?~～…、；;：:\"'“”‘’（）()【】\[\]]+")


def head(text: str, n: int = 5) -> str:
    """开头 n 个字，去掉标点和表情。用来抓「几个号都用同一句话起头」。"""
    t = re.sub(r"\[[^\]]*\]", "", text or "")
    t = _PUNCT.sub("", t)
    return t[:n]


# ---------- 口水词配额 ----------

QUOTA_WORDS = ("蹲", "我也", "确实", "真的", "其实", "就是说", "笑死")


def overused(recent: list[str], window: int = 20, thr: int = 2) -> set[str]:
    """最近 window 条里出现 >= thr 次的口水词，本轮禁用。"""
    pool = [t for t in (recent or [])[-window:] if t]
    if not pool:
        return set()
    blob = "\n".join(pool)
    return {w for w in QUOTA_WORDS if blob.count(w) >= thr}


# ---------- 综合判定 ----------

def check(text: str,
          same_post: list[str] | None = None,
          recent: list[str] | None = None,
          action_thr: int = 2) -> str:
    """返回重写指令；空串表示这条能发。

    参数
    ----
    same_post : 同一条帖下面**已经发出去的**评论（含我们自己号的）。
    recent    : 全局最近发过的评论，用来压口水词和烂大街句式。
    action_thr: 同一个动作在全局最近若干条里出现几次后开始拦（默认 2 次）。
    """
    t = (text or "").strip()
    if not t:
        return ""

    same_post = [x for x in (same_post or []) if x]
    recent = [x for x in (recent or []) if x]

    # 1) 同帖动作撞车。
    # 核心解法（找宿管 / 换寝 / 投诉 / 加微信）撞一次就算重；通用建议
    # （当面说开、自己先查查）撞了不算，只要求整组动作完全一样才算。
    mine = actions_in(t)
    if mine and same_post:
        theirs: set[str] = set()
        for s in same_post:
            theirs |= actions_in(s)
        overlap = mine & theirs
        core_hit = overlap & CORE_ACTIONS
        if core_hit or (overlap and mine == theirs):
            got = "、".join(sorted(core_hit or overlap))
            return (f"这帖下面已经有人说过「{got}」这条路了。别重复同一条建议，"
                    "换一个别人没提到的角度，或者直接说你自己会怎么做。")

    # 2) 同帖骨架撞车：同一帖下同一个句式已经用过
    sk = skeletons_in(t)
    if sk and same_post:
        used: set[str] = set()
        for s in same_post:
            used |= skeletons_in(s)
        hit = sk & used
        if hit:
            return (f"这帖下面已经有人用了「{'、'.join(sorted(hit))}」这种句式。"
                    "换个说法，别套同一个模板。")

    # 2.5) 同帖短语撞车：换词不换事。共同 4-gram >= 3（约 12 个字一样）就算重复。
    if same_post:
        for s in same_post:
            if shared_phrases(t, s) >= 3:
                return ("这帖下面已经有人说得和你几乎一样了。别换个词再说一遍，"
                        "换一个别人没提到的角度。")

    # 3) 全局骨架泛滥：最近 20 条里同骨架 >= 3 次
    if sk and recent:
        pool = recent[-20:]
        for name in sk:
            n = sum(1 for s in pool if name in skeletons_in(s))
            if n >= 3:
                return (f"「{name}」这个句式最近已经用过 {n} 次了。"
                        "换一个说法，别套同一个模板。")

    # 4) 同帖开头撞车
    h = head(t, 5)
    if h and len(h) >= 3:
        for s in same_post:
            if head(s, 5) == h:
                return f"这帖下面已经有人用「{h}…」起头了。换个开头，别从同一句话起手。"

    # 5) 口水词配额
    hot = overused(recent)
    if hot:
        used_hot = [w for w in hot if w in t]
        if used_hot:
            return (f"「{'、'.join(used_hot)}」最近说得太多了，这条别用，"
                    "换成别的话开头。")

    # 6) 全局动作配额：同一个动作最近 20 条里 >= 2 次
    if mine and recent:
        pool = recent[-20:]
        for a in mine:
            n = sum(1 for s in pool if a in actions_in(s))
            if n >= action_thr:
                others = mine - {a}
                if not others:      # 整条只有一个动作，且已经烂大街
                    return (f"「{a}」这个办法最近已经给过 {n} 次了。"
                            "换一条别的路，或者只说你自己的判断，别再给同一类建议。")

    return ""


def brief(text: str) -> str:
    """调试用：一条文本的动作 / 骨架 / 开头。"""
    return f"动作={sorted(actions_in(text))} 骨架={sorted(skeletons_in(text))} 开头={head(text)}"
