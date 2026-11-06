# -*- coding: utf-8 -*-
"""被真人质疑「是不是 AI」时的真人式否认。

2026-11-06 用户反馈：真人问「你们是不是 ai」时，我们的楼中楼回复答非所问，
很不好。真人在这种场合不会解释、不会自证、不会升华，只会短促地反问、
嫌弃、或者把话岔开。本模块负责识别质疑并给一句像人说的话。

刻意不做的事（都是上一轮翻车的原因）：
  - 不写「我是真人」「我不是 AI」——真人被这么问根本不会自证
  - 不解释、不道歉、不升华、不写长句
  - 不走 LLM —— 模型在这种场景极易生成客服腔或辩解
  - 不硬编码「AI 课」「人工智能专业」等正常讨论为质疑

语料依据：kb/raw.db 里 12,087 条 2-22 字短评论中，198 条是「啊？」「？」
「不是，哥们」「什么东西？」这类短促反问/嫌弃句；本模块的否认池按该形态写，
长度中位数控制在 6-10 字。零第三方依赖、零 LLM 调用、结果稳定可复现。
"""
from __future__ import annotations

import random
import re
import zlib

# ---------------------------------------------------------------- 识别

# AI / 批量号相关词。大小写不敏感，中文与英文混写都要认。
_TOPIC = re.compile(
    r"(?:ai|人工智能|机器人|机器号|机器|gpt|chatgpt|(?<![a-z])bot(?![a-z])|"
    r"deepseek|大模型|水军|"
    r"脚本|自动|程序|批量|小号|号矩阵|一眼假|复读机|人机)",
    re.I,
)

# 指向「你 / 这批号 / 这条内容」的质疑语气。
_POINTER = re.compile(
    r"(?:你|你们|这号|这个号|这些号|楼主|lz|这帖|这评论|这文案|发的|写的|"
    r"是不是|不会是|该不会|怕不是|吧|吗|呢|[？?！!]|谁|怎么|一群|全是|都是)",
    re.I,
)

# 本身就很强的指控句式，哪怕没有第二人称也算。
_STRONG = re.compile(
    r"(?:一眼假|ai\s*味|人机互评|复读机|水军|批量注册|统一话术|"
    r"小号矩阵|号矩阵|账号矩阵|矩阵号|"
    r"实锤|不像活人|不是活人|真人谁这么说话|滚粗|滚出去|"
    r"一眼\s*(?:gpt|ai|机器人)|太\s*ai\s*了|"
    r"谁把.{0,8}(?:ai|gpt|机器人).{0,6}接|不是真人|真人吗|活人吗|"
    r"全[是都].{0,6}(?:ai|机器|脚本)|这.{0,4}号.{0,6}(?:ai|机器))",
    re.I,
)

# 正常讨论 AI 的语境，优先级高于 _POINTER，避免误触发。
# 「AI 课怎么选」「用 GPT 帮我写论文」「人工智能专业好考吗」都不该被当成质疑。
_EXCLUDE = re.compile(
    r"(?:ai课|人工智能专业|选修|选课|学分|绩点|考研|作业|论文|帮我|推荐|"
    r"教程|代码|编程|翻译|怎么用|能不能|怎么选|软件|工具|考试|报名|讲座|"
    r"竞赛|实习|招聘|查重|降重|画图|做ppt|写摘要|四六级|课表|老师|教材|"
    r"机器学习|机器人社团|社团|招人|绘图|接单|练口语|口语|"
    r"会员|值不值|值得开|导论|给分|期末|这门课|点名)",
    re.I,
)


def is_accusation(text: str) -> bool:
    """对方这句话是不是在质疑本账号/这批内容是 AI 生成的。

    判定顺序很重要：先看强指控 → 再排除「正常讨论 AI」→ 最后要求
    「AI 相关词 + 指向对方的质疑语气」同时出现。
    """
    t = (text or "").strip().lower()
    if not t or len(t) > 120:
        return False
    if _STRONG.search(t):
        return True
    if _EXCLUDE.search(t):
        return False
    return bool(_TOPIC.search(t) and _POINTER.search(t))


# ---------------------------------------------------------------- 否认池

# 真人式否认：短、随意、带点不耐烦或好笑。没有一句在解释或自证。
_DENIALS: tuple[str, ...] = (
    "？",
    "啊？",
    "啊？这都看出来了",
    "不是吧",
    "不是，我就随手发的",
    "不是，随手回一句而已",
    "你这也太能想了",
    "别闹，我刚下课",
    "什么啊这是",
    "啊这",
    "离谱",
    "你没事吧",
    "咱能别这么玄乎吗",
    "笑死，我打字慢而已",
    "别给我扣帽子",
    "？我发个帖还被审了",
    "不至于吧",
    "你这话说的",
    "啊？我哪像了",
    "行了行了，散了吧",
    "我人还在这呢",
    "？别吓我",
    "不是哥们",
    "啊，行吧你开心就好",
)


def denial(seed: str = "", rng: random.Random | None = None) -> str:
    """按稳定 hash 取一句真人式否认；同一 seed 永远得到同一句。"""
    if rng is not None:
        return _DENIALS[rng.randrange(len(_DENIALS))]
    if not seed:
        seed = "default"
    idx = zlib.crc32(str(seed).encode("utf-8")) % len(_DENIALS)
    return _DENIALS[idx]


def reply_for(text: str, seed: str = "") -> str | None:
    """主入口：命中质疑返回一句否认，否则返回 None（交给原生成链路）。

    tasks.reply_comments() 用这个做前置分流；没命中时行为与原来完全一致。
    """
    if not is_accusation(text):
        return None
    return denial(seed or text)


def all_denials() -> tuple[str, ...]:
    """给离线验收用：返回完整否认池。"""
    return _DENIALS


if __name__ == "__main__":
    POS = (
        "你是 ai 吧", "你不会是机器人吧", "这是 ai 写的吗", "谁把 ai 接进来了",
        "一眼假", "AI味很足", "怎么还有人机互评", "这几个号是不是批量注册的",
        "复读机吧你", "你们是水军吗", "这号是脚本吧", "全是 ai 文案",
        "不是真人吧", "楼主是 GPT 吗", "这评论一股 deepseek 味",
        "谁把 gpt 接进来了", "这批号都是机器人", "你这话怎么像大模型说的",
        "该不会是自动发的吧", "小号矩阵啊这是",
    )
    NEG = (
        "AI 课怎么选", "人工智能专业好考吗", "用 GPT 帮我写论文",
        "AI 能不能过四级", "推荐个画图工具", "怎么用 ai 做 ppt",
        "人工智能导论在哪上课", "gpt 翻译靠谱吗", "AI 竞赛有人组队吗",
        "代码跑不通啊", "这个软件怎么用", "帮我看看作业",
        "深度学习教材推荐", "ai 查重会过吗", "四六级报名时间",
        "老师点名吗", "食堂今天开门没",
    )
    bad = [x for x in POS if not is_accusation(x)]
    wrong = [x for x in NEG if is_accusation(x)]
    print(f"正例 {len(POS) - len(bad)}/{len(POS)}，漏判：{bad}")
    print(f"反例 {len(NEG) - len(wrong)}/{len(NEG)}，误触发：{wrong}")
    print(f"否认池 {len(_DENIALS)} 句，长度中位 "
          f"{sorted(len(x) for x in _DENIALS)[len(_DENIALS) // 2]} 字")
    for s in ("你是 ai 吧", "谁把 ai 接进来了", "AI 课怎么选"):
        print(f"  {s!r} -> {reply_for(s)!r}")
    print("20 次取样：", [denial(f"s{i}") for i in range(8)])
    assert not bad and not wrong, "识别用例未全对"
