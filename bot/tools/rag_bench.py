"""检索层对比测试：稀疏 / +embedding / +rerank 三档，量延迟、看示范质量。

用法：
  python rag_bench.py                 # 用内置样例帖跑一遍
  python rag_bench.py --n 6 -v        # 打印完整示范
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from szlike import rag  # noqa: E402


def cfg_of(dense: bool, rerank: bool) -> dict:
    d = json.loads((BASE / "config.json").read_text(encoding="utf-8"))["rag"]
    d = json.loads(json.dumps(d))
    if not dense:
        d["embed"]["api_key"] = ""
    if not rerank:
        d["rerank"]["enabled"] = False
    return d


def run(query: str, board: str | None, dense: bool, rerank: bool, k: int) -> tuple[list[dict], int]:
    cfg = cfg_of(dense, rerank)
    t0 = time.perf_counter()
    docs = rag.retrieve(query, k=k, board=board, cfg=cfg, dense=dense, kind="pair")
    ms = int((time.perf_counter() - t0) * 1000)
    return docs, ms


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5, help="样例帖数量")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--k", type=int, default=3)
    args = ap.parse_args()

    import sqlite3
    raw = sqlite3.connect(BASE / "kb" / "raw.db")
    rows = raw.execute(
        "SELECT p.content, p.sub_name FROM posts p JOIN comments c ON c.post_id=p.id "
        "WHERE LENGTH(p.content) BETWEEN 12 AND 60 GROUP BY p.id "
        "ORDER BY p.comment_count DESC LIMIT 400").fetchall()
    # 均匀抽，避免全是热帖
    step = max(1, len(rows) // args.n)
    samples = rows[::step][: args.n]

    modes = [("稀疏", False, False), ("+embed", True, False), ("+rerank", False, True)]
    total = {m[0]: 0 for m in modes}
    for i, (text, board) in enumerate(samples, 1):
        print(f"\n{'='*70}\n[{i}] 板块 {board}｜{text[:70]}")
        for name, dense, rerank in modes:
            docs, ms = run(text, board, dense, rerank, args.k)
            total[name] += ms
            tops = " | ".join(f"{d['parent'][:22]}" for d in docs[:args.k])
            print(f"  {name:<12} {ms:>4}ms  {tops}")
            if args.verbose:
                for d in docs[:args.k]:
                    print(f"      {d['score']:.3f} <{d['board']}> {d['text'][:46]} → {d['parent']}")
    print(f"\n{'='*70}\n平均耗时（k={args.k}, n={len(samples)}）")
    for name, *_ in modes:
        print(f"  {name:<12} {total[name]/max(1,len(samples)):.0f}ms")


if __name__ == "__main__":
    main()
