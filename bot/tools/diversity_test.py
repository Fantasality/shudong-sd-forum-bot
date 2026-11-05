"""一次性：同一条帖，四个号分别去评论，看会不会又撞成一样。

用户看到的问题是「几个都是重复的内容」——同一帖下面三个号都在说「找宿管」，
换词不换事。这个脚本就是复现那个现场：拿一条真实帖，让四个不同人设的号
按真实链路各生成一条，前一条算「这帖下面已经有人说过」，逐条过反复读检查。

用法：
    python diversity_test.py                # 用默认帖
    python diversity_test.py --code XP76    # 指定帖
    python diversity_test.py --n 6          # 六个号
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import run as R                                             # noqa: E402
from szlike import antirepeat, human, mood, ops, quality, rag, whisper  # noqa: E402
from szlike.sentinel import make_ctx                        # noqa: E402

# 四个性格差别最大的号：冷漠的、内耗的、直给的、闷着的
ACCOUNTS = ["dsh_ch", "dsh_mu", "fut_ds", "ych_mt", "tcz_cs", "warm"]


def build_one(cfg: dict, post: dict, board: str, acc: str, acc_info: dict,
              seen: list[str], recent: list[str]) -> dict:
    ctx = make_ctx(cfg, acc)
    text = post["content"]
    scene = rag.classify(text, board)
    mp = mood.plan(text, board)
    pl = human.plan("comment", board, scene, ctx.rng("shape"))

    system, user, enabled = ctx.prompt(
        "comment", post=text, style="", board=board, scene=scene,
        sample=text, post_id=post["id"], length=human.brief(pl),
        hint=human.sticker_hint(pl),
    )
    if enabled:
        system = f"{system}\n\n{mood.brief(mp)}"
        if seen:
            body = "\n".join(f"- {x}" for x in seen[:8])
            system = (f"{system}\n\n【这帖下面已经有人说过的】\n{body}\n"
                      "上面这些角度、句式和开头都别再重复。换一个别人没提到的角度。")

    t0 = time.time()
    raw = ctx.brain.chat(system, user, max_tokens=max(40, int(pl["hi"] * 2.2)),
                         temperature=0.95) if enabled else None
    ms = int((time.time() - t0) * 1000)
    text1 = human.shape((raw or "").strip(), pl)
    dup = antirepeat.check(text1, same_post=seen, recent=recent)

    redone = ""
    if dup:
        system2 = system.replace("【这帖下面已经有人说过的】", "【这些路已经有人走过了】")
        raw2 = ctx.brain.chat(system2, f"{user}\n\n{dup}",
                              max_tokens=max(40, int(pl["hi"] * 2.2)), temperature=1.0)
        if raw2:
            redone = human.shape(raw2.strip(), pl)

    final = redone or text1
    score = quality.score(final, pl, campus=ctx.campus)

    return {
        "acc": acc, "label": acc_info.get("label") or acc,
        "campus": acc_info.get("campus") or "", "persona": acc_info.get("persona") or "",
        "mood": mp["mood"], "first": text1, "dup": dup, "redone": redone,
        "final": final, "score": score["total"], "verdict": quality.verdict(score),
        "actions": sorted(antirepeat.actions_in(final)),
        "skel": sorted(antirepeat.skeletons_in(final)),
        "head": antirepeat.head(final), "ms": ms,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", default="")
    ap.add_argument("--n", type=int, default=4)
    ap.add_argument("--publish", action="store_true")
    args = ap.parse_args()

    cfg = R.load_config()
    accounts = cfg.get("accounts") or {}

    client, *_ = R.build(cfg, "warm", manual=True)
    if args.code:
        pid = whisper.code_to_id(args.code.strip().lstrip("#"))
    else:
        pool = ops.feed_posts(client, cfg.get("school_id"), pages=1)
        pid = next(n["id"] for n in pool if len(n.get("content") or "") > 40)
    post = ops.get_post(client, pid)
    board = ""
    cd = ops.list_comments(client, pid, order="LATEST")
    seen = [((e.get("node") or {}).get("content") or "").strip()
            for e in (cd.get("edges") or [])]
    seen = [x for x in seen if x][:8]

    print(f"帖子 {pid}\n{post['content'][:200]}\n已有评论 {len(seen)} 条\n")

    recent = human.shared_recent(120)
    rows = []
    for acc in ACCOUNTS[:max(1, args.n)]:
        info = accounts.get(acc) or {}
        r = build_one(cfg, post, board, acc, info, seen + [x["final"] for x in rows], recent)
        rows.append(r)
        print(f"[{acc}] {r['score']}分 {r['verdict']} {r['ms']}ms")
        if r["dup"]:
            print(f"   首版被拦：{r['dup']}")
            print(f"   首版：{r['first'][:70]}")
            print(f"   重写：{r['final'][:70]}")
        else:
            print(f"   {r['final'][:90]}")
        print(f"   动作={r['actions']} 骨架={r['skel']} 开头={r['head']}")
        print()

    # 撞车统计
    n = len(rows)
    heads = [r["head"] for r in rows]
    acts = [set(r["actions"]) for r in rows]
    skels = [set(r["skel"]) for r in rows]
    dup_heads = n - len(set(heads))
    core_dup = 0
    for i in range(n):
        for j in range(i + 1, n):
            if acts[i] & acts[j] & antirepeat.CORE_ACTIONS:
                core_dup += 1
    skel_dup = 0
    for i in range(n):
        for j in range(i + 1, n):
            if skels[i] & skels[j]:
                skel_dup += 1
    pairs = max(1, n * (n - 1) // 2)
    print(f"=== {n} 条：开头撞 {dup_heads} 处 | 核心动作撞 {core_dup}/{pairs} 对 "
          f"| 句式撞 {skel_dup}/{pairs} 对 ===")

    out = BASE / "logs" / "diversity_test.md"
    with out.open("w", encoding="utf-8") as f:
        f.write(f"# 同帖 {n} 号评论多样性测试\n\n帖子 `{pid}`\n\n> {post['content'][:300]}\n\n")
        f.write(f"已有评论 {len(seen)} 条\n\n")
        for r in rows:
            f.write(f"## {r['acc']} · {r['label']}（{r['mood']}）\n\n")
            f.write(f"{r['score']} 分 / {r['verdict']} / {r['ms']}ms\n\n")
            if r["dup"]:
                f.write(f"首版被反复读拦下：{r['dup']}\n\n首版：{r['first']}\n\n")
            f.write(f"最终：\n\n> {r['final'].replace(chr(10), chr(10) + '> ')}\n\n")
            f.write(f"动作 {r['actions']}｜骨架 {r['skel']}｜开头 {r['head']}\n\n---\n\n")
        f.write(f"\n**开头撞 {dup_heads} 处；核心动作撞 {core_dup}/{pairs} 对；"
                f"句式撞 {skel_dup}/{pairs} 对**\n")
    print(f"\n报告：{out}")


if __name__ == "__main__":
    main()
