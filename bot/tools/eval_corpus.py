"""从 raw.db 抽评估测试集：真实帖子 + 它的真实评论。

用途：量化「输入一条真实帖子，生成的评论能不能复现真人反应」。
抽的是 2024 年的历史帖，跟当前运营内容不重叠，可以当 held-out 用。

用法：
    python eval_corpus.py            # 生成 eval/testset.json
    python eval_corpus.py --n 40
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 运行目录（szlike_bot/kb/raw.db）优先，其次发布包自带的 kb/raw.db
_CANDS = [
    HERE.parent.parent.parent / "szlike_bot" / "kb" / "raw.db",
    HERE.parent.parent / "szlike_bot" / "kb" / "raw.db",
    HERE.parent / "kb" / "raw.db",
]
RAW = next((p for p in _CANDS if p.exists()), _CANDS[0])
OUT = HERE / "testset.json"

# 帖长区间：太短没信息量，太长模型容易跑题
LO, HI = 15, 180
MIN_CMT = 3


def load(n: int, seed: int = 20261105) -> dict:
    conn = sqlite3.connect(RAW)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """SELECT p.id, p.sub_name, p.topic_name, p.content, p.created_at, p.like_count
           FROM posts p
           WHERE length(p.content) BETWEEN ? AND ?
             AND p.id IN (SELECT post_id FROM comments
                          GROUP BY post_id HAVING COUNT(*) >= ?)
           ORDER BY p.id""",
        (LO, HI, MIN_CMT),
    ).fetchall()

    # 按板块分组，每个板块最多取 8 个，避免某个板块垄断
    by_board: dict[str, list] = defaultdict(list)
    for r in rows:
        by_board[r["sub_name"] or "未知"].append(r)

    rng = random.Random(seed)
    picked = []
    per = max(3, n // max(1, len(by_board)))
    for board, items in by_board.items():
        rng.shuffle(items)
        picked.extend(items[:per])
    rng.shuffle(picked)
    picked = picked[:n]

    cases = []
    for r in picked:
        cmts = [
            {"text": c["content"], "likes": c["like_count"] or 0}
            for c in conn.execute(
                "SELECT content, like_count FROM comments WHERE post_id=? "
                "AND length(content) BETWEEN 2 AND 200 ORDER BY like_count DESC",
                (r["id"],),
            )
        ]
        if len(cmts) < MIN_CMT:
            continue
        cases.append({
            "post_id": r["id"],
            "board": r["sub_name"] or "",
            "topic": r["topic_name"] or "",
            "post": r["content"],
            "created_at": r["created_at"],
            "like_count": r["like_count"] or 0,
            "real_comments": cmts[:8],   # 最多 8 条真实评论作参照
        })

    conn.close()
    return {
        "generated_at": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
        "source": str(RAW),
        "n": len(cases),
        "boards": sorted({c["board"] for c in cases}),
        "cases": cases,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40)
    args = ap.parse_args()
    data = load(args.n)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"测试集 {data['n']} 条 -> {OUT}")
    print("板块分布：")
    cnt: dict[str, int] = defaultdict(int)
    for c in data["cases"]:
        cnt[c["board"]] += 1
    for b, n in sorted(cnt.items(), key=lambda x: -x[1]):
        print(f"  {b:14s} {n}")
    lens = sorted(len(c["post"]) for c in data["cases"])
    print(f"帖长 中位{lens[len(lens)//2]} 最短{lens[0]} 最长{lens[-1]}")
    tot = sum(len(c["real_comments"]) for c in data["cases"])
    print(f"真实评论合计 {tot} 条，平均每帖 {tot/data['n']:.1f} 条")


if __name__ == "__main__":
    main()
