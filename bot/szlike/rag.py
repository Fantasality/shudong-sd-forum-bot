"""拟人检索层：从 7.8 万条真实帖子/评论里，为当前这条内容找「同类怎么说话」。

为什么这么做
------------
ChatHaruhi（arXiv 2308.09597）证明：把角色语料里**语义相近的原文**检索出来塞进
system prompt 当示范，比单纯写「你要像XX一样说话」效果好得多。这里把「角色」
换成「苏大学生」：给定一条待评论的帖子，检索出过去同类帖子下**别人怎么回**，
作为 few-shot 示范。

三段式检索（对齐 Generative Agents 的 recency/importance/relevance）
----------------------------------------------------------------
1. 稀疏召回：自建 2-3gram 倒排索引 + IDF 加权，纯本地，<50ms。
2. 稠密重排（可选）：一次 API 调用把 query + top50 候选一起 embed，算余弦。
   只 embed 候选集，不用给 7 万条语料建向量库，省时省钱。
3. Cross-encoder 重排（可选）：bge-reranker / Qwen3-Reranker，命中率最高但最贵。
没有 key 就退化成「稀疏 + LLM 判官」，仍然可用。

最后用 MMR 去重，避免三条示范长得一样。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import struct
import threading
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
KB_DIR = ROOT / "kb"
RAW = KB_DIR / "raw.db"
IDX = KB_DIR / "rag.db"

CJK = re.compile(r"[\u4e00-\u9fff]+")
NOISE = re.compile(r"https?://\S+|@\S+|\[[^\]]{1,8}\]")
# 示范文本要保留 [表情]，但必须去掉联系方式与人名指向
LEAK = re.compile(r"https?://\S+|@\S+|\d{5,}|(微信|qq|QQ|vx|VX|v信)\s*[:：]?\s*\S{0,15}")

# 情绪/意图场景：给「不同模块、不同情绪」用的路由表。
# key 用短名，能跟知识库 emotion_map 的长名做子串匹配（如 学业焦虑 ∈ 学业焦虑与绩点压力）
SCENES: dict[str, list[str]] = {
    "学业焦虑": ["绩点", "保研", "考研", "挂科", "复习", "考试", "期末", "期中", "成绩", "学分",
                 "转专业", "辅修", "论文", "实验报告", "听不懂", "学不会", "作业", "绩"],
    "考试受挫": ["考砸", "没考好", "挂了", "补考", "重修", "不会做", "写不完", "交白卷", "低分"],
    "情绪低落": ["难受", "崩溃", "想哭", "哭了", "emo", "压抑", "孤独", "失眠", "焦虑", "烦",
                 "累", "委屈", "不开心", "撑不住", "没意思", "难过", "心慌", "喘不过"],
    "人际孤独": ["室友", "舍友", "孤立", "排挤", "冷战", "社恐", "尴尬", "不敢", "没朋友",
                 "交不到", "一个人"],
    "家庭矛盾": ["爸妈", "家里", "父母", "催", "亲戚", "原生家庭", "吵架"],
    "恋爱困惑": ["喜欢", "表白", "暗恋", "分手", "复合", "对象", "恋爱", "心动", "暧昧", "crush"],
    "被冒犯": ["恶心", "离谱", "无语", "举报", "素质", "插队", "抽烟", "噪音", "扰民",
               "投诉", "曝光", "没素质"],
    "失物": ["丢了", "捡到", "遗失", "失物", "招领", "钱包", "校园卡", "钥匙", "雨伞", "耳机"],
    "身体不适": ["发烧", "疼", "医院", "生病", "感冒", "挂号", "校医院", "难受得"],
    "兼职打工": ["兼职", "打工", "实习", "工资", "老板", "排班", "押金", "家教"],
    "对校园管理不满": ["教务", "选课系统", "后勤", "管理", "规定", "通知", "办事", "盖章"],
    "被善意治愈": ["治愈", "温暖", "感动", "善意", "帮我", "谢谢", "好人"],
    "生活琐事": ["食堂", "外卖", "快递", "洗澡", "热水", "空调", "断电", "断网", "洗衣机",
                 "下雨", "天气", "早八", "起床", "迟到", "通勤", "校车"],
    "求助实操": ["怎么办", "求助", "请问", "有没有人", "推荐", "教程", "流程", "办理", "申请",
                 "补办", "怎么", "在哪", "多少钱"],
    "二手交易": ["出", "收", "二手", "转让", "价格", "包邮", "成色", "全新", "闲置"],
    "轻松分享": ["今天", "分享", "好可爱", "好看", "开心", "安利", "拍到了", "发现",
                 "笑死", "哈哈哈", "猫", "狗"],
}


def classify(text: str, board: str | None = None) -> str:
    """按关键词投票判场景。够快够稳，不需要模型。"""
    t = (text or "")[:300]
    board = board or ""
    if "失物" in board:
        return "失物"
    if "二手" in board or "拼" in board:
        return "二手交易"
    if "恋爱" in board:
        return "恋爱困惑"
    if "不开心" in board:
        return "情绪低落"
    if "吃什么" in board:
        return "生活琐事"
    best, best_n = "轻松分享", 0
    for scene, kws in SCENES.items():
        n = sum(1 for k in kws if k in t)
        if n > best_n:
            best, best_n = scene, n
    return best


def clean_exemplar(text: str, hi: int = 90) -> str:
    t = LEAK.sub("", text or "").strip()
    t = re.sub(r"\s+", " ", t)
    return t[:hi].strip()

_lock = threading.Lock()


# ------------------------------------------------------------------ 索引
def grams_of(text: str) -> set[str]:
    """中文 2-3gram + 英文单词，够用且不依赖分词库。"""
    t = NOISE.sub(" ", text or "")
    out: set[str] = set()
    for seg in CJK.findall(t):
        L = len(seg)
        for i in range(L - 1):
            out.add(seg[i:i + 2])
        for i in range(L - 2):
            out.add(seg[i:i + 3])
    for w in re.findall(r"[A-Za-z]{2,}", t):
        out.add(w.lower())
    return out


_tls = threading.local()


def conn(path: Path = IDX) -> sqlite3.Connection:
    """线程内复用连接：每次开库要重放 WAL 头，几十毫秒，别在热路径上做。"""
    key = str(path)
    c = getattr(_tls, "conn", None)
    if c is not None and getattr(_tls, "path", None) == key:
        return c
    if c is not None:
        try:
            c.close()
        except Exception:
            pass
    c = sqlite3.connect(path, timeout=60)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA cache_size=-64000")       # 64MB 页缓存，倒排扫描全靠它
    c.execute("PRAGMA temp_store=MEMORY")
    _tls.conn, _tls.path = c, key
    return c


def close() -> None:
    c = getattr(_tls, "conn", None)
    if c is not None:
        try:
            c.close()
        except Exception:
            pass
    _tls.conn = _tls.path = None


def build(rebuild: bool = False, limit: int | None = None) -> dict:
    """从 kb/raw.db 建倒排索引。可重复执行，默认增量补齐新文档。"""
    if rebuild and IDX.exists():
        close()
        IDX.unlink()
    raw = sqlite3.connect(RAW)
    c = conn()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS docs(
      id TEXT PRIMARY KEY, kind TEXT, board TEXT, day TEXT, text TEXT,
      likes INTEGER, cmts INTEGER, parent TEXT, n INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_docs_kind ON docs(kind, board);
    CREATE TABLE IF NOT EXISTS grams(gram TEXT, doc INTEGER);
    CREATE INDEX IF NOT EXISTS idx_grams ON grams(gram, doc);
    CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
    """)
    have = {r[0] for r in c.execute("SELECT id FROM docs")}
    sql = ("SELECT p.id, p.sub_name, p.day, p.content, p.like_count, p.comment_count "
           "FROM posts p WHERE p.content != ''")
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = [r for r in raw.execute(sql) if r[0] not in have]
    print(f"新增待索引 {len(rows)} 条（已有 {len(have)}）")
    t0 = time.time()
    c.execute("PRAGMA synchronous=OFF")
    B = 2000
    for i in range(0, len(rows), B):
        batch = rows[i:i + B]
        c.executemany(
            "INSERT OR REPLACE INTO docs VALUES(?,?,?,?,?,?,?,?,?)",
            [(r[0], "post", r[1], r[2], r[3], r[4] or 0, r[5] or 0, "", len(r[3])) for r in batch])
        g = []
        for r in batch:
            for gram in grams_of(r[3]):
                g.append((gram, r[0]))
        c.executemany("INSERT INTO grams VALUES(?,?)", g)
        c.commit()
        if (i // B) % 5 == 0:
            print(f"  {i + len(batch)}/{len(rows)}  {time.time() - t0:.0f}s", flush=True)
    c.execute("PRAGMA synchronous=NORMAL")
    # 「帖-评论」对：单独一类文档，保证检索到的示范一定有评论
    pairs = raw.execute(
        "SELECT p.id, p.sub_name, p.day, p.content, p.like_count, c.content, c.like_count "
        "FROM posts p JOIN comments c ON c.post_id = p.id "
        "WHERE LENGTH(c.content) BETWEEN 4 AND 46 AND LENGTH(p.content) >= 8"
    ).fetchall()
    seen = set()
    prows = []
    for pid, board, day, ptext, plike, ctext, clike in pairs:
        ct = clean_exemplar(ctext, 46)
        if not ct or ct.startswith(("回复", "回@", "楼主")) or len(grams_of(ct)) < 3:
            continue
        # 必须用稳定摘要：Python 的 hash() 每进程加盐，重启一次索引就多一批重复文档
        key = "pair:%s:%s" % (pid, hashlib.md5(ct.encode("utf-8")).hexdigest()[:10])
        if key in seen or key in have:
            continue
        seen.add(key)
        prows.append((key, "pair", board, day, clean_exemplar(ptext, 90),
                      plike or 0, clike or 0, ct, len(ptext)))
    if prows:
        c.executemany("INSERT OR REPLACE INTO docs VALUES(?,?,?,?,?,?,?,?,?)", prows)
        g = []
        for r in prows:
            for gram in grams_of(r[4]):
                g.append((gram, r[0]))
        c.executemany("INSERT INTO grams VALUES(?,?)", g)
        c.commit()
        print(f"帖-评论对 {len(prows)} 条")
    # df 表：每个 gram 出现在多少篇里，用来算 idf
    c.execute("DROP TABLE IF EXISTS df")
    c.execute("CREATE TABLE df AS SELECT gram, COUNT(DISTINCT doc) n FROM grams GROUP BY gram")
    c.execute("CREATE INDEX idx_df ON df(gram)")
    c.execute("INSERT OR REPLACE INTO meta VALUES('built_at', ?)",
              (time.strftime("%Y-%m-%d %H:%M:%S"),))
    c.commit()
    n_doc = c.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    n_gram = c.execute("SELECT COUNT(*) FROM grams").fetchone()[0]
    close()
    print(f"索引完成：{n_doc} 文档 / {n_gram} 倒排项 / {time.time() - t0:.0f}s")
    return {"docs": n_doc, "grams": n_gram}


# ------------------------------------------------------------------ 召回
def sparse(query: str, k: int = 50, kind: str = "post", board: str | None = None) -> list[dict]:
    """IDF 加权倒排召回。

    一条 SQL 说完：倒排表 join df 表，权重 log(1+N/df) 直接算在聚合里。
    比「先召回候选再回表重算」快 3 倍（长帖 260ms → 80ms），结果也更准——
    原来那版按命中数排序，罕见词和口水词同权。
    """
    qg = grams_of((query or "")[:180])      # 长帖尾巴信息密度低，还拖慢扫描
    if not qg:
        return []
    c = conn()
    N = c.execute("SELECT COUNT(*) FROM docs").fetchone()[0] or 1
    ph = ",".join("?" * len(qg))
    try:
        rows = c.execute(
            f"SELECT g.doc, SUM(ln(1.0 + {N}.0 / df.n)) s "
            f"FROM grams g INDEXED BY idx_grams JOIN df ON df.gram = g.gram "
            f"WHERE g.gram IN ({ph}) GROUP BY g.doc ORDER BY s DESC LIMIT 1200",
            tuple(qg)).fetchall()
    except sqlite3.OperationalError:        # 老 sqlite 没编 math 函数，退回两段式
        rows = _sparse_py(c, qg, N, 1200)
    if not rows:
        return []
    score = dict(rows)
    cand = list(score)
    ph2 = ",".join("?" * len(cand))
    docs: list[dict] = []
    for r in c.execute(
            f"SELECT id, kind, board, day, text, likes, cmts, parent "
            f"FROM docs WHERE id IN ({ph2})", tuple(cand)):
        if kind and r[1] != kind:
            continue
        docs.append({
            "id": r[0], "kind": r[1], "board": r[2], "day": r[3], "text": r[4],
            "likes": r[5], "cmts": r[6], "parent": r[7] or "",
            "sim": score.get(r[0], 0.0),
        })
    if board:
        same = [d for d in docs if d["board"] == board]
        if len(same) >= 4:                  # 同板块够用就锁板块
            docs = same
    top = _fuse(docs, k, board)
    for d in top:                           # 只给最终这几条算 gram，MMR 里要用
        d["g"] = grams_of(d["text"] + (" " + d["parent"] if d.get("parent") else ""))
    return top


def _sparse_py(c: sqlite3.Connection, qg: set[str], N: int, limit: int) -> list[tuple]:
    """无 math 函数时的兜底：先按命中数取候选，再回表按 idf 加权。"""
    ph = ",".join("?" * len(qg))
    idf = {g: math.log(1 + N / max(1, n)) for g, n in
           c.execute(f"SELECT gram, n FROM df WHERE gram IN ({ph})", tuple(qg))}
    if not idf:
        return []
    ids = [d for d, _ in c.execute(
        f"SELECT g.doc, SUM(1.0) FROM grams g WHERE g.gram IN ({ph}) "
        f"GROUP BY g.doc ORDER BY 2 DESC LIMIT {limit}", tuple(qg))]
    if not ids:
        return []
    ph2 = ",".join("?" * len(ids))
    score: dict[str, float] = {}
    for d, g in c.execute(
            f"SELECT doc, gram FROM grams INDEXED BY idx_grams "
            f"WHERE gram IN ({ph}) AND doc IN ({ph2})", tuple(qg) + tuple(ids)):
        score[d] = score.get(d, 0.0) + idf.get(g, 0.0)
    return sorted(score.items(), key=lambda x: -x[1])[:1000]


def _fuse(docs: list[dict], k: int, board: str | None = None) -> list[dict]:
    """稀疏相似度 + 新鲜度 + 互动量 + 同板块，四项归一后加权。"""
    if not docs:
        return []
    now = time.time()
    for d in docs:
        try:
            ts = time.mktime(time.strptime(d["day"][:19], "%Y-%m-%dT%H:%M:%S"))
        except Exception:
            ts = now - 86400 * 365
        days = max(0.0, (now - ts) / 86400)
        d["recency"] = math.exp(-days / 420)          # 半衰期约 10 个月
        d["importance"] = math.log1p(d["likes"] + 2 * d["cmts"]) / 4.0
        d["same_board"] = 1.0 if (board and d.get("board") == board) else 0.0
    for key in ("sim", "importance"):
        mx = max(x[key] for x in docs) or 1
        for d in docs:
            d[key] = d[key] / mx
    for d in docs:
        d["score"] = (0.52 * d["sim"] + 0.17 * d["recency"]
                      + 0.13 * min(1.0, d["importance"]) + 0.18 * d["same_board"])
        if board and not d["same_board"]:
            d["score"] -= 0.07          # 跨板块要更相关才压得住同板块
    docs.sort(key=lambda d: -d["score"])
    return docs[:k]


def mmr(docs: list[dict], k: int, lam: float = 0.72) -> list[dict]:
    """最大边际相关：既要相关，也别三条都一个样。"""
    if len(docs) <= k:
        return docs
    picked: list[dict] = []
    pool = docs[:]
    while pool and len(picked) < k:
        best, best_v = None, -1e9
        for d in pool:
            dg = d.get("g") or grams_of(d["text"])
            div = max((_jaccard(dg, p.get("g") or grams_of(p["text"])) for p in picked),
                      default=0.0)
            v = lam * d["score"] - (1 - lam) * div
            if v > best_v:
                best, best_v = d, v
        picked.append(best)
        pool.remove(best)
    return picked


def _jaccard(a, b) -> float:
    A = a if isinstance(a, set) else grams_of(a)
    B = b if isinstance(b, set) else grams_of(b)
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


# ------------------------------------------------------------------ 可选：embedding / rerank
def _cfg(cfg: dict | None) -> dict:
    if cfg is not None:
        return cfg
    try:
        d = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        return d.get("rag") or {}
    except Exception:
        return {}


def _post_json(url: str, key: str, payload: dict, timeout: float = 8.0) -> dict:
    """带 keep-alive 的 POST：硅基在杭州，每次重握手要 100ms+，热路径上付不起。"""
    import http.client
    from urllib.parse import urlsplit
    u = urlsplit(url)
    path = u.path + (("?" + u.query) if u.query else "")
    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    pool = getattr(_tls, "http", None)
    if pool is None:
        pool = _tls.http = {}
    skey = (u.scheme, u.hostname, u.port)
    body = json.dumps(payload, ensure_ascii=False).encode()
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    last: Exception | None = None
    for attempt in range(2):
        c = pool.get(skey)
        if c is None:
            c = cls(u.hostname, u.port, timeout=timeout)
            pool[skey] = c
        c.timeout = timeout
        try:
            c.request("POST", path, body, headers)
            r = c.getresponse()
            data = r.read()
            if r.status >= 400:
                raise RuntimeError(f"HTTP {r.status}: {data[:200]}")
            return json.loads(data)
        except Exception as e:                    # 连接被服务端回收 → 换一条重试一次
            last = e
            try:
                c.close()
            except Exception:
                pass
            pool.pop(skey, None)
    raise last if last else RuntimeError("request failed")


_emb_cache: dict[tuple[str, str], list[float]] = {}
_emb_lock = threading.Lock()


def _embed(texts: list[str], url: str, key: str, model: str) -> list[list[float]]:
    """短文本向量记忆化：同一条帖子在评论/回复两个任务里会被反复当 query。"""
    todo, hits = [], {}
    for i, t in enumerate(texts):
        hit = _emb_cache.get((model, t))
        if hit is None:
            todo.append((i, t))
        else:
            hits[i] = hit
    if todo:
        r = _post_json(url.rstrip("/") + "/embeddings", key,
                       {"model": model, "input": [t for _, t in todo],
                        "encoding_format": "float"}, timeout=12.0)
        got = [x["embedding"] for x in r["data"]]
        with _emb_lock:
            if len(_emb_cache) > 4096:
                _emb_cache.clear()
            for (i, t), v in zip(todo, got):
                hits[i] = v
                _emb_cache[(model, t)] = v
    return [hits[i] for i in range(len(texts))]


def dense_rerank(query: str, docs: list[dict], cfg: dict | None = None,
                 top: int = 24) -> list[dict]:
    """query + 候选一起 embed，一次请求搞定，无需预建向量库。"""
    c = _cfg(cfg).get("embed") or {}
    key, url, model = c.get("api_key"), c.get("base_url"), c.get("model") or "BAAI/bge-m3"
    if not (key and url and docs):
        return docs
    texts = [query[:500]] + [d["text"][:400] for d in docs[:top]]
    try:
        vs = _embed(texts, url, key, model)
    except Exception as e:
        print(f"  embedding 失败，退回稀疏排序：{e}")
        return docs
    q = vs[0]

    def cos(a, b):
        s = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a)) or 1
        nb = math.sqrt(sum(x * x for x in b)) or 1
        return s / (na * nb)

    for d, v in zip(docs[:top], vs[1:]):
        d["dense"] = cos(q, v)
        d["score"] = 0.45 * d["score"] + 0.55 * max(0.0, d["dense"])
    docs.sort(key=lambda d: -d["score"])
    return docs


def api_rerank(query: str, docs: list[dict], cfg: dict | None = None,
               top: int = 24, w: float = 0.66) -> list[dict]:
    """Cross-encoder 精排。分数不直接覆盖旧分——否则时效/同板块的加权全被冲掉。"""
    c = _cfg(cfg).get("rerank") or {}
    if not (c.get("enabled") and c.get("api_key") and c.get("base_url") and docs):
        return docs
    n = min(top, len(docs))
    try:
        r = _post_json(c["base_url"].rstrip("/") + "/rerank", c["api_key"], {
            "model": c.get("model") or "BAAI/bge-reranker-v2-m3",
            "query": query[:180],
            "documents": [d["text"][:120] for d in docs[:n]],
            "top_n": n,
            "return_documents": False,
        }, timeout=8.0)
        rel = {item["index"]: float(item["relevance_score"]) for item in r.get("results") or []}
        if not rel:
            return docs
        for i in range(n):
            d = docs[i]
            d["rerank"] = rel.get(i, 0.0)
            d["score"] = w * max(0.0, d["rerank"]) + (1 - w) * d["score"]
        # 没进精排的尾部统一压到最低分之下，保持原相对顺序
        floor = min((d["score"] for d in docs[:n]), default=0.0) * 0.85
        for d in docs[n:]:
            d["score"] = min(d["score"] * (1 - w), floor)
        docs.sort(key=lambda d: -d["score"])
    except Exception as e:
        print(f"  rerank 失败，忽略：{e}")
    return docs


# ------------------------------------------------------------------ 语义召回
def dense_recall(query: str, *, k: int = 240, kind: str = "post",
                 board: str | None = None, cfg: dict | None = None) -> list[dict]:
    """整库向量召回：把「处境相似但用词完全不同」的帖子找出来。

    这是稀疏检索做不到的事——「一个人吃饭好安静」和「食堂永远找不到人拼桌」
    一个共同词都没有，但它们是同一个处境。
    """
    from . import dense as dn
    if not dn.ready():
        return []
    try:
        q = dn.encode(query, cfg)
        hits = dn.search(q, k=max(k, 80), kind=kind, board=board)
    except Exception as e:
        print(f"  向量召回失败，只用词面：{e}")
        return []
    if not hits:
        return []
    c = conn()
    ph = ",".join("?" * len(hits))
    rows = {r[0]: r for r in c.execute(
        f"SELECT id, kind, board, day, text, likes, cmts, parent "
        f"FROM docs WHERE id IN ({ph})", tuple(h[0] for h in hits))}
    out = []
    for did, cos in hits:
        r = rows.get(did)
        if not r:
            continue
        out.append({"id": r[0], "kind": r[1], "board": r[2], "day": r[3], "text": r[4],
                    "likes": r[5], "cmts": r[6], "parent": r[7] or "",
                    "sim": 0.0, "dense_cos": round(cos, 4)})
    return out


def _rrf(runs: list[tuple[list[dict], float]]) -> list[dict]:
    """Reciprocal Rank Fusion：只吃名次，不吃分数。

    词面分和余弦分的量纲根本不可比（一个是 IDF 累加，一个是 0~1 余弦），
    归一化硬拼会互相稀释；按名次融合最稳，也是工业界做法。
    """
    pool: dict[str, dict] = {}
    for lst, w in runs:
        for i, d in enumerate(lst):
            cur = pool.get(d["id"])
            if cur is None:
                pool[d["id"]] = dict(d, sim=w / (60 + i))
            else:
                cur["sim"] += w / (60 + i)
                if not cur.get("parent") and d.get("parent"):
                    cur["parent"] = d["parent"]
                cur.setdefault("dense_cos", d.get("dense_cos"))
    return list(pool.values())


def recall(query: str, *, k: int = 80, kind: str = "post", board: str | None = None,
           cfg: dict | None = None, dense: bool = True) -> list[dict]:
    """词面 + 语义两路并行召回，RRF 融合后再按新鲜度/互动量/板块加权一次。"""
    from . import dense as dn
    rcfg = _cfg(cfg)
    use_dense = bool(dense and (rcfg.get("embed") or {}).get("api_key") and dn.ready())
    sp: list[dict] = []
    dn_docs: list[dict] = []
    if use_dense:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as ex:
            f1 = ex.submit(sparse, query, k=max(k, 60), kind=kind, board=board)
            f2 = ex.submit(dense_recall, query, k=max(k * 3, 200), kind=kind,
                           board=board, cfg=cfg)
            sp = f1.result()
            dn_docs = f2.result()
    else:
        sp = sparse(query, k=max(k, 60), kind=kind, board=board)
        if dense and (rcfg.get("embed") or {}).get("api_key"):
            sp = dense_rerank(query, sp, cfg)      # 没有全量库时退回小候选集现算
    if not dn_docs:
        return sp
    merged = _rrf([(sp, 1.0), (dn_docs, 1.0)])
    got = _fuse(merged, len(merged), board)
    for d in got:
        d["g"] = grams_of(d["text"] + (" " + d["parent"] if d.get("parent") else ""))
    return got


# ------------------------------------------------------------------ 对外
def retrieve(query: str, *, k: int = 6, board: str | None = None,
             cfg: dict | None = None, dense: bool = True,
             kind: str = "post", keep: int | None = None) -> list[dict]:
    """混合召回 → 精排 → MMR。

    召回从「查字」升级成「查意思」：倒排保证精确词命中，全量向量库保证
    换个说法也找得到；两路按名次融合，再由 cross-encoder 定生死。
    """
    docs = recall(query, k=max(k * 8, 80), kind=kind, board=board, cfg=cfg, dense=dense)
    if not docs:
        return []
    docs = api_rerank(query, docs, cfg, top=min(24, max((keep or k) * 2, 16)))
    if keep:                       # 上层还要自己分类，MMR 前先多留一些
        return docs[:keep]
    return mmr(docs, k)


# 长文示范要挑「给判断 + 给建议」的高赞评论，不要「自我表演式画面」的。
# 示范是模型学语感的直接来源：给它看「关东煮/汤凉了」，它就照着写。
_ADVICE_HINT = ("直接", "别", "可以", "问他", "问她", "说清楚", "写下来", "约出来",
                "定个", "试试", "问清楚", "谈清楚", "说明白", "摊开", "挑明",
                "不值得", "算了", "问一句")
_SCENE_HINT = ("关东煮", "烤肠", "汤都凉", "影子拉得", "一根一根", "热气往上冒",
               "打了删删了打", "闭馆音乐", "把手机扣在", "围巾绕")


def _advice_density(text: str) -> int:
    return sum(1 for w in _ADVICE_HINT if w in (text or ""))


def _scene_porn(text: str) -> bool:
    return any(w in (text or "") for w in _SCENE_HINT)


def advice_exemplars(query: str, k: int = 3, budget: int = 340) -> str:
    """从全量语料（kb/raw.db）里拉「情感/恋爱帖下的高赞建议型长评」当示范。

    pair 池建库时只收了 4-46 字的短评，长评示范一直是空的——模型没见过
    「给判断 + 讲道理 + 给做法」的长评长什么样，只能自己编画面。
    这里直接查 raw.db：情感类帖下 60-300 字、有赞、含建议动作的评论，
    按和原帖的词面相关度排序取前 k。失败就返回空串，不拖垮主链。
    """
    import sqlite3 as _sql
    from pathlib import Path as _P
    raw = _P(__file__).resolve().parent.parent / "kb" / "raw.db"
    if not raw.exists():
        return ""
    try:
        con = _sql.connect(f"file:{raw}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT c.content, c.like_count, p.content FROM comments c "
            "JOIN posts p ON p.id = c.post_id "
            "WHERE (p.sub_name LIKE ? OR p.sub_name LIKE ? OR p.sub_name LIKE ?) "
            "AND length(c.content) BETWEEN 60 AND 300 AND c.like_count > 0 "
            "ORDER BY c.like_count DESC LIMIT 400",
            ("%恋爱%", "%不开心%", "%女孩%")).fetchall()
        con.close()
    except Exception:
        return ""
    if not rows:
        return ""
    qg = grams_of(query or "")
    scored = []
    for ctext, likes, ptext in rows:
        # 必须是「给东西」的评论：建议动作够密，或者带能说出口的原话
        if _advice_density(ctext) < 2 and ("」" not in ctext and "“" not in ctext):
            continue
        if _scene_porn(ctext):
            continue
        j = len(qg & grams_of(ptext)) / max(1, len(qg | grams_of(ptext)))
        scored.append((j + min(likes or 0, 20) * 0.01, ctext))
    scored.sort(reverse=True)
    picks: list[str] = []
    seen: list[set[str]] = []
    for _, ctext in scored:
        g = grams_of(ctext)
        if any(len(g & s) > 30 for s in seen):
            continue
        seen.append(g)
        picks.append(ctext)
        if len(picks) >= k:
            break
    if not picks:
        return ""
    lines = ["【这个社区里被赞过的建议型长评（学它怎么判断、怎么给做法；"
             "禁止复用内容，别学它的自我经历和画面）】"]
    for p in picks:
        lines.append(f"· {p[:150]}")
    return "\n".join(lines)[:budget]


def comment_pairs(query: str, k: int = 3, board: str | None = None,
                  cfg: dict | None = None, post_id: str | None = None,
                  prefer_advice: bool = False) -> list[dict]:
    """检索「相似帖 + 它的高赞评论」这类成对样本。

    带 post_id 时会把「这条帖子下原有的评论」单独标出来：既让模型知道
    这条帖子已经有人说了什么（避免复读），又不会被它们挤掉同类帖的样本。
    """
    pre = f"pair:{post_id}:" if post_id else ""
    # 本帖已有的评论直接按 ID 前缀取，不走召回：它们一定对题，漏掉就是白丢信息
    same_docs: list[dict] = []
    if pre:
        try:
            same_docs = [{"id": r[0], "kind": r[1], "board": r[2], "day": r[3],
                          "text": r[4], "likes": r[5], "cmts": r[6],
                          "parent": r[7] or "", "sim": 1.0, "score": 1.0}
                         for r in conn().execute(
                "SELECT id, kind, board, day, text, likes, cmts, parent FROM docs "
                "WHERE id LIKE ? ORDER BY likes DESC LIMIT 6", (pre + "%",))]
        except Exception:
            same_docs = []
    # 同类楼层的样本走混合召回（词面 + 语义），精排预算全给它们
    docs = recall(query, k=60, kind="pair", board=board, cfg=cfg)
    other_docs = [d for d in docs if not (pre and d["id"].startswith(pre))]
    if other_docs:
        other_docs = api_rerank(query, other_docs, cfg, top=min(12, len(other_docs)))
        # 同一语义档次里优先拿点赞高的：被赞过说明这套说法在这个社区是成立的，
        # 比语义分只高 0.01 但没人赞的样本更值得学。权重压得很小，
        # 只用来打破并列，不让高赞的无关样本挤掉真正对题的。
        if prefer_advice:
            # 长文要的是「给建议」的语感。建议密度加权、画面堆砌降权，
            # 幅度压小：只用来打破并列，不把不对题的高赞样本顶上来。
            other_docs.sort(
                key=lambda d: (d["score"] + min(d.get("likes") or 0, 30) * 0.008
                               + min(_advice_density(d.get("parent") or ""), 4) * 0.02
                               - (0.10 if _scene_porn(d.get("parent") or "") else 0.0)),
                reverse=True)
        else:
            other_docs.sort(key=lambda d: d["score"] + min(d.get("likes") or 0, 30) * 0.008,
                            reverse=True)
    docs = same_docs[:2] + other_docs

    out: list[dict] = []
    seen: list[set[str]] = []

    def _take(d: dict, same: bool) -> bool:
        cm = clean_exemplar(d["parent"], 46)
        g = grams_of(cm)
        if not cm or not g or any(_jaccard(g, s) > 0.8 for s in seen):
            return False
        # 长文示范里不要「自我表演式画面」的样本（本帖已有评论除外，那是避重用的）
        if prefer_advice and not same and _scene_porn(cm):
            return False
        seen.append(g)
        out.append({"post": clean_exemplar(d["text"], 80), "board": d["board"],
                    "day": d["day"][:10], "comment": cm, "same": same,
                    "score": round(d["score"], 3)})
        return True

    for d in docs:
        if d.get("parent"):
            _take(d, bool(pre and d["id"].startswith(pre)))
    return out


def prompt_block(kind: str, query: str, *, board: str | None = None,
                 k: int = 3, cfg: dict | None = None, budget: int = 520,
                 post_id: str | None = None, prefer_advice: bool = False) -> str:
    """把检索结果排成一段可直接拼进 system 的示范。

    只给「同类场景别人怎么写」，并明确禁止复用内容——它提供的是语感，不是素材。
    """
    if not IDX.exists():
        return ""
    try:
        if kind in ("comment", "reply"):
            pairs = comment_pairs(query, k=k, board=board, cfg=cfg, post_id=post_id,
                                  prefer_advice=prefer_advice)
            if not pairs:
                return ""
            same = [p for p in pairs if p["same"]][:2]
            other = [p for p in pairs if not p["same"]][:k]
            lines: list[str] = []
            if same:
                lines.append("【这条帖子下已经有人说过的话（换个说法，别重复它们的意思）】")
                for p in same:
                    lines.append(f"· {p['comment']}")
            if other:
                lines.append("【同类帖子里别人怎么回（只学语气、长度、口吻，禁止复用任何内容与事实）】")
                for p in other:
                    lines.append(f"· 原帖：{p['post']}")
                    lines.append(f"  评论：{p['comment']}")
            txt = "\n".join(lines)
            if prefer_advice:
                # pair 池建库时只收了 4-46 字的短评，长评示范一直空着；
                # 模型没见过「给判断 + 讲道理 + 给做法」的长评长什么样，只能自己编。
                # 这里从全量语料里拉真实的高赞建议型长评补上这一课。
                extra = advice_exemplars(query)
                if extra:
                    txt = f"{txt}\n\n{extra}"
        else:
            docs = retrieve(query, k=k, board=board, cfg=cfg)
            if not docs:
                return ""
            lines = ["【社区里同类场景的真实帖子（只学语感与信息密度，禁止复用任何内容）】"]
            for d in docs:
                lines.append(f"· {clean_exemplar(d['text'], 80)}")
            txt = "\n".join(lines)
    except Exception as e:
        print(f"  检索失败，跳过示范：{e}")
        return ""
    return txt[:budget]


def similar_posts(query: str, k: int = 3, board: str | None = None,
                  cfg: dict | None = None) -> list[str]:
    return [d["text"] for d in retrieve(query, k=k, board=board, cfg=cfg)]


def probe() -> dict:
    """真打一次 embedding + rerank，给人看「语义层现在通不通、多少毫秒」。"""
    cfg = _cfg(None)
    out: dict = {}
    e = cfg.get("embed") or {}
    if e.get("api_key") and e.get("base_url"):
        t0 = time.perf_counter()
        try:
            v = _embed(["食堂排队好长", "图书馆四楼很冷"], e["base_url"], e["api_key"],
                       e.get("model") or "BAAI/bge-m3")
            out["embed"] = {"ok": True, "ms": int((time.perf_counter() - t0) * 1000),
                            "dim": len(v[0]), "model": e.get("model")}
        except Exception as ex:
            out["embed"] = {"ok": False, "err": str(ex)[:160], "model": e.get("model")}
    else:
        out["embed"] = {"ok": False, "err": "未配置 key", "model": e.get("model")}
    r = cfg.get("rerank") or {}
    if r.get("enabled") and r.get("api_key") and r.get("base_url"):
        t0 = time.perf_counter()
        try:
            resp = _post_json(r["base_url"].rstrip("/") + "/rerank", r["api_key"], {
                "model": r.get("model") or "BAAI/bge-reranker-v2-m3",
                "query": "食堂排队太久了",
                "documents": ["二食堂中午排了二十分钟", "图书馆四楼今天很冷"],
                "top_n": 2, "return_documents": False}, timeout=10.0)
            sc = [round(float(x["relevance_score"]), 4) for x in resp.get("results") or []]
            out["rerank"] = {"ok": True, "ms": int((time.perf_counter() - t0) * 1000),
                             "scores": sc, "model": r.get("model")}
        except Exception as ex:
            out["rerank"] = {"ok": False, "err": str(ex)[:160], "model": r.get("model")}
    else:
        out["rerank"] = {"ok": False, "err": "未开启", "model": r.get("model")}
    # 全量向量库：真跑一次「编码 + 整库扫描」，让人看见召回到底多快
    try:
        from . import dense as dn
        st = dn.info()
        if st.get("ready"):
            t0 = time.perf_counter()
            q = dn.encode("室友半夜打游戏很吵，说了好几次都不改", cfg)
            enc = int((time.perf_counter() - t0) * 1000)
            t0 = time.perf_counter()
            hits = dn.search(q, k=5, kind="post")
            sc = int((time.perf_counter() - t0) * 1000)
            out["dense"] = {"ok": True, "encode_ms": enc, "search_ms": sc,
                            "ms": enc + sc, "docs": st.get("count"),
                            "size_mb": st.get("size_mb"),
                            "top": round(hits[0][1], 3) if hits else 0,
                            "hits": len(hits)}
        else:
            out["dense"] = {"ok": False, "err": "未建库"}
    except Exception as ex:
        out["dense"] = {"ok": False, "err": str(ex)[:160]}
    return out


def info() -> dict:
    if not IDX.exists():
        return {"ready": False}
    c = conn()
    n = c.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    by = dict(c.execute("SELECT board, COUNT(*) FROM docs GROUP BY board").fetchall())
    built = c.execute("SELECT v FROM meta WHERE k='built_at'").fetchone()
    cfg = _cfg(None)
    e = cfg.get("embed") or {}
    r = cfg.get("rerank") or {}
    try:
        from . import dense as dn
        dstat = dn.info()
    except Exception as ex:
        dstat = {"ready": False, "err": str(ex)[:120]}
    return {"ready": True, "docs": n, "by_board": by,
            "built_at": built[0] if built else "",
            "embed_on": bool(e.get("api_key")), "embed_model": e.get("model") or "",
            "rerank_on": bool(r.get("enabled") and r.get("api_key")),
            "rerank_model": r.get("model") or "",
            "dense": dstat}
