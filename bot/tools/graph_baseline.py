# -*- coding: utf-8 -*-
"""真人图谱指标的窗口化基线标定（只用标准库）。

为什么要有这套
--------------
tools/eval_graph.py 把「burst5_max <= 2」当硬门槛，但真人对照组全量
burst5_max=6 —— 这个门槛是假阳性；而且真人 388 条 vs 本方 873 条样本量
不同，max 类指标（窗口内最大值）根本不可直接比：样本越多越容易撞出更大的
瞬时并发。正确做法是「同口径」：两边都切成同样条数的连续时间窗，看每个窗口
里指标的分布，再比 p50/p90/max/mean。

本模块提供
----------
  window_metrics(actions, window=48, samples=200, seed=7) -> dict
      对真人 Action 列表随机取 samples 个连续 window 条窗口，逐窗调用
      grapheval.metrics()，对 9 个关键指标输出 p50/p90/max/mean（分位数用
      线性插值）。eval_graph.py 可直接 import 这个函数复用到本方组，保证
      真人组 / 本方组用完全相同的窗口口径。

  main()
      读 tools/graph_refs.json -> 写 tools/graph_baseline.json。

窗口不足 window 条时的处理
--------------------------
选择「用现有长度」而不是「跳过」：若动作总数 n < window，则把整段 n 条
（即 min(window, n)）作为唯一窗口，samples 次采样都是这同一段，不报错也不
丢弃；n == 0 时直接返回各指标全 0 的统计结构。n >= window 时起点在
[0, n-window] 上均匀取，每个窗口都恰好 window 条。窗口按有放回方式采样
（random.Random(seed) 固定种子可复现，同一窗口可能被重复抽到）。

输入约定
--------
actions 是 grapheval.metrics() 接受的 Action 列表（字段 ts/acc/kind/target/
text/avatar/age_days），按 ts 升序。main() 用 grapheval.real_actions() 把
graph_refs.json 的 items 转成该格式，与 eval_graph.py 的 real 组完全一致。
"""
from __future__ import annotations

import json
import math
import random
import sys
from datetime import datetime
from pathlib import Path

# 让本文件既能 `python tools/graph_baseline.py` 直接跑，也能被
# `from graph_baseline import window_metrics` 导入（项目根进 sys.path）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from szlike import grapheval  # noqa: E402

HERE = Path(__file__).resolve().parent
REFS = HERE / "graph_refs.json"
OUT = HERE / "graph_baseline.json"

# 需要标定分布的指标（eval_graph.py 里 burst5_max 那条硬门槛就是踩了它的全量 max）
_BASELINE_KEYS = (
    "burst5_max", "burst10_max", "accounts_per_post_max",
    "multi_account_posts", "same_target_pairs", "hour_entropy",
    "median_gap_minutes", "default_avatar_rate", "cross_account_lexicon",
)


def _quantile(sorted_vals: list[float], q: float) -> float:
    """线性插值分位数（等价于 numpy percentile 默认 'linear'）。

    sorted_vals 必须已升序；q 在 [0,1]。位置 h=(n-1)*q，取
    x[floor(h)] + (h-floor(h)) * (x[ceil(h)] - x[floor(h)])。
    """
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    if n == 1:
        return float(sorted_vals[0])
    h = (n - 1) * q
    lo = math.floor(h)
    hi = min(lo + 1, n - 1)
    frac = h - lo
    return sorted_vals[lo] + frac * (sorted_vals[hi] - sorted_vals[lo])


def window_metrics(actions: list[dict], window: int = 48,
                   samples: int = 200, seed: int = 7) -> dict:
    """窗口化标定真人指标分布，返回 {指标: {p50, p90, max, mean}}。

    actions 按 ts 升序后用 random.Random(seed) 有放回地随机取 samples 个连续
    window 条窗口，每个窗口调 grapheval.metrics()，再对 _BASELINE_KEYS 的
    每个指标统计 p50 / p90 / max / mean（分位数线性插值，结果保留 3 位小数）。

    窗口不足 window 条时用现有长度（见模块文档字符串）：n < window 取整段 n
    条作为唯一窗口，n == 0 返回全 0 结构。固定 seed 可复现。
    """
    ordered = sorted(actions, key=lambda a: a["ts"])
    n = len(ordered)
    zero = {k: {"p50": 0.0, "p90": 0.0, "max": 0.0, "mean": 0.0}
            for k in _BASELINE_KEYS}
    if n == 0:
        return zero

    w = max(1, min(int(window), n))   # n < window 时用现有长度
    rng = random.Random(seed)
    collected = {k: [] for k in _BASELINE_KEYS}
    for _ in range(max(1, int(samples))):
        start = rng.randint(0, n - w)
        m = grapheval.metrics(ordered[start:start + w])
        for k in _BASELINE_KEYS:
            collected[k].append(m.get(k, 0))

    out = {}
    for k, vals in collected.items():
        s = sorted(vals)
        out[k] = {
            "p50": round(_quantile(s, 0.50), 3),
            "p90": round(_quantile(s, 0.90), 3),
            "max": round(max(vals), 3),
            "mean": round(sum(vals) / len(vals), 3),
        }
    return out


def main() -> dict:
    if not REFS.exists():
        raise SystemExit(f"找不到 {REFS}，拒绝输出可能是假数据的基线")
    real = grapheval.real_actions(str(REFS))
    if not real:
        raise SystemExit(f"{REFS} 没有可用真人动作，拒绝输出可能是假数据的基线")

    result = {
        "generated_at": datetime.now().astimezone().replace(microsecond=0).isoformat(),
        "source": "tools/graph_refs.json",
        "window": 48,
        "samples": 200,
        "seed": 7,
        "n_real_actions": len(real),
        "real_windows": window_metrics(real, window=48, samples=200, seed=7),
        "real_full": grapheval.metrics(real),
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1),
                   encoding="utf-8")

    rw = result["real_windows"]
    rf = result["real_full"]
    print(f"已写入 {OUT}")
    print(f"n_real_actions = {result['n_real_actions']}  "
          f"window={result['window']} samples={result['samples']} seed={result['seed']}")
    print("\n--- real_windows（窗口化分布）---")
    for k in ("burst5_max", "accounts_per_post_max"):
        v = rw[k]
        print(f"  {k:<24} p50={v['p50']:<8} p90={v['p90']:<8} "
              f"max={v['max']:<8} mean={v['mean']}")
    print("\n--- real_windows 全部 9 指标 ---")
    print(f"  {'指标':<24}{'p50':>9}{'p90':>9}{'max':>9}{'mean':>9}")
    for k, v in rw.items():
        print(f"  {k:<24}{v['p50']:>9}{v['p90']:>9}{v['max']:>9}{v['mean']:>9}")
    print("\n--- real_full（全量 grapheval.metrics）---")
    print(f"  default_avatar_rate = {rf['default_avatar_rate']}")
    print(f"  cross_account_lexicon = {rf['cross_account_lexicon']}")
    print(f"  burst5_max = {rf['burst5_max']}  "
          f"accounts_per_post_max = {rf['accounts_per_post_max']}  "
          f"burst10_max = {rf['burst10_max']}")
    return result


if __name__ == "__main__":
    main()
