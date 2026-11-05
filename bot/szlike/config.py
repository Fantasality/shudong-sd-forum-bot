"""配置读写：深合并 + 原子落盘 + mtime 热重载。

多账号模型
----------
全局配置（llm / guard / tasks / prompts / topics）是**默认值**，
`accounts.<name>` 里的同名字段是**该账号的覆盖值**，深合并后生效。
这样一个账号一套人设 prompt、一套任务节奏、一套配额，互不干扰。

`active_account` 只表示「Web 控制台当前查看哪个号」，不再决定 daemon 跑谁；
daemon 会给所有 `enabled=true` 的账号各开一个线程并行跑。
"""
from __future__ import annotations

import copy
import json
import os
import threading
import time

DEFAULTS: dict = {
    "app_id": "",
    "school_id": "",
    "my_user_id": "",
    # 多账号：active_account 只是 Web 的「查看账号」
    "active_account": "main",
    "accounts": {
        "main": {"label": "主账号", "tokens": "tokens.json", "user_id": ""},
    },
    "default_topic_id": "",
    "default_subtopic_id": "",
    "is_anonymous": False,
    "dry_run": True,
    "llm": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "temperature": 0.9,
        "max_tokens": 160,
        "timeout": 25,
    },
    "guard": {
        "quiet_hours": [0, 7],
        "jitter_seconds": [5, 15],
        "skip_keywords": [],
        "daily_limits": {"post": 2, "comment": 6, "reply": 10, "like": 20},
        "hourly_limits": {"post": 1, "comment": 3, "reply": 5, "like": 8},
    },
    "tasks": {
        "scheduled_post": {"enabled": False, "times": [], "use_prompt": True},
        "reply_comments": {"enabled": False, "interval": 600, "max_per_run": 3},
        "autolike": {"enabled": False, "interval": 5400, "max_per_run": 3, "llm_filter": False},
        "random_comment": {"enabled": False, "interval": 2400, "max_per_run": 1},
    },
    "prompts": {},
    "kb": {"enabled": True, "budget": 800, "path": "kb/knowledge.json"},
    "rag": {
        "enabled": True, "shots": 3, "budget": 520,
        "embed": {"base_url": "https://api.siliconflow.cn/v1", "api_key": "",
                  "model": "BAAI/bge-m3"},
        "rerank": {"enabled": False, "base_url": "https://api.siliconflow.cn/v1", "api_key": "",
                   "model": "BAAI/bge-reranker-v2-m3"},
    },
    "web": {"host": "127.0.0.1", "port": 8765},
    "topics": {},
}

# 只允许这些顶层键被 Web UI 写入，防止误改结构
WRITABLE = {
    "dry_run", "is_anonymous", "default_topic_id", "default_subtopic_id",
    "llm", "guard", "tasks", "prompts", "web", "active_account", "accounts", "kb", "rag",
}

# 账号级可覆盖的段
ACCOUNT_OVERRIDABLE = ("prompts", "tasks", "guard", "llm", "topics", "is_anonymous", "kb", "rag")


def resolve_account(cfg: dict, base: str, name: str | None = None) -> dict:
    """把账号名解析成 tokens 文件、user_id、人设等运行期信息。

    返回 {name,label,tokens(绝对路径),user_id,username,enabled,persona,raw}。
    找不到配置时回退到 tokens.json + my_user_id，保证老配置能用。
    """
    cfg = cfg or {}
    name = name or cfg.get("active_account") or "main"
    accounts = cfg.get("accounts") or {}
    acc = accounts.get(name) or {}
    tokens = acc.get("tokens") or f"tokens_{name}.json"
    if not os.path.isabs(tokens):
        tokens = os.path.join(str(base), tokens)
    return {
        "name": name,
        "label": acc.get("label") or name,
        "tokens": tokens,
        "user_id": acc.get("user_id") or (cfg.get("my_user_id") if name == "main" else "") or "",
        "username": acc.get("username") or "",
        "enabled": bool(acc.get("enabled", name == "main")),
        "persona": acc.get("persona") or "warm",
        "profile": acc.get("profile") or "",
        "campus": acc.get("campus") or "",
        "address": acc.get("address") or "",
        "raw": acc,
    }


def account_config(cfg: dict, base: str, name: str | None = None) -> dict:
    """全局默认值 + 该账号覆盖值，合并成这个账号真正要用的配置。"""
    acc = resolve_account(cfg, base, name)
    over = {k: v for k, v in (acc["raw"] or {}).items() if k in ACCOUNT_OVERRIDABLE}
    merged = deep_merge(cfg, over)
    merged["my_user_id"] = acc["user_id"]
    merged["_account"] = acc
    return merged


def accounts_index(cfg: dict) -> list[dict]:
    """给 Web UI 的账号清单（不含敏感 token）。

    DEFAULTS 里预置了一个 `main` 占位号，全新安装时它没有 user_id 也没有用户名，
    列出来只会让用户以为「怎么凭空多了一个号」，所以这里直接过滤掉。
    """
    out = []
    for name, acc in ((cfg or {}).get("accounts") or {}).items():
        if not isinstance(acc, dict):
            continue
        if not (acc.get("user_id") or acc.get("username")):
            continue
        out.append({
            "name": name,
            "label": acc.get("label") or name,
            "user_id": acc.get("user_id") or "",
            "username": acc.get("username") or "",
            "persona": acc.get("persona") or "",
            "enabled": bool(acc.get("enabled", name == "main")),
            "tokens": acc.get("tokens") or f"tokens_{name}.json",
            "dry_run": acc.get("dry_run"),
            "kind": acc.get("kind") or ("wechat" if name == "main" else "password"),
        })
    return out


def deep_merge(base: dict, override: dict) -> dict:
    """override 覆盖 base，dict 递归合并，其他类型直接替换。"""
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


_cache: dict = {"path": "", "mtime": 0.0, "raw": None, "merged": None}
_cache_lock = threading.Lock()


def load(path: str, with_defaults: bool = True) -> dict:
    """按 mtime 缓存的读配置。

    32 个 worker 每 5 秒各读一次配置，等于每分钟解析 384 次 100KB JSON；
    加这层缓存后只有文件真变了才重新解析（实测空闲 CPU 从 0.5% 降到 0.1%）。
    """
    try:
        mt = os.path.getmtime(path)
    except OSError:
        mt = 0.0
    with _cache_lock:
        if _cache["path"] == path and _cache["mtime"] == mt and _cache["raw"] is not None:
            src = _cache["merged"] if with_defaults else _cache["raw"]
            return copy.deepcopy(src)
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        merged = deep_merge(DEFAULTS, raw) if with_defaults else raw
        _cache.update({"path": path, "mtime": mt, "raw": raw, "merged": merged})
    return copy.deepcopy(merged if with_defaults else raw)


def save(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def update(path: str, patch: dict) -> dict:
    """把 patch 合并进磁盘上的配置。只接受白名单内的键。"""
    cur = load(path, with_defaults=False)
    clean = {k: v for k, v in (patch or {}).items() if k in WRITABLE}
    if not clean:
        return load(path)
    merged = deep_merge(cur, clean)
    save(path, merged)
    return merged


class LiveConfig:
    """按 mtime 自动重载的配置句柄。线程安全。"""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._mtime = 0.0
        self._data: dict = {}
        self.reload(force=True)

    def reload(self, force: bool = False) -> dict:
        with self._lock:
            m = os.path.getmtime(self.path)
            if force or m != self._mtime:
                self._data = load(self.path)
                self._mtime = m
            return self._data

    @property
    def data(self) -> dict:
        return self.reload()

    def update(self, patch: dict) -> dict:
        with self._lock:
            cur = load(self.path, with_defaults=False)
            clean = {k: v for k, v in (patch or {}).items() if k in WRITABLE}
            if clean:
                save(self.path, deep_merge(cur, clean))
            self._data = load(self.path)
            self._mtime = os.path.getmtime(self.path)
            return self._data


def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")
