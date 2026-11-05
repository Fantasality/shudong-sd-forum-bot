"""抓一点真实的楼中楼对话，做 reply 的评估基准。

raw.db 里只存了一级评论（is_sub 全是 0），没有楼中楼，所以没法评估
「回复别人的回复」写得像不像。这个脚本从线上抓「原帖 + 原评论 + 楼里已有的回复」。

产出 eval/replyset.json，每条含：
    post        原帖正文
    comment     被回复的那条一级评论
    thread      楼里在它之前的回复（按时间）
    their       这次要回的那句话
    real_replies 该楼里其他真实回复，作对比参照

用法：
    python eval_replies.py --n 40
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BOT = HERE.parent
sys.path.insert(0, str(BOT))

from szlike import ops                      # noqa: E402
from szlike.client import Client            # noqa: E402

RAW = BOT / "kb" / "raw.db"
CONFIG = BOT / "config.json"
OUT = HERE / "replyset.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40, help="要抓多少个楼中楼样本")
    ap.add_argument("--max-posts", type=int, default=120, help="最多翻多少个帖子")
    args = ap.parse_args()

    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    accs = [v for k, v in (cfg.get("accounts") or {}).items() if (v or {}).get("enabled")]
    acc = accs[0] if accs else {}
    tok = acc.get("tokens") or "tokens.json"
    c = Client(cfg["app_id"], cfg["school_id"], tokens_path=str(BOT / tok))

    conn = sqlite3.connect(RAW)
    # 评论多一点的帖子更可能有楼中楼
    posts = [r[0] for r in conn.execute(
        "SELECT id FROM posts WHERE comment_count >= 3 ORDER BY RANDOM() LIMIT ?",
        (args.max_posts,))]
    conn.close()

    out: list[dict] = []
    seen: set[str] = set()
    for i, pid in enumerate(posts):
        if len(out) >= args.n:
            break
        try:
            d = ops.list_comments(c, pid, order="LATEST")
        except Exception:
            continue
        for e in (d.get("edges") or []):
            n = e.get("node") or {}
            subs = (((n.get("subcomments") or {}).get("edges")) or [])
            if len(subs) < 2:
                continue          # 只有一条、凑不成对话
            cid = n.get("id")
            if not cid or cid in seen:
                continue
            try:
                full = ops.comment_context(c, cid)
            except Exception:
                continue
            post_txt = ((full.get("post") or {}).get("content") or "").strip()
            if not post_txt:
                continue
            chain = []
            for s in ((full.get("subcomments") or {}).get("edges") or []):
                sn = s.get("node") or {}
                txt = (sn.get("content") or "").strip()
                if txt:
                    chain.append({"text": txt, "at": sn.get("createdAt") or "",
                                  "id": sn.get("id")})
            if len(chain) < 2:
                continue
            seen.add(cid)
            out.append({
                "post": post_txt[:600],
                "comment": (n.get("content") or "").strip()[:300],
                "thread": [x["text"][:120] for x in chain[:-1]][:6],
                "their": chain[-1]["text"][:200],
                "real_replies": [x["text"][:200] for x in chain],
                "board": "",
                "post_id": pid,
                "comment_id": cid,
            })
            if len(out) >= args.n:
                break
        if (i + 1) % 20 == 0:
            print(f"  已翻 {i+1} 帖，收到 {len(out)} 个样本")
        time.sleep(0.4)

    data = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "n": len(out), "cases": out}
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n楼中楼样本 {len(out)} 条 -> {OUT}")
    for x in out[:3]:
        print(f"\n[帖] {x['post'][:50]}")
        print(f"[评] {x['comment'][:40]}")
        for t in x["thread"]:
            print(f"   - {t[:40]}")
        print(f"[回] {x['their'][:40]}")


if __name__ == "__main__":
    main()
