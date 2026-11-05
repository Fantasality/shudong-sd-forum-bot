"""发帖体检：抽若干条干跑，和真实语料的形态指标对比。

    python post_eval.py -n 12

只看六件事：长度、问句、表情、换行、时间开头、吃相关。
这六项是「怪味」的直接来源，改 prompt 之后跑一遍就知道有没有回来。
"""
from __future__ import annotations

import argparse
import logging
import random
import re
import statistics
import sys
import uuid
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import run as R                                      # noqa: E402
from szlike import human                            # noqa: E402
from szlike.tasks import scheduled_post               # noqa: E402

ACC = ["tcz_cs", "tcz_ce", "tcz_ma", "tcz_oe", "warm", "dsh_nm",
       "dsh_ps", "dsh_me", "ych_tr", "ych_me", "fut_ai", "fut_ds"]

FOOD = ("食堂", "吃", "饭", "面", "外卖", "奶茶", "烤", "汤", "饼", "咖啡", "零食", "夜宵")
TIME_HEAD = ("早上", "上午", "中午", "下午", "傍晚", "晚上", "凌晨", "昨晚", "今天", "昨天")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=12)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    logging.disable(logging.INFO)
    rng = random.Random(args.seed or None)
    accs = (ACC * 3)[: max(1, args.n)]
    rng.shuffle(accs)

    posts = []
    for acc in accs:
        cfg = R.load_config()
        client, store, brain, guard, ctx = R.build(cfg, acc, manual=True)
        ctx.dry_run = True
        ok, msg = scheduled_post(ctx, f"eval-{uuid.uuid4().hex[:6]}")
        text = msg.split("：", 1)[1].rsplit("（", 1)[0] if ok and "：" in msg else ""
        if text:
            posts.append((acc, text))

    if not posts:
        print("没有产出")
        return

    lens = [len(t) for _, t in posts]
    def rate(f):
        return sum(1 for _, t in posts if f(t)) / len(posts) * 100

    print(f"\n样本 {len(posts)} 条")
    for acc, t in posts:
        print(f"  [{acc:8}] ({len(t):2d}) {t}")
    print("\n指标          本次      目标/真实")
    print(f"长度 p50      {statistics.median(lens):5.0f}      30")
    print(f"长度 p90      {sorted(lens)[int(len(lens)*0.9)]:5d}      75")
    print(f"问句率        {rate(lambda t: '？' in t or '?' in t):5.0f}%    13%")
    print(f"表情率        {rate(lambda t: bool(re.search(r'\[[^\]]{1,6}\]', t))):5.0f}%    35%")
    print(f"换行率        {rate(lambda t: chr(10) in t):5.0f}%    15%")
    print(f"时间开头      {rate(lambda t: t[:2] in TIME_HEAD):5.0f}%    ≤30%")
    print(f"吃相关        {rate(lambda t: any(w in t for w in FOOD)):5.0f}%    ≤15%")
    print(f"平均数字      {sum(len(re.findall(r'\d+', t)) for _, t in posts)/len(posts):5.1f}     0.3")


if __name__ == "__main__":
    main()
