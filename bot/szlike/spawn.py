"""自动开号：程序自己向服务端注册新账号，再把人设分配给它。

为什么不需要用户的账号
----------------------
`register` 这个 mutation 不要求登录态，用 app-id / school-id 两个头部就能直接调，
所以用户拿到包之后不用提供任何现成账号，点一次「生成」就从零开号。

分配规则
--------
花名册里 32 条人设（校区 / 学院 / 专业 / 性格 / 作息各不同）。生成时优先挑
**还没被用过**的条目，所以头 32 个号彼此不重样；超过 32 个才开始循环复用，
重复的那份 key 加后缀（`warm2`、`warm3`），人设内容照旧。
"""
from __future__ import annotations

import json
import random
import secrets
import string
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import persona
from .client import jwt_payload

ENDPOINT = "https://api.szlikeyou.com/graphql"
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.40")

M_REGISTER = """
mutation M($u: String!, $p: String!) {
  register(input:{basicInput:{username:$u, password:$p}}) {
    id accessToken refreshToken
  }
}
"""

GUARD = {
    "quiet_hours": [0, 7],
    "jitter_seconds": [5, 15],
    "daily_limits": {"post": 1, "comment": 2, "reply": 3, "like": 6},
    "hourly_limits": {"post": 1, "comment": 1, "reply": 2, "like": 3},
}
TASKS = {
    "scheduled_post": {"enabled": True, "times": [], "use_prompt": True,
                       "seed": "", "content_pool": []},
    "reply_comments": {"enabled": True, "interval": 2400, "max_per_run": 1},
    "autolike": {"enabled": True, "interval": 7200, "max_per_run": 2, "llm_filter": False},
    "random_comment": {"enabled": True, "interval": 4200, "max_per_run": 1,
                       "skip_if_comments_over": 20, "max_age_hours": 48},
}


def rand_username(prefix: str) -> str:
    return f"{prefix or 'user'}{''.join(random.choice(string.digits) for _ in range(4))}"


def rand_password(n: int = 18) -> str:
    abc = string.ascii_letters + string.digits
    return "".join(secrets.choice(abc) for _ in range(n))


def _headers(app_id: str, school_id: str) -> dict:
    return {
        "content-type": "application/json",
        "mp-app-id": app_id,
        "mp-school-id": school_id,
        "xweb_xhr": "1",
        "accept": "*/*",
        "accept-language": "zh-CN,zh;q=0.9",
        "user-agent": UA,
        "referer": f"https://servicewechat.com/{app_id}/16/page-frame.html",
    }


def register_anon(app_id: str, school_id: str, username: str, password: str,
                  timeout: float = 25.0) -> dict:
    """不带 Authorization 直接注册。返回 {id, accessToken, refreshToken}。"""
    body = json.dumps({
        "operationName": "M",
        "variables": {"u": username, "p": password},
        "query": M_REGISTER,
    }).encode()
    req = urllib.request.Request(ENDPOINT, data=body, method="POST",
                                 headers=_headers(app_id, school_id))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode())
    if d.get("errors"):
        raise RuntimeError(json.dumps(d["errors"], ensure_ascii=False)[:200])
    reg = (d.get("data") or {}).get("register")
    if not reg or not reg.get("accessToken"):
        raise RuntimeError("注册没有返回 token")
    return reg


def _unique_key(base: str, taken: set[str]) -> str:
    if base not in taken:
        return base
    i = 2
    while f"{base}{i}" in taken:
        i += 1
    return f"{base}{i}"


def _pick_slots(roster: list[dict], accounts: dict, count: int) -> list[dict]:
    """优先没被用过的人设；用完了再循环（key 加后缀）。"""
    used = {k for k, v in accounts.items() if (v or {}).get("user_id")}
    taken = set(accounts)
    free = [x for x in roster if x["key"] not in used]
    random.shuffle(free)
    out: list[dict] = []

    for item in free[:count]:
        key = _unique_key(item["key"], taken)
        taken.add(key)
        out.append({**item, "_key": key})

    while len(out) < count:
        item = random.choice(roster)
        key = _unique_key(item["key"], taken)
        taken.add(key)
        out.append({**item, "_key": key})
    return out


def _entry(item: dict, key: str, user_id: str, username: str) -> dict:
    return {
        "label": item.get("label") or key,
        "tokens": f"tokens_{key}.json",
        "user_id": user_id,
        "username": username,
        "persona": item.get("persona") or "warm",
        "kind": "password",
        "enabled": True,
        "profile": item.get("profile") or "",
        "campus": item.get("campus") or "",
        "address": item.get("address") or "",
        "college": item.get("college") or "",
        "major": item.get("major") or "",
        "year": item.get("year") or "",
        "prompts": persona.default_prompts(item.get("persona") or "warm"),
        "guard": json.loads(json.dumps(GUARD)),
        "tasks": {**json.loads(json.dumps(TASKS)),
                  "scheduled_post": {**json.loads(json.dumps(TASKS["scheduled_post"])),
                                     "times": [item.get("post_time") or "12:00"]}},
    }


def spawn(count: int, base_dir: str | Path, app_id: str, school_id: str,
          roster_path: str | Path, config_path: str | Path,
          on_progress=None, dry: bool = False) -> dict:
    """注册 count 个号并写进配置。on_progress(done, total, text, ok) 用于前端进度。"""
    base = Path(base_dir)
    roster = json.loads(Path(roster_path).read_text(encoding="utf-8"))
    raw = json.loads(Path(config_path).read_text(encoding="utf-8"))
    accounts = raw.setdefault("accounts", {})

    slots = _pick_slots(roster, accounts, max(1, int(count)))
    done, ok_n, fails = 0, 0, 0
    added: list[dict] = []

    for item in slots:
        key = item["_key"]
        if dry:
            done += 1
            if on_progress:
                on_progress(done, len(slots), f"[dry] {key} {item.get('label')}", True)
            continue

        reg = None
        username = ""
        last = ""
        for attempt in range(3):
            username = rand_username(item.get("username_prefix") or "user")
            password = rand_password()
            try:
                reg = register_anon(app_id, school_id, username, password)
                break
            except Exception as e:                     # 用户名撞了 / 限流 / 网络
                last = str(e)[:120]
                time.sleep(2 + attempt * 2)
        if not reg:
            fails += 1
            done += 1
            if on_progress:
                on_progress(done, len(slots), f"{key} 注册失败：{last}", False)
            if fails >= 4:
                if on_progress:
                    on_progress(done, len(slots), "连续失败，先停下（可能被限流）", False)
                break
            continue

        try:
            acc_exp = jwt_payload(reg["accessToken"])["exp"]
            ref_exp = jwt_payload(reg["refreshToken"])["exp"]
        except Exception:
            acc_exp = int(time.time()) + 6 * 3600
            ref_exp = int(time.time()) + 60 * 86400

        (base / f"tokens_{key}.json").write_text(json.dumps({
            "access_token": reg["accessToken"],
            "refresh_token": reg["refreshToken"],
            "access_exp": acc_exp,
            "refresh_exp": ref_exp,
            "updated_at": int(time.time()),
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        (base / f"account_{key}.json").write_text(json.dumps({
            "username": username, "password": password,
            "user_id": reg["id"], "persona": item.get("persona"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        accounts[key] = _entry(item, key, reg["id"], username)
        Path(config_path).write_text(json.dumps(raw, ensure_ascii=False, indent=1),
                                     encoding="utf-8")
        ok_n += 1
        done += 1
        added.append({"key": key, "label": item.get("label"), "username": username,
                      "user_id": reg["id"], "persona": item.get("persona")})
        if on_progress:
            on_progress(done, len(slots),
                        f"{key} · {item.get('label')} → @{username}", True)
        time.sleep(random.uniform(2.5, 4.5))       # 别把注册接口打穿

    return {"total": len(slots), "ok": ok_n, "fail": fails, "added": added}
