"""抓取「真人账号」的行为片段，作为账号图谱层对抗式图灵评估的负对照。

只读：仅调用 list_posts / list_comments 两个 GraphQL query，不做任何 mutation，
也不写平台上的任何内容。结果覆盖写到同目录的 graph_refs.json。

用法（在 szlike_bot/ 目录下，运行前务必 unset 各类代理环境变量）：
    unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy
    python tools/graph_refs.py
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent          # .../szlike_bot/tools
BASE = HERE.parent                              # .../szlike_bot
sys.path.insert(0, str(BASE))

from szlike.client import Client  # noqa: E402
from szlike import ops            # noqa: E402

CFG_PATH = BASE / "config.json"
TOKENS_PATH = BASE / "retired" / "tokens_warm.json"
OUT_PATH = HERE / "graph_refs.json"

MIN_PAGES = 40          # 至少翻 40 页
MAX_PAGES = 80          # 目标不够时最多翻到 80 页
COMMENT_PAGES = 2       # 每帖最多 2 页评论
MIN_ACCOUNTS = 12       # 至少 12 个「每个账号 >=2 条动作」的真人账号
MIN_ITEMS = 150         # 输出总条数下限
TARGET_SPAN_H = 24.0    # 时间跨度尽量 >= 24 小时（软目标）
TEXT_LIMIT = 120

TZ8 = timezone(timedelta(hours=8))


def parse_ts(s: Any) -> float | None:
    """ISO8601 -> Unix 秒。createdAt 可能带 +08:00 / Z / 毫秒，解析失败返回 None。"""
    if not isinstance(s, str) or not s:
        return None
    try:
        # 按约定：Z 换成 +00:00 再交给 fromisoformat
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:                 # 极少数无时区字符串按 +08:00 处理
            d = d.replace(tzinfo=TZ8)
        return d.timestamp()
    except Exception:
        return None


def load_cfg() -> dict:
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


def excluded_ids(cfg: dict) -> set[str]:
    """config.accounts 里所有 user_id + my_user_id，一律排除（本方账号）。"""
    ids: set[str] = set()
    for v in (cfg.get("accounts") or {}).values():
        if isinstance(v, dict) and v.get("user_id"):
            ids.add(str(v["user_id"]))
    if cfg.get("my_user_id"):
        ids.add(str(cfg["my_user_id"]))
    return ids


def real_creator(cr: Any, excluded: set[str]) -> str | None:
    """返回可归属的真人 creator.id；本方账号 / 无 id / 匿名空 creator 一律丢弃。"""
    if not isinstance(cr, dict):
        return None
    cid = cr.get("id")
    if not cid or cid in excluded:
        return None
    return str(cid)


def creator_name(cr: dict, cid: str) -> str:
    prof = cr.get("profile")
    nick = prof.get("nickname") if isinstance(prof, dict) else None
    name = nick or cr.get("username") or ""
    if not name:
        name = cid[-8:]
    return str(name)


def make_item(node: dict, kind: str, target: str, ts: float,
              cr: dict, cid: str) -> dict:
    text = (node.get("content") or "").replace("\r", " ").replace("\n", " ")
    return {
        "creator_id": cid,
        "creator_name": creator_name(cr, cid),
        "ts": ts,
        "kind": kind,
        "target": target,
        "text": text[:TEXT_LIMIT],
    }


def retry(fn, *args, label: str = "") -> tuple[bool, Any]:
    """单次调用：失败重试 2 次，仍失败则返回 (False, None) 由调用方跳过。"""
    for attempt in range(3):
        try:
            return True, fn(*args)
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                print(f"[warn] {label} 连续3次失败，跳过: {e!r}", flush=True)
                return False, None
            time.sleep(0.8 + attempt)
    return False, None


def qualified_stats(items: list[dict]) -> tuple[int, int, float]:
    """返回 (账号数[每账号>=2条], 这些账号的总条数, 时间跨度小时)。"""
    counts = Counter(it["creator_id"] for it in items)
    n_accounts = sum(1 for v in counts.values() if v >= 2)
    kept = sum(v for v in counts.values() if v >= 2)
    ts = [it["ts"] for it in items]
    span_h = (max(ts) - min(ts)) / 3600.0 if ts else 0.0
    return n_accounts, kept, span_h


def main() -> None:
    cfg = load_cfg()
    excluded = excluded_ids(cfg)
    app_id = cfg["app_id"]
    school_id = cfg["school_id"]
    print(f"app_id={app_id} school_id={school_id} 排除本方账号 {len(excluded)} 个", flush=True)

    c = Client(app_id, school_id, str(TOKENS_PATH), min_interval=0.45)
    c.ensure_token()   # token 仍有效，不会触发任何 mutation

    all_items: list[dict] = []
    seen_posts: set[str] = set()
    seen_comments: set[str] = set()
    after: str | None = None
    pages_flipped = 0
    posts_seen = comments_seen = 0
    n_posts = n_comments = 0

    for page in range(1, MAX_PAGES + 1):
        ok, d = retry(ops.list_posts, c, school_id, "LATEST", after,
                      label=f"feed page {page}")
        if not ok:
            # 游标链断了没法继续翻页，保留已抓到的数据
            break
        pages_flipped += 1
        edges = d.get("edges") or []
        page_posts: list[dict] = []

        for e in edges:
            n = e.get("node") or {}
            pid = n.get("id")
            if not pid or pid in seen_posts:
                continue
            seen_posts.add(pid)
            posts_seen += 1
            cr = n.get("creator")
            cid = real_creator(cr, excluded)
            ts = parse_ts(n.get("createdAt"))
            if cid and ts is not None:
                all_items.append(make_item(n, "post", pid, ts, cr, cid))
                n_posts += 1
            page_posts.append(n)

        # 对 commentCount>0 的帖子拉评论（每帖最多 COMMENT_PAGES 页）
        for n in page_posts:
            if (n.get("commentCount") or 0) <= 0:
                continue
            pid = n["id"]
            ca: str | None = None
            for cpi in range(COMMENT_PAGES):
                ok, cd = retry(ops.list_comments, c, pid, "LATEST", ca,
                               label=f"comments {pid} p{cpi + 1}")
                if not ok:
                    break
                for ce in (cd.get("edges") or []):
                    cm = ce.get("node") or {}
                    mid = cm.get("id")
                    if not mid or mid in seen_comments:
                        continue
                    seen_comments.add(mid)
                    comments_seen += 1
                    ccr = cm.get("creator")
                    ccid = real_creator(ccr, excluded)
                    cts = parse_ts(cm.get("createdAt"))
                    if ccid and cts is not None:
                        # 评论的 target 填所属 post_id，便于图谱统计同帖共现
                        all_items.append(make_item(cm, "comment", pid, cts, ccr, ccid))
                        n_comments += 1
                pi = cd.get("pageInfo") or {}
                if (cpi == COMMENT_PAGES - 1
                        or not pi.get("hasNextPage")
                        or not pi.get("endCursor")):
                    break
                ca = pi["endCursor"]

        n_accounts, kept, span_h = qualified_stats(all_items)
        print(f"[page {page:>2}] posts_seen={posts_seen} comments_seen={comments_seen} "
              f"real_posts={n_posts} real_comments={n_comments} "
              f"accounts(>=2)={n_accounts} kept={kept} span={span_h:.1f}h",
              flush=True)

        pi = d.get("pageInfo") or {}
        if not pi.get("hasNextPage") or not pi.get("endCursor"):
            print("到达 feed 末尾，停止翻页", flush=True)
            break
        after = pi["endCursor"]

        # 至少 40 页 + 硬性目标达成；时间跨度 >=24h 为软目标，最多翻到 80 页
        if (page >= MIN_PAGES and n_accounts >= MIN_ACCOUNTS
                and kept >= MIN_ITEMS and span_h >= TARGET_SPAN_H):
            print("目标达成，停止翻页", flush=True)
            break

    # 只保留「每个账号 >=2 条动作」的账号；极端数据不足时放宽为全部
    counts = Counter(it["creator_id"] for it in all_items)
    qualified = {cid for cid, v in counts.items() if v >= 2}
    items = [it for it in all_items if it["creator_id"] in qualified]
    if len(items) < MIN_ITEMS or len(qualified) < MIN_ACCOUNTS:
        print("[warn] 合格账号/条数不足，放宽为全部条目", flush=True)
        items = all_items
        qualified = set(counts)
    items.sort(key=lambda x: x["ts"])

    n_accounts = len({it["creator_id"] for it in items})
    ts_vals = [it["ts"] for it in items]
    span_h = (max(ts_vals) - min(ts_vals)) / 3600.0 if ts_vals else 0.0

    out = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": "szlikeyou graphql LATEST feed",
        "n_items": len(items),
        "n_accounts": n_accounts,
        "items": items,
    }
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n==== 抓取统计 ====", flush=True)
    print(f"翻页数            : {pages_flipped}", flush=True)
    print(f"扫描帖子节点      : {posts_seen}", flush=True)
    print(f"扫描评论节点      : {comments_seen}", flush=True)
    print(f"真人帖子条目      : {n_posts}", flush=True)
    print(f"真人评论条目      : {n_comments}", flush=True)
    print(f"输出真人账号数    : {n_accounts}", flush=True)
    print(f"输出总条数        : {len(items)}", flush=True)
    print(f"时间跨度(小时)    : {span_h:.1f}", flush=True)
    print(f"JSON 落盘         : {OUT_PATH} ({OUT_PATH.stat().st_size} bytes)", flush=True)


if __name__ == "__main__":
    main()
