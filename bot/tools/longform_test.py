"""一次性：#XP76 男 / 女号长文能力测试。

走的是真实链路，不是另写一套 prompt：
    帖子详情 → 情绪/性别/姿态判定 → 长文档位 → 知识库 + 向量库同类高赞真实评论示范
    → 该号人设 prompt（含固定身份/校区）→ DeepSeek → human.shape 句号分段整形 → 六项评分卡

用法：
    python longform_test.py                  # 干跑：生成 + 评分 + 出报告，不发
    python longform_test.py --publish        # 把两条长评按 20-40s 间隔发到 #XP76 楼下
    python longform_test.py --code XP76      # 换一条帖（App 里的 base36 短码）

默认不发布：能力测试看的是产出，没必要往真人楼下丢两条东西。
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import run as R                                             # noqa: E402
from szlike import human, longform, mood, ops, quality, rag, whisper   # noqa: E402
from szlike.sentinel import make_ctx                        # noqa: E402

# 一个女性号、一个男性号。人设来自花名册，档位按语料里恋爱帖长评的两种典型写法：
# 共鸣 p50≈142 字（说自己也这样 + 一个更具体的画面），拆解 p75≈203 字（拆点 + 每点给做法）。
TARGETS = [
    {"key": "dsh_ps", "gender": "女", "who": "独墅湖·应用心理学大二 · 清醒痴女",
     "persona": "obsess", "kind": "建议"},
    {"key": "ych_mt", "gender": "男", "who": "阳澄湖·冶金工程大三 · 孤独男",
     "persona": "lone", "kind": "拆解"},
]

OUT = BASE / "logs" / "longform_XP76_test.md"


def _ngrams(s: str, n: int = 3) -> set[str]:
    s = re.sub(r"\s+", "", s or "")
    return {s[i:i + n] for i in range(max(0, len(s) - n + 1))}


def overlap(a: str, b: str) -> float:
    """跟楼下已有评论的 3-gram 重合率，防「复读机」。"""
    x, y = _ngrams(a), _ngrams(b)
    return len(x & y) / max(1, len(x))


def build_one(cfg: dict, post: dict, board: str, existing: list[str], t: dict) -> dict:
    ctx = make_ctx(cfg, t["key"])
    text = post["content"]
    scene = rag.classify(text, board)
    mp = mood.plan(text, board)

    pl = human.plan("comment", board, scene, ctx.rng("shape"))
    is_long = t["kind"] in longform.COMMENT_KINDS
    if is_long:
        spec = longform.COMMENT_KINDS[t["kind"]]
        lf = {"kind": t["kind"], "lo": spec["lo"], "hi": spec["hi"],
              "how": spec["how"], "avoid": spec["avoid"], "p": 1.0}
        pl = dict(pl, lo=lf["lo"], hi=lf["hi"], newline=True, longform=lf["kind"])
        system, user, enabled = ctx.prompt(
            "comment", post=text, style="先接住楼主问的那件事，再给判断和建议，别写成短回复",
            board=board, scene=scene, sample=text, post_id=post["id"],
            length=longform.brief_comment(lf), hint="", long_text=True,
        )
    else:
        lf = None
        system, user, enabled = ctx.prompt(
            "comment", post=text, style="随口接一句",
            board=board, scene=scene, sample=text, post_id=post["id"],
            length=human.brief(pl), hint=human.sticker_hint(pl), long_text=False,
        )

    # 账号档案里没有 gender 字段（花名册只有校区/学院/专业/性格），测试时显式注入。
    # 不写「我是男生」这种自我介绍，只约束称呼和视角，让性别自然带出来。
    system = (f"你的性别是{t['gender']}，写到自己或对方时按这个来，不用专门声明性别。\n\n"
              + system)
    system += f"\n\n{longform.mood_note(mp) if is_long else mood.brief(mp)}"
    system += ("\n\n不确定的具体事实（别人的专业、给分、课表、具体日期、地名细节）不许编，"
               "只写你自己能确定的感受、动作和场景。")
    if existing:
        joined = " / ".join(x[:36] for x in existing[:3])
        system += (f"\n\n楼下已经有人说过这些：{joined}。"
                   "别复述他们的观点，也别把他们的例子换个说法再说一遍，讲你自己的具体事。")

    t0 = time.time()
    raw = ctx.brain.chat(system, user, max_tokens=1000, temperature=0.95)
    shaped = human.shape(raw or "", pl)
    retried = False
    if is_long and len(shaped) > pl["hi"] + 8:
        # 复用主链路的超长重写：random_comment 里超过 hi+12 就会重写一轮，
        # 测试不复用这一步，测出来的长度就不是线上真实表现。
        retried = True
        why = (f"刚才写了 {len(shaped)} 字，太长了。压到 {pl['lo']}-{pl['hi']} 字："
               "保留第一句的判断和那条带原话的建议，删掉重复的解释和多余的点，"
               "不要新加内容，也不要改成短回复。")
        raw2 = ctx.brain.chat(system, f"{user}\n\n{why}", max_tokens=800, temperature=0.7)
        if raw2:
            shaped = human.shape(raw2, pl)
    ms = int((time.time() - t0) * 1000)
    sc = quality.score(shaped, pl, campus=ctx.campus)

    return {
        "key": t["key"], "who": t["who"], "gender": t["gender"], "kind": t["kind"],
        "persona": ctx.persona, "campus": ctx.campus, "profile": ctx.profile,
        "mood": mp["mood"], "scene": scene,
        "lo": pl.get("lo"), "hi": pl.get("hi"), "enabled": enabled,
        "raw": raw or "", "text": shaped, "ms": ms,
        "len": len(shaped), "dots": shaped.count("。"),
        "paras": len([p for p in shaped.split("\n") if p.strip()]),
        "score": sc, "verdict": quality.verdict(sc, 80),
        "overlap": max((overlap(shaped, e) for e in existing), default=0.0),
        "finish": getattr(ctx.brain, "last_finish", ""),
        "prompt_len": len(system),
    }


def report(code: str, post: dict, board: str, existing: list[str],
           rows: list[dict], published: bool) -> str:
    L = []
    L.append(f"# #{code} 长文能力测试（男 / 女各一号）")
    L.append("")
    L.append(f"- 帖子：`{post['id']}` · {board} · {post['createdAt']}")
    L.append(f"- 原帖：{post['content']}")
    L.append(f"- 楼下原有 {len(existing)} 条评论：")
    for e in existing:
        L.append(f"  - {e}")
    L.append(f"- 模式：{'已发布到楼下' if published else '干跑（只生成，不发布）'}")
    L.append("")
    for r in rows:
        sc = r["score"]
        L.append(f"## {r['gender']}号 · {r['key']}（{r['who']}）· {r['kind']}档")
        L.append("")
        L.append(f"- 固定身份：{r['profile']}｜校区 {r['campus']}｜persona `{r['persona']}`")
        L.append(f"- 情绪判定：{r['mood']} / {r['scene']}｜目标 {r['lo']}-{r['hi']} 字")
        L.append(f"- 实测：**{r['len']} 字 / {r['dots']} 个句号 / {r['paras']} 段**"
                 f"｜生成 {r['ms']}ms（finish={r['finish']}）｜prompt {r['prompt_len']} 字")
        L.append(f"- 评分卡：**{sc['total']}/100 → {r['verdict']}**"
                 f"｜六项 " + " / ".join(f"{k} {v}" for k, v in sc["parts"].items()))
        L.append(f"- 与楼下已有评论 3-gram 重合：{r['overlap']:.1%}")
        if sc["fixes"]:
            L.append(f"- 扣分项：{'；'.join(sc['fixes'])}")
        L.append("")
        L.append("```text")
        L.append(r["text"])
        L.append("```")
        L.append("")
        if r["raw"] != r["text"]:
            L.append("<details><summary>模型原始输出</summary>")
            L.append("")
            L.append("```text")
            L.append(r["raw"])
            L.append("```")
            L.append("")
            L.append("</details>")
            L.append("")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", default="XP76")
    ap.add_argument("--publish", action="store_true", help="真的发到该帖楼下")
    args = ap.parse_args()

    code = args.code.strip().lstrip("#").upper()
    pid = whisper.code_to_id(code)
    if not pid:
        raise SystemExit(f"短码 {code} 解析失败")

    cfg = R.load_config()
    client, *_ = R.build(cfg, "warm", manual=True)
    post = ops.get_post(client, pid)
    board = ((post.get("topic") or {}).get("name") or "").strip()
    d = ops.list_comments(client, pid, order="LATEST")
    existing = [((e.get("node") or {}).get("content") or "").strip()
                for e in (d.get("edges") or [])]
    existing = [x for x in existing if x]

    print(f"#{code} -> {pid} | {board} | {post['createdAt']}")
    print(f"原帖：{post['content']}")
    print(f"楼下已有 {len(existing)} 条评论\n")

    rows = []
    for t in TARGETS:
        r = build_one(cfg, post, board, existing, t)
        rows.append(r)
        sc = r["score"]
        print(f"[{r['gender']}号 {r['key']} / {r['kind']}] {r['len']}字 "
              f"{r['dots']}句号 {r['paras']}段 | {sc['total']}分 {r['verdict']} "
              f"| 撞车 {r['overlap']:.1%} | {r['ms']}ms")
        print("-" * 70)
        print(r["text"])
        print("-" * 70 + "\n")

    if args.publish:
        for i, r in enumerate(rows):
            if not r["text"]:
                print(f"跳过 {r['key']}：空内容")
                continue
            ctx = make_ctx(cfg, r["key"])
            c = ops.create_comment(ctx.client, pid, r["text"])
            print(f"已发布 {r['key']} -> comment {c.get('id')}")
            r["comment_id"] = c.get("id")
            if i < len(rows) - 1:
                time.sleep(random.randint(20, 38))

    md = report(code, post, board, existing, rows, args.publish)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(md, encoding="utf-8")
    print(f"\n报告：{OUT}")


if __name__ == "__main__":
    main()
