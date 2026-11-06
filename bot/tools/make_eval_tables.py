#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""评估数据 Markdown 表格生成器（tools/make_eval_tables.py）。

把散落的 *_last.json / baseline json 现场汇总成可直接粘进 README 的表格。
所有数字都从数据源读取；缺失的数据源一律写「未生成」并跳过，
不手写任何数字、不硬编码任何「达标/未达标」结论。

输出：tools/README_TABLES.md
用法：python tools/make_eval_tables.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
BOT = HERE.parent
OUT = HERE / "README_TABLES.md"
RAW_DB = BOT / "kb" / "raw.db"
KB_DIR = BOT / "kb"

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

NOW = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
NA = "未生成"
DASH = "—"

# 数据源登记表：key -> 相对 BOT 的路径
SOURCES = {
    "l1": "tools/eval_last.json",
    "l1_base": "tools/eval_baseline_fixed.json",
    "graph": "tools/graph_eval_last.json",
    "graph_base": "tools/graph_baseline.json",
    "all": "tools/eval_all_last.json",
    "scenarios": "tools/scenarios_last.json",
    "thread": "tools/thread_context_last.json",
}

LOADED = {}

GROUP_LABELS = [
    ("real", "真人基线"),
    ("before", "改造前"),
    ("before_textonly", "改造前·仅文本"),
    ("after", "改造后"),
    ("after_gated", "改造后·画像门控"),
]


# ---------------------------------------------------------------- 读取与格式化
def load_all() -> None:
    for key, rel in SOURCES.items():
        p = BOT / rel
        rec = {"path": rel, "data": None, "data_time": NA}
        if p.exists():
            try:
                rec["data"] = json.loads(p.read_text(encoding="utf-8"))
                dt = ""
                if isinstance(rec["data"], dict):
                    dt = str(rec["data"].get("generated_at") or "")
                if not dt:
                    dt = datetime.fromtimestamp(p.stat().st_mtime).strftime(
                        "%Y-%m-%d %H:%M:%S") + "（文件修改时间）"
                rec["data_time"] = dt
            except Exception as exc:
                rec["data_time"] = "读取失败：%s" % str(exc)[:80]
                rec["data"] = None
        LOADED[key] = rec


def fmt(v) -> str:
    if v is None:
        return DASH
    if isinstance(v, bool):
        return "通过" if v else "未通过"
    if isinstance(v, float):
        return "%g" % v
    if isinstance(v, int):
        return str(v)
    if isinstance(v, (list, tuple, set)):
        return " / ".join(fmt(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    return str(v)


def md(v) -> str:
    return fmt(v).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def pct(v) -> str:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return "%g%%" % (v * 100)
    return DASH


def status_cn(v) -> str:
    """判定类字段：bool 与 pass/fail/skip 统一成中文，SKIP 显式标注不算通过。"""
    if isinstance(v, bool):
        return "通过" if v else "未通过"
    return {"pass": "通过", "fail": "未通过",
            "skip": "SKIP（未验收，不算通过）"}.get(str(v).lower(), fmt(v))


def flag(v) -> str:
    """开关/状态类布尔（skipped、offline、available）用「是/否」，别误译成判定。"""
    if isinstance(v, bool):
        return "是" if v else "否"
    return fmt(v)


def human_size(n: int) -> str:
    v = float(n)
    if v < 1024:
        return "%d B" % n
    for unit in ("KB", "MB", "GB"):
        v /= 1024.0
        if v < 1024 or unit == "GB":
            return "%.1f %s" % (v, unit)
    return "%.1f GB" % v


def table(headers, rows) -> list[str]:
    lines = ["| " + " | ".join(md(h) for h in headers) + " |",
             "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows:
        lines.append("| " + " | ".join(md(x) for x in r) + " |")
    return lines


def source_line(keys) -> str:
    parts = []
    for k in keys:
        rec = LOADED[k]
        if rec["data"] is None:
            parts.append("%s（%s）" % (rec["path"], NA))
        else:
            parts.append("%s（数据时间 %s）" % (rec["path"], rec["data_time"]))
    return "数据来源：%s，生成时间 %s" % (" + ".join(parts), NOW)


def section(out: list[str], title: str, keys, lines: list[str]) -> None:
    out.append("## %s" % title)
    out.append("")
    out.append(source_line(keys))
    out.append("")
    out.extend(lines)
    out.append("")


def subsection(out: list[str], title: str, keys, lines: list[str]) -> None:
    out.append("### %s" % title)
    out.append("")
    out.append(source_line(keys))
    out.append("")
    out.extend(lines)
    out.append("")


# ---------------------------------------------------------------- ① 三层评估总览
def build_overview(out: list[str]) -> None:
    a = LOADED["all"]["data"]
    s = LOADED["scenarios"]["data"]
    t = LOADED["thread"]["data"]
    headers = ["层级", "评估项", "判定", "关键指标", "来源文件", "备注"]
    rows = []

    if a is None:
        rows.append(["L1 / L2+L3 / 总判定", "三层评估", NA, NA,
                     LOADED["all"]["path"], NA])
    else:
        l1 = a.get("l1") or {}
        rows.append(["L1 文本层", "评论/帖子/楼中楼文本", status_cn(l1.get("ok")),
                     "三项 total=%s；被抓率=%s；门槛 min_total=%s；耗时=%ss"
                     % (fmt(l1.get("totals")), fmt(l1.get("catch_rates")),
                        fmt(l1.get("min_total")), fmt(l1.get("elapsed_s"))),
                     LOADED["all"]["path"], status_cn(l1.get("ok"))])
        l23 = a.get("l23") or {}
        votes = l23.get("votes") or {}
        dr = {k: (v or {}).get("detection_rate") for k, v in votes.items()}
        rows.append(["L2/L3 行为图谱层", "图检测 + 图灵投票 + gates",
                     status_cn(l23.get("pass")),
                     "graph_pass=%s；是否跳过 skipped=%s；五组检出率=%s；checks=%d 项"
                     % (status_cn(l23.get("graph_pass")), flag(l23.get("skipped")),
                        fmt(dr), len(l23.get("checks") or [])),
                     LOADED["all"]["path"], status_cn(l23.get("pass"))])
        rows.append(["总体", "overall_pass", status_cn(a.get("overall_pass")),
                     "gate_blockers=%d 项" % len(a.get("gate_blockers") or []),
                     LOADED["all"]["path"], status_cn(a.get("overall_pass"))])

    if s is None:
        rows.append(["新增能力", "AI 质疑场景", NA, NA,
                     LOADED["scenarios"]["path"], NA])
    else:
        sm = s.get("summary") or {}
        rows.append(["新增能力", "AI 质疑场景识别/否认", status_cn(s.get("overall")),
                     "PASS=%s FAIL=%s SKIP=%s（共 %s 组）"
                     % (fmt(sm.get("pass")), fmt(sm.get("fail")),
                        fmt(sm.get("skip")), fmt(sm.get("total"))),
                     LOADED["scenarios"]["path"],
                     s.get("skip_note") or DASH])

    if t is None:
        rows.append(["新增能力", "楼中楼上下文", NA, NA,
                     LOADED["thread"]["path"], NA])
    else:
        tm = t.get("summary") or {}
        rows.append(["新增能力", "楼中楼上下文三项齐全", status_cn(t.get("overall")),
                     "PASS=%s FAIL=%s SKIP=%s（共 %s 项）；prompt %s 字"
                     % (fmt(tm.get("pass")), fmt(tm.get("fail")),
                        fmt(tm.get("skip")), fmt(tm.get("total")),
                        fmt(t.get("prompt_len"))),
                     LOADED["thread"]["path"],
                     t.get("skip_note") or DASH])

    section(out, "① 三层评估总览表", ["all", "scenarios", "thread"],
            table(headers, rows))


# ---------------------------------------------------------------- ② L1 分项
def build_l1(out: list[str]) -> None:
    e = LOADED["l1"]["data"]
    b = LOADED["l1_base"]["data"]
    keys = ["l1"] + (["l1_base"] if b is not None else [])
    if e is None:
        section(out, "② L1 分项表", keys,
                table(["模式", "结果"], [[NA, "%s 未生成，整表跳过" % LOADED["l1"]["path"]]]))
        return

    headers = ["模式", "样本数 n", "总分 total", "图灵分 turing",
               "图灵被抓率 catch_rate", "被抓率(%)", "分布 dist", "题材 topic",
               "同质化 dup", "形式 form", "AI 味 flavor", "事实率 fact_rate"]
    if b is not None:
        headers.append("固定考卷基线 total")
    rows = []
    for mode in ("comment", "post", "reply"):
        d = e.get(mode) or {}
        row = [mode, fmt(d.get("n")), fmt(d.get("total")), fmt(d.get("turing")),
               fmt(d.get("catch_rate")), pct(d.get("catch_rate")),
               fmt(d.get("dist")), fmt(d.get("topic")), fmt(d.get("dup")),
               fmt(d.get("form")), fmt(d.get("flavor")), fmt(d.get("fact_rate"))]
        if b is not None:
            bd = b.get(mode) or {}
            row.append(fmt(bd.get("total")) if "total" in bd else NA)
        rows.append(row)
    section(out, "② L1 分项表", keys, table(headers, rows))

    # ②-a 分项明细：JSON 原值照抄，不对三元组做语义假设
    detail_rows = []
    for mode in ("comment", "post", "reply"):
        d = e.get(mode) or {}
        for cat in ("dist_detail", "topic_detail", "form_detail", "dup_detail"):
            blk = d.get(cat)
            if not isinstance(blk, dict):
                continue
            for metric, val in blk.items():
                detail_rows.append([mode, cat, metric, fmt(val)])
    if detail_rows:
        subsection(out, "②-a L1 分项明细（各 *_detail 的 JSON 原值）", keys,
                   table(["模式", "分项类别", "指标", "JSON 原值"], detail_rows))


# ---------------------------------------------------------------- ③ 图灵测试对照
def build_graph(out: list[str]) -> None:
    g = LOADED["graph"]["data"]
    if g is None:
        section(out, "③ 图灵测试对照表（含五组）", ["graph"],
                table(["组别", "结果"], [[NA, "%s 未生成，整表跳过" % LOADED["graph"]["path"]]]))
        return

    votes = g.get("votes") or {}
    rows = []
    for key, label in GROUP_LABELS:
        v = votes.get(key)
        if not isinstance(v, dict):
            rows.append([key, label, NA, NA, NA, NA, NA, NA])
            continue
        rows.append([key, label, fmt(v.get("votes")), fmt(v.get("bot_votes")),
                     fmt(v.get("detection_rate")), pct(v.get("detection_rate")),
                     fmt(v.get("parse_failed")), fmt(v.get("truncated"))])
    section(out, "③ 图灵测试对照表（含五组）", ["graph"],
            table(["组别", "说明", "投票数 votes", "判为机器 bot_votes",
                   "检出率 detection_rate", "折算百分比", "parse_failed",
                   "truncated"], rows))

    # ③-a 确定性指标：行=指标，列=五组，全部动态取字段名
    metrics = g.get("metrics") or {}
    group_keys = [k for k, _ in GROUP_LABELS]
    present = [k for k in group_keys if isinstance(metrics.get(k), dict)]
    metric_names = []
    for k in present:
        for m in metrics[k]:
            if m not in metric_names:
                metric_names.append(m)
    if present and metric_names:
        mrows = []
        for m in metric_names:
            mrows.append([m] + [fmt((metrics.get(k) or {}).get(m)) for k in present])
        subsection(out, "③-a L2/L3 确定性指标", ["graph"],
                   table(["指标"] + present, mrows))

    # ③-b checks 明细
    checks = g.get("checks") or []
    crows = [[c.get("category"), c.get("name"), status_cn(c.get("pass")),
              c.get("detail")] for c in checks]
    if crows:
        subsection(out, "③-b L2/L3 checks 明细", ["graph"],
                   table(["类别", "检查项", "判定", "明细"], crows))

    # ③-c 汇总字段
    srows = []
    for k in ("generated_at", "graph_pass", "pass", "baseline_available",
              "hard_total", "hard_pass", "graph_total", "graph_hard_pass"):
        if k not in g:
            continue
        v = g[k]
        if k in ("graph_pass", "pass"):
            srows.append([k, status_cn(v)])
        elif k == "baseline_available":
            srows.append([k, flag(v)])
        else:
            srows.append([k, fmt(v)])
    cfg = g.get("config") or {}
    for k, v in cfg.items():
        srows.append(["config.%s" % k, flag(v)])
    counts = g.get("counts") or {}
    for k, v in counts.items():
        srows.append(["counts.%s" % k, fmt(v)])
    blockers = g.get("gate_blockers") or []
    srows.append(["gate_blockers 数量", fmt(len(blockers))])
    for i, blk in enumerate(blockers, 1):
        if isinstance(blk, dict):
            srows.append(["gate_blocker.%d" % i,
                          "%s｜%s" % (blk.get("name"), blk.get("detail"))])
    subsection(out, "③-c L2/L3 汇总字段", ["graph"],
               table(["项目", "数值"], srows))


# ---------------------------------------------------------------- ④ 真人基准标定
def build_baseline(out: list[str]) -> None:
    b = LOADED["graph_base"]["data"]
    if b is None:
        section(out, "④ 真人基准标定表", ["graph_base"],
                table(["指标", "结果"], [[NA, "%s 未生成，整表跳过" % LOADED["graph_base"]["path"]]]))
        return

    rw = b.get("real_windows") or {}
    rows = []
    for metric, vals in rw.items():
        if isinstance(vals, dict):
            rows.append([metric, fmt(vals.get("p50")), fmt(vals.get("p90")),
                         fmt(vals.get("max")), fmt(vals.get("mean"))])
        else:
            rows.append([metric, fmt(vals), DASH, DASH, DASH])
    section(out, "④ 真人基准标定表", ["graph_base"],
            table(["指标", "p50", "p90", "max", "mean"], rows))

    rf = b.get("real_full") or {}
    if rf:
        subsection(out, "④-a 真人全窗口 real_full", ["graph_base"],
                   table(["指标", "数值"], [[k, fmt(v)] for k, v in rf.items()]))

    params = [(k, fmt(b.get(k))) for k in
              ("generated_at", "source", "window", "samples", "seed",
               "n_real_actions") if k in b]
    if params:
        subsection(out, "④-b 标定参数", ["graph_base"],
                   table(["参数", "数值"], params))


# ---------------------------------------------------------------- ⑤ 场景/上下文验收
def build_acceptance(out: list[str]) -> None:
    s = LOADED["scenarios"]["data"]
    t = LOADED["thread"]["data"]
    headers = ["来源", "验收组/检查项", "状态", "明细"]
    rows = []
    if s is None:
        rows.append([LOADED["scenarios"]["path"], NA, NA,
                     "%s 未生成，跳过该来源" % LOADED["scenarios"]["path"]])
    else:
        for g in s.get("groups") or []:
            rows.append([LOADED["scenarios"]["path"], g.get("name"),
                         status_cn(g.get("status")), g.get("detail")])
        rows.append([LOADED["scenarios"]["path"], "SUMMARY(overall)",
                     status_cn(s.get("overall")),
                     "PASS=%s FAIL=%s SKIP=%s"
                     % (fmt((s.get("summary") or {}).get("pass")),
                        fmt((s.get("summary") or {}).get("fail")),
                        fmt((s.get("summary") or {}).get("skip")))])
    if t is None:
        rows.append([LOADED["thread"]["path"], NA, NA,
                     "%s 未生成，跳过该来源" % LOADED["thread"]["path"]])
    else:
        for c in t.get("checks") or []:
            rows.append([LOADED["thread"]["path"], c.get("name"),
                         status_cn(c.get("status")), c.get("detail")])
        rows.append([LOADED["thread"]["path"], "SUMMARY(overall)",
                     status_cn(t.get("overall")),
                     "PASS=%s FAIL=%s SKIP=%s"
                     % (fmt((t.get("summary") or {}).get("pass")),
                        fmt((t.get("summary") or {}).get("fail")),
                        fmt((t.get("summary") or {}).get("skip")))])
    section(out, "⑤ 场景/上下文验收表", ["scenarios", "thread"],
            table(headers, rows))


# ---------------------------------------------------------------- ⑥ 语料库规模
def build_corpus(out: list[str]) -> None:
    rows = []
    if not RAW_DB.exists():
        rows.append(["posts / comments 计数", NA,
                     "%s 不存在，跳过现场查询" % RAW_DB.relative_to(BOT).as_posix()])
    else:
        try:
            con = sqlite3.connect(str(RAW_DB))
            for tbl in ("posts", "comments", "state"):
                try:
                    n = con.execute('SELECT COUNT(*) FROM "%s"' % tbl).fetchone()[0]
                    rows.append(["%s 表行数" % tbl, fmt(n),
                                 "SELECT COUNT(*) FROM %s（现场查询）" % tbl])
                except Exception as exc:
                    rows.append(["%s 表行数" % tbl, NA, "查询失败：%s" % str(exc)[:80]])
            try:
                n = con.execute("SELECT COUNT(*) FROM comments WHERE is_sub=1").fetchone()[0]
                rows.append(["comments 中楼中楼(is_sub=1)", fmt(n),
                             "SELECT COUNT(*) FROM comments WHERE is_sub=1（现场查询）"])
            except Exception as exc:
                rows.append(["comments 中楼中楼(is_sub=1)", NA,
                             "查询失败：%s" % str(exc)[:80]])
            try:
                n = con.execute("SELECT COUNT(DISTINCT day) FROM posts").fetchone()[0]
                rows.append(["posts 覆盖天数 day", fmt(n),
                             "SELECT COUNT(DISTINCT day) FROM posts（现场查询）"])
            except Exception as exc:
                rows.append(["posts 覆盖天数 day", NA, "查询失败：%s" % str(exc)[:80]])
            con.close()
        except Exception as exc:
            rows.append(["posts / comments 计数", NA,
                         "打开 %s 失败：%s" % (RAW_DB.name, str(exc)[:80])])

    # 各文件大小
    total = 0
    if KB_DIR.is_dir():
        files = sorted((f for f in KB_DIR.iterdir() if f.is_file()),
                       key=lambda f: f.name)
        for f in files:
            sz = f.stat().st_size
            total += sz
            rows.append(["kb/%s" % f.name, "%s (%d B)" % (human_size(sz), sz),
                         "文件大小（现场 stat）"])
        rows.append(["kb/ 目录文件合计", "%s (%d B)" % (human_size(total), total),
                     "%d 个文件" % len(files)])
    else:
        rows.append(["kb/ 各文件大小", NA,
                     "%s 不存在，跳过" % KB_DIR.relative_to(BOT).as_posix()])

    out.append("## ⑥ 语料库规模表")
    out.append("")
    out.append("数据来源：kb/raw.db（现场 SQL 查询）+ kb/ 目录文件（现场 stat），生成时间 %s"
               % NOW)
    out.append("")
    out.extend(table(["项目", "数值", "说明"], rows))
    out.append("")


# ---------------------------------------------------------------- 主流程
def main() -> int:
    load_all()
    out = [
        "# 评估数据汇总表（自动生成）",
        "",
        "> 由 `tools/make_eval_tables.py` 从各评估 JSON 与 `kb/raw.db` 现场汇总生成。",
        "> 所有数字均取自数据源，不含手写数值或硬编码结论；缺失的数据源写「未生成」并跳过。",
        "> 生成时间：%s" % NOW,
        "",
        "## 数据源状态",
        "",
        "数据来源：%s，生成时间 %s" % (" + ".join(SOURCES.values()), NOW),
        "",
    ]
    status_rows = []
    for key, rel in SOURCES.items():
        rec = LOADED[key]
        status_rows.append([rel, "已生成" if rec["data"] is not None else NA,
                            rec["data_time"]])
    out.extend(table(["数据源文件", "状态", "数据时间"], status_rows))
    out.append("")

    build_overview(out)
    build_l1(out)
    build_graph(out)
    build_baseline(out)
    build_acceptance(out)
    build_corpus(out)

    text = "\n".join(out).rstrip() + "\n"
    OUT.write_text(text, encoding="utf-8")

    print("=" * 66)
    print("评估表格生成器  tools/make_eval_tables.py")
    print("生成时间：%s" % NOW)
    print("-" * 66)
    for key, rel in SOURCES.items():
        rec = LOADED[key]
        print("  %-38s %s   %s"
              % (rel, "已生成" if rec["data"] is not None else NA,
                 rec["data_time"]))
    print("-" * 66)
    print("输出：%s（%d 字节，%d 行）" % (OUT, len(text.encode("utf-8")),
                                     len(text.splitlines())))
    print("章节：", "、".join(l[3:] for l in text.splitlines()
                          if l.startswith("## ") and not l.startswith("## 数据源")))
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
