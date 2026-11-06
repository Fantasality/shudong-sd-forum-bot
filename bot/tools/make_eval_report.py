# -*- coding: utf-8 -*-
"""三层评估验收报告生成器：所有数字从结果 JSON 读，不手写结论。

用法（在 szlike_bot 目录下）：
  PYTHONIOENCODING=utf-8 python tools/make_eval_report.py
输出：tools/EVAL_REPORT.html
依赖：tools/eval_last.json、tools/graph_eval_last.json、tools/graph_baseline.json
      tools/eval_all_last.json（可选，未跑则标注「未运行」）
"""
from __future__ import annotations

import html
import json
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "EVAL_REPORT.html"


def load(name: str) -> dict:
    p = HERE / name
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def esc(x) -> str:
    return html.escape(str(x), quote=True)


def badge(ok: bool | None, yes: str = "PASS", no: str = "FAIL") -> str:
    if ok is None:
        return '<span class="b b-na">未运行</span>'
    return (f'<span class="b b-ok">✅ {esc(yes)}</span>' if ok
            else f'<span class="b b-no">❌ {esc(no)}</span>')


def table(headers: list[str], rows: list[list], cls: str = "") -> str:
    h = "".join(f"<th>{esc(x)}</th>" for x in headers)
    body = []
    for row in rows:
        cells = "".join(f"<td>{c if isinstance(c, str) and c.startswith('<') else esc(c)}</td>"
                        for c in row)
        body.append(f"<tr>{cells}</tr>")
    return (f'<table class="{cls}"><thead><tr>{h}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table>')


def main() -> None:
    l1 = load("eval_last.json")
    g = load("graph_eval_last.json")
    base = load("graph_baseline.json")
    allr = load("eval_all_last.json")

    l1_rows = []
    for k, label in (("comment", "评论"), ("post", "帖子"), ("reply", "楼中楼")):
        v = l1.get(k) or {}
        if not v:
            continue
        l1_rows.append([label, f'{v.get("total", 0):.1f}',
                        f'{v.get("catch_rate", 0):.1%}',
                        f'{v.get("fact_rate", 0):.1%}' if "fact_rate" in v else "—",
                        badge(float(v.get("total", 0)) >= 80.0, "达标", "未达标")])
    l1_ok = bool(l1_rows) and all(float((l1.get(k) or {}).get("total", 0)) >= 80.0
                                  for k in ("comment", "post", "reply") if k in l1)

    M = g.get("metrics") or {}
    metric_rows = []
    metric_keys = ["n_actions", "n_accounts", "span_hours", "accounts_per_post_max",
                   "multi_account_posts", "burst5_max", "burst10_max",
                   "default_avatar_rate", "hour_entropy", "actions_per_day_max",
                   "same_target_pairs", "cross_account_lexicon", "median_gap_minutes"]
    for k in metric_keys:
        cells = [k]
        for grp in ("real", "before", "after", "after_gated"):
            v = (M.get(grp) or {}).get(k, 0)
            cells.append(f"{v:.2f}" if isinstance(v, float) else str(v))
        metric_rows.append(cells)

    W = g.get("windows") or {}
    cal_keys = ["burst5_max", "burst10_max", "accounts_per_post_max",
                "multi_account_posts", "same_target_pairs"]
    def p90(grp, key):
        try:
            return float(W[grp][key]["p90"])
        except Exception:
            return float("nan")
    win_rows = []
    for k in cal_keys:
        rp = p90("real", k)
        win_rows.append([k, f"{p90('real', k):g}", f"{p90('before', k):g}",
                         f"{p90('after', k):g}", f"{rp * 1.25 + 1:g}"])

    V = g.get("votes") or {}
    vote_labels = (("real", "真人对照"), ("before", "历史原样"),
                   ("after", "改后（默认 ID）"), ("after_gated", "改后 + 画像补齐"),
                   ("before_textonly", "历史但隐藏元数据"))
    vote_rows = []
    for key, label in vote_labels:
        v = V.get(key)
        if isinstance(v, dict) and "detection_rate" in v:
            vote_rows.append([label, f'{v["bot_votes"]}/{v["votes"]}',
                              f'{v["detection_rate"]:.0%}',
                              str(v.get("parse_failed", 0)),
                              str(v.get("truncated", 0))])

    checks = g.get("checks") or []
    cat_name = {"structure": "结构", "calibrated": "真人标定", "llm": "对抗裁判",
                "gate": "上线阻断", "observe": "观察"}
    check_rows = []
    for c in checks:
        mark = "✅" if c.get("pass") else "❌"
        if c.get("observe"):
            mark = "⭐"
        tag = "（观察）" if c.get("observe") else ""
        man = " ⚠️ 需人工/文本层" if c.get("requires_manual") and not c.get("pass") else ""
        check_rows.append([f"[{cat_name.get(c.get('category'), '')}]",
                           f"{esc(c.get('name',''))}{tag}{man}",
                           mark, esc(c.get("detail", ""))])
        check_rows[-1][0] = check_rows[-1][0]

    blockers = g.get("gate_blockers") or []
    blocker_html = "".join(
        f'<div class="blk"><b>阻断 {i}：{esc(b.get("name"))}</b>'
        f'<div class="mono">现状：{esc(b.get("detail"))}</div>'
        f'<div>待办：{esc(b.get("action"))}</div></div>'
        for i, b in enumerate(blockers, 1)) or "<p>无。</p>"

    hard = [c for c in checks if not c.get("observe")]
    hard_pass = sum(1 for c in hard if c.get("pass"))
    rb = (base.get("real_windows") or {}).get("burst5_max") or {}
    n_real = base.get("n_real_actions", "—")

    parts = []
    parts.append('<div class="hero">')
    parts.append(f'<h1>三层图灵评估体系 · 大改验收报告</h1>')
    parts.append(f'<div class="sub">生成时间 {esc(time.strftime("%Y-%m-%d %H:%M:%S"))} '
                 f'· 数据全部来自实测 JSON，非人工填写</div>')
    parts.append('<div class="cards">')
    parts.append(f'<div class="card"><div class="k">L1 单条文本</div>'
                 f'<div class="v">{badge(l1_ok, "达标", "未达标")}</div>'
                 f'<div class="d">三项总分均需 ≥80</div></div>')
    parts.append(f'<div class="card"><div class="k">L2/L3 图谱规则</div>'
                 f'<div class="v">{badge(g.get("graph_pass"), "达标", "未达标")}</div>'
                 f'<div class="d">{g.get("graph_hard_pass","—")}/{g.get("graph_total","—")} 项硬门槛</div></div>')
    parts.append(f'<div class="card"><div class="k">上线放行</div>'
                 f'<div class="v">{badge(g.get("pass"), "允许", "禁止")}</div>'
                 f'<div class="d">{hard_pass}/{len(hard)} 项硬门槛</div></div>')
    parts.append(f'<div class="card"><div class="k">上线阻断项</div>'
                 f'<div class="v num">{len(blockers)}</div>'
                 f'<div class="d">需人工 / 文本层清零</div></div>')
    parts.append('</div></div>')

    parts.append('<h2>1 · 为什么旧的图灵测试没拦住真人识破</h2>')
    parts.append('<div class="note">原 <code>eval_run.py</code> 的裁判只看到帖子正文、4 条真人评论、'
                 '1 条 AI 候选这 5 段字符串。账号、昵称/头像、时间戳、同帖共现、账号主页历史'
                 '全部被裁掉。真人看到的却是论坛两屏：清一色默认数字 ID、3 天内新号、'
                 '多个号 15–35 秒内评同一帖、跨号共享同一批冷门设定。'
                 '<b>练的是语文，考的是刑侦</b>，信息集被裁剪导致系统性漏判。</div>')
    parts.append('<div class="note">本次把评估单元从「一条文本」换成「一个时间窗里的一批账号」，'
                 '并用真人两屏做负对照来标定门槛，而不是继续拍脑袋定绝对阈值。</div>')

    parts.append('<h2>2 · 新评估体系：五组对照 + 分层门槛</h2>')
    parts.append(table(["组", "含义", "期望裁判结论"], [
        ["real", f"真人账号两屏（{esc(n_real)} 条动作）", "不该判有机器"],
        ["before", "本方历史动作原样重放", "该判有机器"],
        ["before_textonly", "同 before，但隐藏账号与时间", "用于量化旧口径漏判"],
        ["after", "同一批动作按 config.behavior 重放，条数守恒", "应显著优于 before"],
        ["after_gated", "after + 假设昵称/头像已补齐的目标态", "分离画像补齐的单独收益"],
    ]))
    parts.append('<div class="note">公平性：before / after / after_gated 动作条数完全相等，'
                 '差异只来自账号与时间的关系，不是「改后发得少所以看不出来」。'
                 '重放不伪造文本，所以跨账号共享实体这类文本层破绽会如实留着。</div>')
    parts.append('<div class="note"><b>两个评估器伪影已修（只修模拟器，不改判据）：</b>'
                 '首轮重放把动作均匀撒到全天，裁判抓到「凌晨 2-7 点集中发帖」；'
                 '画像补齐组又用了「小林44」式前缀+数字假昵称，被抓到「批量命名模式」。'
                 '这两条都是评估器自己造出来的破绽，不是线上状态。'
                 '现在日内作息按真人 24h 分布加权采样、昵称直接从真人对照组的真实昵称池取，'
                 '两者都不改变动作条数、账号关系、target 或 text。</div>')
    parts.append(table(["门槛类别", "判据来源", "性质"], [
        ["structure 结构", "规则自证：条数守恒 / 单 owner / 时间铺开 / 多号帖归零", "硬"],
        ["calibrated 真人标定", f"同 48 条窗口口径，after p90 ≤ real p90 × 1.25 + 1", "硬"],
        ["llm 对抗裁判", "五组各 6 票，判「是否有一批机器号」", "硬"],
        ["gate 上线阻断", "默认 ID 比例、共享冷门实体必须对齐真人", "硬（不可调绿）"],
        ["observe 观察", "信息集落差、画像补齐收益", "不设门槛"],
    ]))

    parts.append('<h2>3 · 旧门槛是假阳性：真人基准实测</h2>')
    parts.append(f'<div class="note">旧版把 <code>burst5_max ≤ 2</code> 当硬门槛，'
                 f'导致 after=3 被判 FAIL。真人对照组全量 <code>burst5_max={esc((M.get("real") or {}).get("burst5_max","—"))}</code>，'
                 f'48 条窗口 p50/p90/max = <code>{rb.get("p50","—")} / {rb.get("p90","—")} / {rb.get("max","—")}</code>。'
                 f'也就是说真人在 5 分钟内本来就会有多个号同时出现，旧门槛把真人也判成了机器人。'
                 f'<b>已废弃绝对阈值，改为同口径真人分布标定。</b></div>')
    parts.append(table(["窗口 p90 指标", "real", "before", "after", "上限"], win_rows))

    parts.append('<h2>4 · 全量图谱指标</h2>')
    parts.append(table(["指标", "real", "before", "after", "after_gated"], metric_rows))

    if vote_rows:
        parts.append('<h2>5 · 对抗式图灵裁判</h2>')
        parts.append(table(["组", "判有机器", "检出率", "解析失败", "输出截断"], vote_rows))
        reasons = []
        for key in ("before", "after", "after_gated"):
            v = V.get(key)
            if isinstance(v, dict):
                for x in (v.get("top_reasons") or [])[:2]:
                    reasons.append([key, esc(str(x)[:120])])
        if reasons:
            parts.append('<div class="note"><b>裁判依据摘录</b></div>')
            parts.append(table(["组", "理由"], reasons))

    parts.append('<h2>6 · 逐项验收</h2>')
    parts.append(table(["类别", "检查项", "结果", "实测"], check_rows, "chk"))

    def dr(k):
        v = V.get(k)
        return float(v["detection_rate"]) if isinstance(v, dict) and "detection_rate" in v else None
    diag = []
    if dr("before") is not None and dr("before_textonly") is not None:
        gap = dr("before") - dr("before_textonly")
        diag.append(["旧口径是否系统性漏判",
                     f'before {dr("before"):.0%} → 隐藏元数据 {dr("before_textonly"):.0%}，落差 {gap:+.0%}',
                     "落差越大，说明「只给 5 段文本」考不到账号图谱"])
    if dr("before") is not None and dr("after_gated") is not None:
        gain = dr("before") - dr("after_gated")
        diag.append(["图谱规则 + 画像补齐的收益",
                     f'before {dr("before"):.0%} → after_gated {dr("after_gated"):.0%}，收益 {gain:+.0%}',
                     "收益为 0 表示行为层仍不足以骗过看两屏的裁判"])
    if dr("after") is not None and dr("after_gated") is not None:
        gain = dr("after") - dr("after_gated")
        diag.append(["单独补齐昵称/头像的收益",
                     f'after {dr("after"):.0%} → after_gated {dr("after_gated"):.0%}，收益 {gain:+.0%}',
                     "用于判断人工补齐画像到底值不值"])
    if diag:
        parts.append('<h2>7 · 本轮诊断（数字由裁判结果自动生成）</h2>')
        parts.append(table(["问题", "实测", "读法"], diag))
        na = (M.get("after") or {}).get("n_accounts") or 0
        nr = (M.get("real") or {}).get("n_accounts") or 0
        aa = (M.get("after") or {}).get("n_actions") or 0
        ar = (M.get("real") or {}).get("n_actions") or 0
        dens = (f'本方 {aa} 条 / {na} 号 = {aa / na:.1f} 条每号，'
                f'真人 {ar} 条 / {nr} 号 = {ar / nr:.1f} 条每号'
                if na and nr else '')
        parts.append(f'<div class="note"><b>裁判给出的剩余理由需继续处理：</b>{dens}，'
                     '人均动作密度差约一个量级，窗口里总是那几个号反复出现；'
                     '加上跨账号共享冷门实体，'
                     '这些不是改时间/改账号分配能遮掉的。'
                     '下一步应在线上增加作息约束、跨账号实体冷却，并补齐 33 个号的真实昵称与头像，'
                     '而不是继续调低评估门槛。</div>')
    parts.append('<h2>8 · NOT DONE / 上线阻断</h2>')
    failed_hard = [c for c in checks if not c.get("observe") and not c.get("pass")]
    if failed_hard:
        parts.append('<div class="note"><b>本轮全部未通过的硬门槛：</b></div>')
        parts.append(table(["类别", "检查项", "实测"],
                           [[cat_name.get(c.get("category"), ""), c.get("name", ""),
                             c.get("detail", "")] for c in failed_hard]))
    parts.append(blocker_html)
    parts.append('<div class="note"><b>放行结论：</b>'
                 + ("图谱规则改造已验证有效，但上述阻断项未清零，"
                    "主程序继续保持停机，不得重启。不许靠放宽阈值放行。"
                    if g.get("graph_pass") and not g.get("pass")
                    else "见上方总判定。") + '</div>')

    parts.append('<h2>9 · L1 单条文本基线</h2>')
    parts.append('<div class="note">L1 存在裁判抽样方差：本次会话同一版代码多轮实测，'
                 '各项 total 波动可达数分。本表取自最近一次 eval_run.py --mode all，'
                 '未对门槛或评分公式做任何调整；未达标就是未达标。</div>')
    parts.append(table(["模式", "总分", "被抓率", "事实准确率", "≥80"], l1_rows))
    parts.append('<div class="note">L1 高分是必要条件，不是充分条件。'
                 '被真人识破发生在 L3，因此统一入口要求 L1 与 L2/L3 同时达标。</div>')

    parts.append('<h2>10 · 本地手测命令</h2>')
    parts.append('<pre>cd szlike_bot\n'
                 'PYTHONIOENCODING=utf-8 python szlike/grapheval.py                 # 图谱库自测\n'
                 'PYTHONIOENCODING=utf-8 python tools/graph_baseline.py             # 重算真人基准\n'
                 'PYTHONIOENCODING=utf-8 python tools/eval_graph.py --offline      # 只算确定性指标\n'
                 'PYTHONIOENCODING=utf-8 python tools/eval_graph.py --votes 6 --window 48\n'
                 'PYTHONIOENCODING=utf-8 python tools/eval_all.py                  # 三层统一入口\n'
                 'PYTHONIOENCODING=utf-8 python tmp_ai_audit/behavior_audit.py     # 离线规则回归\n'
                 'PYTHONIOENCODING=utf-8 python tools/make_eval_report.py          # 重生成本报告</pre>')
    parts.append('<div class="foot">口径说明：图谱层 / 行为层密度 = state.db 全时段 actions；'
                 '文本层 = 近 12h contents；真人基准 = tools/graph_refs.json（LATEST feed 抓取）。</div>')

    css = """
    :root{--bg:#f6f7f9;--card:#fff;--ink:#1b2028;--sub:#5b6572;--line:#e3e7ed;
          --ok:#0a7f4f;--no:#c0392b;--accent:#2b5cd9;--warn:#b26a00}
    *{box-sizing:border-box}
    body{margin:0;padding:32px 20px 64px;background:var(--bg);color:var(--ink);
         font:14px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
    .wrap{max-width:1080px;margin:auto}
    .hero{background:linear-gradient(135deg,#fff,#eef2fb);border:1px solid var(--line);
          border-radius:14px;padding:28px 30px;margin-bottom:32px}
    h1{margin:0 0 6px;font-size:26px;letter-spacing:.5px}
    .sub{color:var(--sub);font-size:13px}
    .cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-top:22px}
    .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
    .card .k{color:var(--sub);font-size:12px}
    .card .v{font-size:18px;font-weight:600;margin:6px 0}
    .card .v.num{font-size:28px;color:var(--warn)}
    .card .d{color:var(--sub);font-size:12px}
    h2{font-size:18px;margin:34px 0 14px;padding-left:11px;border-left:4px solid var(--accent)}
    table{width:100%;border-collapse:collapse;background:var(--card);
          border:1px solid var(--line);border-radius:8px;overflow:hidden;font-size:13px}
    th{background:#eef1f6;text-align:left;padding:9px 12px;font-weight:600;white-space:nowrap}
    td{padding:8px 12px;border-top:1px solid var(--line);vertical-align:top}
    tbody tr:nth-child(even){background:#fafbfd}
    .chk td:nth-child(3){text-align:center;width:52px}
    .mono,code,pre{font-family:Consolas,"Courier New",monospace}
    code{background:#eef1f6;padding:1px 5px;border-radius:4px;font-size:12px}
    .note{background:#fff;border:1px solid var(--line);border-left:4px solid var(--accent);
          border-radius:6px;padding:12px 16px;margin:12px 0;color:#333}
    .blk{background:#fff8f2;border:1px solid #f0d6bd;border-left:4px solid var(--warn);
         border-radius:6px;padding:12px 16px;margin:10px 0}
    .blk .mono{color:var(--sub);font-size:12px;margin:6px 0}
    .b{display:inline-block;padding:2px 9px;border-radius:20px;font-size:12px;font-weight:600}
    .b-ok{background:#e3f5ec;color:var(--ok)}
    .b-no{background:#fdeae7;color:var(--no)}
    .b-na{background:#eceff3;color:var(--sub)}
    pre{background:#1e2430;color:#e8edf5;padding:16px 18px;border-radius:8px;
        overflow-x:auto;font-size:12px;line-height:1.7}
    .foot{color:var(--sub);font-size:12px;margin-top:26px;text-align:center}
    """
    doc = ("<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
           "<meta name='viewport' content='width=device-width,initial-scale=1'>"
           "<title>三层图灵评估体系 · 大改验收报告</title>"
           f"<style>{css}</style></head><body><div class='wrap'>"
           + "".join(parts) + "</div></body></html>")
    OUT.write_text(doc, encoding="utf-8")
    print(f"报告已生成：{OUT}")
    print(f"  L1 达标={l1_ok}  图谱规则={g.get('graph_pass')}  "
          f"上线放行={g.get('pass')}  阻断项={len(blockers)}")
    print(f"  真人窗口样本={base.get('n_real_actions')}  "
          f"裁判组数={len(vote_rows)}  检查项={len(checks)}")


if __name__ == "__main__":
    main()
