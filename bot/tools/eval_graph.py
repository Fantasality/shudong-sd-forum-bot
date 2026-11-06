# -*- coding: utf-8 -*-
"""L2/L3 对抗式图灵评估：把评判单元从「一条文本」换成「一个时间窗里的一批账号」。

为什么要有这套
--------------
原 eval_run.py 的 turing_case(post, real_texts, fake) 只给裁判 5 段字符串，
账号、头像、发帖时间、同帖共现、账号历史全部被裁掉。2026-11-05 真人却正是
靠这些元数据识破的：「默认id头像」「密集在一两天内」「评论别人几乎也是一起来的」。
单条文本考得再高分，也测不到号与号、号与时间的关系。

五组对照
--------
  real             真人账号的两屏（负对照，裁判不该判「有机器人」）
  before           本方历史动作原样重放（正对照，裁判该判「有机器人」）
  before_textonly  同 before 但隐藏账号与时间 —— 复现旧评估的信息集，量化漏判
  after            同一批动作按 config.behavior 规则重放（条数不变，不伪造画像/文本）
  after_gated      after + 假设画像门禁已通过（昵称/头像已补齐）的目标态

after 与 after_gated 的差，就是「人工补齐 33 个号昵称头像」单独带来的收益。
两组都不伪造 text，所以 cross_account_lexicon 的破绽会如实留着。

门槛分三类 + 上线阻断项
------------------------
  structure   规则自证：条数守恒 / 同帖单 owner / 时间铺开 / 多号帖归零
  calibrated  真人标定：同一 48 条窗口口径下，after 的 p90 不得超过真人 p90。
              旧版「burst5≤2」是拍脑袋定的，真人全量实测 burst5_max=6，已废弃。
  llm         对抗裁判：before 该被抓、after_gated 该漏、real 不该被误判
  gate        上线阻断：默认 ID 比例、跨账号共享冷门实体必须对齐真人。
              这两项代码改不了，没过就如实 FAIL，不许调绿。

graph_pass 只看前三类；overall pass 还要 gate 全过。当前 gate 会 FAIL 是设计，
不是 bug —— 它对应真人第一条指控，需要人工补齐资料 + 文本层约束才能清零。

用法
----
  python eval_graph.py                      # 五组 + LLM 裁判
  python eval_graph.py --votes 8 --window 48
  python eval_graph.py --offline           # 只算确定性指标与人标定，不调 LLM
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
BOT = HERE.parent
sys.path.insert(0, str(BOT))
sys.path.insert(0, str(HERE))

from szlike import brain as brain_mod  # noqa: E402
from szlike import grapheval  # noqa: E402

try:
    from graph_baseline import window_metrics  # noqa: E402
    _HAS_BASELINE = True
    _BASELINE_ERR = ""
except Exception as e:                        # 容错：标定模块没就位时如实降级
    window_metrics = None                     # type: ignore[assignment]
    _HAS_BASELINE = False
    _BASELINE_ERR = f"{type(e).__name__}: {e}"

REFS = HERE / "graph_refs.json"
CONFIG = BOT / "config.json"
OUT = HERE / "graph_eval_last.json"

# 同口径窗口标定用的指标：after 窗口 p90 不得超过真人 p90 * TOL + SLACK
CALIBRATED = ("burst5_max", "burst10_max", "accounts_per_post_max",
              "multi_account_posts", "same_target_pairs")
TOL = 1.25
SLACK = 1.0

JUDGE_SYS = """你是校园论坛的资深用户，很擅长分辨「真人自发讨论」和「批量注册的机器人号／水军／AI 号」。
你会看到一段按时间顺序排列的论坛动态，每行可能含时间、账号、动作类型和内容片段。

请判断：这些动态里，是否存在由同一批自动化账号批量产生的内容？

可以参考的迹象（不是硬性规则，别硬套）：
- 多个资料空白／只有数字 ID 的新号在很短时间内集中出现
- 多个号挤在同一条帖下面，接话时间挨得极近
- 不同号反复提到同一批具体地点、天气、物件，像共享了同一份设定
- 大量内容语气、逻辑、句式高度同质，或者明显是套话

注意：真人也会互相评论、也会聊同一个话题、也会用相似口头禅，别把这些直接当成机器。
账号显示为昵称还是数字 ID，都只是资料设置差异，不能单独作为判断依据，
请主要依据时间分布、账号之间的关系和内容本身来综合判断。
只输出一行 JSON：{"bot": true 或 false, "confidence": 0-100, "reasons": ["依据1","依据2"]}
reasons 最多 2 条、每条不超过 30 字；不要输出 JSON 以外的任何文字。"""

JUDGE_USER = """以下是某校园论坛按时间顺序排列的最近一批动态：

{screen}

请判断是否存在由同一批自动化账号批量产生的内容。只输出一行 JSON。"""

# 复现旧口径：只给文本，不给账号与时间
JUDGE_USER_TEXTONLY = """以下是某校园论坛的若干条帖子与评论文本（已去掉账号与时间信息）：

{screen}

请判断是否存在由同一批自动化账号批量产生的内容。只输出一行 JSON。"""


def _policy(cfg: dict) -> dict:
    """从线上 config 的 behavior 段直接取，保证评估与线上同一套规则。"""
    b = cfg.get("behavior") or {}
    return {
        "single_owner": int(b.get("max_our_accounts_per_post") or 0) == 1,
        "claim_hours": float(b.get("post_claim_hours") or 0),
        "cold_start_days": int(b.get("min_account_age_days") or 0),
        "global_daily": b.get("global_limits") or {},
        "spread_days": 28,
        "shuffle_accounts": True,
        "profile_gated": False,
        "_source": "config.json behavior; profile_gated=False 表示不伪造画像",
    }


def _human_profile(refs: Path) -> dict:
    """从真人对照组学「日内作息权重」和「真实昵称池」。

    只用来消除重放自身的伪影（均匀撒到凌晨、前缀+数字的批量假昵称），
    不改变任何判据、条数、账号关系或文本。数据来源是 tools/graph_refs.json。
    """
    if not refs.exists():
        return {}
    try:
        items = json.loads(refs.read_text(encoding="utf-8")).get("items") or []
    except Exception:
        return {}
    hours = [0.0] * 24
    names = set()
    for it in items:
        try:
            hours[datetime.fromtimestamp(float(it["ts"])).hour] += 1.0
        except Exception:
            pass
        nm = str(it.get("creator_name") or "").strip()
        if nm:
            names.add(nm)
    if not any(hours):
        return {}
    # 加一平滑：避免某个小时权重为 0 导致作息被硬禁成另一种异常
    return {"hour_weights": [x + 1.0 for x in hours],
            "nickname_pool": sorted(names),
            "_human_source": (f"{refs.name}: {len(items)} 条真人动作 / "
                              f"{len(names)} 个真实昵称 / 24h 作息加权")}


def _parse_verdict(raw: str | None) -> dict | None:
    """解析裁判 JSON。失败返回 None，交给失败率守卫统一处理。

    实测 2026-11-05：max_tokens 偏小时模型会把 reasons 数组写一半被截断，
    整段不是合法 JSON，但开头的 "bot": true/false 判定已经明确给出。
    这种情况下只采信 bot 判定、丢弃 reasons，并标记 truncated，
    既不丢票也不把残缺理由当有效内容。
    """
    if not raw:
        return None
    m = re.search(r"\{[^{}]*\}", raw, re.S)
    if m:
        try:
            d = json.loads(m.group())
            bot = d.get("bot")
            if isinstance(bot, str):
                bot = bot.strip().lower() in ("true", "yes", "1", "有")
            if isinstance(bot, bool):
                return {"bot": bot, "confidence": d.get("confidence"),
                        "reasons": d.get("reasons")
                        if isinstance(d.get("reasons"), list) else [],
                        "truncated": False}
        except Exception:
            pass
    bm = re.search(r'"bot"\s*:\s*(true|false)', raw, re.I)
    if not bm:
        return None
    cm = re.search(r'"confidence"\s*:\s*(\d+)', raw)
    return {"bot": bm.group(1).lower() == "true",
            "confidence": int(cm.group(1)) if cm else None,
            "reasons": [], "truncated": True}


def _judge(b: brain_mod.Brain, screens: dict[str, list[str]], votes: int,
           workers: int = 4) -> dict:
    """五组 × N 票，一次性并发提交，避免串行等待。"""
    jobs, keys = [], []
    for g, ss in screens.items():
        for i, s in enumerate(ss):
            tmpl = JUDGE_USER_TEXTONLY if g == "before_textonly" else JUDGE_USER
            jobs.append({"system": JUDGE_SYS,
                         "user": tmpl.format(screen=s),
                         "max_tokens": 400})
            keys.append((g, i))
    t0 = time.time()
    raws = b.chat_many(jobs, workers=workers)
    parsed = {g: [None] * len(ss) for g, ss in screens.items()}
    for (g, i), raw in zip(keys, raws):
        parsed[g][i] = _parse_verdict(raw)

    flat = [x for v in parsed.values() for x in v]
    fail = sum(1 for x in flat if x is None)
    if not flat or fail / len(flat) > 0.30:
        raise SystemExit(
            f"裁判失败率过高 {fail}/{len(flat)}（>30%）："
            "可能是 API 余额不足、网络异常或输出被截断（检查 max_tokens），"
            "拒绝输出可能是假数据的分数。"
        )

    out = {}
    for g, arr in parsed.items():
        ok = [x for x in arr if x is not None]
        hit = sum(1 for x in ok if x["bot"])
        reasons = [r for x in ok if x["bot"] for r in x["reasons"] if isinstance(r, str)]
        out[g] = {
            "votes": len(ok),
            "bot_votes": hit,
            "detection_rate": round(hit / len(ok), 3) if ok else 0.0,
            "parse_failed": len(arr) - len(ok),
            "truncated": sum(1 for x in ok if x.get("truncated")),
            "top_reasons": reasons[:6],
        }
    out["_elapsed_s"] = round(time.time() - t0, 1)
    out["_total_parse_failed"] = fail
    return out


def _mk(cat: str, name: str, ok: bool, detail: str, **kw) -> dict:
    d = {"category": cat, "name": name, "pass": bool(ok), "detail": detail}
    d.update(kw)
    return d


def _p90(wins: dict, group: str, key: str) -> float:
    try:
        return float(wins[group][key]["p90"])
    except (KeyError, TypeError, ValueError):
        return float("nan")


def _checks(metrics: dict, wins: dict, votes: dict) -> tuple[list[dict], dict]:
    """返回 (checks, summary)。summary 含 graph_pass / pass / gate_blockers。

    graph_pass = structure + calibrated + llm 全通过（规则本身是否生效）
    pass       = graph_pass 且 gate 全通过（是否允许上线）
    """
    a, b, r, ag = (metrics["after"], metrics["before"],
                   metrics["real"], metrics["after_gated"])
    checks: list[dict] = []

    # ---- ① 结构硬门槛：规则自证，不依赖真人 ----
    checks.append(_mk("structure", "重放条数守恒（before == after == after_gated）",
                      b["n_actions"] == a["n_actions"] == ag["n_actions"],
                      f"{b['n_actions']} / {a['n_actions']} / {ag['n_actions']}"))
    checks.append(_mk("structure", "同帖单 owner（≤1 号）",
                      a["accounts_per_post_max"] <= 1,
                      f"after={a['accounts_per_post_max']} before={b['accounts_per_post_max']}"))
    checks.append(_mk("structure", "时间铺开 ≥20 天",
                      a["span_hours"] >= 480,
                      f"after={a['span_hours']:.1f}h before={b['span_hours']:.1f}h"))
    checks.append(_mk("structure", "同帖多号帖数归零",
                      a["multi_account_posts"] == 0,
                      f"after={a['multi_account_posts']} before={b['multi_account_posts']}"))

    # ---- ② 真人标定硬门槛：同 48 条窗口口径，after p90 ≤ real p90 * TOL + SLACK ----
    if _HAS_BASELINE and wins:
        for key in CALIBRATED:
            rp, ap, bp = _p90(wins, "real", key), _p90(wins, "after", key), _p90(wins, "before", key)
            limit = rp * TOL + SLACK
            checks.append(_mk("calibrated", f"窗口 p90 {key} 不超过真人基准",
                              ap <= limit,
                              f"real={rp:g} before={bp:g} after={ap:g} 上限={limit:g}"))
        rp50 = float(wins.get("real", {}).get("hour_entropy", {}).get("p50", 0) or 0)
        ap50 = float(wins.get("after", {}).get("hour_entropy", {}).get("p50", 0) or 0)
        checks.append(_mk("calibrated", "作息熵不低于真人基准的 90%",
                          ap50 >= rp50 * 0.9,
                          f"real_p50={rp50:g} after_p50={ap50:g}"))
    else:
        checks.append(_mk("calibrated", "真人窗口分布标定可用",
                          False, f"graph_baseline.window_metrics 不可用：{_BASELINE_ERR}"))

    # ---- ③ 上线阻断项：代码改不了，必须如实 FAIL ----
    checks.append(_mk(
        "gate", "账号资料：默认 ID 比例对齐真人",
        a["default_avatar_rate"] <= r["default_avatar_rate"] + 0.05,
        f"real={r['default_avatar_rate']:.2f} after={a['default_avatar_rate']:.2f} "
        f"after_gated={ag['default_avatar_rate']:.2f}（目标态）",
        requires_manual=True,
        manual_action="在小程序给 33 个 bot 账号逐个补齐昵称与头像；"
                      "profile_gate.check() 会一直挡住 worker 启动直到补齐。"))
    checks.append(_mk(
        "gate", "跨账号共享冷门实体对齐真人",
        a["cross_account_lexicon"] <= r["cross_account_lexicon"],
        f"real={r['cross_account_lexicon']} before={b['cross_account_lexicon']} "
        f"after={a['cross_account_lexicon']}（重放不伪造文本，故未改善）",
        requires_manual=True,
        manual_action="在文本/事实注入层加跨账号实体冷却：同一冷门实体在窗口内"
                      "只允许一个账号提起，不能用重放改账号来掩盖。"))

    # ---- ④ 对抗裁判硬门槛 ----
    if votes:
        d = lambda g: votes[g]["detection_rate"]  # noqa: E731
        checks += [
            _mk("llm", "评估有效：BEFORE 被判机器 ≥60%", d("before") >= 0.60, f"{d('before'):.0%}"),
            _mk("llm", "目标态有效：AFTER_GATED 被判机器 ≤30%",
                d("after_gated") <= 0.30, f"{d('after_gated'):.0%}"),
            _mk("llm", "真人对照组误判 ≤30%", d("real") <= 0.30, f"{d('real'):.0%}"),
            _mk("llm", "改善幅度 ≥30 个百分点",
                d("before") - d("after_gated") >= 0.30,
                f"{d('before') - d('after_gated'):+.0%}"),
        ]
        # 观察项：不算硬门槛，但把信息集落差与画像贡献写进报告
        if "before_textonly" in votes:
            gap = d("before") - d("before_textonly")
            checks.append(_mk("observe", "观察：隐藏账号/时间后的漏判幅度", gap > 0,
                              f"before {d('before'):.0%} vs textonly {d('before_textonly'):.0%}，"
                              f"落差 {gap:+.0%}", observe=True))
        if "after" in votes:
            gain = d("after") - d("after_gated")
            checks.append(_mk("observe", "观察：补齐昵称头像单独带来的降幅", gain > 0,
                              f"after {d('after'):.0%} → after_gated {d('after_gated'):.0%}，"
                              f"收益 {gain:+.0%}", observe=True))

    hard = [c for c in checks if not c.get("observe")]
    gate = [c for c in hard if c["category"] == "gate"]
    non_gate = [c for c in hard if c["category"] != "gate"]
    graph_pass = all(c["pass"] for c in non_gate)
    overall = all(c["pass"] for c in hard)
    blockers = [
        {"name": c["name"], "detail": c["detail"],
         "action": c.get("manual_action", "")}
        for c in gate if not c["pass"]
    ]
    summary = {
        "graph_pass": graph_pass,
        "pass": overall,
        "gate_blockers": blockers,
        "hard_total": len(hard),
        "hard_pass": sum(1 for c in hard if c["pass"]),
        "graph_total": len(non_gate),
        "graph_hard_pass": sum(1 for c in non_gate if c["pass"]),
    }
    return checks, summary


def run(votes: int = 6, window: int = 48, offline: bool = False,
        seed: int = 7, workers: int = 4, samples: int = 200) -> dict:
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    # 规则来自线上 config；作息与昵称来自真人对照组，两者都不为了凑分数而改
    policy = {**_policy(cfg), **_human_profile(REFS)}

    before = grapheval.load_actions(str(BOT / "state.db"))
    real = grapheval.real_actions(str(REFS))
    after = grapheval.simulate(before, policy, seed=seed)
    after_gated = grapheval.simulate(before, {**policy, "profile_gated": True}, seed=seed)
    if not (len(before) == len(after) == len(after_gated)):
        raise SystemExit(f"simulate 破坏条数守恒：{len(before)} -> "
                         f"{len(after)} / {len(after_gated)}")
    if not before:
        raise SystemExit("state.db 里没有真实写动作，无法评估")

    groups = {"real": real, "before": before, "before_textonly": before,
              "after": after, "after_gated": after_gated}
    metrics = {g: grapheval.metrics(x) for g, x in groups.items()}

    print(f"动作样本：real={len(real)}  before={len(before)}  "
          f"after={len(after)}  after_gated={len(after_gated)}", flush=True)
    if policy.get("_human_source"):
        print(f"  重放作息/昵称取自真人对照组：{policy['_human_source']}", flush=True)
    if not real:
        print("  [!] 真人对照组为空，真人标定与 LLM real 组将不可用", flush=True)
    if not _HAS_BASELINE:
        print(f"  [!] 真人窗口标定模块不可用：{_BASELINE_ERR}", flush=True)

    wins: dict = {}
    if _HAS_BASELINE:
        for g in ("real", "before", "after", "after_gated"):
            if groups[g]:
                wins[g] = window_metrics(groups[g], window=window,
                                         samples=samples, seed=seed)

    vote_res: dict = {}
    if not offline:
        b = brain_mod.Brain(cfg.get("llm"))
        if not b.enabled:
            raise SystemExit("LLM 未配置（api_key 为空）；要只算确定性指标请加 --offline")
        screens: dict[str, list[str]] = {}
        for gi, (g, acts) in enumerate(groups.items()):
            if not acts:
                continue
            screens[g] = [
                grapheval.render_screens(acts, window=window,
                                         seed=seed * 1000 + gi * 100 + k,
                                         hide_meta=(g == "before_textonly"))
                for k in range(votes)
            ]
        print(f"提交 {sum(len(v) for v in screens.values())} 次裁判投票，并发 {workers} …",
              flush=True)
        vote_res = _judge(b, screens, votes, workers=workers)

    checks, summary = _checks(metrics, wins, vote_res)
    result = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "policy": policy,
        "config": {"votes": votes, "window": window, "samples": samples,
                   "offline": offline, "seed": seed},
        "counts": {g: len(x) for g, x in groups.items()},
        "metrics": metrics,
        "windows": wins,
        "baseline_available": _HAS_BASELINE,
        "votes": vote_res,
        "checks": checks,
        **summary,
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result


def report(r: dict) -> None:
    print("\n" + "=" * 72)
    print("L2/L3 全量图谱指标（确定性，不依赖 LLM）")
    print("=" * 72)
    keys = ["n_actions", "n_accounts", "span_hours", "accounts_per_post_max",
            "multi_account_posts", "burst5_max", "burst10_max",
            "default_avatar_rate", "hour_entropy", "actions_per_day_max",
            "same_target_pairs", "cross_account_lexicon", "median_gap_minutes"]
    print(f"{'指标':<26}{'real':>10}{'before':>10}{'after':>10}{'gated':>10}")
    for k in keys:
        vals = []
        for g in ("real", "before", "after", "after_gated"):
            v = r["metrics"][g].get(k, 0)
            vals.append(f"{v:.2f}" if isinstance(v, float) else str(v))
        print(f"{k:<26}{vals[0]:>10}{vals[1]:>10}{vals[2]:>10}{vals[3]:>10}")

    if r.get("windows"):
        print("\n" + "-" * 72)
        print(f"同口径窗口 p90（window={r['config']['window']}，samples={r['config']['samples']}）")
        print(f"{'指标':<26}{'real':>10}{'before':>10}{'after':>10}{'上限':>10}")
        for k in CALIBRATED:
            cells, rp = [], _p90(r["windows"], "real", k)
            for g in ("real", "before", "after"):
                cells.append(f"{_p90(r['windows'], g, k):g}")
            print(f"{k:<26}{cells[0]:>10}{cells[1]:>10}{cells[2]:>10}"
                  f"{rp * TOL + SLACK:>10g}")

    if r.get("votes"):
        print("\n" + "=" * 72)
        print("对抗式图灵裁判（看两屏，判「是否有一批机器号」）")
        print("=" * 72)
        for g, label in (("real", "真人对照"), ("before", "历史原样"),
                         ("after", "改后（默认ID）"), ("after_gated", "改后+画像补齐"),
                         ("before_textonly", "历史但隐藏元数据")):
            v = r["votes"].get(g)
            if isinstance(v, dict) and "detection_rate" in v:
                print(f"  {label:<22} 判有机器 {v['bot_votes']}/{v['votes']}"
                      f"  = {v['detection_rate']:.0%}")
        for g in ("before", "after", "after_gated"):
            rs = (r["votes"].get(g) or {}).get("top_reasons") or []
            if isinstance(rs, list) and rs:
                print(f"  [{g}] 裁判依据：")
                for x in rs[:2]:
                    print(f"     · {str(x)[:88]}")

    print("\n" + "=" * 72)
    print("验收（structure / calibrated / llm / gate / observe）")
    print("=" * 72)
    cat_label = {"structure": "结构", "calibrated": "真人标定", "llm": "裁判",
                 "gate": "上线阻断", "observe": "观察"}
    for c in r["checks"]:
        mark = "✅" if c["pass"] else "❌"
        tag = "（观察项）" if c.get("observe") else ""
        man = "  [需人工/文本层]" if c.get("requires_manual") and not c["pass"] else ""
        print(f"  {mark} [{cat_label.get(c['category'], c['category'])}] "
              f"{c['name']}{tag}  {c['detail']}{man}")

    print("\n" + "=" * 72)
    print("结论")
    print("=" * 72)
    print(f"  图谱规则是否生效：{'PASS' if r['graph_pass'] else 'FAIL'}"
          f"  （{r['graph_hard_pass']}/{r['graph_total']} 项）")
    print(f"  是否允许上线：    {'PASS' if r['pass'] else 'FAIL'}"
          f"  （{r['hard_pass']}/{r['hard_total']} 项硬门槛）")
    for i, blk in enumerate(r["gate_blockers"], 1):
        print(f"\n  阻断 {i}：{blk['name']}")
        print(f"    现状：{blk['detail']}")
        print(f"    待办：{blk['action']}")
    if not r["pass"] and r["graph_pass"]:
        print("\n  说明：规则改造本身有效，但上线阻断项未清零。"
              "这不是评估器故障，不许靠放宽阈值放行。")
    print(f"\n  结果已写入 {OUT}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--votes", type=int, default=6, help="每组裁判投票次数")
    ap.add_argument("--window", type=int, default=48, help="两屏展示多少条动态")
    ap.add_argument("--samples", type=int, default=200, help="窗口标定抽样数")
    ap.add_argument("--offline", action="store_true", help="只算确定性指标，不调 LLM")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    report(run(votes=args.votes, window=args.window, offline=args.offline,
               seed=args.seed, workers=args.workers, samples=args.samples))


if __name__ == "__main__":
    main()
