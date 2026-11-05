"""32 号批量管理。一条命令做一件事，不碰 daemon 的运行逻辑。

    python manage.py status              # 全矩阵状态：校区/人设/token 余期/今日动作
    python manage.py sync                # 把 accounts_roster.json 同步进 config.json
    python manage.py register --limit 8  # 批量注册还没号的条目（带抖动，失败即停）
    python manage.py verify              # 用每个号自己的 token 调 viewer
    python manage.py pause tcz_cs        # 暂停某个号（或 all）
    python manage.py resume tcz_cs
    python manage.py times               # 打印 32 个发帖时刻

为什么单独一个脚本
------------------
注册、改配额、看状态这些是「运维动作」，塞进 daemon 会让运行逻辑和批量操作互相纠缠。
这里只读写 config.json 和 tokens_*.json，daemon 每 5 秒重读配置，改完即生效。
"""
from __future__ import annotations

import argparse
import json
import random
import secrets
import string
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from szlike import ops, persona                        # noqa: E402
from szlike.client import Client, jwt_payload          # noqa: E402
from szlike.config import load, save                   # noqa: E402

ROSTER = BASE / "accounts_roster.json"
CFG = BASE / "config.json"

# 32 个号的统一节奏。总量算下来：32 帖 + 64 评论 + 最多 96 回复 + 192 赞/天。
# 单号降额是为了不让「每个号都很活跃」这件事本身变成特征。
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


def roster() -> list[dict]:
    return json.loads(ROSTER.read_text(encoding="utf-8"))


def rand_username(prefix: str) -> str:
    return f"{prefix}{''.join(random.choice(string.digits) for _ in range(4))}"


def rand_password(n: int = 18) -> str:
    abc = string.ascii_letters + string.digits
    return "".join(secrets.choice(abc) for _ in range(n))


def account_entry(item: dict, old: dict | None = None) -> dict:
    """花名册一行 → config.accounts 的一条。保留已有 token / user_id / 用户名。"""
    acc = dict(old or {})
    acc.update({
        "label": item["label"],
        "persona": item["persona"],
        "profile": item["profile"],
        "campus": item["campus"],
        "address": item["address"],
        "college": item["college"],
        "major": item["major"],
        "year": item["year"],
        "kind": "password",
        "prompts": persona.default_prompts(item["persona"]),
        "guard": json.loads(json.dumps(GUARD)),
        "tasks": {**json.loads(json.dumps(TASKS)),
                  "scheduled_post": {**json.loads(json.dumps(TASKS["scheduled_post"])),
                                     "times": [item["post_time"]]}},
    })
    acc.setdefault("tokens", f"tokens_{item['key']}.json")
    if not acc.get("user_id"):
        acc["enabled"] = False        # 还没注册的号不进 daemon，免得开线程报错
    else:
        acc.setdefault("enabled", True)
    return acc


# ------------------------------------------------------------------ sync
def cmd_sync(args) -> None:
    cfg = load(str(CFG))
    raw = load(str(CFG), with_defaults=False)
    accounts = raw.setdefault("accounts", {})
    added = updated = 0
    for item in roster():
        key = item["key"]
        old = accounts.get(key)
        accounts[key] = account_entry(item, old)
        if old:
            updated += 1
        else:
            added += 1
    # 花名册里已经没有、又没注册过的占位条目直接清掉（main/bot 有 user_id，保留但关闭）
    keys = {x["key"] for x in roster()}
    for k in [k for k in accounts if k not in keys and not (accounts[k] or {}).get("user_id")]:
        accounts.pop(k)

    # 主号与旧小号不再参与
    for k in ("main", "bot"):
        if k in accounts:
            accounts[k]["enabled"] = False
    save(str(CFG), raw)
    print(f"同步完成：新增 {added}，更新 {updated}，config 里共 {len(accounts)} 个账号")


# ------------------------------------------------------------------ register
def cmd_register(args) -> None:
    raw = load(str(CFG), with_defaults=False)
    accounts = raw.setdefault("accounts", {})
    cfg = load(str(CFG))
    todo = [x for x in roster()
            if not (accounts.get(x["key"]) or {}).get("user_id")
            or not (BASE / f"tokens_{x['key']}.json").exists()]
    if args.limit:
        todo = todo[: args.limit]
    print(f"待注册 {len(todo)} 个")
    if args.dry:
        for x in todo:
            print("  计划", x["key"], x["label"], rand_username(x["username_prefix"]))
        return
    if not todo:
        return
    boot = None
    for cand in ("tokens_warm.json", "tokens_bot.json", "tokens.json"):
        if (BASE / cand).exists():
            boot = Client(cfg["app_id"], cfg["school_id"], str(BASE / cand))
            break
    if boot is None:
        sys.exit("没有可用 token 当注册通道")
    boot.ensure_token()

    fails = 0
    for n, item in enumerate(todo, 1):
        key = item["key"]
        username, pwd, reg = rand_username(item["username_prefix"]), rand_password(), None
        for attempt in range(3):
            try:
                reg = ops.register(boot, username, pwd)
                break
            except Exception as e:
                print(f"  [{n}/{len(todo)}] {key} 第 {attempt+1} 次失败：{str(e)[:60]}")
                username = rand_username(item["username_prefix"])
                time.sleep(3)
        if not reg:
            fails += 1
            if fails >= 3:
                print("连续失败 3 个，先停下，可能是注册被限流")
                break
            continue
        fails = 0
        tokens = {
            "access_token": reg["accessToken"], "refresh_token": reg["refreshToken"],
            "access_exp": jwt_payload(reg["accessToken"])["exp"],
            "refresh_exp": jwt_payload(reg["refreshToken"])["exp"],
            "updated_at": int(time.time()),
        }
        (BASE / f"tokens_{key}.json").write_text(
            json.dumps(tokens, ensure_ascii=False, indent=2), encoding="utf-8")
        (BASE / f"account_{key}.json").write_text(
            json.dumps({"username": username, "password": pwd,
                        "user_id": reg["id"], "persona": item["persona"]},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        accounts[key] = account_entry(item, accounts.get(key))
        accounts[key].update({"user_id": reg["id"], "username": username,
                              "tokens": f"tokens_{key}.json", "enabled": True})
        save(str(CFG), raw)
        print(f"  [{n}/{len(todo)}] {key} ok  user_id={reg['id']}  username={username}")
        time.sleep(random.uniform(6, 11))     # 别把注册接口打穿


# ------------------------------------------------------------------ status
def cmd_status(args) -> None:
    cfg = load(str(CFG))
    import sqlite3
    db = sqlite3.connect(str(BASE / "state.db"))
    today0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    rows = []
    for item in roster():
        key = item["key"]
        acc = (cfg.get("accounts") or {}).get(key) or {}
        tok = BASE / f"tokens_{key}.json"
        left = "—"
        if tok.exists():
            try:
                exp = json.loads(tok.read_text(encoding="utf-8")).get("refresh_exp", 0)
                left = f"{(exp - time.time()) / 86400:.0f}d"
            except Exception:
                left = "?"
        cnt = {}
        for kind in ("post", "comment", "reply", "like"):
            cnt[kind] = db.execute(
                "SELECT COUNT(*) FROM actions WHERE kind=? AND ts>=?",
                (f"{key}:{kind}", today0)).fetchone()[0]
        rows.append((item["campus"], item["major"], item["persona"],
                     "on" if acc.get("enabled") else "off",
                     "有" if acc.get("user_id") else "无", left,
                     cnt["post"], cnt["comment"], cnt["reply"], cnt["like"]))
    db.close()
    print(f"{'校区':<4}{'专业':<16}{'人设':<8}{'启':<4}{'号':<4}{'token':<7}"
          f"{'帖':>3}{'评':>3}{'回':>3}{'赞':>3}")
    for r in rows:
        print(f"{r[0]:<4}{r[1][:14]:<16}{r[2]:<8}{r[3]:<4}{r[4]:<4}{r[5]:<7}"
              f"{r[6]:>3}{r[7]:>3}{r[8]:>3}{r[9]:>3}")
    c = Counter(x[0] for x in rows)
    print(f"\n合计 {len(rows)} 个号，" + "，".join(f"{k}{v}" for k, v in c.items())
          + f"；已注册 {sum(1 for x in rows if x[4] == '有')}，启用 {sum(1 for x in rows if x[3] == 'on')}")


def cmd_verify(args) -> None:
    cfg = load(str(CFG))
    bad = 0
    for item in roster():
        key = item["key"]
        arc = BASE / f"account_{key}.json"
        tok = BASE / f"tokens_{key}.json"
        if not tok.exists():
            print(f"  {key:10} 未注册")
            continue
        try:
            c = Client(cfg["app_id"], cfg["school_id"], str(tok),
                       account_path=str(arc) if arc.exists() else None)
            v = ops.viewer(c)
            u = v.get("user") or {}
            print(f"  {key:10} ok  {(u.get('profile') or {}).get('nickname') or '(空昵称)'}  {u.get('id')}")
        except Exception as e:
            bad += 1
            print(f"  {key:10} 失败：{str(e)[:70]}")
    print(f"\n失败 {bad} 个")


def cmd_pause(args) -> None:
    raw = load(str(CFG), with_defaults=False)
    keys = [x["key"] for x in roster()] if args.key == "all" else [args.key]
    for k in keys:
        if k in raw.get("accounts", {}):
            raw["accounts"][k]["enabled"] = False
    save(str(CFG), raw)
    print("已暂停：", ", ".join(keys))


def cmd_resume(args) -> None:
    raw = load(str(CFG), with_defaults=False)
    keys = [x["key"] for x in roster()] if args.key == "all" else [args.key]
    for k in keys:
        if k in raw.get("accounts", {}):
            raw["accounts"][k]["enabled"] = True
    save(str(CFG), raw)
    print("已启用：", ", ".join(keys))


def cmd_times(args) -> None:
    for item in roster():
        print(f"  {item['post_time']}  {item['key']:<10} {item['campus']:<4} "
              f"{item['major'][:12]:<14}{item['persona']}")


def cmd_review(args) -> None:
    """命令行处理待审队列。不开控制台时用这个，也是批量放行的入口。"""
    import json as _json
    import time as _time

    from szlike.store import Store

    st = Store(str(BASE / "state.db"))
    status = args.status or "pending"
    items = st.draft_list(status, limit=200)

    if args.approve_all:
        n = 0
        for it in items:
            if it["score"] >= (args.min or 0):
                st.draft_decide(it["id"], "approved", by="cli", note="批量通过")
                n += 1
        print(f"已通过 {n} 条（≥{args.min or 0} 分）")
        return
    if args.approve:
        st.draft_decide(args.approve, "approved", by="cli", note="命令行通过")
        print(f"#{args.approve} 已通过，daemon 下一轮发")
        return
    if args.reject:
        st.draft_decide(args.reject, "rejected", by="cli", note=args.note or "命令行打回")
        print(f"#{args.reject} 已打回")
        return

    if not items:
        print(f"{status} 队列是空的")
        return
    for it in items:
        try:
            parts = _json.loads(it["detail"] or "{}")
        except Exception:
            parts = {}
        when = _time.strftime("%m-%d %H:%M", _time.localtime(it["created"]))
        print(f"\n#{it['id']}  {it['label'] or it['account']}  {it['score']} 分  "
              f"[{it['board'] or '—'}]  {when}  {it['status']}")
        print("   " + it["content"].replace("\n", "\n   "))
        if parts:
            print("   " + "  ".join(f"{k}{v}" for k, v in parts.items()))
        if it["fixes"]:
            print("   扣分：" + it["fixes"])
    print(f"\n共 {len(items)} 条。通过：manage.py review --approve <id>　"
          "打回：--reject <id>　批量：--approve-all --min 85")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync").set_defaults(fn=cmd_sync)
    r = sub.add_parser("register")
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--dry", action="store_true")
    r.set_defaults(fn=cmd_register)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("verify").set_defaults(fn=cmd_verify)
    sub.add_parser("times").set_defaults(fn=cmd_times)
    r = sub.add_parser("review")
    r.add_argument("--status", default="pending")
    r.add_argument("--approve", type=int, default=0)
    r.add_argument("--reject", type=int, default=0)
    r.add_argument("--approve-all", action="store_true")
    r.add_argument("--min", type=int, default=0)
    r.add_argument("--note", default="")
    r.set_defaults(fn=cmd_review)
    for name, fn in (("pause", cmd_pause), ("resume", cmd_resume)):
        p = sub.add_parser(name)
        p.add_argument("key")
        p.set_defaults(fn=fn)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
