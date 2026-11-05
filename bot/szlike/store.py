"""SQLite 状态库：记录已点赞/已评论/已回复，避免重复动作。"""
from __future__ import annotations

import sqlite3
import threading
import time


class Store:
    def __init__(self, path: str = "state.db", ns: str = ""):
        # ns 用来把不同账号的去重记录/配额隔开。
        # 主账号用空串，兼容已有数据；其他账号用自己的名字做前缀。
        self.path = path
        self.ns = ns or ""
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=8.0)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        # 32 个号是 32 个进程，同时写这一张库。没有 busy_timeout 时后到的直接
        # 抛 database is locked，被上层吞掉就成了「这次没评论」——偶发但难查。
        self.conn.execute("PRAGMA busy_timeout=8000")
        self._init()
        self._bootstrap_ts = time.time()

    def _k(self, kind: str) -> str:
        return f"{self.ns}:{kind}" if self.ns else kind

    def _init(self) -> None:
        with self.conn:
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS actions (
                       kind   TEXT NOT NULL,
                       target TEXT NOT NULL,
                       ts     REAL NOT NULL,
                       meta   TEXT DEFAULT '',
                       PRIMARY KEY (kind, target)
                   )"""
            )
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS contents (
                       id      INTEGER PRIMARY KEY AUTOINCREMENT,
                       kind    TEXT NOT NULL,
                       text    TEXT NOT NULL,
                       ts      REAL NOT NULL
                   )"""
            )
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS workers (
                       name   TEXT PRIMARY KEY,
                       ts     REAL NOT NULL,
                       status TEXT DEFAULT '',
                       note   TEXT DEFAULT ''
                   )"""
            )
            # 待审草稿。发帖不再「生成即发」：先评分，不过线就压在这里等人点头。
            # 这张表是全局的（不带 ns），用 account 字段区分是哪个号。
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS drafts (
                       id       INTEGER PRIMARY KEY AUTOINCREMENT,
                       account  TEXT NOT NULL,
                       label    TEXT DEFAULT '',
                       campus   TEXT DEFAULT '',
                       content  TEXT NOT NULL,
                       score    INTEGER DEFAULT 0,
                       detail   TEXT DEFAULT '',
                       fixes    TEXT DEFAULT '',
                       topic    TEXT DEFAULT '',
                       board    TEXT DEFAULT '',
                       subtopic_id TEXT DEFAULT '',
                       topic_id    TEXT DEFAULT '',
                       form     TEXT DEFAULT '',
                       seed_doc TEXT DEFAULT '',
                       status   TEXT DEFAULT 'pending',
                       note     TEXT DEFAULT '',
                       created  REAL NOT NULL,
                       decided  REAL DEFAULT 0,
                       decided_by TEXT DEFAULT '',
                       published_id TEXT DEFAULT ''
                   )"""
            )
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_actions_ts ON actions(ts)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_contents_kind ON contents(kind, ts)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status, created)")
            # 跨号占坑。32 个 worker 是 32 个进程，「评论前先看别人评过没」这件事
            # 用文件读写做不到原子：大家同时读到空，然后同时发。
            # 靠主键唯一约束抢插，只有一个能成功，其余换帖。
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS claims (
                       kind   TEXT NOT NULL,
                       target TEXT NOT NULL,
                       acc    TEXT DEFAULT '',
                       ts     REAL NOT NULL,
                       PRIMARY KEY (kind, target)
                   )"""
            )

    # ---------- 跨进程占坑 ----------

    def claim_target(self, target: str, acc: str = "", kind: str = "comment",
                     hours: float = 6.0) -> bool:
        """抢占一条帖子，抢到返回 True。过期的坑会被回收。

        这是**唯一**能保证「同一条帖不会被两个号同时评论」的机制：
        别的号在生成评论之前就来抢，抢不到就换帖。
        """
        if not target:
            return False
        cutoff = time.time() - hours * 3600
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM claims WHERE kind=? AND ts<?", (kind, cutoff))
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO claims(kind,target,acc,ts) VALUES(?,?,?,?)",
                (kind, target, acc or "", time.time()),
            )
            return cur.rowcount == 1

    def release_target(self, target: str, kind: str = "comment") -> None:
        """生成失败/没发出去时把坑还回去，别占着不放。"""
        if not target:
            return
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM claims WHERE kind=? AND target=?", (kind, target))

    def claim_owner(self, target: str, kind: str = "comment") -> str:
        with self._lock:
            cur = self.conn.execute(
                "SELECT acc FROM claims WHERE kind=? AND target=? LIMIT 1", (kind, target)
            )
            row = cur.fetchone()
            return (row[0] if row else "") or ""

    # ---------- 动作去重 ----------

    def seen(self, kind: str, target: str) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "SELECT 1 FROM actions WHERE kind=? AND target=? LIMIT 1", (self._k(kind), target)
            )
            return cur.fetchone() is not None

    def mark(self, kind: str, target: str, meta: str = "") -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO actions(kind,target,ts,meta) VALUES(?,?,?,?)",
                (self._k(kind), target, time.time(), meta),
            )

    # ---------- 配额统计 ----------

    def count_since(self, kind: str, since_ts: float) -> int:
        with self._lock:
            cur = self.conn.execute(
                "SELECT COUNT(*) FROM actions WHERE kind=? AND ts>=?", (self._k(kind), since_ts)
            )
            return cur.fetchone()[0]

    def count_today(self, kind: str) -> int:
        now = time.localtime()
        midnight = time.mktime(
            (now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, now.tm_wday, now.tm_yday, -1)
        )
        return self.count_since(kind, midnight)

    def count_last_hour(self, kind: str) -> int:
        return self.count_since(kind, time.time() - 3600)

    # ---------- 内容去重 ----------

    def remember_text(self, kind: str, text: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO contents(kind,text,ts) VALUES(?,?,?)", (self._k(kind), text, time.time())
            )

    def recent_texts(self, kind: str, limit: int = 30) -> list[str]:
        with self._lock:
            cur = self.conn.execute(
                "SELECT text FROM contents WHERE kind=? ORDER BY id DESC LIMIT ?", (self._k(kind), limit)
            )
            return [r[0] for r in cur.fetchall()]

    # ---------- 给 Web UI 用 ----------

    def forget(self, kind: str, target: str) -> None:
        """删除线上内容后，把对应去重记录也清掉，允许重新操作。"""
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM actions WHERE kind=? AND target=?", (self._k(kind), target))

    def list_actions(self, kind: str | None = None, limit: int = 100) -> list[dict]:
        sql = "SELECT kind,target,ts,meta FROM actions"
        args: list = []
        if kind:
            sql += " WHERE kind=?"
            args.append(self._k(kind))
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [{"kind": r[0], "target": r[1], "ts": r[2], "meta": r[3]} for r in rows]

    def stats_today(self) -> dict:
        return {k: self.count_today(k) for k in ("post", "comment", "reply", "like")}

    def stats_hour(self) -> dict:
        return {k: self.count_last_hour(k) for k in ("post", "comment", "reply", "like")}

    # ---------- 选题去重 ----------

    def recent_meta(self, kind: str, limit: int = 20) -> list[str]:
        """最近用过的 meta（选题场景、角度等），用来避免连续重复。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT meta FROM actions WHERE kind=? ORDER BY ts DESC LIMIT ?",
                (self._k(kind), limit),
            ).fetchall()
        return [r[0] for r in rows if r[0]]

    # ---------- 多账号心跳 ----------

    def beat(self, name: str, status: str, note: str = "") -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO workers(name,ts,status,note) VALUES(?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET ts=excluded.ts,"
                "status=excluded.status,note=excluded.note",
                (name, time.time(), status, note[:200]),
            )

    def workers(self) -> dict:
        with self._lock:
            rows = self.conn.execute(
                "SELECT name,ts,status,note FROM workers"
            ).fetchall()
        return {
            r[0]: {"ts": r[1], "status": r[2], "note": r[3],
                   "ago": max(0, int(time.time() - r[1]))}
            for r in rows
        }

    # ---------- 待审草稿 ----------

    def draft_add(self, account: str, content: str, **kw) -> int:
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO drafts(account,label,campus,content,score,detail,fixes,topic,"
                "board,subtopic_id,topic_id,form,seed_doc,status,created) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (account, kw.get("label", ""), kw.get("campus", ""), content,
                 int(kw.get("score") or 0), kw.get("detail", ""), kw.get("fixes", ""),
                 kw.get("topic", ""), kw.get("board", ""), kw.get("subtopic_id", ""),
                 kw.get("topic_id", ""), kw.get("form", ""), kw.get("seed_doc", ""),
                 kw.get("status", "pending"), time.time()),
            )
            return int(cur.lastrowid)

    def draft_list(self, status: str | None = "pending", limit: int = 60) -> list[dict]:
        sql = ("SELECT id,account,label,campus,content,score,detail,fixes,topic,board,"
               "subtopic_id,topic_id,form,seed_doc,status,note,created,decided,decided_by,"
               "published_id FROM drafts")
        args: list = []
        if status:
            sql += " WHERE status=?"
            args.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self.conn.execute(sql, args).fetchall()
        cols = ("id account label campus content score detail fixes topic board "
                "subtopic_id topic_id form seed_doc status note created decided "
                "decided_by published_id").split()
        return [dict(zip(cols, r)) for r in rows]

    def draft_get(self, did: int) -> dict | None:
        rows = [d for d in self.draft_list(None, 500) if d["id"] == int(did)]
        return rows[0] if rows else None

    def draft_decide(self, did: int, status: str, *, content: str | None = None,
                     note: str = "", by: str = "web", published_id: str = "") -> None:
        sets = ["status=?", "decided=?", "decided_by=?", "note=?"]
        args: list = [status, time.time(), by, note[:300]]
        if content is not None:
            sets.append("content=?")
            args.append(content)
        if published_id:
            sets.append("published_id=?")
            args.append(published_id)
        args.append(int(did))
        with self._lock, self.conn:
            self.conn.execute(f"UPDATE drafts SET {','.join(sets)} WHERE id=?", args)

    def draft_pending(self) -> int:
        with self._lock:
            return int(self.conn.execute(
                "SELECT COUNT(*) FROM drafts WHERE status='pending'").fetchone()[0])

    def draft_expire(self, days: int = 3) -> int:
        """待审超过 N 天自动过期。

        不清理的话队列会一直涨：每天 32 条进来，看不过来就越堆越多，
        过期的帖子发出去时间也对不上（「今天下午」是三天前的今天）。
        """
        cut = time.time() - days * 86400
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE drafts SET status='expired', decided=?, decided_by='auto', "
                "note=? WHERE status='pending' AND created<?",
                (time.time(), f"超过 {days} 天没审，自动过期", cut))
            return int(cur.rowcount)

    def close(self) -> None:
        self.conn.close()
