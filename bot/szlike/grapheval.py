# -*- coding: utf-8 -*-
"""账号图谱确定性评估库（L2 账号时间序列 / L3 群体协同）。

原评估 tools/eval_run.py 只判单条文本，测不到「一批机器人号」这种群体特征。
本模块把 state.db 里的真实写动作还原成 Action 列表，提供：
  load_actions    读库
  simulate        按 policy 重演（动作总条数守恒）
  metrics         确定性图谱指标
  render_screens  渲染成给 LLM 裁判看的「论坛最近两屏」
  real_actions    真人对照片段 -> Action
零第三方依赖，只用标准库 sqlite3/math/random/datetime/collections/json/os/zlib。
"""
from __future__ import annotations

import json
import math
import os
import random
import sqlite3
import zlib
from collections import Counter, defaultdict
from datetime import datetime

Action = dict
# Action 字段: ts:float, acc:str, kind:str("post"|"comment"|"reply"|"like"),
#              target:str, text:str, avatar:str, profile_set:bool, age_days:float

_WRITE_KINDS = ("post", "comment", "reply", "like")
_LEXICON = ("炳麟", "毛毛雨", "轨道实验室", "方塔食堂",
            "钟楼草坪", "二期食堂", "阳澄湖操场")
_METRIC_KEYS = (
    "n_actions", "n_accounts", "span_hours", "accounts_per_post_max",
    "multi_account_posts", "burst5_max", "burst10_max",
    "default_avatar_rate", "hour_entropy", "actions_per_day_max",
    "same_target_pairs", "cross_account_lexicon", "median_gap_minutes",
)
_DAY = 86400.0

# 画像补齐用的小昵称池（纯中文昵称，不含可被字符串匹配分辨组别的字面量）。
_NICKNAME_POOL = ("小林", "阿哲", "小满", "青禾", "大山", "阿澈",
                  "小鹿", "南风", "木子", "阿岚", "小舟", "云舒")


def _crc(s: str) -> int:
    """账号名 -> 稳定的无符号 crc32。"""
    return zlib.crc32(s.encode("utf-8"))


def _default_avatar(acc: str) -> str:
    """平台默认 ID 的真实形态：由账号名稳定派生的一串数字。"""
    return f"用户{_crc(acc):08d}"


def _gated_avatar(acc: str, pool: list | tuple | None = None) -> str:
    """画像补齐后的昵称。

    传入真人昵称池时直接从池里按 crc32 取真实昵称（不加数字后缀）——
    「固定前缀+两位数字」本身就是真人不会有的批量命名模式，会被裁判当成新破绽，
    那是评估器自己造的伪影，不是线上状态。池为空才回退到内置小昵称池。
    """
    c = _crc(acc)
    if pool:
        return str(pool[c % len(pool)])
    return f"{_NICKNAME_POOL[c % len(_NICKNAME_POOL)]}{c % 100:02d}"


def _draw_seconds(rng: random.Random, hour_weights) -> float:
    """按真人 24 小时作息权重采样一天内的秒数；无权重时保持旧的均匀铺开。

    均匀铺开会往凌晨 2-7 点撒大量动作，而真人那几个小时几乎不发帖，
    裁判会据此判「时间分布异常」。作息只能从真人数据学，不能自己猜。
    """
    if not hour_weights or len(hour_weights) != 24:
        return rng.uniform(0.0, 0.98 * _DAY)
    w = [max(0.0, float(x)) for x in hour_weights]
    if sum(w) <= 0:
        return rng.uniform(0.0, 0.98 * _DAY)
    h = rng.choices(range(24), weights=w, k=1)[0]
    return h * 3600.0 + rng.uniform(0.0, 3600.0)


def _profile_set(a: dict) -> bool:
    """资料是否已设置；字段缺失时回退旧的字符串判断（历史默认头像标记视为未设置）。"""
    if "profile_set" in a:
        return bool(a["profile_set"])
    return a.get("avatar") != "默认ID"


def load_actions(db_path: str = "state.db") -> list[dict]:
    """从 state.db 的 actions 表读真实写动作，按 ts 升序返回。

    actions 表: kind TEXT, target TEXT, ts REAL, meta TEXT, PRIMARY KEY(kind,target)。
    kind 形如 'warm:comment' / 'bot:post' / 'wit:dry:comment' / 'dsh_pa:pick:angle'。
    只保留末段为 post/comment/reply/like 且不含 ':dry:' 的真实写动作
    （':dry:' 是空跑占位；pick:angle、pick:topic、post_slot 都不是写动作）。
    账号名取 kind 第一个冒号前的部分；text 从 meta 取（可能是空串）。
    target 对 comment/reply 是帖 id 或 'reply_comment:xxx' 这类 key。
    avatar 为平台默认 ID 的真实形态（一串数字，由账号名 crc32 稳定派生）；
    profile_set=False（实测 33/33 个号都没设昵称头像）；
    age_days 统一 2.0（历史数据都是 3 天内）。
    """
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT kind, target, ts, meta FROM actions ORDER BY ts"
        ).fetchall()
    finally:
        conn.close()

    out = []
    for kind, target, ts, meta in rows:
        kind = kind or ""
        if ":dry:" in kind:
            continue
        k = kind.rsplit(":", 1)[-1]
        if k not in _WRITE_KINDS:
            continue
        acc = kind.split(":", 1)[0]
        out.append({
            "ts": float(ts),
            "acc": acc,
            "kind": k,
            "target": str(target or ""),
            "text": str(meta or ""),
            "avatar": _default_avatar(acc),
            "profile_set": False,
            "age_days": 2.0,
        })
    out.sort(key=lambda a: a["ts"])
    return out


def simulate(actions: list[dict], policy: dict, seed: int = 7) -> list[dict]:
    """把历史动作按 policy 重新分配时间与账号，动作总条数保持不变。

    before 型 policy（single_owner=False, cold_start_days=0, spread_days=1,
    shuffle_accounts=False）直接深拷贝返回，几乎原样。
    after 型 policy 会：把同一批动作均匀铺到 spread_days 天（每天遵守
    global_daily 的 per-kind 上限）、同一 target 只归一个 acc（single_owner）、
    账号身份可打散重分配（shuffle_accounts），并把 age_days 提升到不低于
    cold_start_days（模拟已过冷启动的成熟账号）。

    条数守恒是硬约束：被当日配额挤掉的动作顺延到后面的天，绝不丢弃；若动作
    数超过 spread_days * 每日上限总和，溢出的动作再循环覆盖更早的时段（同一天
    叠加、日内时间错开）。cold_start_days 的「年轻账号动作本应丢弃」在条数守恒
    前提下通过把账号催熟到门槛来体现，而非删除。random.Random(seed) 保证可复现。

    policy["profile_gated"]=True 时，每条动作改用从账号稳定派生的中文昵称并置
    profile_set=True（画像门禁通过后的目标态），用于单独量化「人工补齐昵称头像」
    的收益；默认 False，avatar / profile_set 原样保留，不改变图谱结构。

    policy["hour_weights"]（24 个数）和 policy["nickname_pool"]（真人昵称列表）
    来自 tools/graph_refs.json，用来消除重放自身的两个伪影：均匀撒到凌晨、
    以及「前缀+数字」的批量假昵称。两者都只改变显示名与日内时刻，
    不改动作条数、账号关系、target 或 text，也不降低任何评估判据。
    """
    rng = random.Random(seed)
    n = len(actions)
    ordered = sorted(actions, key=lambda a: a["ts"])

    single_owner = bool(policy.get("single_owner", False))
    claim_hours = float(policy.get("claim_hours", 0) or 0)
    cold = int(policy.get("cold_start_days", 0) or 0)
    spread = max(1, int(policy.get("spread_days", 1) or 1))
    shuffle = bool(policy.get("shuffle_accounts", False))
    profile_gated = bool(policy.get("profile_gated", False))
    caps = {k: int(v) for k, v in (policy.get("global_daily") or {}).items()}
    hour_weights = policy.get("hour_weights")
    nickname_pool = policy.get("nickname_pool") or []

    # 无操作配置：原样（深拷贝，dict(a) 对扁平标量 Action 即为深拷贝）返回
    if spread <= 1 and not single_owner and cold <= 0 and not shuffle:
        if not profile_gated:
            return [dict(a) for a in ordered]
        gated = []
        for a in ordered:
            d = dict(a)
            d["avatar"] = _gated_avatar(d["acc"], nickname_pool)
            d["profile_set"] = True
            gated.append(d)
        return gated
    if n == 0:
        return []

    t0 = ordered[0]["ts"]
    acc_pool = sorted({a["acc"] for a in ordered}) or ["用户"]

    def cap_of(kind):
        """返回某 kind 的每日上限；未配置或 <=0 视为不限（n+1）。"""
        c = caps.get(kind, 0)
        return c if c > 0 else n + 1

    def fresh_acc(a):
        """按 shuffle 决定新账号：打散则随机取，否则沿用原账号。"""
        return rng.choice(acc_pool) if shuffle else a["acc"]

    def profile_fields(a, acc):
        """非门禁：avatar / profile_set 原样保留（缺省回退）；门禁：稳定中文昵称 + 已设置。"""
        if profile_gated:
            return _gated_avatar(acc, nickname_pool), True
        return a.get("avatar", _default_avatar(acc)), _profile_set(a)

    # --- 阶段 1：给每条分配新 ts（铺到 spread 天，遵守每日 per-kind 上限） ---
    used = defaultdict(lambda: defaultdict(int))   # day -> kind -> 已用配额
    overflow = 0
    timed = []                                    # (new_ts, 原动作)
    for i, a in enumerate(ordered):
        kind = a["kind"]
        cap = cap_of(kind)
        ideal = (i * spread) // n if spread > 1 else 0   # 均匀铺满 0..spread-1 天
        day = None
        for step in range(spread):
            d = (ideal + step) % spread
            if used[d][kind] < cap:
                day = d
                break
        if day is None:                           # 所有天该 kind 都满：循环覆盖更早时段
            day = overflow % spread
            overflow += 1
        used[day][kind] += 1
        new_ts = t0 + day * _DAY + _draw_seconds(rng, hour_weights)
        timed.append((new_ts, a))
    timed.sort(key=lambda x: x[0])

    # --- 阶段 2：按新时间顺序分配账号（single_owner 占坑 / shuffle 打散） ---
    owner = {}                                    # target -> (acc, first_ts)
    out = []
    for new_ts, a in timed:
        if single_owner and a["target"] in owner:
            oacc, ots = owner[a["target"]]
            if claim_hours <= 0 or (new_ts - ots) <= claim_hours * 3600.0:
                acc = oacc                        # 占坑未过期（或永久），沿用
            else:
                acc = fresh_acc(a)                # 占坑过期，换新号并刷新占坑
                owner[a["target"]] = (acc, new_ts)
        else:
            acc = fresh_acc(a)
            if single_owner:
                owner[a["target"]] = (acc, new_ts)
        avatar, profile_set = profile_fields(a, acc)
        out.append({
            "ts": new_ts,
            "acc": acc,
            "kind": a["kind"],
            "target": a["target"],
            "text": a.get("text", ""),
            "avatar": avatar,
            "profile_set": profile_set,
            "age_days": max(float(a.get("age_days", 0.0)), float(cold)),
        })
    out.sort(key=lambda a: a["ts"])
    return out


def metrics(actions: list[dict]) -> dict:
    """返回确定性图谱指标（L2 时间序列 / L3 群体协同），键名固定，空输入全 0。

    accounts_per_post_max / multi_account_posts 按 target 下不同 acc 集合统计；
    burst5_max / burst10_max 是任意 5/10 分钟滑动窗口内不同 acc 数最大值；
    same_target_pairs 按每个 target 累加 C(不同acc数, 2)，衡量跨号共现强度；
    cross_account_lexicon 统计被 >=3 个不同 acc 的 text 同时提到的校园冷门实体数；
    hour_entropy 是「一天中 24 小时」分布的信息熵（log2，保留 3 位小数）。
    default_avatar_rate = 1 - mean(profile_set)，语义是「资料未设置比例」；
    profile_set 字段缺失时回退到旧的字符串判断以保持向后兼容。
    """
    if not actions:
        return {k: 0 for k in _METRIC_KEYS}

    n = len(actions)
    ordered = sorted(actions, key=lambda a: a["ts"])
    tss = [a["ts"] for a in ordered]
    accs = [a["acc"] for a in ordered]

    per_target = defaultdict(set)
    for a in ordered:
        per_target[a["target"]].add(a["acc"])
    target_counts = [len(s) for s in per_target.values()]

    span_hours = (tss[-1] - tss[0]) / 3600.0

    def burst(window_sec):
        """任意 window_sec 秒滑动窗口内出现的不同 acc 数最大值。"""
        cnt = defaultdict(int)
        distinct = 0
        j = 0
        best = 0
        for i in range(n):
            while j < n and tss[j] < tss[i] + window_sec:
                if cnt[accs[j]] == 0:
                    distinct += 1
                cnt[accs[j]] += 1
                j += 1
            if distinct > best:
                best = distinct
            cnt[accs[i]] -= 1
            if cnt[accs[i]] == 0:
                distinct -= 1
        return best

    hour_cnt = Counter(datetime.fromtimestamp(t).hour for t in tss)
    entropy = -sum((c / n) * math.log2(c / n) for c in hour_cnt.values())
    day_cnt = Counter(datetime.fromtimestamp(t).date() for t in tss)

    lex_accs = defaultdict(set)
    for a in ordered:
        txt = a.get("text", "") or ""
        for w in _LEXICON:
            if w in txt:
                lex_accs[w].add(a["acc"])

    gaps = sorted((tss[i + 1] - tss[i]) / 60.0 for i in range(n - 1))
    if gaps:
        m = len(gaps)
        median_gap = (gaps[m // 2] if m % 2
                      else (gaps[m // 2 - 1] + gaps[m // 2]) / 2.0)
    else:
        median_gap = 0.0

    return {
        "n_actions": n,
        "n_accounts": len(set(accs)),
        "span_hours": round(span_hours, 2),
        "accounts_per_post_max": max(target_counts) if target_counts else 0,
        "multi_account_posts": sum(1 for c in target_counts if c >= 2),
        "burst5_max": burst(300.0),
        "burst10_max": burst(600.0),
        "default_avatar_rate": round(
            1.0 - sum(_profile_set(a) for a in ordered) / n, 3),
        "hour_entropy": round(entropy, 3),
        "actions_per_day_max": max(day_cnt.values()) if day_cnt else 0,
        "same_target_pairs": sum(c * (c - 1) // 2 for c in target_counts),
        "cross_account_lexicon": sum(1 for w in _LEXICON
                                     if len(lex_accs[w]) >= 3),
        "median_gap_minutes": round(median_gap, 2),
    }


def render_screens(actions: list[dict], window: int = 48, seed: int = 0,
                   hide_meta: bool = False) -> str:
    """把动作渲染成「论坛最近两屏」的纯文本，给 LLM 裁判看。

    按 ts 排序后用 random.Random(seed) 随机取连续 window 条。非对照模式每行带
    [MM-DD HH:MM]、@昵称_账号序号 与动作；展示名直接取 action['avatar']（真实昵称
    或平台默认 ID 数字串），不再使用可分辨组别的魔法字面；序号保留以避免重名混淆。
    同一 target 保留后 6 位并标注「(同一条帖)」，让裁判看出是同一条帖。
    hide_meta=True 时只留「动作：文本」，复现旧评估（只判单条文本）的口径。
    """
    rng = random.Random(seed)
    ordered = sorted(actions, key=lambda a: a["ts"])
    n = len(ordered)
    w = max(1, int(window))
    start = rng.randint(0, max(0, n - w)) if n > w else 0
    chunk = ordered[start:start + w]

    acc_map = {acc: i for i, acc in
               enumerate(sorted({a["acc"] for a in ordered}))}
    lines = [f"以下是某校园论坛按时间顺序排列的最近 {window} 条动态。"]
    seen = set()
    verbs = {"comment": "评论了", "reply": "回复了", "like": "赞了"}
    plain = {"post": "发帖", "comment": "评论", "reply": "回复", "like": "点赞"}
    for a in chunk:
        tail = (a["target"] or "")[-6:] or "------"
        text = a.get("text", "") or ""
        clip = text[:80] + ("…" if len(text) > 80 else "")
        kind = a["kind"]
        if hide_meta:
            lines.append(f"{plain.get(kind, '互动')}：{clip}")
            continue
        stamp = datetime.fromtimestamp(a["ts"]).strftime("%m-%d %H:%M")
        label = f"@{a.get('avatar') or '用户'}_{acc_map.get(a['acc'], 0)}"
        same = tail in seen
        seen.add(tail)
        if kind == "post":
            act = f"发帖 [帖{tail}]"
        else:
            mark = " (同一条帖)" if same else ""
            act = f"{verbs.get(kind, '互动了')} {tail}{mark}"
        lines.append(f"[{stamp}] {label} {act}：{clip}")
    return "\n".join(lines)


def real_actions(refs_path: str) -> list[dict]:
    """把真人对照片段 JSON 转成 Action 列表，按 ts 升序返回；文件不存在返回 []。

    JSON 格式 {'items':[{'creator_id','creator_name','ts','kind','target','text'}]}。
    acc 用 creator_id 后 8 位；avatar 用 creator_name 的真实昵称（strip 后非空才用，
    否则回退 '用户' + cid 后 4 位）；profile_set=True（真人对照组视为资料完整）；
    age_days 填 400.0。
    """
    if not os.path.exists(refs_path):
        return []
    with open(refs_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    out = []
    for it in data.get("items", []) or []:
        cid = str(it.get("creator_id", "") or "")
        name = str(it.get("creator_name", "") or "").strip()
        out.append({
            "ts": float(it.get("ts", 0) or 0),
            "acc": cid[-8:],
            "kind": str(it.get("kind", "post") or "post"),
            "target": str(it.get("target", "") or ""),
            "text": str(it.get("text", "") or ""),
            "avatar": name or ("用户" + cid[-4:]),
            "profile_set": True,
            "age_days": 400.0,
        })
    out.sort(key=lambda a: a["ts"])
    return out


if __name__ == "__main__":
    db = "state.db"
    if not os.path.isfile(db):
        db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "state.db")

    acts = load_actions(db)
    n_acc = len(set(a["acc"] for a in acts))
    print(f"load_actions: {len(acts)} 条写动作，{n_acc} 个账号")

    before_policy = {
        "single_owner": False, "claim_hours": 0, "cold_start_days": 0,
        "global_daily": {"post": 3, "comment": 6, "reply": 8, "like": 30},
        "spread_days": 1, "shuffle_accounts": False,
    }
    after_policy = {
        "single_owner": True, "claim_hours": 0, "cold_start_days": 30,
        "global_daily": {"post": 3, "comment": 6, "reply": 8, "like": 30},
        "spread_days": 30, "shuffle_accounts": True,
    }
    before = simulate(acts, before_policy)
    after = simulate(acts, after_policy)
    after_gated = simulate(acts, {**after_policy, "profile_gated": True})
    mb, ma = metrics(before), metrics(after)

    print("\n指标                       before        after")
    print("-" * 52)
    for k in _METRIC_KEYS:
        print(f"  {k:<24s}{str(mb[k]):>12s}{str(ma[k]):>14s}")

    assert len(before) == len(acts) == len(after), "before/after 条数必须相等"
    assert ma["accounts_per_post_max"] <= 1, "after 同帖账号数必须 <= 1"
    assert ma["span_hours"] >= 24 * 20, "after 时间跨度必须 >= 20 天"
    assert all(v == 0 for v in metrics([]).values()), "metrics 空输入须全 0"
    assert real_actions("__no_such_refs__.json") == [], "缺文件须返回 []"

    # 画像门禁：after 诚实保留默认 ID；after_gated 单独量化「补齐昵称头像」的收益。
    mg = metrics(after_gated)
    mr = metrics(real_actions("tools/graph_refs.json"))
    sa = render_screens(after, window=6)
    sg = render_screens(after_gated, window=6)
    assert len(after_gated) == len(acts), "after_gated 条数必须相等"
    assert ma["default_avatar_rate"] == 1.0, "after 须诚实反映默认 ID 未补齐"
    assert mg["default_avatar_rate"] == 0.0, "after_gated 画像须全部补齐"
    assert mr["default_avatar_rate"] == 0.0, "真人对照资料须完整"
    assert "默认ID" not in sa and "自定义昵称" not in sa, "after 渲染不得含魔法字面"
    assert "默认ID" not in sg and "自定义昵称" not in sg, "after_gated 渲染不得含魔法字面"
    print(f"\n画像门禁：after default_avatar_rate={ma['default_avatar_rate']} "
          f"/ after_gated={mg['default_avatar_rate']} / real={mr['default_avatar_rate']}")
    print(f"cross_account_lexicon: after={ma['cross_account_lexicon']} "
          f"/ after_gated={mg['cross_account_lexicon']}（文本层不受画像改动影响）")
    print("\n断言全部通过：条数相等 / accounts_per_post_max<=1 / span_hours>=480 / 空输入全0 "
          "/ 画像门禁 / 渲染无魔法字面")

    print("\n--- render_screens 样例（前 6 行）---")
    print("\n".join(render_screens(after, window=6, seed=0).splitlines()[:7]))
    print("\n--- render_screens after_gated 样例（前 6 行）---")
    print("\n".join(render_screens(after_gated, window=6, seed=0).splitlines()[:7]))
    print("\n--- render_screens hide_meta 样例（前 3 行）---")
    print("\n".join(render_screens(after, window=3, seed=0, hide_meta=True).splitlines()[:4]))
