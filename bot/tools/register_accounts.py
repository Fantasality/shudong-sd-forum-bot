"""一次性脚本：注册 3 个「非微信」小号并写入人设。

用法：python register_accounts.py
      python register_accounts.py --dry   只打印计划，不真的注册

做的事情：
1. 调 register mutation（用户名+密码，不经过微信）
2. 把 access/refresh token 写进 tokens_<key>.json
3. 把用户名密码写进 account_<key>.json（refresh 过期时可自动重新登录）
4. 把账号、人设 prompt、任务节奏写进 config.json
5. 用新 token 调 viewer 验证，失败会明确报出来
"""
from __future__ import annotations

import argparse
import json
import os
import random
import secrets
import string
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from szlike import ops, persona                       # noqa: E402
from szlike.client import Client, jwt_payload         # noqa: E402
from szlike.config import load, save                  # noqa: E402

CONFIG_PATH = BASE / "config.json"

# key, 展示名, 人设, 用户名前缀, 发帖时间点, 固定身份
# 时间点全部错开：七个号同一分钟发帖，一眼就是脚本
SPECS = [
    ("warm", "暖阳 · 温柔", "warm", "linxiaoyu", ["10:20"],
     "汉语言文学大二，住本部7舍，常在图书馆三楼靠窗的位置"),
    ("wit", "冷面 · 精辟搞笑", "wit", "chenmomo", ["15:40"],
     "法学大三，住独墅湖二期，自习室和打印店常客"),
    ("zest", "火苗 · 热情奔放", "zest", "zhaoyang", ["20:10"],
     "体育教育大二，跑操场，报了校运会三项"),
    ("cool", "青柠 · 寡言", "cool", "qning", ["08:40"],
     "建筑学大二，住本部，爱在教学楼连廊待着画图"),
    ("nerd", "阿岩 · 理工宅", "nerd", "ayan", ["12:30"],
     "电子信息大三，宿舍里自己修东西，实验室常客"),
    ("food", "汤圆 · 吃货", "food", "tangyuan", ["17:20"],
     "食品科学大二，把两个食堂的窗口吃了一遍，记价格"),
    ("night", "夜行 · 夜猫", "night", "yexing", ["23:10"],
     "新闻传播大三，熬夜写稿，凌晨在操场夜跑"),
]

TASK_TEMPLATE = {
    "scheduled_post": {
        "enabled": True,
        "times": [],
        "seed": "写一条今天的校园日常，不要写食堂排队和早八迟到这两个烂大街的主题",
        "content_pool": [],
        "use_prompt": True,
    },
    "reply_comments": {"enabled": True, "interval": 1200, "max_per_run": 2},
    "autolike": {"enabled": True, "interval": 5400, "max_per_run": 3, "llm_filter": False},
    "random_comment": {
        "enabled": True,
        "interval": 3000,
        "max_per_run": 1,
        "skip_if_comments_over": 25,
        "max_age_hours": 48,
    },
}

GUARD_TEMPLATE = {
    "quiet_hours": [0, 7],
    "jitter_seconds": [5, 15],
    "daily_limits": {"post": 2, "comment": 4, "reply": 6, "like": 15},
    "hourly_limits": {"post": 1, "comment": 2, "reply": 3, "like": 6},
}


def rand_username(prefix: str) -> str:
    # 用户名只允许字母数字，用日期数字结尾更像真人随手起的
    tail = "".join(random.choice(string.digits) for _ in range(4))
    return f"{prefix}{tail}"


def rand_password(n: int = 18) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def tokens_from(reg: dict) -> dict:
    return {
        "access_token": reg["accessToken"],
        "refresh_token": reg["refreshToken"],
        "access_exp": jwt_payload(reg["accessToken"])["exp"],
        "refresh_exp": jwt_payload(reg["refreshToken"])["exp"],
        "updated_at": int(time.time()),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    cfg = load(str(CONFIG_PATH))
    cfg_raw = load(str(CONFIG_PATH), with_defaults=False)
    accounts = cfg_raw.setdefault("accounts", {})

    # 主号绝不能被 daemon 自动操作
    accounts.setdefault("main", {})["enabled"] = False

    # 用现有可用 token 当「注册通道」
    boot = None
    for cand in ("tokens_bot.json", "tokens.json"):
        p = BASE / cand
        if p.exists():
            boot = Client(cfg["app_id"], cfg["school_id"], str(p))
            break
    if boot is None:
        sys.exit("找不到任何可用 token，无法调注册接口")
    boot.ensure_token()

    for key, label, pkey, prefix, times, profile in SPECS:
        if key in accounts and accounts[key].get("user_id"):
            acc = accounts[key]
            acc["profile"] = acc.get("profile") or profile
            # prompt 以 persona 模块为准：里面已经没有写死的字数限制
            acc["prompts"] = persona.default_prompts(pkey)
            print(f"[跳过注册] {key} 已存在：{acc.get('user_id')}（已同步 profile/prompt）")
            continue

        username = rand_username(prefix)
        password = rand_password()
        print(f"[注册] {label}  username={username}")
        if args.dry:
            continue

        reg = None
        for attempt in range(5):
            try:
                reg = ops.register(boot, username, password)
                break
            except Exception as e:
                print(f"  重试 {attempt+1}: {e}")
                username = rand_username(prefix)
                time.sleep(2)
        if not reg:
            print(f"  [失败] {key} 注册不成功，跳过")
            continue

        uid = reg["id"]
        (BASE / f"tokens_{key}.json").write_text(
            json.dumps(tokens_from(reg), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (BASE / f"account_{key}.json").write_text(
            json.dumps({"username": username, "password": password,
                        "user_id": uid, "persona": pkey},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        acc = {
            "label": label,
            "tokens": f"tokens_{key}.json",
            "user_id": uid,
            "username": username,
            "persona": pkey,
            "kind": "password",
            "enabled": True,
            "profile": profile,
            "prompts": persona.default_prompts(pkey),
            "tasks": {**json.loads(json.dumps(TASK_TEMPLATE)),
                      "scheduled_post": {**json.loads(json.dumps(TASK_TEMPLATE["scheduled_post"])),
                                         "times": times}},
            "guard": json.loads(json.dumps(GUARD_TEMPLATE)),
        }
        accounts[key] = acc
        save(str(CONFIG_PATH), cfg_raw)
        print(f"  [成功] user_id={uid}")

        # 用新号自己的 token 验证一次
        try:
            c = Client(cfg["app_id"], cfg["school_id"], str(BASE / f"tokens_{key}.json"),
                       account_path=str(BASE / f"account_{key}.json"))
            v = ops.viewer(c)
            print(f"  验证：viewer={v.get('user', {}).get('id')} "
                  f"昵称={(v.get('user', {}).get('profile') or {}).get('nickname') or '(空)'}")
        except Exception as e:
            print(f"  [警告] viewer 验证失败：{e}")

        time.sleep(3)

    save(str(CONFIG_PATH), cfg_raw)
    print("\n完成，当前账号：")
    for k, v in accounts.items():
        print(f"  {k:6s} enabled={str(v.get('enabled')):5s} {v.get('label')}")


if __name__ == "__main__":
    main()
