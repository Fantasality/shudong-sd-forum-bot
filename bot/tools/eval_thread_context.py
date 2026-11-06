#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""楼中楼上下文只读验收（tools/eval_thread_context.py）。

本轮新增能力：楼中楼回复必须同时看到
    ① 本楼原帖全文  ② 被回复的那条评论  ③ 楼里之前的 2-3 条楼中楼（含 replyTo）

本脚本本地造一条假楼，调用线上同一个 prompt 拼装函数
szlike.tasks.build_reply_prompt（名字按源码核对，不猜），打印最终 prompt，
逐项断言三样东西都在、顺序正确、原帖没被截成几十字、
且没有把 comment.content（一级评论）错填成「原帖」。

全程只读本地文件，不发网络请求、不调 LLM：
构造 Ctx 时关掉 kb / rag，避免 _with_kb 里的 embedding / rerank 联网。

用法：  python tools/eval_thread_context.py
输出：  tools/thread_context_last.json
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
BOT = HERE.parent
sys.path.insert(0, str(BOT))
OUT = HERE / "thread_context_last.json"
CONFIG = BOT / "config.json"

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

STATUS_PASS, STATUS_FAIL, STATUS_SKIP = "pass", "fail", "skip"

# ---------------------------------------------------------------- 假楼数据
# 三段文本各有独特句，用来区分「原帖 / 被回复评论 / 一级评论」，
# 也用来验证有没有把 comment.content 错填进 {post}。
POST_TAIL = "最后我把伞落在二期教学楼门口了"
POST = (
    "昨天下午四点多从图书馆出来，天忽然下大了，没带伞，"
    "在门口等了快二十分钟才等到雨小一点。本来想去一期食堂吃碗面，"
    "结果绕到那家奶茶店发现卷帘门拉着，贴了张纸说设备检修停两天。"
    "旁边水果摊阿姨说这学期那边换过两回老板了，口味一次不如一次，"
    "还不如去食堂二楼的窗口。回来路上鞋全湿了，袜子能拧出水，"
    "宿舍楼下的自动售货机还吞了我三块钱没出货，找宿管登记了半天。"
    + POST_TAIL + "，今天还得回去拿，顺便看看那家店到底开不开。"
)

THEIR_TAG = "我上周去扑了个空"
THEIR = "那家店还开吗？" + THEIR_TAG + "，白跑一趟，有没有人知道具体几点营业"

COMMENT_TAG = "这家奶茶确实一般"
# 这是「一级评论」的内容，模拟历史上被错填进 {post} 的那个值：
# 它绝不该出现在【原帖】段里。
COMMENT_CONTENT = COMMENT_TAG + "，不如去食堂，价格还便宜一半"

THREAD = [
    {"replyTo": "c_01", "content": "东门那家好像周三就关了，别白跑"},
    {"replyTo": "c_01", "content": "楼上说的是二期那家吧，一期还开着"},
    {"replyTo": "r_01", "content": "我前天刚去过，营业到晚上九点"},
]
THREAD_TAGS = [x["content"] for x in THREAD]

M_POST = "【原帖】"
M_THEIR = "【对方说】"
M_THREAD = "【楼里已经说过的】"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _thread_txt() -> str:
    """仿线上 reply_comments._build_reply 的拼法，replyTo 一并带进上下文。"""
    lines = ["- %s（replyTo: %s）" % (x["content"], x["replyTo"]) for x in THREAD]
    return ("\n\n" + M_THREAD + "\n" + "\n".join(lines) +
            "\n别再重复上面的意思，也别问他们说过的信息。")


# ---------------------------------------------------------------- Ctx 构造
def make_ctx():
    """只读构造一个仅用于拼 prompt 的 Ctx；关掉 kb/rag 以免联网重排。"""
    from szlike import tasks as t
    cfg = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
    cfg2 = dict(cfg)
    cfg2["school_id"] = cfg.get("school_id") or "eval"
    cfg2["_account"] = {"name": "eval_thread", "persona": "warm",
                        "campus": "", "profile": ""}
    cfg2["kb"] = {"enabled": False}
    cfg2["rag"] = {"enabled": False}
    return t, t.Ctx(client=None, store=None, brain=None, guard=None,
                    cfg=cfg2, manual=True)


def _check(name, status, detail, **metrics):
    d = {"name": name, "status": status, "detail": detail}
    if metrics:
        d["metrics"] = metrics
    return d


def _slice(user: str, start: str, end: str | None) -> str:
    """取 user 里两个 marker 之间的文本；end 为 None 时取到末尾。"""
    i = user.find(start)
    if i < 0:
        return ""
    j = user.find(end, i + len(start)) if end else len(user)
    return user[i + len(start): j if j >= 0 else len(user)]


# ---------------------------------------------------------------- 主流程
def main() -> int:
    checks = []
    prompt = ""
    system = ""
    user = ""
    skip_reason = ""

    # 1) 拼装函数可用性（并行改造中可能暂时不可用 -> SKIP）
    try:
        t, ctx = make_ctx()
        build = getattr(t, "build_reply_prompt", None)
        if not callable(build):
            raise ImportError("szlike.tasks.build_reply_prompt 不存在")
        checks.append(_check("api_available", STATUS_PASS,
                             "已定位线上拼装函数 szlike.tasks.build_reply_prompt"))
    except Exception as exc:
        skip_reason = "拼装函数暂时不可用（并行改造中）：%s" % exc
        checks.append(_check("api_available", STATUS_SKIP, skip_reason))

    # 2) 拼装并打印最终 prompt
    if not skip_reason:
        try:
            r = build(ctx, POST, THEIR, _thread_txt(), "c_01")
            system = r.get("system") or ""
            user = r.get("user") or ""
            prompt = system + "\n\n----- USER -----\n" + user
            if not r.get("enabled", True):
                skip_reason = "reply prompt enabled=False，无法验收上下文"
                checks.append(_check("prompt_built", STATUS_SKIP, skip_reason))
            else:
                checks.append(_check("prompt_built", STATUS_PASS,
                                     "build_reply_prompt 调用成功，system %d 字 / user %d 字"
                                     % (len(system), len(user))))
        except Exception as exc:
            skip_reason = "调用 build_reply_prompt 异常（可能在改造中）：%s" % exc
            checks.append(_check("prompt_built", STATUS_SKIP, skip_reason))

    print("=" * 70)
    print("楼中楼上下文验收  tools/eval_thread_context.py")
    print("生成时间：%s   输出：%s" % (_now(), OUT.name))
    print("=" * 70)
    print("【构造的假楼】")
    print("  原帖全文（%d 字）：%s" % (len(POST), POST))
    print("  被回复评论（%d 字）：%s" % (len(THEIR), THEIR))
    print("  模拟错填值 comment.content（%d 字）：%s"
          % (len(COMMENT_CONTENT), COMMENT_CONTENT))
    print("  楼里之前的楼中楼 %d 条：" % len(THREAD))
    for x in THREAD:
        print("    - %s（replyTo: %s）" % (x["content"], x["replyTo"]))
    print("=" * 70)
    print("【最终 prompt】")
    print(prompt if prompt else "（未生成：%s）" % (skip_reason or "SKIP"))
    print("=" * 70)

    # 3) 上下文断言（只在未 SKIP 时执行）
    if skip_reason:
        existing = {c["name"] for c in checks}
        for name in ("prompt_built", "post_full_present", "post_not_truncated",
                     "their_present", "thread_all_present", "reply_to_present",
                     "order_correct", "no_comment_as_post"):
            if name not in existing:
                checks.append(_check(name, STATUS_SKIP,
                                     "因上游 SKIP 未执行：%s" % skip_reason))
    else:
        # ① 原帖全文
        checks.append(_check(
            "post_full_present",
            STATUS_PASS if POST in prompt else STATUS_FAIL,
            "原帖全文 %d 字在最终 prompt 中%s" % (len(POST), "" if POST in prompt else "：未找到"),
            post_len=len(POST)))

        # ② 原帖没被截成几十字
        post_seg = _slice(user, M_POST, M_THEIR)
        ok = POST_TAIL in post_seg and len(post_seg) >= len(POST) * 0.9 and len(post_seg) > 60
        checks.append(_check(
            "post_not_truncated",
            STATUS_PASS if ok else STATUS_FAIL,
            "【原帖】段 %d 字（原文 %d 字），尾部独特句%s，判定阈值 >60 字且 ≥90%% 原文长度"
            % (len(post_seg), len(POST), "在" if POST_TAIL in post_seg else "不在"),
            post_segment_len=len(post_seg), post_original_len=len(POST)))

        # ③ 被回复的那条评论
        checks.append(_check(
            "their_present",
            STATUS_PASS if THEIR in prompt else STATUS_FAIL,
            "被回复评论 %d 字在最终 prompt 中%s"
            % (len(THEIR), "" if THEIR in prompt else "：未找到"),
            their_len=len(THEIR)))

        # ④ 楼里之前的 2-3 条楼中楼
        missing_lines = [x for x in THREAD_TAGS if x not in prompt]
        checks.append(_check(
            "thread_all_present",
            STATUS_PASS if not missing_lines else STATUS_FAIL,
            "楼中楼 %d 条，命中 %d 条%s"
            % (len(THREAD), len(THREAD) - len(missing_lines),
               "" if not missing_lines else "；缺失：%s" % "、".join(missing_lines)),
            thread_n=len(THREAD), missing=missing_lines))

        # ⑤ replyTo
        missing_rt = [x["replyTo"] for x in THREAD if x["replyTo"] not in prompt]
        checks.append(_check(
            "reply_to_present",
            STATUS_PASS if not missing_rt else STATUS_FAIL,
            "replyTo %d 个，命中 %d 个%s"
            % (len(THREAD), len(THREAD) - len(missing_rt),
               "" if not missing_rt else "；缺失：%s" % "、".join(missing_rt)),
            reply_to_n=len(THREAD), missing=missing_rt))

        # ⑥ 顺序：原帖 < 对方说 < 楼里已说过（在 user 里查，避开 system 里的措辞）
        pos = [user.find(M_POST), user.find(M_THEIR), user.find(M_THREAD)]
        thread_pos = [user.find(x) for x in THREAD_TAGS]
        order_ok = (all(p >= 0 for p in pos) and pos[0] < pos[1] < pos[2]
                    and all(p >= 0 for p in thread_pos)
                    and thread_pos == sorted(thread_pos))
        checks.append(_check(
            "order_correct",
            STATUS_PASS if order_ok else STATUS_FAIL,
            "user 内 marker 位置 原帖=%d / 对方说=%d / 楼里已说过=%d；楼中楼三条位置=%s"
            % (pos[0], pos[1], pos[2], thread_pos),
            marker_positions=pos, thread_positions=thread_pos))

        # ⑦ 没有把 comment.content 错填成 post
        their_seg = _slice(user, M_THEIR, M_THREAD)
        wrongly_filled = (COMMENT_TAG in post_seg or THEIR_TAG in post_seg)
        their_ok = THEIR_TAG in their_seg
        clean = (not wrongly_filled) and their_ok and POST_TAIL in post_seg
        checks.append(_check(
            "no_comment_as_post",
            STATUS_PASS if clean else STATUS_FAIL,
            "【原帖】段=原帖正文（尾部独特句%s，未混入一级评论/被回复评论=%s）；"
            "【对方说】段含被回复评论=%s"
            % ("在" if POST_TAIL in post_seg else "不在",
               "否" if not wrongly_filled else "是", "是" if their_ok else "否"),
            wrongly_filled=wrongly_filled, their_in_their_segment=their_ok))

    summary = {"pass": sum(1 for c in checks if c["status"] == STATUS_PASS),
               "fail": sum(1 for c in checks if c["status"] == STATUS_FAIL),
               "skip": sum(1 for c in checks if c["status"] == STATUS_SKIP),
               "total": len(checks)}
    if summary["fail"]:
        overall, code = STATUS_FAIL, 1
    elif summary["skip"] or skip_reason:
        overall, code = STATUS_SKIP, 2
    else:
        overall, code = STATUS_PASS, 0

    report = {
        "generated_at": _now(),
        "overall": overall,
        "exit_code": code,
        "exit_code_meaning": {"0": "全部通过", "1": "存在 FAIL",
                              "2": "无 FAIL 但存在 SKIP（SKIP 不算通过）"},
        "skip_note": "SKIP 不算通过；上游拼装函数不可用时整轮标记 SKIP",
        "skip_reason": skip_reason or None,
        "prompt_function": "szlike.tasks.build_reply_prompt",
        "fixture": {"post": POST, "their": THEIR,
                    "comment_content_should_not_be_post": COMMENT_CONTENT,
                    "thread": THREAD},
        "prompt": prompt,
        "prompt_len": len(prompt),
        "system_len": len(system), "user_len": len(user),
        "summary": summary,
        "checks": checks,
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("【逐项断言】")
    for c in checks:
        tag = {STATUS_PASS: "[PASS]", STATUS_FAIL: "[FAIL]",
               STATUS_SKIP: "[SKIP]"}[c["status"]]
        print("%s %-22s %s" % (tag, c["name"], c["detail"]))
    print("-" * 70)
    print("汇总：PASS %d / FAIL %d / SKIP %d（共 %d 项）"
          % (summary["pass"], summary["fail"], summary["skip"], summary["total"]))
    print("总判定：%s    退出码：%d" % (overall.upper(), code))
    if overall == STATUS_SKIP:
        print("注意：SKIP 不算通过，退出码 2 表示本轮未验收完成。")
    print("=" * 70)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
