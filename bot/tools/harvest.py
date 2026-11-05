"""鼠洞知识库采集器 —— 把帖子与评论批量落到 kb/raw.db，供后续分析。

用法：
    python harvest.py posts              # 全板块抓帖（断点续传）
    python harvest.py comments --limit 2000
    python harvest.py all
    python harvest.py stat

设计要点：
- 每个板块单独记录游标，进程被杀也能接着跑。
- 双线程，但共用一个 Client（续期有文件锁，安全）。
- 只读，不写任何东西到对方服务器。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from szlike.client import Client
from szlike import ops

ROOT = Path(__file__).parent
DB = ROOT / "kb" / "raw.db"
APP_ID = "wxeeda8eef0cb4339e"
SCHOOL = "U2Nob29sOjIy"
TOKENS = "tokens_warm.json"
ACCOUNT = "account_warm.json"

Q_POSTS = """
query Q($f:PostFilter,$after:String) {
  posts(filter:$f, orderBy:LATEST, first:50, after:$after) {
    edges { node {
      id content createdAt likeCount commentCount isAnonymous
      topic { name }
      subtopic { name }
    } }
    pageInfo { hasNextPage endCursor }
  }
}
"""

Q_CMTS = """
query Q($pid:ID!,$after:String) {
  comments(postId:$pid, first:20, order:RANK, after:$after) {
    edges { node {
      id content createdAt likeCount isAnonymous
      subcomments(first:5) { edges { node { id content createdAt } } }
    } }
    pageInfo { hasNextPage endCursor }
  }
}
"""

DDL = """
CREATE TABLE IF NOT EXISTS posts(
  id TEXT PRIMARY KEY,
  sub_id TEXT, sub_name TEXT, topic_name TEXT,
  content TEXT, created_at TEXT,
  like_count INTEGER, comment_count INTEGER,
  anonymous INTEGER, day TEXT
);
CREATE INDEX IF NOT EXISTS idx_posts_sub ON posts(sub_id, created_at);
CREATE INDEX IF NOT EXISTS idx_posts_day ON posts(day);

CREATE TABLE IF NOT EXISTS comments(
  id TEXT PRIMARY KEY,
  post_id TEXT, content TEXT, created_at TEXT, like_count INTEGER,
  anonymous INTEGER, day TEXT, is_sub INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cmt_post ON comments(post_id);

CREATE TABLE IF NOT EXISTS state(k TEXT PRIMARY KEY, v TEXT);
"""


def db() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB, timeout=60)
    conn.executescript(DDL)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def get_state(conn, k: str, default=None):
    row = conn.execute("SELECT v FROM state WHERE k=?", (k,)).fetchone()
    return row[0] if row else default


def set_state(conn, k: str, v: str) -> None:
    conn.execute("INSERT INTO state(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


def client() -> Client:
    c = Client(APP_ID, SCHOOL, TOKENS, account_path=ACCOUNT, min_interval=0.12)
    c.login_basic()
    return c


# ---------------------------------------------------------------- posts
def harvest_subtopic(c: Client, conn: sqlite3.Connection, sub: dict, cutoff: str,
                     max_pages: int, lock: threading.Lock) -> int:
    key = f"cursor:{sub['id']}"
    after = get_state(conn, key)
    if get_state(conn, f"done:{sub['id']}") == "1":
        return 0
    page = 0
    got = 0
    while page < max_pages:
        try:
            d = c.gql(Q_POSTS, {"f": {"schoolId": SCHOOL, "subtopicId": sub["id"]},
                                "after": after}, "Q")["posts"]
        except Exception as e:
            print(f"  ! {sub['sub']} 第{page}页失败 {type(e).__name__}: {e}", flush=True)
            time.sleep(3)
            break
        edges = d.get("edges") or []
        if not edges:
            break
        rows = []
        oldest = None
        for e in edges:
            n = e["node"]
            ca = n.get("createdAt") or ""
            oldest = ca or oldest
            rows.append((
                n["id"], sub["id"], sub["sub"], (n.get("topic") or {}).get("name") or sub["topic"],
                (n.get("content") or "").replace("\u200b", "").strip(), ca,
                int(n.get("likeCount") or 0), int(n.get("commentCount") or 0),
                1 if n.get("isAnonymous") else 0, ca[:10],
            ))
        with lock:
            conn.executemany("INSERT OR IGNORE INTO posts VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
            conn.commit()
            set_state(conn, key, d["pageInfo"].get("endCursor") or "")
            conn.commit()
        got += len(rows)
        page += 1
        if oldest and oldest[:10] < cutoff:
            with lock:
                set_state(conn, f"done:{sub['id']}", "1")
                conn.commit()
            break
        if not d["pageInfo"].get("hasNextPage"):
            with lock:
                set_state(conn, f"done:{sub['id']}", "1")
                conn.commit()
            break
        after = d["pageInfo"].get("endCursor")
        if page % 10 == 0:
            print(f"    {sub['sub']} {page*50} 帖 · 最早 {oldest[:10]}", flush=True)
    print(f"  ✓ {sub['sub']} +{got}", flush=True)
    return got


def cmd_posts(args) -> None:
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    c = client()
    conn = db()
    subs = []
    for t in ops.topics(c, SCHOOL):
        for s in t["subtopics"]["edges"]:
            subs.append({"id": s["node"]["id"], "sub": s["node"]["name"], "topic": t["name"]})
    cutoff = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%d")
    print(f"板块 {len(subs)} 个，早于 {cutoff} 即停止", flush=True)
    lock = threading.Lock()
    total = 0
    bucket = [subs[i::args.workers] for i in range(args.workers)]
    done = [0] * args.workers

    def run(i):
        nonlocal total
        cc = client()
        cn = db()
        n = 0
        for s in bucket[i]:
            n += harvest_subtopic(cc, cn, s, cutoff, args.max_pages, lock)
        done[i] = n
        cn.close()

    ths = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(args.workers)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    total = sum(done)
    n = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    old = conn.execute("SELECT MIN(created_at) FROM posts").fetchone()[0]
    print(f"\n本轮新增 {total}，库内共 {n} 帖，最早 {old}", flush=True)


# ---------------------------------------------------------------- comments
Q_PICK = """
SELECT id, comment_count, sub_name, created_at FROM posts
WHERE comment_count > 0 AND id NOT IN (SELECT DISTINCT post_id FROM comments)
ORDER BY {order}
LIMIT ?
"""


def cmd_comments(args) -> None:
    c = client()
    conn = db()
    # 分层：高评论帖优先铺满，再补老帖，保证各板块/各时段都有
    picks: list[tuple] = []
    per_sub = max(20, args.limit // 13)
    for row in conn.execute("SELECT DISTINCT sub_name FROM posts").fetchall():
        sub = row[0]
        q = ("SELECT id, comment_count, sub_name, created_at FROM posts p "
             "WHERE sub_name=? AND comment_count>0 "
             "AND p.id NOT IN (SELECT DISTINCT post_id FROM comments) "
             "ORDER BY (comment_count*1.0 + (abs(random())%40)) DESC LIMIT ?")
        picks += conn.execute(q, (sub, per_sub)).fetchall()
    # 再按高赞/高评论补足
    if len(picks) < args.limit:
        q = ("SELECT id, comment_count, sub_name, created_at FROM posts p "
             "WHERE comment_count>0 AND p.id NOT IN (SELECT DISTINCT post_id FROM comments) "
             "ORDER BY comment_count DESC LIMIT ?")
        picks += conn.execute(q, (args.limit - len(picks),)).fetchall()
    seen = set()
    uniq = []
    for p in picks:
        if p[0] not in seen:
            seen.add(p[0])
            uniq.append(p)
    uniq = uniq[: args.limit]
    print(f"待抓评论的帖子 {len(uniq)} 条", flush=True)

    lock = threading.Lock()
    idx = [0]

    def worker(wid: int):
        cc = client()
        cn = db()
        while True:
            with lock:
                if idx[0] >= len(uniq):
                    return
                pid, ccnt = uniq[idx[0]][0], uniq[idx[0]][1]
                idx[0] += 1
                n = idx[0]
            try:
                d = cc.gql(Q_CMTS, {"pid": pid}, "Q")["comments"]
            except Exception as e:
                print(f"  ! {pid} {type(e).__name__}", flush=True)
                continue
            rows = []
            for e in d.get("edges") or []:
                nd = e["node"]
                rows.append((nd["id"], pid, (nd.get("content") or "").strip(),
                             nd.get("createdAt") or "", int(nd.get("likeCount") or 0),
                             1 if nd.get("isAnonymous") else 0,
                             (nd.get("createdAt") or "")[:10], 0))
            with lock:
                cn.executemany("INSERT OR IGNORE INTO comments VALUES(?,?,?,?,?,?,?,?)", rows)
                cn.commit()
            if n % 100 == 0:
                print(f"    {n}/{len(uniq)}", flush=True)
        cn.close()

    ths = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(args.workers)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    n_c = conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
    n_p = conn.execute("SELECT COUNT(DISTINCT post_id) FROM comments").fetchone()[0]
    print(f"\n评论 {n_c} 条，覆盖 {n_p} 帖", flush=True)


def cmd_stat(args) -> None:
    conn = db()
    p = conn.execute("SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM posts").fetchone()
    c = conn.execute("SELECT COUNT(*), COUNT(DISTINCT post_id) FROM comments").fetchone()
    print(f"帖子 {p[0]} 条  {p[1][:10] if p[1] else '-'} → {p[2][:10] if p[2] else '-'}")
    print(f"评论 {c[0]} 条，覆盖 {c[1]} 帖，均 {(c[0] / max(1, c[1])):.1f} 条/帖")
    print("\n按板块：")
    for r in conn.execute("SELECT sub_name, COUNT(*) n, AVG(LENGTH(content)) L, "
                          "AVG(comment_count) c FROM posts GROUP BY sub_name ORDER BY n DESC"):
        print(f"  {r[0]:10s} {r[1]:>6} 帖  均长 {r[2]:.0f} 字  均评论 {r[3]:.1f}")
    print("\n按年份：")
    for r in conn.execute("SELECT substr(day,1,4) y, COUNT(*) n FROM posts GROUP BY y ORDER BY y"):
        print(f"  {r[0]}  {r[1]}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-w", "--workers", type=int, default=2)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("posts")
    p.add_argument("--days", type=int, default=1095)
    p.add_argument("--max-pages", type=int, default=10000)
    p = sub.add_parser("comments")
    p.add_argument("--limit", type=int, default=1800)
    sub.add_parser("stat")
    args = ap.parse_args()
    if args.cmd == "posts":
        cmd_posts(args)
    elif args.cmd == "comments":
        cmd_comments(args)
    else:
        cmd_stat(args)


if __name__ == "__main__":
    main()
