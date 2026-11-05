"""全量稠密向量层：给 9.1 万条真实语料建一份本地向量矩阵。

为什么要它
----------
倒排索引是**字面**的：「孤独」永远检索不到只写了「一个人吃饭」的帖子。而拟人恰恰
靠这类不共享字面的相似场景——同一个处境，别人用的是另一套说法。只有把语料整体
向量化，才能把「处境相似」和「用词相似」分开。

设计取舍
--------
- **float32 存储**：一开始用 fp16 省一半空间，实测整库扫描 349ms——其中 288ms 耗在
  fp16→fp32 的转换上，矩阵乘本身只要 12ms。改成 fp32 直接 memmap 相乘后降到 18ms，
  用 187MB 页缓存换 19 倍速度，这笔账很划算（页缓存是可回收的，不占进程匿名内存）。
- **分块矩阵乘**：按 65536 行一块扫，峰值内存受控，也避免大矩阵乘的线程抖动。
- **写时归一化**：向量入库前先 L2 归一，查询侧同样归一，余弦退化成一次点积。
- **增量续建**：`vecmap` 记 doc→ord→offset，进程挂了重启接着写，不重复花 token。

文件
----
kb/dense.db         vecmap(doc, ord, off, kind, board, day) + meta
kb/dense.f16        归一化向量，按 ord 顺序连续排列
kb/dense_meta.json  模型、维度、条数、构建时间
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

ROOT = Path(__file__).parent.parent
KB_DIR = ROOT / "kb"
DB = KB_DIR / "dense.db"
VEC = KB_DIR / "dense.f32"
VEC16 = KB_DIR / "dense.f16"
META = KB_DIR / "dense_meta.json"

DIM = 1024
_lock = threading.Lock()
_state: dict = {}


# ------------------------------------------------------------------ 小工具
def _cfg_embed(cfg: dict | None = None) -> dict:
    from . import rag
    return (rag._cfg(cfg).get("embed") or {})


def _connect() -> sqlite3.Connection:
    c = sqlite3.connect(DB, timeout=60)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS vecmap(
        doc TEXT PRIMARY KEY, ord INTEGER, off INTEGER,
        kind TEXT, board TEXT, day TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_vm_ord ON vecmap(ord)")
    c.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    return c


def _meta_get(c: sqlite3.Connection, k: str, dflt: str = "") -> str:
    r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else dflt


def _meta_set(c: sqlite3.Connection, k: str, v: str) -> None:
    c.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
              (k, v))


# ------------------------------------------------------------------ 建库
def _doc_text(row: tuple) -> str:
    """帖-评对把评论一起编进去：这一对的意义就在「评论怎么接」，只编码原帖会丢掉它。"""
    _id, kind, _board, _day, text, _likes, _cmts, parent = row
    t = " ".join((text or "").split())
    if kind == "pair" and parent:
        t = t[:160] + "　" + " ".join(parent.split())[:120]
    else:
        t = t[:280]
    return t


def _embed_batch(texts: list[str], cfg: dict) -> list[list[float]]:
    from . import rag
    e = _cfg_embed(cfg)
    last = None
    for attempt in range(4):
        try:
            r = rag._post_json(e["base_url"].rstrip("/") + "/embeddings", e["api_key"],
                               {"model": e.get("model") or "BAAI/bge-m3",
                                "input": texts, "encoding_format": "float"},
                               timeout=60.0)
            return [x["embedding"] for x in r["data"]]
        except Exception as ex:                      # 429 / 连接抖动 → 退避重试
            last = ex
            time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"embed 失败：{last}")


def build(batch: int = 48, workers: int = 6, limit: int | None = None,
          force: bool = False, log=print) -> dict:
    """增量构建/续建向量库。已有向量不会重算。"""
    from . import rag
    e = _cfg_embed(None)
    if not (e.get("api_key") and e.get("base_url")):
        raise RuntimeError("未配置 rag.embed.api_key")
    c = _connect()
    if force:
        c.execute("DELETE FROM vecmap")
        c.execute("DELETE FROM meta")
        c.commit()
        if VEC.exists():
            VEC.unlink()

    rows = rag.conn().execute(
        "SELECT id, kind, board, day, text, likes, cmts, parent FROM docs ORDER BY rowid"
    ).fetchall()
    if limit:
        rows = rows[:limit]
    done = {r[0] for r in c.execute("SELECT doc FROM vecmap")}
    todo = [r for r in rows if r[0] not in done]
    total, have = len(rows), len(rows) - len(todo)
    log(f"语料 {total:,} 条，已有 {have:,}，待编码 {len(todo):,}（dim={DIM}）")
    if not todo:
        _finalize(c, total)
        return {"docs": total, "new": 0, "elapsed": 0}

    # 续建前先对齐文件：上次崩溃可能留下半条尾巴
    expect = have * DIM * 4
    mode = "r+b" if VEC.exists() else "w+b"
    with open(VEC, mode) as f:
        if VEC.stat().st_size > expect:
            f.truncate(expect)
        f.seek(expect)
        ord0 = have
        t0 = time.time()
        done_n = 0

        batches = [todo[i:i + batch] for i in range(0, len(todo), batch)]

        def work(group):
            return _embed_batch([_doc_text(r) for r in group], None)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            for bi, (group, vecs) in enumerate(zip(batches, ex.map(work, batches)), 1):
                arr = np.asarray(vecs, dtype=np.float32)
                norms = np.linalg.norm(arr, axis=1, keepdims=True)
                arr = arr / np.maximum(norms, 1e-9)
                f.write(arr.astype(np.float32).tobytes())
                base = ord0 + done_n
                ins = [(r[0], base + i, base + i, r[1], r[2], r[3])
                       for i, r in enumerate(group)]
                c.executemany("INSERT OR REPLACE INTO vecmap VALUES(?,?,?,?,?,?)", ins)
                c.commit()
                done_n += len(group)
                if bi % 20 == 0 or bi == len(batches):
                    el = time.time() - t0
                    rate = done_n / el if el else 0
                    left = (len(todo) - done_n) / rate if rate else 0
                    log(f"  {done_n:,}/{len(todo):,}  {rate:5.1f} 条/s  剩余 {left:4.0f}s")
        f.flush()
        os.fsync(f.fileno())
    _finalize(c, total)
    el = time.time() - t0
    log(f"完成：{total:,} 条 / {(VEC.stat().st_size) / 1048576:.0f}MB / {el:.0f}s")
    return {"docs": total, "new": len(todo), "elapsed": round(el, 1)}


def _finalize(c: sqlite3.Connection, total: int | None = None) -> None:
    n = c.execute("SELECT COUNT(*) FROM vecmap").fetchone()[0]
    _meta_set(c, "model", _cfg_embed(None).get("model") or "BAAI/bge-m3")
    _meta_set(c, "dim", str(DIM))
    _meta_set(c, "count", str(n))
    _meta_set(c, "built_at", time.strftime("%Y-%m-%d %H:%M:%S"))
    c.commit()
    payload = {"model": _meta_get(c, "model"), "dim": DIM, "count": n,
               "built_at": _meta_get(c, "built_at"), "file": VEC.name}
    META.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with _lock:
        _state.clear()


# ------------------------------------------------------------------ 查询
def migrate() -> bool:
    """老版本是 fp16 存的，检测到就一次性转成 fp32，行顺序完全不动。"""
    if VEC.exists() or not VEC16.exists():
        return False
    c = _connect()
    n = c.execute("SELECT COUNT(*) FROM vecmap").fetchone()[0]
    c.close()
    if not n:
        return False
    mm = np.memmap(VEC16, dtype=np.float16, mode="r", shape=(n, DIM))
    with open(VEC, "wb") as f:
        for s in range(0, n, 16384):
            f.write(np.asarray(mm[s:s + 16384], dtype=np.float32).tobytes())
    del mm
    try:
        VEC16.unlink()
    except Exception:
        pass
    c = _connect()
    _meta_set(c, "file", VEC.name)
    c.commit()
    c.close()
    try:                                   # meta 里的文件名要跟着改，否则前端显示错
        m = json.loads(META.read_text(encoding="utf-8"))
        m["file"] = VEC.name
        META.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    with _lock:
        _state.clear()
    return True


def ready() -> bool:
    migrate()
    if not (VEC.exists() and META.exists()):
        return False
    try:
        m = json.loads(META.read_text(encoding="utf-8"))
        return m.get("count", 0) > 0 and VEC.stat().st_size >= m["count"] * DIM * 4
    except Exception:
        return False


def _load() -> dict:
    with _lock:
        if _state:
            return _state
        m = json.loads(META.read_text(encoding="utf-8"))
        n, dim = int(m["count"]), int(m.get("dim") or DIM)
        mm = np.memmap(VEC, dtype=np.float32, mode="r", shape=(n, dim))
        c = _connect()
        rows = c.execute("SELECT doc, kind, board FROM vecmap ORDER BY ord").fetchall()
        c.close()
        if len(rows) != n:                     # 文件与映射不一致 → 视为不可用
            raise RuntimeError("vecmap 与向量文件行数不一致")
        ids = [r[0] for r in rows]
        kinds = [r[1] for r in rows]
        boards = [r[2] for r in rows]
        state = {"mm": mm, "n": n, "dim": dim, "ids": ids, "pos": {d: i for i, d in enumerate(ids)},
                 "kind": np.array([{"post": 0, "pair": 1}.get(k, 2) for k in kinds], dtype=np.int8),
                 "board": np.array(boards, dtype=object), "meta": m}
        _state.update(state)
        return _state


def prefetch() -> bool:
    """把向量文件读进页缓存。daemon/控制台启动时跑一次，省掉当天第一次检索的冷读。

    实测冷读 358MB 要 400ms，暖了只要 25ms——这条差距全在页缓存上。
    """
    if not ready():
        return False
    try:
        st = _load()
        mm, n = st["mm"], st["n"]
        for s in range(0, n, 16384):
            float(np.asarray(mm[s:s + 16384]).sum())
        return True
    except Exception:
        return False


def encode(text: str, cfg: dict | None = None) -> np.ndarray:
    from . import rag
    e = _cfg_embed(cfg)
    v = rag._embed([text[:400]], e["base_url"], e["api_key"],
                   e.get("model") or "BAAI/bge-m3")[0]
    a = np.asarray(v, dtype=np.float32)
    return a / max(float(np.linalg.norm(a)), 1e-9)


def search(qvec: np.ndarray, k: int = 240, kind: str | None = None,
           board: str | None = None, min_cos: float = 0.2) -> list[tuple[str, float]]:
    """整库打分 → 过滤 → 取 top-k。分块矩阵乘，常驻内存只占一个 33MB 块。"""
    st = _load()
    mm, n, dim = st["mm"], st["n"], st["dim"]
    if qvec.shape[0] != dim:
        return []
    scores = np.empty(n, dtype=np.float32)
    step = 65536
    q = np.ascontiguousarray(qvec, dtype=np.float32)
    for s in range(0, n, step):
        scores[s:s + step] = mm[s:s + step] @ q
    mask = np.ones(n, dtype=bool)
    if kind is not None:
        mask &= (st["kind"] == (0 if kind == "post" else 1))
    if board is not None:
        mask &= (st["board"] == board)
    scores[~mask] = -2.0
    kk = min(k, n)
    idx = np.argpartition(-scores, kk - 1)[:kk]
    idx = idx[np.argsort(-scores[idx])]
    ids = st["ids"]
    return [(ids[i], float(scores[i])) for i in idx if scores[i] >= min_cos]


def info() -> dict:
    if not ready():
        return {"ready": False}
    try:
        m = json.loads(META.read_text(encoding="utf-8"))
        m["ready"] = True
        m["size_mb"] = round(VEC.stat().st_size / 1048576, 1)
        m["loaded"] = bool(_state)
        return m
    except Exception as e:
        return {"ready": False, "err": str(e)}
