"""拟人度评测：拿真实语料的分布当尺子，量生成结果的偏差。

不碰线上帖池，直接从 kb/raw.db 随机抽样帖子，走**和线上完全一样**的链路
（persona 抽写法 → human 抽长度 → 检索示范 → 生成 → 客服味重写 → 形态修正），
然后对比：长度分位、表情率、句号结尾率、AI 词命中。

用法：
    python human_eval.py -n 8          # 每个账号 8 条评论
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import run as R
from szlike import human, persona, rag
from szlike.tasks import _clean_content

RAW = Path(__file__).parent / "kb" / "raw.db"


def sample_posts(n: int, seed: int = 0) -> list[tuple]:
    c = sqlite3.connect(RAW)
    rows = c.execute(
        "SELECT content, sub_name FROM posts "
        "WHERE LENGTH(content) BETWEEN 12 AND 160 "
        "ORDER BY RANDOM() LIMIT ?", (n,)).fetchall()
    c.close()
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=6)
    ap.add_argument("-a", action="append", default=None)
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    posts = sample_posts(args.n * 3)
    accs = args.a or ["warm", "wit", "zest"]
    rows = []
    for k, acc in enumerate(accs):
        cfg = R.load_config()
        client, store, brain, guard, ctx = R.build(cfg, acc, manual=True)
        ctx.dry_run = True
        batch = posts[k * args.n:(k + 1) * args.n]
        jobs, plans = [], []
        for content, board in batch:
            st = persona.pick_comment_style(ctx.persona)
            scene = rag.classify(content, board)
            pl = human.plan("comment", board, scene, ctx.rng("shape"))
            plans.append((pl, scene, st, board, content))
            system, user, enabled = ctx.prompt(
                "comment", post=content[:300], style=st, board=board, scene=scene,
                sample=content[:200], length=human.brief(pl))
            jobs.append({"system": system, "user": user,
                         "max_tokens": max(20, min(180, int(pl["hi"] * 2)))})
        got = brain.chat_many(jobs, workers=4)
        for (pl, scene, st, board, content), t in zip(plans, got):
            txt = _clean_content(t or "", lo=2, hi=max(60, pl["hi"] * 2)) or ""
            txt = human.shape(txt, pl)
            rows.append({"acc": acc, "board": board, "scene": scene, "style": st,
                         "band": f"{pl['lo']}-{pl['hi']}", "text": txt,
                         "len": len(txt), "sticker": "[" in txt and "]" in txt,
                         "dot": txt.endswith("。"), "ai": human.ai_score(txt)})

    st = human.style()["comment"]
    lens = sorted(r["len"] for r in rows)
    n = len(lens) or 1
    print(f"\n生成 {n} 条 | 长度中位 {statistics.median(lens):.0f} "
          f"(真实 {st['p50']}) | 最长 {lens[-1]} | 最短 {lens[0]}")
    print(f"表情率 {sum(r['sticker'] for r in rows)/n:.1%}（真实 {st['sticker_rate']:.1%}）"
          f" | 句号结尾 {sum(r['dot'] for r in rows)/n:.1%}（真实 {st['dot_rate']:.1%}）"
          f" | 客服词均分 {sum(r['ai'] for r in rows)/n:.2f}")
    for r in rows:
        print(f"  [{r['acc']:<4} {r['band']:>6} {r['len']:3d}字 ai={r['ai']}] {r['text']}")
    Path("logs/human_eval.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
