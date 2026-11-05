"""发帖质量评分卡。

之前的问题不是 prompt 写得差，而是**没有出厂检验**：同一套 prompt 跑 20 条，
好的很好、坏的不能用，方差大到只能靠抽签。这个模块把「像不像人」拆成六个
可测维度，每一条发布前都要过一遍闸门。

权重按「多容易被本校人一眼看破」排，不是按好写程度：
- 断句 20：断在半句（「……就在里面躺着，下雨」）是最刺眼的，比长一点严重得多
- 长度 20：真人七成帖子不到 30 字，写长了一定像作文
- 开头 15：满屏「今天下午」比内容重复更容易暴露
- 结构 15：一条到底的逗号串，和真人的碎片/两行/问句混着发不一样
- 细节 15：每条都堆数字和牌子，是在完成「写细节」的作业
- 事实 15：跨校区食堂、别的校区的楼，本校人当场就能指出来
"""
from __future__ import annotations

import re

from . import human

# 每条帖子满分 100，各项上限即权重
W = {"length": 20, "end": 20, "open": 15, "shape": 15, "detail": 15, "fact": 15}

# 每个校区别的校区才有的词。命中就是硬错，不给部分分。
CAMPUS_DENY = {
    "天赐庄": ["独墅湖", "仁爱路", "炳麟", "阳澄湖", "济学路", "未来校区", "吴江",
               "久泳西路", "相城"],
    "独墅湖": ["天赐庄", "庄里", "十梓街", "阳澄湖", "济学路", "未来校区", "吴江",
               "久泳西路", "相城"],
    "阳澄湖": ["天赐庄", "庄里", "十梓街", "独墅湖", "仁爱路", "炳麟", "未来校区",
               "吴江", "久泳西路"],
    "未来": ["天赐庄", "庄里", "十梓街", "独墅湖", "仁爱路", "炳麟", "阳澄湖", "济学路", "相城"],
}

TIME_HEAD = ("早上", "上午", "中午", "下午", "傍晚", "晚上", "凌晨", "深夜",
             "昨晚", "今早", "今天", "昨天")

# 通知腔、汇报腔：帖子里出现基本可以断定是照着通知复述的
NOTICE = ("根据", "现将", "有关事项", "通知如下", "请各单位", "特此", "校历第",
          "教学周", "为进一步")

_DIGIT = re.compile(r"\d+(?:\.\d+)?")
# 「北区全家」也是具体地点。但「门口/楼下/路上/桌」这类词太宽：
# 塞一个「楼下」就够 detail 满分，正好奖励「路灯、台阶、桌面」这种场景装饰，
# 是「吃烤肠式」细节的制度性来源。只保留指向明确地点/场所的词。
_CONCRETE = ("食堂", "图书馆", "宿舍", "教室", "操场", "超市", "快递", "校车",
             "澡堂", "教学楼", "实验", "球场", "自习", "窗口", "驿站", "便利店",
             "本部", "东区", "北区", "一期", "二期", "校区",
             "全家", "罗森", "菜鸟",
             "医院", "门诊", "打印店", "水果店", "洗衣房", "水房", "工位")
# 有这些词就算真的在问，不用非得有问号
_ASK_WORD = re.compile(r"(吗|么|哪|几|多少|怎么|为什么|能不能|要不要|是不是|有没有|谁|啥|什么)")

# 建议型长文的「具体」不是地名，是可执行的东西：一个判断、一个动作、
# 一句能直接说出口的原话。原来的细节项要求「必须有地点或具体物」，
# 逼着模型给每条评论塞一个食堂或一份关东煮——「吃烤肠式」AI 味就是这么来的。
_ADVICE = ("直接", "别", "先", "可以", "问他", "问她", "说清楚", "讲清楚", "写下来",
           "约出来", "当面", "定个", "拉黑", "分手", "算了", "不值得", "别急着",
           "试着", "试试", "改成", "换成", "列出来", "问清楚", "谈清楚", "说明白",
           "摊开", "挑明", "问一句", "说一句")
_QUOTE = re.compile(r"[「“\"]")

# 我们自己产出过的典型「自我表演式画面」。真实用户也会写这些词，
# 所以只在长文里扣，而且要命中才扣，不做一刀切。
_SCENE_PORN = ("关东煮", "烤肠", "汤都凉", "路灯把影子", "影子拉得", "一根一根",
               "一小口一小口", "热气往上冒", "风挺大", "围巾绕", "输入框里打了删",
               "打了删删了打", "手指在屏幕上滑", "闭馆音乐", "砂锅端上来", "把手机扣在")

# 这些短结尾是完整的口语收束，不是残句。评分卡原来把它们当「最后一段只有一个词」，误杀。
_OK_TAIL = {"就这", "绝了", "服了", "笑死", "无语", "真行", "可以", "行吧", "离谱",
            "谢谢", "好的", "没事", "算了", "恶心", "值了", "亏了", "牛", "行", "对",
            "真的", "确实", "是吧", "也是", "完了", "寄了", "麻了", "破防"}


def _r(score: float, limit: int) -> int:
    return max(0, min(limit, int(round(score))))


# 板块名。它们出现在正文开头是模板泄漏：模型把「发到哪个板」当成了句子成分，
# 写出「新生专栏问一下电子餐券是不是只能专窗用」这种一句话里带两个主语的怪句。
BOARD_NAMES = ("抛抛帖子", "今天不开心", "今天吃什么", "苏大迷惑大赏", "苏大神奇动物",
               "苏大恋爱日常", "女孩，女孩", "打工电波", "新生专栏", "求助提问箱")


# 只有晴天才能干的事。下雨天写「在阳台晒太阳」「把被子晒了」是硬伤：
# 天气是所有人能当场验证的事实，写错一条整条就废了。
# 只做这一个方向——反向（晴天写淋雨）可能是回忆昨天，误伤太大。
_SUNNY_ONLY = ("晒太阳", "晒被子", "晒了一下午", "晒得暖", "阳光很好", "太阳很毒",
               "大太阳", "太阳底下")


def weather_clash(text: str) -> str:
    """正文和今天的天气打架了吗。返回原因，空串表示没问题。"""
    t = text or ""
    if not any(w in t for w in _SUNNY_ONLY):
        return ""
    try:
        from . import events
        if events.rain_now():
            return "今天在下雨，不能写晒太阳、晒被子、阳光很好这类"
    except Exception:
        pass
    return ""


def board_leak(text: str) -> str:
    """正文里出现了板块名的位置。返回命中的板块名，空串表示干净。"""
    t = (text or "").strip()
    for b in BOARD_NAMES:
        if t.startswith(b) or f"【{b}】" in t:
            return b
    return ""


def check_campus(text: str, campus: str) -> list[str]:
    """返回命中的跨校区词。空列表表示没问题。"""
    return [w for w in CAMPUS_DENY.get(campus or "", []) if w in (text or "")]


def scene_porn_count(text: str) -> int:
    """数命中的「自我表演式画面」短语个数。≥1 就该重写。"""
    return sum(1 for w in _SCENE_PORN if w in (text or ""))


def advice_gap(text: str) -> str:
    """长评的「有没有给真东西」出厂检查。

    一条判断、一点分析、一个可执行动作、一句能说出口的原话，四个都没有，
    这条长评就只是把情绪拉长。返回修改指令；空串表示合格。
    """
    t = (text or "").strip()
    if len(t) < 90:
        return ""
    n = sum(1 for w in _ADVICE if w in t)
    if n == 0 and not _QUOTE.search(t):
        return ("这条全是情绪和空话。重写：第一句给判断，再补一条能照做的建议，"
                "最好带一句可以直接说出口的原话。")
    return ""


def score(text: str, pl: dict | None = None, *, campus: str = "",
          recent: list[str] | tuple[str, ...] = ()) -> dict:
    """给一条帖子打分。返回总分、各项得分和「该怎么改」的具体建议。"""
    t = (text or "").strip()
    pl = pl or {}
    form = pl.get("form") or "one"
    lo, hi = int(pl.get("lo") or 10), int(pl.get("hi") or 40)
    n = len(t)
    parts: dict[str, int] = {}
    fixes: list[str] = []

    # ---------- 长度 ----------
    if n == 0:
        parts["length"] = 0
        fixes.append("正文是空的")
    elif n < max(4, lo - 4):
        parts["length"] = _r(W["length"] - (lo - 4 - n) * 2, W["length"])
        fixes.append(f"太短了（{n} 字），这件事还没说清楚")
    elif n > hi + 16:
        # 超长按每字扣分。这里宽松过一次：56 字的「一句话」形态只扣 11 分，
        # 最后还是 82 分放行，等于没拦住。
        parts["length"] = _r(W["length"] - (n - hi - 16) * 1.5, W["length"])
        fixes.append(f"太长了（{n} 字，目标是 {lo}-{hi}），只留一件事")
    else:
        parts["length"] = W["length"]

    # ---------- 断句 ----------
    end_ok = bool(t) and human.ends_ok(t)
    tail = t[-1] if t else ""
    if not end_ok or tail in "，,、；;：:（(【[" or re.search(r"[，,、；;]\s*[？！?!]\s*$", t):
        parts["end"] = 0
        fixes.append("句子断在半句上了，要把话说完")
    elif re.search(r"[，,、；;]\s*\[[^\]]{1,6}\]\s*$", t):
        parts["end"] = 0
        fixes.append("表情前面还挂着一个逗号，像是句号被吃掉了一半")
    elif re.search(r"\[[^\]]*$", t):
        parts["end"] = 0
        fixes.append("表情符号被截断了一半")
    else:
        parts["end"] = W["end"]

    # ---------- 开头 ----------
    head = t[:2]
    if head in TIME_HEAD:
        parts["open"] = 0
        fixes.append("用时间词开头了，删掉时间直接从事情写起")
    else:
        parts["open"] = W["open"]
        op = human.opener(t, 3)
        if op:
            same = sum(1 for x in recent if human.opener(x, 3) == op)
            if same >= 2:
                parts["open"] = _r(W["open"] * 0.45, W["open"])
                fixes.append(f"「{op}」这个开头最近用过 {same} 次了，换一个")

    # ---------- 结构 ----------
    has_nl = "\n" in t
    commas = len(re.findall(r"[，,]", t))
    segs = [s for s in re.split(r"[，,。！？!?；;]", t) if s.strip()]
    if form in ("two", "list") and not has_nl:
        parts["shape"] = _r(W["shape"] * 0.5, W["shape"])
        fixes.append("这一档要分行写，中间断一次")
    elif form in ("frag", "one") and has_nl:
        parts["shape"] = _r(W["shape"] * 0.6, W["shape"])
        fixes.append("这一档只要一句话，不用分行")
    elif form == "ask" and not _ASK_WORD.search(t) and "？" not in t and "?" not in t:
        parts["shape"] = _r(W["shape"] * 0.55, W["shape"])
        fixes.append("这一档要写成问句，加个「吗/哪/几」")
    elif len(segs) >= 3 and len(segs[-1].strip()) <= 3 and segs[-1].strip() not in _OK_TAIL:
        # 末段只有一个词（「……，下雨」），前面几句再完整，收尾也是没落地的。
        # 但「就这」「绝了」「服了」这些短词本身就是完整的收尾，不能算残句。
        parts["shape"] = _r(W["shape"] * 0.35, W["shape"])
        parts["end"] = min(parts["end"], _r(W["end"] * 0.4, W["end"]))
        fixes.append("最后一段只有一个词，补成完整的一句，或者直接删掉")
    elif commas >= 2 and n / max(1, len(segs)) < 6:
        # 「我过去看了一眼，就在里面躺着，下雨」：三个分句平均不到六字，
        # 不是短句节奏，是把碎片直接堆在一起，最后一段还没说完
        parts["shape"] = _r(W["shape"] * 0.5, W["shape"])
        fixes.append("碎句堆太多了，合并成一到两个完整的句子")
    elif commas >= 6 and n < 60:
        parts["shape"] = _r(W["shape"] * 0.55, W["shape"])
        fixes.append(f"一口气 {commas} 个逗号，像流水账，砍成一到两个动作")
    else:
        parts["shape"] = W["shape"]

    # ---------- 细节密度 / 有没有真东西 ----------
    nums = len(_DIGIT.findall(t))
    hits = sum(1 for w in _CONCRETE if w in t)
    ai = human.ai_score(t)
    # 只有长评论按「建议型」判。长帖是发帖不是给建议，仍按地点/实物看。
    advice_mode = bool(pl.get("longform")) and (pl.get("kind") or "comment") != "post"
    long_text = n >= 100
    advice = sum(1 for w in _ADVICE if w in t)
    has_quote = bool(_QUOTE.search(t))
    scene_porn = sum(1 for w in _SCENE_PORN if w in t)
    if ai >= 3:
        # 「岁月斑驳」「氤氲着一丝温柔」这类词在 7.8 万条真帖里出现 0-1 次，
        # 出现即判死。放进细节项是因为它和「堆形容词」是同一个毛病：没有具体东西可写。
        parts["detail"] = _r(W["detail"] * 0.45, W["detail"])
        fixes.append("有很重的 AI 腔（岁月/氤氲/不由得/治愈这类词），换成大白话")
    elif scene_porn >= 2 or (advice_mode and scene_porn >= 1):
        parts["detail"] = _r(W["detail"] * 0.4, W["detail"])
        fixes.append("在堆「关东煮/汤凉了/路灯影子」这种自我表演式画面。"
                     "删掉画面，改成对这件事的判断，加一条能照做的建议")
    elif advice_mode:
        if advice >= 2 or has_quote or (advice >= 1 and nums >= 1):
            parts["detail"] = W["detail"]
        elif advice == 1:
            parts["detail"] = _r(W["detail"] * 0.8, W["detail"])
        else:
            parts["detail"] = _r(W["detail"] * 0.45, W["detail"])
            fixes.append("全是抽象话。点破这件事卡在哪，再加一个能照做的动作，"
                         "最好带一句可以直接说出口的原话")
    elif nums >= 4:
        parts["detail"] = _r(W["detail"] * 0.4, W["detail"])
        fixes.append(f"堆了 {nums} 个数字，留一个就行")
    elif nums == 3:
        parts["detail"] = _r(W["detail"] * 0.7, W["detail"])
        fixes.append("数字有点多，最多留两个")
    elif nums <= 2 and hits >= 1:
        parts["detail"] = W["detail"]
    elif nums <= 2 and hits == 0 and advice >= 1:
        # 短建议句「找宿管记一次，比你说三次管用」：给了办法就是具体，不需要塞地名。
        # 原规则会把这种句子扣到 78 分触发重写，把好句子改坏。
        parts["detail"] = W["detail"]
    elif nums <= 2 and hits == 0 and n > 34:
        parts["detail"] = _r(W["detail"] * 0.65, W["detail"])
        fixes.append("没有具体地点或具体物，读起来是空的")
    else:
        parts["detail"] = _r(W["detail"] * 0.85, W["detail"])

    # ---------- 事实 ----------
    bad = check_campus(t, campus)
    leak = board_leak(t)
    if bad:
        parts["fact"] = 0
        fixes.append(f"出现了别的校区（{bad[0]}），你在{campus}")
    elif leak:
        parts["fact"] = _r(W["fact"] * 0.3, W["fact"])
        fixes.append(f"把板块名「{leak}」写进正文了，删掉它直接写事")
    elif weather_clash(t):
        parts["fact"] = 0
        fixes.append(weather_clash(t))
    elif any(w in t for w in NOTICE):
        parts["fact"] = _r(W["fact"] * 0.5, W["fact"])
        fixes.append("像在复述通知原文，改成你自己遇到的事")
    else:
        parts["fact"] = W["fact"]

    total = sum(parts.values())
    return {"total": total, "parts": parts, "fixes": fixes, "len": n, "form": form}


def verdict(res: dict, min_score: int = 80) -> str:
    """publish / rewrite / queue 三种结论。

    除了总分，还有「硬伤否决」：断在半句、跨校区这两项任意一项不满分，直接出局。
    只看总分是不够的——「雨大得伞都快撑不」能拿 85，六项里只有断句这一项失分，
    但这一项就是最刺眼的地方。宁可比总分更严。
    """
    parts = res["parts"]
    if parts.get("end", 0) < W["end"] or parts.get("fact", 0) < W["fact"]:
        return "rewrite" if res["total"] >= min_score - 12 else "queue"
    if res["total"] >= min_score:
        return "publish"
    if res["total"] >= min_score - 18 and len(res["fixes"]) <= 2:
        return "rewrite"
    return "queue"


def brief(res: dict, limit: int = 3) -> str:
    """给模型看的一句修改指令。"""
    if not res["fixes"]:
        return ""
    return "；".join(res["fixes"][:limit]) + "。按这个重写一条，不要在原句上删字。"
