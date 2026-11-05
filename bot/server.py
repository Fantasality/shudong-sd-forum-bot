"""鼠洞SD 本地控制台（多账号版）。

启动：python run.py web            （默认 http://127.0.0.1:8765）

只监听 127.0.0.1，不对外网开放。所有写操作走 application/json，
跨站表单无法伪造，因此不需要额外 token。

账号模型
--------
`active_account` = 控制台当前查看/编辑哪个号；
daemon 不受它影响，会并行跑所有 enabled 账号。
"""
from __future__ import annotations

import json
import logging
import os
import random
import secrets
import string
import subprocess
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from szlike import kb as kbmod, ops, persona, rag                   # noqa: E402
from szlike.brain import Brain                                     # noqa: E402
from szlike.client import AuthError, Client, SzlikeError, jwt_payload  # noqa: E402
from szlike.config import (                                        # noqa: E402
    LiveConfig, account_config, accounts_index, load, resolve_account, save,
)
from szlike.guard import Guard                                     # noqa: E402
from szlike.store import Store                                     # noqa: E402
from szlike.tasks import (                                         # noqa: E402
    Ctx, autolike, publish_draft, random_comment, reply_comments, scheduled_post,
)

CFG_PATH = BASE / "config.json"
STATE_PATH = BASE / "state.db"
LOG_PATH = BASE / "logs" / "bot.log"
PID_PATH = BASE / "daemon.pid"
WEB_DIR = BASE / "web"
KB_DIR = BASE / "kb"
KB_PATH = KB_DIR / "knowledge.json"
KB_LOG = KB_DIR / "build.log"

kb_state = {"building": False, "started": 0.0, "step": ""}

cfg = LiveConfig(str(CFG_PATH))

_log_ring: deque[str] = deque(maxlen=400)


class RingHandler(logging.Handler):
    def __init__(self, cap: int = 400):
        super().__init__()
        self.buf: deque[str] = deque(maxlen=cap)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.buf.append(
                "%s %-7s %s | %s"
                % (
                    datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
                    record.levelname,
                    record.name.replace("szlike.", ""),
                    record.getMessage(),
                )
            )
        except Exception:
            pass


ring = RingHandler()
ring.setLevel(logging.INFO)


def setup_logging() -> None:
    (BASE / "logs").mkdir(exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s | %(message)s", datefmt="%H:%M:%S"
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(isinstance(h, logging.FileHandler) for h in root.handlers):
        fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    ring.setFormatter(logging.Formatter("%(message)s"))
    if ring not in root.handlers:
        root.addHandler(ring)


log = logging.getLogger("szlike.web")

# ---------- 运行时单例 ----------

_stores: dict[str, Store] = {}
_store_lock = threading.Lock()
_viewer_cache: dict[str, dict] = {}
_register_lock = threading.Lock()
_brains: dict[tuple, Brain] = {}
_brain_lock = threading.Lock()

# 批量开号是几十秒到几分钟的活（注册要控速，不然会被限流），
# 不能挂在一次 HTTP 请求里等。开一个后台线程，前端轮询 /api/spawn/status。
_spawn_lock = threading.Lock()
_spawn_state: dict = {"running": False, "done": 0, "total": 0, "log": [], "result": None}


def _spawn_progress(done: int, total: int, text: str, ok: bool) -> None:
    with _spawn_lock:
        _spawn_state["done"] = done
        _spawn_state["total"] = total
        _spawn_state["log"].append({"t": time.strftime("%H:%M:%S"), "text": text, "ok": ok})
        if len(_spawn_state["log"]) > 300:
            _spawn_state["log"] = _spawn_state["log"][-300:]


def get_brain(d: dict) -> Brain:
    """同一个 LLM 配置复用同一个 Brain，保住 keep-alive 连接（省掉每次 TLS 握手）。"""
    llm = d.get("llm") or {}
    sig = (llm.get("base_url"), llm.get("api_key"), llm.get("model"),
           llm.get("temperature"), llm.get("max_tokens"))
    with _brain_lock:
        b = _brains.get(sig)
        if b is None:
            _brains.clear()
            b = Brain(llm)
            _brains[sig] = b
        return b


def ns_of(name: str) -> str:
    return "" if name == "main" else name


def get_store(name: str | None = None) -> Store:
    """按账号返回对应命名空间的 Store。"""
    acc = resolve_account(cfg.data, str(BASE), name) if name else resolve_account(cfg.data, str(BASE))
    ns = ns_of(acc["name"])
    with _store_lock:
        if ns not in _stores:
            _stores[ns] = Store(str(STATE_PATH), ns=ns)
        return _stores[ns]


def view_name() -> str:
    return resolve_account(cfg.data, str(BASE))["name"]


def new_client(d: dict | None = None, name: str | None = None) -> Client:
    d = d or cfg.data
    acc = resolve_account(d, str(BASE), name)
    af = BASE / f"account_{acc['name']}.json"
    return Client(d["app_id"], d["school_id"], acc["tokens"],
                  account_path=str(af) if af.exists() else None)


def build(name: str | None = None, manual: bool = True):
    d = account_config(cfg.data, str(BASE), name)
    acc = d["_account"]
    d["my_user_id"] = acc["user_id"]
    client = new_client(d, acc["name"])
    store = get_store(acc["name"])
    brain = get_brain(d)
    guard = Guard(d.get("guard"), store)
    ctx = Ctx(client, store, brain, guard, d, manual=manual)
    return client, store, brain, guard, ctx


def fmt_left(sec: float) -> str:
    if sec <= 0:
        return "已过期"
    if sec > 86400:
        return f"{sec / 86400:.1f} 天"
    return f"{sec / 3600:.1f} 小时"


# ---------- daemon 进程管理 ----------


def pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
        return str(pid) in out
    except Exception:
        return False


def daemon_state() -> dict:
    if not PID_PATH.exists():
        return {"running": False, "pid": None}
    try:
        pid = int(PID_PATH.read_text().strip() or 0)
    except Exception:
        return {"running": False, "pid": None}
    alive = pid_alive(pid)
    if not alive:
        try:
            PID_PATH.unlink()
        except Exception:
            pass
    return {"running": alive, "pid": pid if alive else None}


def daemon_start() -> dict:
    st = daemon_state()
    if st["running"]:
        return {"ok": False, "error": f"已经在跑（PID {st['pid']}）"}
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    p = subprocess.Popen(
        [sys.executable, str(BASE / "run.py"), "daemon"],
        cwd=str(BASE), creationflags=flags, close_fds=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(1.5)
    if p.poll() is not None:
        return {"ok": False, "error": "进程启动后立刻退出了，去看日志"}
    return {"ok": True, "pid": p.pid}


def daemon_stop() -> dict:
    st = daemon_state()
    if not st["running"]:
        return {"ok": False, "error": "没有在跑"}
    try:
        subprocess.run(
            ["taskkill", "/PID", str(st["pid"]), "/F"],
            capture_output=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            PID_PATH.unlink()
        except Exception:
            pass
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": repr(e)}


# ---------- 业务封装 ----------


def get_account(force: bool = False, name: str | None = None) -> dict:
    key = name or view_name()
    c = _viewer_cache.get(key) or {}
    if not force and time.time() - c.get("ts", 0) < 120:
        return c.get("data") or {}
    client = new_client(name=key)
    v = ops.viewer(client)
    prof = v["user"].get("profile") or {}
    vf = v.get("viewerVerify") or {}
    data = {
        "nickname": prof.get("nickname"),
        "grade": prof.get("grade"),
        "institute": prof.get("institute"),
        "userId": v["user"]["id"],
        "role": (v.get("role") or {}).get("role"),
        "school": (vf.get("school") or {}).get("name"),
        "campus": (vf.get("campus") or {}).get("name"),
    }
    _viewer_cache[key] = {"ts": time.time(), "data": data}
    return data


def account_card(a: dict, beats: dict) -> dict:
    name = a["name"]
    card = dict(a)
    tp = BASE / a["tokens"]
    if tp.exists():
        try:
            c = Client(cfg.data["app_id"], cfg.data["school_id"], str(tp))
            card["token"] = {
                "access_left": c.tokens.access_left(),
                "refresh_left": c.tokens.refresh_left(),
                "access_text": fmt_left(c.tokens.access_left()),
                "refresh_text": fmt_left(c.tokens.refresh_left()),
            }
        except Exception as e:
            card["token"] = {"error": str(e)}
    else:
        card["token"] = {"error": "缺少 token 文件"}
    s = get_store(name)
    card["today"] = s.stats_today()
    # 停用的号不再展示心跳，否则会留着上一轮 daemon 的僵尸状态
    card["worker"] = (beats.get(name) or {}) if card.get("enabled") else {}
    return card


def kb_payload() -> dict:
    """知识库页的数据：结构化 JSON、人读 Markdown、构建日志。"""
    data = kbmod.load(str(KB_PATH))
    md = ""
    p = KB_DIR / "KNOWLEDGE.md"
    if p.exists():
        md = p.read_text(encoding="utf-8")
    logtxt = ""
    if KB_LOG.exists():
        logtxt = KB_LOG.read_text(encoding="utf-8", errors="replace")[-4000:]
    return {
        "stats": kbmod.stats(str(KB_PATH)),
        "json": data,
        "md": md,
        "log": logtxt,
        "building": kb_state["building"],
        "step": kb_state["step"],
        "corpus": _kb_corpus(),
        "rag": rag.info(),
        "style": _style_summary(),
        "enabled": bool((cfg.data.get("kb") or {}).get("enabled", True)),
        "rag_enabled": bool((cfg.data.get("rag") or {}).get("enabled", True)),
    }


def _style_summary() -> dict:
    """拟人标尺：真实语料的长度/表情/句号分布，前端直接展示，便于对照。"""
    try:
        from szlike import human
        st = human.style()
        c = st.get("comment") or {}
        p = st.get("post") or {}
        return {"comment_p50": c.get("p50"), "comment_sticker": c.get("sticker_rate"),
                "comment_dot": c.get("dot_rate"), "post_p50": p.get("p50"),
                "post_sticker": p.get("sticker_rate"), "bands": c.get("bands") or []}
    except Exception as e:
        return {"err": str(e)[:120]}


def _kb_corpus() -> dict:
    import sqlite3
    db = KB_DIR / "raw.db"
    if not db.exists():
        return {}
    try:
        c = sqlite3.connect(db)
        n, lo, hi = c.execute("SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM posts").fetchone()
        m, np = c.execute("SELECT COUNT(*), COUNT(DISTINCT post_id) FROM comments").fetchone()
        by_year = dict(c.execute("SELECT substr(day,1,4), COUNT(*) FROM posts GROUP BY 1 ORDER BY 1").fetchall())
        by_board = c.execute("SELECT sub_name, COUNT(*) FROM posts GROUP BY 1 ORDER BY 2 DESC").fetchall()
        c.close()
        return {"posts": n, "comments": m, "covered_posts": np,
                "span": [(lo or "")[:10], (hi or "")[:10]],
                "by_year": by_year, "by_board": by_board}
    except Exception:
        return {}


def kb_save(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError("知识库必须是 JSON 对象")
    data.setdefault("version", 1)
    data["edited_at"] = datetime.now().isoformat(timespec="seconds")
    tmp = str(KB_PATH) + ".tmp"
    Path(tmp).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, KB_PATH)
    try:
        from kb_build import md_from
        (KB_DIR / "KNOWLEDGE.md").write_text(md_from(data), encoding="utf-8")
    except Exception as e:
        log.warning("KNOWLEDGE.md 重建失败：%s", e)
    log.info("知识库已保存（%d 个顶层段）", len(data))
    return kb_payload()


def kb_rebuild(steps: str = "all") -> dict:
    """后台跑 采集分析 + LLM 合成。steps: analyze / build / all"""
    if kb_state["building"]:
        return {"ok": False, "error": "已经在构建中"}
    kb_state.update({"building": True, "started": time.time(), "step": "启动"})

    def run():
        try:
            with open(KB_LOG, "w", encoding="utf-8") as f:
                if steps in ("index", "all"):
                    kb_state["step"] = "建检索索引"
                    f.write("$ rag.build()\n")
                    f.flush()
                    r = subprocess.run(
                        [sys.executable, "-c",
                         "import sys;sys.path.insert(0,'.');from szlike import rag;print(rag.build())"],
                        cwd=str(BASE), stdout=f, stderr=subprocess.STDOUT, timeout=1800)
                    f.write(f"\n[index exit {r.returncode}]\n")
                    f.flush()
                if steps in ("analyze", "all"):
                    kb_state["step"] = "分析语料"
                    f.write("$ kb_analyze.py\n")
                    f.flush()
                    r = subprocess.run([sys.executable, "kb_analyze.py"], cwd=str(BASE),
                                       stdout=f, stderr=subprocess.STDOUT, timeout=900)
                    f.write(f"\n[analyze exit {r.returncode}]\n")
                    f.flush()
                if steps in ("build", "all"):
                    kb_state["step"] = "LLM 提炼"
                    f.write("$ kb_build.py\n")
                    f.flush()
                    r = subprocess.run([sys.executable, "kb_build.py"], cwd=str(BASE),
                                       stdout=f, stderr=subprocess.STDOUT, timeout=1800)
                    f.write(f"\n[build exit {r.returncode}]\n")
                    f.flush()
            kb_state["step"] = "完成"
            log.info("知识库重建完成")
        except Exception as e:
            kb_state["step"] = f"失败：{e}"
            log.error("知识库重建失败：%s", e)
        finally:
            kb_state["building"] = False

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True}


def build_state() -> dict:
    vname = view_name()
    d = account_config(cfg.data, str(BASE), vname)
    # 全新安装时一个号都没有，这里会找不到 tokens.json。
    # 不能让它把整个首页 500 掉——用户第一次打开就是要看到「去生成号」。
    try:
        client = new_client(d, vname)
    except Exception as e:
        client = None
        log.info("账号 %s 暂无可用凭据：%s", vname, e)
    store = get_store(vname)
    beats = store.workers()
    out: dict = {
        "dry_run": bool(cfg.data.get("dry_run", True)),
        "is_anonymous": False,
        "tasks": d.get("tasks") or {},
        "prompts": d.get("prompts") or {},
        "guard": d.get("guard") or {},
        "llm": {
            "base_url": (cfg.data.get("llm") or {}).get("base_url"),
            "model": (cfg.data.get("llm") or {}).get("model"),
            "temperature": (cfg.data.get("llm") or {}).get("temperature"),
            "has_key": bool((cfg.data.get("llm") or {}).get("api_key")),
        },
        "rag": {
            "enabled": bool((cfg.data.get("rag") or {}).get("enabled", True)),
            "embed": {
                "base_url": ((cfg.data.get("rag") or {}).get("embed") or {}).get("base_url"),
                "model": ((cfg.data.get("rag") or {}).get("embed") or {}).get("model"),
                "has_key": bool(((cfg.data.get("rag") or {}).get("embed") or {}).get("api_key")),
            },
            "rerank": {
                "base_url": ((cfg.data.get("rag") or {}).get("rerank") or {}).get("base_url"),
                "model": ((cfg.data.get("rag") or {}).get("rerank") or {}).get("model"),
                "has_key": bool(((cfg.data.get("rag") or {}).get("rerank") or {}).get("api_key")),
                "enabled": ((cfg.data.get("rag") or {}).get("rerank") or {}).get("enabled", True),
            },
        },
        "topics": cfg.data.get("topics") or {},
        "active_account": vname,
        "accounts": [account_card(a, beats) for a in accounts_index(cfg.data)],
        "personas": {k: {"label": v["label"], "tag": v["tag"], "desc": v["desc"]}
                     for k, v in persona.PERSONAS.items()},
        "defaults": {
            "topic_id": cfg.data.get("default_topic_id"),
            "subtopic_id": cfg.data.get("default_subtopic_id"),
        },
        "daemon": daemon_state(),
        "quota": {
            "today": store.stats_today(),
            "hour": store.stats_hour(),
            "daily_limits": (d.get("guard") or {}).get("daily_limits") or {},
            "hourly_limits": (d.get("guard") or {}).get("hourly_limits") or {},
        },
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        if client is None:
            raise SzlikeError("还没有登录的号")
        out["token"] = {
            "access_left": client.tokens.access_left(),
            "refresh_left": client.tokens.refresh_left(),
            "access_text": fmt_left(client.tokens.access_left()),
            "refresh_text": fmt_left(client.tokens.refresh_left()),
            "uid": jwt_payload(client.tokens.access).get("uid"),
        }
    except Exception as e:
        out["token"] = {"error": str(e)}
    try:
        out["account"] = get_account()
    except Exception as e:
        out["account"] = {"error": str(e)}
        out["account_error"] = str(e)
    return out


def list_content(kind: str, limit: int = 15, name: str | None = None) -> dict:
    # 前端切 tab 时偶尔会把 undefined 传上来（tab 还没绑好就点了刷新），
    # 直接抛 ValueError 会在日志里刷一屏堆栈。默认回落到「我发的帖子」。
    kind = (kind or "").strip().lower()
    if kind in ("", "undefined", "null"):
        kind = "posts"
    d = account_config(cfg.data, str(BASE), name)
    client = new_client(d, d["_account"]["name"])
    me = d["my_user_id"]
    if kind == "feed":
        return {"edges": [{"node": n} for n in ops.feed_posts(client, d["school_id"], 2)]}
    if kind == "posts":
        return ops.my_posts(client, me)
    if kind == "comments":
        return ops.my_comments(client, me)
    if kind == "replies":
        return ops.my_subcomments(client, me)
    raise ValueError(f"未知 kind {kind}")


def do_delete(kind: str, oid: str, name: str | None = None) -> dict:
    client = new_client(name=name)
    store = get_store(name)
    if kind == "post":
        r = ops.delete_post(client, oid)
        store.forget("post", oid)
    elif kind == "comment":
        r = ops.delete_comment(client, oid)
        store.forget("comment", oid)
    elif kind == "subcomment":
        r = ops.delete_subcomment(client, oid)
        store.forget("reply", oid)
    else:
        raise ValueError(f"未知类型 {kind}")
    log.warning("已删除 %s %s -> %s", kind, oid, r)
    return {"result": r}


def do_action(payload: dict) -> dict:
    kind = payload.get("kind")
    force = bool(payload.get("force"))
    name = payload.get("account") or None
    client, store, brain, guard, ctx = build(name)
    dry = ctx.dry_run and not force

    if kind == "post":
        content = (payload.get("content") or "").strip()
        if not content:
            raise ValueError("内容不能为空")
        topic = payload.get("topic_id") or cfg.data["default_topic_id"]
        sub = payload.get("subtopic_id") or cfg.data["default_subtopic_id"]
        if dry:
            return {"dry": True, "preview": content, "topic": topic, "subtopic": sub}
        r = ops.create_post(client, content, cfg.data["school_id"], topic, sub,
                            is_anonymous=False)
        store.mark("post", r["id"], content[:80])
        store.remember_text("post", content)
        log.warning("[REAL] %s 手动发帖 %s", ctx.label, r["id"])
        return {"dry": False, "id": r["id"], "content": r.get("content")}

    if kind == "comment":
        pid = (payload.get("post_id") or "").strip()
        content = (payload.get("content") or "").strip()
        if not pid or not content:
            raise ValueError("post_id 和内容都要填")
        if dry:
            return {"dry": True, "preview": content, "target": pid}
        r = ops.create_comment(client, pid, content, is_anonymous=False)
        store.mark("comment", r["id"], content[:60])
        store.remember_text("comment", content)
        log.warning("[REAL] %s 手动评论 %s", ctx.label, r["id"])
        return {"dry": False, "id": r["id"], "content": r.get("content")}

    if kind in ("like", "unlike"):
        pid = (payload.get("post_id") or "").strip()
        if not pid:
            raise ValueError("post_id 必填")
        if dry:
            return {"dry": True, "preview": f"{kind} {pid}"}
        r = ops.add_like(client, pid) if kind == "like" else ops.remove_like(client, pid)
        store.mark("like", pid, "manual")
        log.warning("[REAL] %s 手动 %s %s", ctx.label, kind, pid)
        return {"dry": False, "result": str(r)[:200]}

    if kind == "gen":
        # 只跑 LLM 生成，不发出去
        key = payload.get("prompt_key") or "comment"
        sample = payload.get("sample") or ""
        # 生成时也带时间种子选题，调试台看到的就是真实效果
        if key == "post":
            plan = persona.pick_post_plan(ctx.cfg, store, ctx.account, ctx.persona)
            system, user, enabled = ctx.prompt(
                "post", topic=plan["subtopic_name"], board=plan["subtopic_name"],
                sample=f"{plan['subtopic_name']} {plan['angle']} {plan['style']}", **plan)
            extra = {"topic": plan["subtopic_name"], "angle": plan["angle"]}
        else:
            st = persona.pick_comment_style(ctx.persona)
            system, user, enabled = ctx.prompt(
                key, post=sample, their=sample, seed=sample, style=st,
                angle=sample, topic="", time=persona.time_context()["text"],
                scene=rag.classify(sample), sample=sample,
            )
            extra = {"style": st, "scene": rag.classify(sample)}
        if not enabled:
            raise ValueError(f"prompt「{key}」已关闭")
        out = brain.probe(system, user, max_tokens=200 if key == "post" else 64)
        return {"dry": True, **out, **extra}

    raise ValueError(f"未知动作 {kind}")


def run_task(name: str, account: str | None = None) -> dict:
    client, store, brain, guard, ctx = build(account, manual=True)
    t0 = time.time()
    if name == "scheduled_post":
        ok, msg = scheduled_post(ctx, datetime.now().strftime("%H:%M"))
    elif name == "reply_comments":
        ok, msg = reply_comments(ctx, int(
            (ctx.cfg["tasks"].get("reply_comments") or {}).get("max_per_run") or 3))
    elif name == "autolike":
        ok, msg = autolike(ctx, int(
            (ctx.cfg["tasks"].get("autolike") or {}).get("max_per_run") or 3))
    elif name == "random_comment":
        ok, msg = random_comment(ctx, int(
            (ctx.cfg["tasks"].get("random_comment") or {}).get("max_per_run") or 1))
    else:
        raise ValueError(f"未知任务 {name}")
    log.info("[%s] 手动触发 %s -> %s %s", ctx.label, name, ok, msg)
    return {"ran": ok, "message": msg, "dry_run": ctx.dry_run,
            "elapsed_ms": int((time.time() - t0) * 1000)}


# ---------------------------------------------------------------- 待审队列
#
# 发帖不再「生成即发」：评分不过线就压进这里，人看过、改过才发。
# 面板上能直接改字、通过、打回，不用去翻数据库。

def _review_store():
    return get_store("main")


def _decorate(items: list[dict]) -> list[dict]:
    for it in items:
        try:
            it["parts"] = json.loads(it.get("detail") or "{}")
        except Exception:
            it["parts"] = {}
        it["ago"] = int(time.time() - (it.get("created") or time.time()))
    return items


def review_list(status: str = "pending") -> dict:
    store = _review_store()
    status = (status or "pending").lower()
    items = _decorate(store.draft_list(status, limit=80))
    counts = {s: len(store.draft_list(s, limit=500))
              for s in ("pending", "approved", "rejected", "published")}
    return {"items": items, "counts": counts, "status": status}


def review_act(body: dict) -> dict:
    store = _review_store()
    did = int(body.get("id") or 0)
    if not did:
        raise ValueError("id 必填")
    d = store.draft_get(did)
    if not d:
        raise ValueError(f"草稿 {did} 不存在")
    action = (body.get("action") or "").lower()
    text = body.get("content")
    if isinstance(text, str):
        text = text.strip() or None

    if action in ("save", "approve"):
        store.draft_decide(did, "approved", content=text, by="web",
                           note=body.get("note") or "面板通过")
        return {"id": did, "status": "approved", "message": "已通过，daemon 本轮会发出去"}

    if action == "reject":
        store.draft_decide(did, "rejected", content=text, by="web",
                           note=body.get("note") or "面板打回")
        return {"id": did, "status": "rejected", "message": "已打回，这条不会发"}

    if action == "publish":
        draft = dict(d)
        if text:
            draft["content"] = text
        client, st, brain, guard, ctx = build(d["account"], manual=True)
        ok, msg = publish_draft(ctx, draft)
        log.info("[待审] 手动发布 #%s -> %s %s", did, ok, msg)
        return {"id": did, "status": "published" if ok else "failed",
                "ok": ok, "message": msg}

    if action == "rewrite":
        # 不改字，让模型按当前评分意见再写一条，仍留在待审队列
        client, st, brain, guard, ctx = build(d["account"], manual=True)
        from szlike import quality
        fixes = d.get("fixes") or ""
        pl = {"kind": "post", "form": d.get("form") or "one", "lo": 10, "hi": 50,
              "sticker": False, "scene": "", "newline": False}
        new = None
        if text or d.get("content"):
            base = text or d["content"]
            q = quality.score(base, pl, campus=d.get("campus") or "")
            new = brain.chat(
                "你在校园论坛发一条日常帖子。像在群里跟同学说话，不用比喻、不押韵、"
                "不排比、结尾不写金句。不要用句号。只输出帖子正文。",
                f"【原来这条】{base}\n\n【问题】{fixes or quality.brief(q)}\n\n"
                "重写一条，不要在原句上删字。", max_tokens=320)
        if new:
            q = quality.score(new, pl, campus=d.get("campus") or "")
            store.draft_decide(did, "pending", content=new, by="web",
                               note=f"模型重写，评分 {q['total']}")
            return {"id": did, "status": "pending", "content": new,
                    "score": q["total"], "message": "已重写，请再看一眼"}
        return {"id": did, "status": d["status"], "message": "重写失败，原样保留"}

    raise ValueError(f"未知动作 {action}")


def ready_state() -> dict:
    """两把 key 配好没有。前端拿这个决定要不要挡一层引导页。"""
    d = cfg.data
    llm = ((d.get("llm") or {}).get("api_key") or "").strip()
    rag = d.get("rag") or {}
    emb = ((rag.get("embed") or {}).get("api_key") or "").strip()
    rr = ((rag.get("rerank") or {}).get("api_key") or "").strip()
    return {"llm": bool(llm), "embed": bool(emb), "rerank": bool(rr),
            "ready": bool(llm and emb)}


def save_keys(body: dict) -> dict:
    """只写非空的 key。前端留空的输入框不能把已经填好的值清掉。"""
    raw = load(str(CFG_PATH), with_defaults=False)
    llm = raw.setdefault("llm", {})
    if (body.get("llm_base") or "").strip():
        llm["base_url"] = body["llm_base"].strip()
    if (body.get("llm_model") or "").strip():
        llm["model"] = body["llm_model"].strip()
    if (body.get("llm_key") or "").strip():
        llm["api_key"] = body["llm_key"].strip()

    rag = raw.setdefault("rag", {})
    for name in ("embed", "rerank"):
        seg = rag.setdefault(name, {})
        b = (body.get(f"{name}_base") or "").strip()
        m = (body.get(f"{name}_model") or "").strip()
        k = (body.get(f"{name}_key") or "").strip()
        if b:
            seg["base_url"] = b
        if m:
            seg["model"] = m
        if k:
            seg["api_key"] = k
    if (body.get("rerank_on") is not None):
        rag.setdefault("rerank", {})["enabled"] = bool(body["rerank_on"])
    save(str(CFG_PATH), raw)
    cfg.reload(force=True)
    return ready_state()


def roster_pack() -> dict:
    """导出「拿到就能登录」的花名册。

    二维码用紧凑行格式（省掉重复字段名），32 个号才塞得进一张码；
    文件导入用完整 JSON。
    """
    raw = load(str(CFG_PATH), with_defaults=False)
    accounts = raw.get("accounts") or {}
    rows, lines = [], []
    for key, a in accounts.items():
        if not (a or {}).get("user_id"):
            continue
        # 只导出「矩阵里正在用的号」：主账号、手工小号这些 enabled=False 的一律不进包，
        # 一是它们本来不该出现在手机端，二是主账号没有用户名密码，导了也登录不了。
        if not a.get("enabled"):
            continue
        cred: dict = {}
        p = BASE / f"account_{key}.json"
        if p.exists():
            try:
                cred = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                cred = {}
        username = cred.get("username") or a.get("username") or ""
        password = cred.get("password") or ""
        if not username or not password:
            continue
        label = a.get("label") or key
        name = (label.split("·")[0] or key).strip()
        rows.append({"key": key, "name": name, "label": label,
                     "campus": a.get("campus") or "", "college": a.get("college") or "",
                     "persona": a.get("persona") or "",
                     "username": username, "password": password,
                     "userId": a.get("user_id")})
        # 二维码里只放「够登录」的四个字段。带上 label 会到 2900+ 字节，
        # 正好卡在 QR v40 的容量天花板上，手机端识别率会崩。
        lines.append(f"{name}|{username}|{password}|{a.get('user_id')}")
    payload = "SD1\n" + "\n".join(lines)
    return {"v": 1, "n": len(rows), "list": rows,
            "payload": payload, "bytes": len(payload.encode("utf-8"))}


def register_account(body: dict) -> dict:
    """注册一个非微信账号并接入矩阵。"""
    with _register_lock:
        pkey = body.get("persona") or "warm"
        if pkey not in persona.PERSONAS:
            raise ValueError(f"未知人设 {pkey}")
        label = (body.get("label") or "").strip() or (
            f"{persona.PERSONAS[pkey]['label']} · 新号"
        )
        username = (body.get("username") or "").strip()
        if username and not username.isalnum():
            raise ValueError("用户名只能是字母和数字")

        cfg_raw = load(str(CFG_PATH), with_defaults=False)
        accounts = cfg_raw.setdefault("accounts", {})
        base_key = pkey
        key = base_key
        i = 2
        while key in accounts:
            key = f"{base_key}{i}"
            i += 1

        boot = None
        for cand in ("tokens_bot.json", "tokens.json"):
            p = BASE / cand
            if p.exists():
                boot = Client(cfg.data["app_id"], cfg.data["school_id"], str(p))
                break
        if boot is None:
            raise SzlikeError("没有可用的注册通道 token")

        password = "".join(secrets.choice(string.ascii_letters + string.digits)
                           for _ in range(18))
        reg = None
        last_err = None
        for _ in range(4):
            u = username or _gen_username()
            try:
                reg = ops.register(boot, u, password)
                username = u
                break
            except Exception as e:
                last_err = e
                username = ""
                time.sleep(1.5)
        if not reg:
            raise SzlikeError(f"注册失败：{last_err}")

        (BASE / f"tokens_{key}.json").write_text(json.dumps({
            "access_token": reg["accessToken"],
            "refresh_token": reg["refreshToken"],
            "access_exp": jwt_payload(reg["accessToken"])["exp"],
            "refresh_exp": jwt_payload(reg["refreshToken"])["exp"],
            "updated_at": int(time.time()),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        (BASE / f"account_{key}.json").write_text(json.dumps({
            "username": username, "password": password,
            "user_id": reg["id"], "persona": pkey,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        times = body.get("times") or _default_times(len(accounts))
        accounts[key] = {
            "label": label,
            "tokens": f"tokens_{key}.json",
            "user_id": reg["id"],
            "username": username,
            "persona": pkey,
            "kind": "password",
            "enabled": bool(body.get("enabled", True)),
            "prompts": persona.default_prompts(pkey),
            "tasks": {
                "scheduled_post": {"enabled": True, "times": times,
                                   "seed": "写一条今天的校园日常", "content_pool": [],
                                   "use_prompt": True},
                "reply_comments": {"enabled": True, "interval": 1200, "max_per_run": 2},
                "autolike": {"enabled": True, "interval": 5400, "max_per_run": 3},
                "random_comment": {"enabled": True, "interval": 3000, "max_per_run": 1,
                                   "skip_if_comments_over": 25, "max_age_hours": 48},
            },
            "guard": {
                "quiet_hours": [0, 7], "jitter_seconds": [5, 15],
                "daily_limits": {"post": 2, "comment": 4, "reply": 6, "like": 15},
                "hourly_limits": {"post": 1, "comment": 2, "reply": 3, "like": 6},
            },
        }
        save(str(CFG_PATH), cfg_raw)
        cfg.reload(force=True)
        log.warning("新账号已注册：%s（%s / %s）", label, key, username)
        return {"name": key, "label": label, "user_id": reg["id"],
                "username": username, "persona": pkey}


def _gen_username() -> str:
    pools = ["xiaoyu", "linmu", "chenxi", "nanfeng", "qinghe", "yueguang",
             "shulin", "anran", "muxi", "beichen"]
    return random.choice(pools) + "".join(random.choice(string.digits) for _ in range(4))


def _default_times(n: int) -> list[str]:
    slots = ["10:20", "15:40", "20:10", "12:30", "18:50", "08:50"]
    return [slots[n % len(slots)]]


def set_account(name: str, patch: dict) -> dict:
    cfg_raw = load(str(CFG_PATH), with_defaults=False)
    acc = (cfg_raw.get("accounts") or {}).get(name)
    if not acc:
        raise ValueError(f"没有账号 {name}")
    for k, v in patch.items():
        if k in ("enabled", "label", "persona"):
            acc[k] = v
    save(str(CFG_PATH), cfg_raw)
    cfg.reload(force=True)
    return {"ok": True}


def tail_log(n: int = 120) -> list[str]:
    if LOG_PATH.exists():
        try:
            with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
                return [ln.rstrip() for ln in f.readlines()[-n:]]
        except Exception:
            pass
    return list(ring.buf)[-n:]


# ---------- HTTP ----------


class Handler(BaseHTTPRequestHandler):
    server_version = "szlike-web/2.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        return

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _err(self, msg: str, code: int = 200) -> None:
        self._json({"ok": False, "error": msg}, code)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            raise ValueError("请求体不是合法 JSON") from None

    # --- GET ---

    def do_GET(self) -> None:
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        path = u.path

        try:
            if path in ("/", "/index.html"):
                f = WEB_DIR / "index.html"
                if not f.exists():
                    return self._send(500, b"web/index.html missing", "text/plain")
                return self._send(200, f.read_bytes(), "text/html; charset=utf-8")

            if path == "/api/state":
                return self._json({"ok": True, "data": build_state()})

            if path == "/api/content":
                kind = (qs.get("kind") or ["posts"])[0]
                acct = (qs.get("account") or [None])[0]
                return self._json({"ok": True, "data": list_content(kind, name=acct)})

            if path == "/api/logs":
                n = int((qs.get("n") or ["120"])[0])
                return self._json({"ok": True, "data": tail_log(n)})

            if path == "/api/history":
                kind = (qs.get("kind") or [None])[0]
                acct = (qs.get("account") or [None])[0]
                rows = get_store(acct).list_actions(kind, 200)
                for r in rows:
                    r["time"] = datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M")
                return self._json({"ok": True, "data": rows})

            if path == "/api/kb":
                return self._json({"ok": True, "data": kb_payload()})

            if path == "/api/rag/probe":
                return self._json({"ok": True, "data": rag.probe()})

            if path == "/api/review":
                qs = parse_qs(u.query)
                return self._json({"ok": True, "data": review_list(
                    (qs.get("status") or ["pending"])[0])})

            if path == "/api/ping":
                return self._json({"ok": True})

            if path == "/api/ready":
                return self._json({"ok": True, "data": ready_state()})

            if path == "/api/roster/pack":
                return self._json({"ok": True, "data": roster_pack()})

            if path == "/api/spawn/status":
                with _spawn_lock:
                    snap = json.loads(json.dumps(_spawn_state, ensure_ascii=False))
                return self._json({"ok": True, "data": snap})

            return self._err("not found: " + path, 404)

        except Exception as e:
            log.error("GET %s 失败: %s", path, traceback.format_exc(limit=3))
            return self._err(f"{type(e).__name__}: {e}")

    # --- POST ---

    def do_POST(self) -> None:
        u = urlparse(self.path)
        path = u.path
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._err("Content-Type 必须是 application/json", 415)
        try:
            body = self._body()
        except ValueError as e:
            return self._err(str(e), 400)

        try:
            if path == "/api/config":
                patch = body.get("patch") or {}
                llm = patch.get("llm")
                if isinstance(llm, dict) and not llm.get("api_key"):
                    llm.pop("api_key", None)
                if patch.get("is_anonymous") is True:
                    patch.pop("is_anonymous")
                cfg.update(patch)
                _viewer_cache.clear()
                log.info("配置已更新：%s", list((body.get("patch") or {}).keys()))
                return self._json({"ok": True, "data": build_state()})

            if path == "/api/account":
                acct = body.get("name")
                if not acct:
                    return self._err("name 必填")
                if body.get("view"):
                    cfg.update({"active_account": acct})
                    _viewer_cache.clear()
                else:
                    set_account(acct, {k: body[k] for k in ("enabled", "label", "persona")
                                       if k in body})
                return self._json({"ok": True, "data": build_state()})

            if path == "/api/settings/keys":
                return self._json({"ok": True, "data": save_keys(body)})

            if path == "/api/spawn":
                n = max(1, min(32, int(body.get("count") or 1)))
                with _spawn_lock:
                    if _spawn_state["running"]:
                        return self._err("正在生成，等这一批跑完")
                    _spawn_state.update(running=True, done=0, total=n, log=[], result=None)
                raw = load(str(CFG_PATH), with_defaults=False)
                app_id = raw.get("app_id") or cfg.data.get("app_id")
                school_id = raw.get("school_id") or cfg.data.get("school_id")

                def _run() -> None:
                    try:
                        from szlike import spawn as spawnmod
                        roster = BASE / "accounts_roster.json"
                        if not roster.exists():
                            roster = BASE / "accounts_roster.example.json"
                        r = spawnmod.spawn(n, BASE, app_id, school_id,
                                           roster, CFG_PATH,
                                           on_progress=_spawn_progress)
                        with _spawn_lock:
                            _spawn_state["result"] = r
                    except Exception as e:
                        log.error("spawn 失败：%s", traceback.format_exc(limit=3))
                        _spawn_progress(0, n, f"出错：{type(e).__name__}: {e}", False)
                    finally:
                        with _spawn_lock:
                            _spawn_state["running"] = False
                        try:
                            cfg.reload(force=True)
                            _viewer_cache.clear()
                        except Exception:
                            pass

                threading.Thread(target=_run, daemon=True).start()
                return self._json({"ok": True, "data": {"started": n}})

            if path == "/api/register":
                return self._json({"ok": True, "data": register_account(body)})

            if path == "/api/prompt":
                key = body.get("key")
                if not key:
                    return self._err("缺少 key")
                acct = body.get("account") or view_name()
                cfg_raw = load(str(CFG_PATH), with_defaults=False)
                acc = (cfg_raw.get("accounts") or {}).get(acct)
                if not acc:
                    return self._err(f"没有账号 {acct}")
                prompts = acc.setdefault("prompts", {})
                cur = dict(prompts.get(key) or {})
                for f in ("system", "user", "label"):
                    if f in body:
                        cur[f] = body[f]
                if "enabled" in body:
                    cur["enabled"] = bool(body["enabled"])
                prompts[key] = cur
                acc["prompts"] = prompts
                save(str(CFG_PATH), cfg_raw)
                cfg.reload(force=True)
                log.info("「%s」prompt「%s」已保存", acct, key)
                merged = account_config(cfg.data, str(BASE), acct).get("prompts") or {}
                return self._json({"ok": True, "data": merged})

            if path == "/api/task":
                key = body.get("key")
                if not key:
                    return self._err("缺少 key")
                acct = body.get("account") or view_name()
                cfg_raw = load(str(CFG_PATH), with_defaults=False)
                acc = (cfg_raw.get("accounts") or {}).get(acct)
                if not acc:
                    return self._err(f"没有账号 {acct}")
                tasks = acc.setdefault("tasks", {})
                tasks[key] = {**(tasks.get(key) or {}), **(body.get("patch") or {})}
                acc["tasks"] = tasks
                save(str(CFG_PATH), cfg_raw)
                cfg.reload(force=True)
                log.info("「%s」任务「%s」已保存", acct, key)
                return self._json({"ok": True, "data": build_state()})

            if path == "/api/delete":
                kind = body.get("kind")
                oid = body.get("id")
                if not kind or not oid:
                    return self._err("kind 和 id 必填")
                return self._json({"ok": True, "data": do_delete(kind, oid, body.get("account"))})

            if path == "/api/action":
                return self._json({"ok": True, "data": do_action(body)})

            if path == "/api/run":
                return self._json({"ok": True, "data": run_task(
                    body.get("task") or "", body.get("account"))})

            if path == "/api/review":
                return self._json({"ok": True, "data": review_act(body)})

            if path == "/api/kb":
                return self._json({"ok": True, "data": kb_save(body.get("json") or {})})

            if path == "/api/kb/rebuild":
                return self._json({"ok": True, "data": kb_rebuild(body.get("steps") or "all")})

            if path == "/api/llm/test":
                acct = body.get("account")
                b = build(acct)[2]
                return self._json({"ok": True, "data": b.probe(
                    body.get("system") or "", body.get("user") or "",
                    body.get("max_tokens"))})

            if path == "/api/token/refresh":
                acct = body.get("account")
                names = [acct] if acct else [a["name"] for a in accounts_index(cfg.data)
                                             if a["enabled"]]
                out = {}
                for n in names:
                    try:
                        c = new_client(name=n)
                        before = c.tokens.refresh
                        c.refresh_now(force=True)
                        out[n] = {"rotated": c.tokens.refresh != before,
                                  "access_text": fmt_left(c.tokens.access_left()),
                                  "refresh_text": fmt_left(c.tokens.refresh_left())}
                    except Exception as e:
                        out[n] = {"error": str(e)[:120]}
                return self._json({"ok": True, "data": out})

            if path == "/api/daemon":
                act = body.get("action")
                if act == "start":
                    return self._json({"ok": True, "data": daemon_start()})
                if act == "stop":
                    return self._json({"ok": True, "data": daemon_stop()})
                return self._json({"ok": True, "data": daemon_state()})

            return self._err("not found: " + path, 404)

        except AuthError as e:
            return self._err(f"鉴权失败：{e}")
        except SzlikeError as e:
            return self._err(str(e))
        except Exception as e:
            log.error("POST %s 失败: %s", path, traceback.format_exc(limit=4))
            return self._err(f"{type(e).__name__}: {e}")


def main(host: str | None = None, port: int | None = None) -> None:
    setup_logging()
    d = cfg.data
    web = d.get("web") or {}
    host = host or web.get("host") or "127.0.0.1"
    port = int(port or web.get("port") or 8765)

    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    print("=" * 58)
    print(f"  鼠洞SD 控制台  ->  http://{host}:{port}")
    print(f"  配置：{CFG_PATH}")
    print(f"  dry_run = {d.get('dry_run')}   账号数 = {len(accounts_index(d))}")
    print("  Ctrl+C 退出")
    print("=" * 58)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
