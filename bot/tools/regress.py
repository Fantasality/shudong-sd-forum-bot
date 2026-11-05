"""全体回归：建议型长文 + 短内容不受影响。

跑 8 个场景：恋爱征询（真实 #XP76）、长情感帖、孤独帖走长文建议/拆解档；
抱怨、求助、吃的、日常走短档，确认这次大改没有把短内容改坏。

用法：python regress.py            # 干跑，不发
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import run as R                                   # noqa: E402
from szlike import human, ops, quality, whisper   # noqa: E402
from longform_test import TARGETS, build_one      # noqa: E402

F_ADVICE = {"key": "dsh_ps", "gender": "女", "who": "独墅湖·应用心理学大二 · 清醒痴女",
            "persona": "obsess", "kind": "建议"}
M_BREAK = TARGETS[1]
M_ADVICE = dict(TARGETS[1], kind="建议")
WIT = {"key": "wit", "gender": "男", "who": "天赐庄·法学大三 · 毒舌",
       "persona": "wit", "kind": "short"}
WARM = {"key": "warm", "gender": "女", "who": "独墅湖·汉语言文学大二 · 温柔",
        "persona": "warm", "kind": "short"}
FOOD = {"key": "food", "gender": "女", "who": "未来·药学大二 · 吃货",
        "persona": "food", "kind": "short"}
COOL = {"key": "cool", "gender": "男", "who": "天赐庄·物理学大三 · 冷淡",
        "persona": "cool", "kind": "short"}

LONG_LOVE = (
    "跟他在一起一年零四个月，异地八个月。他在南京我在苏州，高铁四十分钟，"
    "但这四十分钟我们一个月才走一次。上个月他生日我提前两周买了票，"
    "结果他临时说要跟实验室聚餐，我把票退了，手续费扣了二十多。"
    "我不是怪他聚餐，我是发现我已经不敢提前做任何安排了。"
    "每次视频他都说等忙完这阵，这话我从三月听到现在。"
    "想问问大家，异地到底是不是只能这样耗着"
)
LONE = "今天生日，一个人过的。早上起来没人记得，自己下楼买了个小蛋糕，回宿舍吃完了。也没觉得多惨，就是突然想，是不是我这种人就一直这样了"

CASES = [
    ("恋爱征询（真实 #XP76）· 女号建议", "REAL", "校园生活", F_ADVICE),
    ("恋爱征询（真实 #XP76）· 男号拆解", "REAL", "校园生活", M_BREAK),
    ("长情感帖（异地一年）· 女号建议", LONG_LOVE, "苏大恋爱日常", F_ADVICE),
    ("孤独帖（一个人过生日）· 男号建议", LONE, "今天不开心", M_ADVICE),
    ("抱怨帖（室友外放）· 短评", "室友天天晚上外放短视频到一点，说了三次没用，我真的服了", "今天不开心", WIT),
    ("求助帖（校园卡丢了）· 短评", "校园卡丢了怎么补办啊，中午还要吃饭", "求助提问箱", WARM),
    ("吃的帖（新窗口）· 短评", "二期食堂一楼新开了个窗口，排队的人好多，不知道好不好吃", "今天吃什么", FOOD),
    ("日常帖（满课）· 短评", "今天满课，从早上八点坐到晚上六点，腰断了", "抛抛帖子", COOL),
]

OUT = BASE / "logs" / "regress_advice.md"


def main() -> None:
    cfg = R.load_config()
    client, *_ = R.build(cfg, "warm", manual=True)
    real = ops.get_post(client, whisper.code_to_id("XP76"))

    L = ["# 全体回归：建议型长文 + 短内容不受影响", "",
         f"- 时间：{__import__('time').strftime('%Y-%m-%d %H:%M')}",
         "- 模式：干跑（真实链路生成 + 评分，不发布）", ""]
    long_rows, short_rows = [], []

    for name, content, board, t in CASES:
        post = real if content == "REAL" else {
            "id": "", "content": content, "createdAt": "", "topic": {"name": board}}
        r = build_one(cfg, post, board, [], t)
        sc = r["score"]
        advice = quality.sum_advice(r["text"]) if hasattr(quality, "sum_advice") else \
            sum(1 for w in quality._ADVICE if w in r["text"])
        scene = sum(1 for w in quality._SCENE_PORN if w in r["text"])
        ai = human.ai_score(r["text"])
        r["advice_n"], r["scene_n"], r["ai_n"] = advice, scene, ai
        (long_rows if r["kind"] in ("建议", "拆解", "分析") else short_rows).append(r)

        print(f"\n### {name}")
        print(f"[{r['key']} / {r['kind']}] {r['len']}字 {r['dots']}句号 {r['paras']}段 "
              f"| {sc['total']}分 {r['verdict']} | 建议{advice} 画面{scene} AI味{ai}")
        print(r["text"])

        L += [f"## {name}", "",
              f"- 账号 `{r['key']}`（{r['who']}）｜档位 {r['kind']}｜情绪 {r['mood']}",
              f"- **{r['len']} 字 / {r['dots']} 句号 / {r['paras']} 段**"
              f"｜评分 **{sc['total']} → {r['verdict']}**"
              f"｜建议词 {advice}｜画面堆砌 {scene}｜AI 味 {ai}",
              f"- 扣分项：{'；'.join(sc['fixes']) or '无'}", "",
              "```text", r["text"], "```", ""]

    L += ["## 汇总", "", "| 指标 | 长文（建议/拆解） | 短内容 |", "|---|---|---|"]
    if long_rows:
        L.append(f"| 条数 | {len(long_rows)} | {len(short_rows)} |")
        L.append(f"| 平均字数 | {sum(r['len'] for r in long_rows)/len(long_rows):.0f} | "
                 f"{sum(r['len'] for r in short_rows)/len(short_rows):.0f} |")
        L.append(f"| 平均建议词命中 | {sum(r['advice_n'] for r in long_rows)/len(long_rows):.1f} | "
                 f"{sum(r['advice_n'] for r in short_rows)/len(short_rows):.1f} |")
        L.append(f"| 画面堆砌命中 | {sum(r['scene_n'] for r in long_rows)} | "
                 f"{sum(r['scene_n'] for r in short_rows)} |")
        L.append(f"| 平均分 | {sum(r['score']['total'] for r in long_rows)/len(long_rows):.0f} | "
                 f"{sum(r['score']['total'] for r in short_rows)/len(short_rows):.0f} |")
        L.append(f"| 短内容句号总数 | — | {sum(r['dots'] for r in short_rows)} |")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(L), encoding="utf-8")
    print(f"\n报告：{OUT}")


if __name__ == "__main__":
    main()
