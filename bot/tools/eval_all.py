"""三层评估统一入口：L1 单条文本 + L2 账号时间序列 + L3 群体协同图谱。

原 eval_run.py 保持不动（原版保留），这里用子进程调它，再叠加新的 eval_graph。
总判定要求三层同时达标：只靠 L1 高分不足以放行 —— 2026-11-05 的教训。

用法
----
  python eval_all.py                  # 三层全跑（L1 较慢，约 10 分钟）
  python eval_all.py --skip-l1        # 只跑 L2/L3（快，约 1-3 分钟）
  python eval_all.py --offline        # L2/L3 只算确定性指标，不调 LLM
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BOT = HERE.parent
sys.path.insert(0, str(BOT))
sys.path.insert(0, str(HERE))

import eval_graph  # noqa: E402

L1_OUT = HERE / "eval_last.json"
OUT = HERE / "eval_all_last.json"
L1_MIN_TOTAL = 80.0


def run_l1(n: int = 0) -> dict:
    cmd = [sys.executable, str(HERE / "eval_run.py"), "--mode", "all"]
    if n:
        cmd += ["--n", str(n)]
    print("\n########## L1 单条文本评估（原 eval_run.py）##########", flush=True)
    t0 = time.time()
    p = subprocess.run(cmd, cwd=str(HERE), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=1800)
    tail = (p.stdout or "")[-2500:]
    print(tail, flush=True)
    if p.returncode != 0:
        print("L1 stderr:", (p.stderr or "")[-1200:], flush=True)
        return {"ok": False, "error": f"eval_run.py 退出码 {p.returncode}",
                "elapsed_s": round(time.time() - t0, 1)}
    if not L1_OUT.exists():
        return {"ok": False, "error": "eval_last.json 未生成"}
    d = json.loads(L1_OUT.read_text(encoding="utf-8"))
    totals = {k: float(v.get("total", 0)) for k, v in d.items() if isinstance(v, dict)}
    catches = {k: float(v.get("catch_rate", 0)) for k, v in d.items() if isinstance(v, dict)}
    ok = bool(totals) and all(v >= L1_MIN_TOTAL for v in totals.values())
    return {"ok": ok, "totals": totals, "catch_rates": catches,
            "min_total": L1_MIN_TOTAL, "elapsed_s": round(time.time() - t0, 1)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-l1", action="store_true")
    ap.add_argument("--l1-only", action="store_true")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--votes", type=int, default=6)
    ap.add_argument("--window", type=int, default=48)
    ap.add_argument("--n", type=int, default=0, help="L1 每种模式的样本数，0=全量")
    args = ap.parse_args()

    l1 = {"ok": None, "skipped": True}
    if not args.skip_l1:
        l1 = run_l1(args.n)

    l23 = {"skipped": True}
    if not args.l1_only:
        print("\n########## L2/L3 图谱对抗评估（eval_graph.py）##########", flush=True)
        r = eval_graph.run(votes=args.votes, window=args.window,
                           offline=args.offline)
        eval_graph.report(r)
        l23 = {"skipped": False, "pass": r["pass"],
               "graph_pass": r.get("graph_pass"),
               "gate_blockers": r.get("gate_blockers", []),
               "metrics": {g: r["metrics"][g]
                           for g in ("real", "before", "after", "after_gated")},
               "votes": {g: {"detection_rate": v.get("detection_rate")}
                         for g, v in r["votes"].items() if isinstance(v, dict)},
               "checks": r["checks"]}

    parts = []
    if not l1.get("skipped"):
        parts.append(("L1 单条文本", bool(l1.get("ok")),
                      f"综合分 {l1.get('totals')}（门槛 {L1_MIN_TOTAL}）"))
    if not l23.get("skipped"):
        hard = [c for c in l23["checks"] if not c.get("observe")]
        npass = sum(1 for c in hard if c["pass"])
        nb = len(l23.get("gate_blockers") or [])
        parts.append(("L2/L3 账号图谱", bool(l23.get("pass")),
                      f"{npass}/{len(hard)} 硬门槛；图谱规则"
                      f"{'PASS' if l23.get('graph_pass') else 'FAIL'}；"
                      f"上线阻断 {nb} 项"))
    overall = bool(parts) and all(p[1] for p in parts)

    print("\n" + "=" * 68)
    print("三层总判定")
    print("=" * 68)
    for name, ok, detail in parts:
        print(f"  {'✅' if ok else '❌'} {name:<20} {detail}")
    print(f"\n  总判定：{'PASS' if overall else 'FAIL'}")
    print("  注：L1 高分是必要条件，不是充分条件；被真人识破发生在 L3。")

    blockers = l23.get("gate_blockers") or []
    if blockers:
        print("\n  上线阻断项（代码层无法清零，不许靠放宽阈值放行）：")
        for i, blk in enumerate(blockers, 1):
            print(f"    {i}. {blk['name']}")
            print(f"       现状：{blk['detail']}")
            print(f"       待办：{blk['action']}")
    l1_ok = bool(l1.get("ok")) if not l1.get("skipped") else None
    if l1_ok and l23.get("graph_pass") and blockers:
        print("\n  放行结论：文本层与图谱规则均达标，但画像/实体阻断项未清零，"
              "主程序继续保持停机，不得重启。")

    OUT.write_text(json.dumps(
        {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
         "l1": l1, "l23": l23, "overall_pass": overall,
         "gate_blockers": blockers},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  已写入 {OUT}")


if __name__ == "__main__":
    main()
