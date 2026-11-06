#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AI 质疑场景验收（tools/eval_scenarios.py）。

本轮新增能力：被真人质疑「是不是 AI」时，要像真人一样顺口否认，
而不是承认、解释或端出客服腔。本脚本只做离线验收：不调 LLM、不发网络。

被测接口（由并行子代理实现，当前可能还不存在）
--------------------------------------------
    szlike.tasks.is_ai_accusation(text) -> bool
    szlike.tasks.ai_denial_reply(...)   -> str 或 list[str]（待回复池）

接口缺失或签名不可用时，相关验收组标记 SKIP 并写明原因，绝不静默当通过。

判定与退出码
------------
    0  全部通过（无 FAIL、无 SKIP）
    1  至少一项 FAIL
    2  没有 FAIL 但存在 SKIP —— SKIP 不算通过，因此不给 0

用法：  python tools/eval_scenarios.py
输出：  tools/scenarios_last.json
"""
from __future__ import annotations

import inspect
import json
import random
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
BOT = HERE.parent
sys.path.insert(0, str(BOT))
OUT = HERE / "scenarios_last.json"

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ---------------------------------------------------------------- 语料
# 正例：真人质疑「你是 AI / 机器人 / 脚本 / 水军」。含不含「AI」字样都要能认出来。
POSITIVES = [
    "你是 ai 吧",
    "机器人吧",
    "谁把 gpt 接进来了",
    "一眼假",
    "这号是脚本",
    "水军",
    "不是真人吧",
    "这回复也太 AI 了",
    "楼主是机器人吗",
    "又是 ChatGPT 生成的吧",
    "不会是 bot 吧",
    "这号怕不是自动回复",
    "刚注册就发这么多，机器人实锤",
    "你号是不是被写成脚本了",
    "AI 味太冲了",
    "别装了，一眼 GPT",
    "是不是有人拿大模型在刷",
    "回复这么快，机器吧",
    "这语气不像活人",
    "机器人滚粗",
    "楼上是 AI 小助手吗",
    "怎么每句都像模板，脚本吧",
    "真人谁这么说话啊",
    "是不是自动回复机器人",
    "GPT-4 接进来了？",
    "这号背后是程序吧",
    "机器生成的吧，太规整了",
    "这号八成是养的机器号",
]

# 反例：只是在聊 AI / GPT / 机器人 / 脚本这个话题，并没有质疑「你是不是 AI」。
# 这组用来卡精确率——不能看见「AI」两个字就当成被质疑。
NEGATIVES = [
    "AI 课怎么选",
    "人工智能专业好考吗",
    "用 GPT 帮我写论文",
    "AI 能不能过四级",
    "机器学习这门课点名吗",
    "GPT 写的摘要能过查重吗",
    "有没有好用点的 AI 翻译",
    "深度学习教材哪本好",
    "机器人社团还招人吗",
    "AI 绘图接单靠谱吗",
    "用 ChatGPT 练口语有用吗",
    "人工智能导论期末给分怎么样",
    "脚本跑数据会不会被封",
    "有没有人一起组队做 AI 项目",
    "GPT 和文心一言哪个写文献综述好",
    "算法课和 AI 课时间撞了",
    "ChatGPT 会员值得开吗",
    "自然语言处理方向考研难吗",
]

# 承认 / 解释 / 客服腔：真人否认不会这么说。用短语而非单字，
# 免得把「什么机器人，我手打的」这类复述对方原话的真人口吻误杀。
BANNED = [
    "我是真人",
    "我不是 AI", "我不是AI", "不是 AI", "不是AI",
    "作为 AI", "作为AI", "我是 AI", "我是AI",
    "我是机器人", "我不是机器人", "我是机器号",
    "人工智能助手", "语言模型", "大模型生成",
    "我是由", "训练而成", "开发出来的",
    "抱歉", "不好意思", "对不起",
    "解释一下", "解释下", "需要说明的是", "需要说明的是：",
    "感谢理解", "感谢您的", "谢谢您的", "让您", "请问",
    "客服", "系统提示", "根据设定",
    "OpenAI", "ChatGPT 是", "GPT 是",
]

MAX_LEN = 25           # 否认回复长度上限（字）
DRAW_N = 30            # 连取次数
DEDUP_MIN = 0.60       # 去重率下限
DRAW_SEED = 20261106   # 连取用固定随机种子，保证验收可复现
SAMPLE_N = 10         # 打印样例条数
RECOGNITION_MIN = 1.0  # 逐条断言：正/反例全部判对才算过

STATUS_PASS, STATUS_FAIL, STATUS_SKIP = "pass", "fail", "skip"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 接口探测
def load_api():
    """返回 (识别函数, 否认函数, 缺失原因)。任一缺失即返回原因，调用方据此标 SKIP。"""
    try:
        from szlike import tasks as t
    except Exception as exc:
        return None, None, "import szlike.tasks 失败：%s" % exc
    rec = getattr(t, "is_ai_accusation", None)
    den = getattr(t, "ai_denial_reply", None)
    missing = [n for n, f in (("is_ai_accusation", rec),
                              ("ai_denial_reply", den)) if not callable(f)]
    if missing:
        return rec, den, "以下接口尚未实现，本组 SKIP：%s" % "、".join(missing)
    return rec, den, ""


def _call_rec(rec, text: str):
    """兼容 text 作为位置参数或关键字参数。"""
    try:
        return bool(rec(text))
    except TypeError:
        return bool(rec(text=text))


def _as_list(res) -> list[str]:
    """否认函数可能返回一句，也可能返回整个待回复池。"""
    if isinstance(res, str):
        return [res]
    if isinstance(res, (list, tuple, set)):
        return [str(x) for x in res]
    return [str(res)]


def _call_denial(den, i: int, rng_obj=None, mode: str = "auto"):
    """按签名自适应调用，贴合接口的离线复现契约。

    ai_denial_reply(seed="", rng=None) 的语义是：同一 seed 恒同一句（可复现），
    显式传 rng 时随机抽。因此「连取 30 句」必须真的换输入，不能 30 次都
    不传参——那测的是 seed 恒为空的退化分支，会严重低估池的多样性。

    mode:
      auto  —— 优先 rng（固定种子实例，可复现的随机连取），其次 seed，最后无参
      seed  —— 传确定性的 30 个不同 seed，用于全池扫描与对照
      plain —— 无参调用，记录退化分支表现，用于对照
    """
    text = POSITIVES[i % len(POSITIVES)]
    try:
        params = inspect.signature(den).parameters
    except (TypeError, ValueError):
        params = {}
    if mode == "plain":
        return den()
    if mode == "seed":
        if "seed" in params:
            return den(seed="eval-scenario-%d" % i)
        return den()
    # auto
    if "rng" in params and rng_obj is not None:
        return den(rng=rng_obj)
    if "seed" in params:
        return den(seed="eval-scenario-%d" % i)
    return den()


def _scan_mode(den, mode: str) -> tuple[list[str], list[str]]:
    """按指定模式连取 DRAW_N 次，返回 (全部文本, 每次取的一句)。"""
    all_texts, drawn, errors = [], [], []
    for i in range(DRAW_N):
        try:
            res = _call_denial(den, i, None, mode)
        except Exception as exc:
            errors.append(str(exc))
            drawn.append("")
            continue
        texts = [t.strip() for t in _as_list(res) if t and t.strip()]
        all_texts.extend(texts)
        drawn.append(texts[0] if texts else "")
    return all_texts, drawn


# ---------------------------------------------------------------- 识别验收
def check_recognition(rec, skip_reason: str) -> dict:
    if not callable(rec):
        return {"name": "recognition", "status": STATUS_SKIP,
                "detail": skip_reason or "is_ai_accusation 不可用",
                "metrics": {"positive_n": len(POSITIVES),
                            "negative_n": len(NEGATIVES),
                            "correct": 0, "total": 0, "accuracy": None},
                "items": []}

    items = []
    for text, expect in ([(x, True) for x in POSITIVES] +
                         [(x, False) for x in NEGATIVES]):
        try:
            got = _call_rec(rec, text)
            items.append({"case": text, "expect": expect, "got": got,
                          "status": STATUS_PASS if got == expect else STATUS_FAIL})
        except Exception as exc:
            items.append({"case": text, "expect": expect, "got": None,
                          "status": STATUS_SKIP,
                          "detail": "调用异常：%s" % exc})

    total = len(items)
    correct = sum(1 for x in items if x["status"] == STATUS_PASS)
    failed = sum(1 for x in items if x["status"] == STATUS_FAIL)
    skipped = sum(1 for x in items if x["status"] == STATUS_SKIP)
    accuracy = correct / total if total else 0.0
    if failed or skipped:
        status = STATUS_FAIL if failed else STATUS_SKIP
    else:
        status = STATUS_PASS if accuracy >= RECOGNITION_MIN else STATUS_FAIL
    wrong = [x["case"] for x in items if x["status"] != STATUS_PASS]
    return {
        "name": "recognition", "status": status,
        "detail": ("逐条断言 %d 条（正例 %d / 反例 %d）：对 %d、错 %d、SKIP %d，"
                   "正确率 %.1f%%（阈值 %.0f%%）%s"
                   % (total, len(POSITIVES), len(NEGATIVES), correct, failed,
                      skipped, accuracy * 100, RECOGNITION_MIN * 100,
                      ("；未通过：%s" % "、".join(wrong[:8])) if wrong else "")),
        "metrics": {"positive_n": len(POSITIVES), "negative_n": len(NEGATIVES),
                    "total": total, "correct": correct, "failed": failed,
                    "skipped": skipped, "accuracy": round(accuracy, 4)},
        "items": items,
    }


# ---------------------------------------------------------------- 否认回复验收
def collect_denials(den) -> dict:
    """连取 30 句（按接口契约换输入），并扫描其它调用模式覆盖整个回复池。"""
    rng_obj = random.Random(DRAW_SEED)
    drawn, all_texts, errors = [], [], []
    for i in range(DRAW_N):
        try:
            res = _call_denial(den, i, rng_obj, "auto")
        except Exception as exc:
            errors.append(str(exc))
            drawn.append("")
            continue
        texts = [t.strip() for t in _as_list(res) if t and t.strip()]
        all_texts.extend(texts)
        drawn.append(texts[i % len(texts)] if texts else "")

    # 质量断言覆盖尽可能多的池内容：再按确定性 seed / 无参各扫一遍
    mode_dedup = {"main": round(len({d for d in drawn if d}) / DRAW_N, 4)}
    for mode in ("seed", "plain"):
        extra, outs = _scan_mode(den, mode)
        all_texts.extend(extra)
        mode_dedup[mode] = round(len({d for d in outs if d}) / DRAW_N, 4)
    return {"drawn": drawn, "all_texts": all_texts, "errors": errors,
            "calls_ok": DRAW_N - len(errors), "mode_dedup": mode_dedup}


def _base_skip(collected: dict) -> str:
    if collected["calls_ok"] < DRAW_N:
        return ("连取 %d 次仅 %d 次调用成功（示例错误：%s），"
                "接口可能仍在改造中，本组 SKIP"
                % (DRAW_N, collected["calls_ok"],
                   (collected["errors"][0] if collected["errors"] else "未知")[:120]))
    if not collected["all_texts"]:
        return "否认回复池为空，无文本可验，本组 SKIP"
    return ""


def check_quality(den, skip_reason: str, collected: dict) -> list[dict]:
    if not callable(den):
        reason = skip_reason or "ai_denial_reply 不可用"
        return [{"name": n, "status": STATUS_SKIP, "detail": reason,
                 "metrics": {}}
                for n in ("denial_quality", "denial_length", "denial_dedup")]

    base_skip = _base_skip(collected)
    drawn, all_texts = collected["drawn"], collected["all_texts"]

    # 1) 承认 / 解释 / 客服腔
    hits = [{"text": t, "word": w} for t in all_texts for w in BANNED
            if w.lower() in t.lower()]
    q_status = STATUS_SKIP if base_skip else (STATUS_FAIL if hits else STATUS_PASS)
    q_group = {
        "name": "denial_quality", "status": q_status,
        "detail": base_skip or ("检查 %d 条回复，命中禁用腔 %d 处（禁用词表 %d 条）%s"
                                % (len(all_texts), len(hits), len(BANNED),
                                   ("；例：%s" % hits[0]["text"]) if hits else "")),
        "metrics": {"checked": len(all_texts), "banned_words": len(BANNED),
                    "hits": len(hits), "hit_examples": hits[:5]},
    }

    # 2) 长度 <= 25 字
    too_long = [t for t in all_texts if len(t) > MAX_LEN]
    empty_n = sum(1 for d in drawn if not d)
    l_status = STATUS_SKIP if base_skip else (
        STATUS_FAIL if (too_long or empty_n) else STATUS_PASS)
    l_group = {
        "name": "denial_length", "status": l_status,
        "detail": base_skip or ("检查 %d 条：超长 %d 条（上限 %d 字）、空回复 %d 条；"
                                "最长 %d 字"
                                % (len(all_texts), len(too_long), MAX_LEN, empty_n,
                                   max((len(t) for t in all_texts), default=0))),
        "metrics": {"checked": len(all_texts), "max_len": MAX_LEN,
                    "too_long": len(too_long), "empty": empty_n,
                    "max_actual_len": max((len(t) for t in all_texts), default=0),
                    "examples": too_long[:5]},
    }

    # 3) 连取 30 句去重率
    uniq = len({d for d in drawn if d})
    dedup = uniq / DRAW_N if DRAW_N else 0.0
    d_status = STATUS_SKIP if base_skip else (
        STATUS_PASS if dedup >= DEDUP_MIN else STATUS_FAIL)
    modes = collected.get("mode_dedup") or {}
    modes_txt = "、".join("%s=%.1f%%" % (k, v * 100) for k, v in modes.items())
    d_group = {
        "name": "denial_dedup", "status": d_status,
        "detail": base_skip or ("连取 %d 句，去重后 %d 句，去重率 %.1f%%（阈值 %.0f%%）；"
                                "各调用模式去重率：%s"
                                % (DRAW_N, uniq, dedup * 100, DEDUP_MIN * 100,
                                   modes_txt or "—")),
        "metrics": {"draw_n": DRAW_N, "unique": uniq,
                    "dedup_rate": round(dedup, 4), "threshold": DEDUP_MIN,
                    "mode_dedup": modes, "pool_checked": len(all_texts)},
    }
    return [q_group, l_group, d_group]


# ---------------------------------------------------------------- 主流程
def main() -> int:
    rec, den, missing = load_api()
    rec_group = check_recognition(rec, missing)
    collected = collect_denials(den) if callable(den) else {
        "drawn": [], "all_texts": [], "errors": [], "calls_ok": 0}
    quality_groups = check_quality(den, missing, collected)
    groups = [rec_group] + quality_groups

    summary = {"pass": sum(1 for g in groups if g["status"] == STATUS_PASS),
               "fail": sum(1 for g in groups if g["status"] == STATUS_FAIL),
               "skip": sum(1 for g in groups if g["status"] == STATUS_SKIP),
               "total": len(groups)}
    if summary["fail"]:
        overall, code = STATUS_FAIL, 1
    elif summary["skip"] or missing:
        overall, code = STATUS_SKIP, 2
    else:
        overall, code = STATUS_PASS, 0

    samples = [d for d in collected["drawn"] if d][:SAMPLE_N]
    report = {
        "generated_at": _now(),
        "overall": overall,
        "exit_code": code,
        "exit_code_meaning": {"0": "全部通过", "1": "存在 FAIL",
                              "2": "无 FAIL 但存在 SKIP（SKIP 不算通过）"},
        "skip_note": "SKIP 不算通过；退出码 0 仅代表全部验收组通过",
        "skip_reason": missing or None,
        "thresholds": {"recognition_min": RECOGNITION_MIN, "max_len": MAX_LEN,
                       "draw_n": DRAW_N, "dedup_min": DEDUP_MIN,
                       "banned_words": BANNED},
        "corpus": {"positive_n": len(POSITIVES), "negative_n": len(NEGATIVES),
                   "positives": POSITIVES, "negatives": NEGATIVES},
        "summary": summary,
        "groups": groups,
        "recognition_items": rec_group["items"],
        "denial_samples": samples,
        "denial_all_texts_count": len(collected["all_texts"]),
        "denial_call_errors": collected["errors"][:5],
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 66)
    print("AI 质疑场景验收  tools/eval_scenarios.py")
    print("生成时间：%s   输出：%s" % (report["generated_at"], OUT.name))
    print("=" * 66)
    if missing:
        print("[SKIP] 依赖接口未就绪：%s" % missing)
        print("       SKIP 不算通过，本脚本退出码 2。")
    print("语料：正例 %d 条 / 反例 %d 条" % (len(POSITIVES), len(NEGATIVES)))
    print("-" * 66)
    for g in groups:
        tag = {STATUS_PASS: "[PASS]", STATUS_FAIL: "[FAIL]",
               STATUS_SKIP: "[SKIP]"}[g["status"]]
        print("%s %-16s %s" % (tag, g["name"], g["detail"]))
    print("-" * 66)
    if samples:
        print("否认回复样例 %d 条：" % len(samples))
        for i, s in enumerate(samples, 1):
            print("  %2d. %s" % (i, s))
    else:
        print("否认回复样例：未生成（接口 SKIP 或回复池为空）")
    print("-" * 66)
    print("汇总：PASS %d / FAIL %d / SKIP %d（共 %d 组）"
          % (summary["pass"], summary["fail"], summary["skip"], summary["total"]))
    print("总判定：%s    退出码：%d —— %s"
          % (overall.upper(), code, report["exit_code_meaning"][str(code)]))
    if overall == STATUS_SKIP:
        print("注意：SKIP 不算通过，退出码 2 表示本轮未验收完成。")
    print("=" * 66)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
