"""拟人化「考试」：一键跑当前模型 + 组装答卷 + 导出（试卷 / 答卷 / 对比 / 成绩单）。

设计要点
--------
- 只通过 ``subprocess`` 调 ``eval_run.py``，不改动它、不改动 config.json。
- 临时模型（base / key / model）经 **环境变量** 传给子进程：
  ``SZLIKE_LLM_BASE`` / ``SZLIKE_LLM_KEY`` / ``SZLIKE_LLM_MODEL``。
  key 只存在于父进程内存 + 子进程环境，绝不落盘、绝不进日志、绝不进结果 JSON。
- 逐题答卷直接读 ``tools/exam_answers.json``（eval_run.py 每次考试结束自动写，
  含全部 N 题，不再是 samples 前 10-12 条）：
    * 人类标准答案 = ``human_answers``（真人原句）；
    * AI 答案 = ``ai_answer``；
    * 图灵判定 = ``turing_caught`` / ``turing_votes``（**逐题真实值**）；
    * post 模式的 ``human_answers`` 是 4 条真实帖子作**风格参照**，
      ``human_answers_kind`` 有说明，导出时一律标注「非唯一标准答案」。
    * reply 模式另有 ``their``（对方那句话）。
- 汇总分数（total / 各分项）来自 ``eval_last.json``（只存汇总）；逐题分项无数据，标「无」。
- ``--from`` 可直接读一份已有答卷文件导出，不重跑考试；此时汇总分项标「无」，
  图灵得分 / 被抓率由逐题答卷现算。

自测（不调用 LLM）::

    python tools/exam.py --mock --export /tmp/exam_mock.zip
    python tools/exam.py --from tools/exam_answers_sample.json --export /tmp/x.zip
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import html
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BOT = HERE.parent
EVAL_RUN = HERE / "eval_run.py"
EVAL_LAST = HERE / "eval_last.json"                 # 汇总分数（只存汇总）
ANSWERS_FILE = HERE / "exam_answers.json"           # eval_run.py 每次考试写的逐题答卷（优先）
TESTSET = HERE / "testset.json"                     # 仅 fallback 用
REPLYSET = HERE / "replyset.json"                   # 仅 fallback 用

MISSING = "无"
REF_MARK = "【风格参照·非唯一标准答案】"

# fallback 时 post 模式按位置补的板块（与 eval_run.run_post 的轮转表一致）
POST_BOARDS = ["抛抛帖子", "今天不开心", "求助提问箱", "今天吃什么", "苏大迷惑大赏",
               "苏大恋爱日常", "打工电波", "新生专栏", "二手交易", "失物招领"]

# 任何命名字段都不会被写进导出；同时把这些字段里的字符串值当作「秘密」做全局脱敏。
SENSITIVE_KEYS = {
    "api_key", "key", "password", "passwd", "token", "secret",
    "llm_override", "access_key", "sk", "authorization", "credential",
    "_secrets", "_raw_key",
}

EXTS = {"json": ".json", "csv": ".csv", "md": ".md", "zip": ".zip"}
CTYPES = {
    "json": "application/json; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "zip": "application/zip",
}

CANARY = "sk-CANARY-DO-NOT-LEAK-9f3a"          # 自测用假 key，验证导出绝不泄漏

# 字节级兜底：model_input 等「原样导出」的文本可能意外夹带密钥形态的内容，
# 而它并不在任何 SENSITIVE_KEYS 字段里、不会进入 secrets 清单。这里直接按形态抹掉：
#   1) sk-xxx 前缀的厂商密钥值；2) api_key/token/password=xxx 之类的赋值片段。
_SECRET_TOKEN_RE = re.compile(
    rb"sk-[A-Za-z0-9_\-]{6,}"
    rb"|\b(?:api[_-]?key|password|passwd|token|secret|authorization|access[_-]?key)"
    rb"\s*[:=]\s*[\"']?[A-Za-z0-9_\-\./]{4,}[\"']?",
    re.IGNORECASE,
)


# ---------------------------------------------------------------- 工具
def _now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _load_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _fmt_from_path(path: str) -> str | None:
    ext = Path(path or "").suffix.lower().lstrip(".")
    return ext if ext in EXTS else None


def _scrub_collect(obj, out: list) -> None:
    """收集所有敏感字段里的字符串值，作为全局脱敏用的秘密清单。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in SENSITIVE_KEYS:
                if isinstance(v, str) and v:
                    out.append(v)
                elif isinstance(v, (list, tuple)):
                    for x in v:
                        if isinstance(x, str) and x:
                            out.append(x)
            else:
                _scrub_collect(v, out)
    elif isinstance(obj, (list, tuple)):
        for x in obj:
            _scrub_collect(x, out)


def _scrub(obj, secrets: list):
    """删掉敏感字段，并把已知秘密值在任意字符串里替换为 ***。"""
    if isinstance(obj, dict):
        return {k: _scrub(v, secrets) for k, v in obj.items()
                if str(k).lower() not in SENSITIVE_KEYS}
    if isinstance(obj, (list, tuple)):
        return [_scrub(x, secrets) for x in obj]
    if isinstance(obj, str):
        for s in secrets:
            if s:
                obj = obj.replace(s, "***")
        return obj
    return obj


def _finalize_bytes(data: bytes, secrets: list) -> bytes:
    """最后一道保险：即便前面的白名单拼装漏了，也不让秘密值出现在字节里。

    先按 secrets 清单做值级替换（原三重脱敏不变），再按形态抹掉 model_input
    等原文里意外夹带的密钥片段 —— 两者叠加，保证导出字节里没有 sk- 等凭据。
    """
    for s in secrets:
        if s:
            b = s.encode("utf-8")
            if b in data:
                data = data.replace(b, b"***")
    return _SECRET_TOKEN_RE.sub(b"***", data)


def _safe(result: dict):
    """返回 (脱敏后的干净结果, 秘密清单)。"""
    secrets: list = []
    _scrub_collect(result, secrets)
    return _scrub(result, secrets), secrets


def sanitize(result: dict) -> dict:
    """公开脱敏入口：删掉敏感字段、抹掉已知秘密值，供服务端存储/回包前调用。"""
    return _safe(result or {})[0]


def export_filename(result: dict, fmt: str = "zip") -> str:
    """ASCII 文件名，避免 Content-Disposition 的编码坑。"""
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    eid = str((result or {}).get("exam_id") or "exam")
    eid = "".join(c for c in eid if c.isalnum() or c in "-_")[:16] or "exam"
    return f"exam_{ts}_{eid}{EXTS.get(fmt, '.txt')}"


def _human_text(v) -> str:
    if isinstance(v, (list, tuple)):
        return " | ".join(str(x) for x in v)
    return str(v if v is not None else MISSING)


def _norm_model_input(v):
    """把可选的 model_input 归一化成 {system, user}；字段缺失/为空/非法时返回 None。

    约定：源答卷里逐题可选字段 ``model_input = {"system": ..., "user": ...}``。
    有内容才返回，绝不给没有该字段的题塞空字符串或「无」占位。
    """
    if isinstance(v, dict) and (v.get("system") or v.get("user")):
        return {"system": str(v.get("system") or ""),
                "user": str(v.get("user") or "")}
    return None


def _md_cell(v, limit: int = 160) -> str:
    s = _human_text(v).replace("|", "\\|").replace("\r", " ").replace("\n", "<br>")
    return (s[:limit] + "…") if len(s) > limit else (s or MISSING)


def _meta_from_payload(payload: dict, **extra) -> dict:
    """从 exam_answers.json 头部取非敏感元信息（绝不取 key）。"""
    m = {
        "model": payload.get("model") or MISSING,
        "base_url": payload.get("base_url") or MISSING,
        "seed": payload.get("seed", MISSING),
        "candidates": payload.get("candidates", MISSING),
        "n": payload.get("n", MISSING),
        "exam_time": payload.get("generated_at", MISSING),
    }
    m.update(extra)
    return m


# ---------------------------------------------------------------- 组装答卷
def _verdict(caught, votes) -> str:
    """把逐题 turing_caught / turing_votes 转成可读判定；0 票 = 无有效判定。"""
    if not isinstance(votes, int) or votes <= 0:
        return MISSING
    c = caught if isinstance(caught, int) else 0
    if c <= 0:
        return f"未露馅（被抓 0/{votes}）"
    if c >= votes:
        return f"露馅（被抓 {c}/{votes}）"
    return f"部分露馅（被抓 {c}/{votes}）"


def _mode_scores(r: dict) -> dict:
    """把 eval_last.json 的汇总分项整理成固定 schema（便于表格渲染）。"""
    parts = {
        "turing": {"label": "图灵测试(40)", "v": r.get("turing", MISSING)},
        "mid": {"label": "分布匹配(25)" if "dist" in r else "题材分布(25)",
                "v": r.get("dist", r.get("topic", MISSING))},
        "third": {"label": "同质化(20)" if "dup" in r else "形式(20)",
                  "v": r.get("dup", r.get("form", MISSING))},
        "flavor": {"label": "AI味(15)", "v": r.get("flavor", MISSING)},
    }
    return {
        "total": r.get("total", MISSING),
        "turing": r.get("turing", MISSING),
        "catch_rate": r.get("catch_rate", MISSING),
        "n": r.get("n", MISSING),
        "parts": parts,
        "source": "eval_last.json（汇总）",
    }


def _scores_from_answers(questions: list[dict]) -> dict:
    """--from 直接导出时用：图灵得分/被抓率由逐题答卷现算，分项标「无」。"""
    by: dict = {}
    for q in questions:
        m = q.get("mode") or "unknown"
        d = by.setdefault(m, {"caught": 0, "votes": 0, "n": 0})
        d["n"] += 1
        c, v = q.get("turing_caught"), q.get("turing_votes")
        if isinstance(c, int) and isinstance(v, int) and v > 0:
            d["caught"] += c
            d["votes"] += v
    out = {}
    for m, d in by.items():
        if d["votes"] > 0:
            rate = d["caught"] / d["votes"]
            turing, cr = round((1 - rate) * 40, 1), round(rate, 3)
        else:
            turing, cr = MISSING, MISSING
        out[m] = {
            "total": MISSING,          # 缺分布/同质/AI味分项，无法重算总分
            "turing": turing,
            "catch_rate": cr,
            "n": d["n"],
            "parts": {
                "turing": {"label": "图灵测试(40)", "v": turing},
                "mid": {"label": "分布/题材(25)", "v": MISSING},
                "third": {"label": "同质/形式(20)", "v": MISSING},
                "flavor": {"label": "AI味(15)", "v": MISSING},
            },
            "source": "由逐题答卷汇总（分布/同质/AI味分项需 eval_last.json，故标「无」）",
        }
    return out


def assemble_exam(answers_payload: dict | None = None,
                  eval_last: dict | None = None, meta: dict | None = None,
                  testset: dict | None = None, replyset: dict | None = None) -> dict:
    """优先从 ``exam_answers.json``（逐题，全量 260 题）组装。

    - ``answers_payload`` 给了就直接用（供 ``--from`` 读任意答卷文件）；
    - 没给则自动读 ``tools/exam_answers.json``；
    - 该文件也不存在才回退到旧的 ``eval_last.json`` samples + testset/replyset
      按位置拼接，并在结果里标注 ``source: "fallback"``（此路径只有前 10-12 条
      samples、无逐题图灵判定，属降级行为，会在结果里写明）。
    """
    meta = dict(meta or {})
    if answers_payload is None and ANSWERS_FILE.exists():
        try:
            answers_payload = _load_json(ANSWERS_FILE)
            meta.setdefault("source", "exam_answers.json（assemble_exam 自动读取）")
        except Exception:
            answers_payload = None
    if answers_payload is None:
        return _assemble_fallback(eval_last, testset, replyset, meta)

    exam_id = meta.pop("exam_id", None) or "exam"
    answers = (answers_payload or {}).get("answers") or []

    questions: list[dict] = []
    for i, a in enumerate(answers, 1):
        human = a.get("human_answers")
        if isinstance(human, list):
            human = [str(x).strip() for x in human if str(x).strip()]
            if not human:
                human = MISSING
        elif human is None or human == "":
            human = MISSING
        else:
            human = str(human)

        kind_text = str(a.get("human_answers_kind") or "")
        is_ref = bool(kind_text)
        caught = a.get("turing_caught", MISSING)
        votes = a.get("turing_votes", MISSING)
        note = (REF_MARK + (kind_text or "真实帖子作风格参照")) if is_ref else ""

        q = {
            "qid": i,
            "mode": str(a.get("mode") or MISSING),
            "board": str(a.get("board") or MISSING),
            "question": str(a.get("question") or MISSING),
            "their": str(a.get("their") or ""),          # reply 的对方原句
            "human": human,                              # 人类标准答案 / 风格参照（list 或 "无"）
            "human_kind": "reference" if is_ref else "standard",
            "human_kind_text": kind_text,
            "ai": str(a.get("ai_answer") or MISSING),
            "turing_caught": caught,                     # 逐题真实值
            "turing_votes": votes,
            "verdict": _verdict(caught, votes),
            "subscore": MISSING,                         # 逐题分项无（只有汇总级）
            "note": note,
        }
        # 模型原输入为可选字段：题目带就带进答卷结构，没有就整个省略，不塞占位。
        mi = (_norm_model_input(a.get("model_input"))
              or _norm_model_input(a.get("模型原输入")))
        if mi:
            q["model_input"] = mi
        questions.append(q)

    scores: dict = {}
    if eval_last:
        for mode in ("comment", "post", "reply"):
            r = (eval_last or {}).get(mode)
            if isinstance(r, dict):
                scores[mode] = _mode_scores(r)
    for m, v in _scores_from_answers(questions).items():
        scores.setdefault(m, v)

    totals = [scores[m]["total"] for m in scores
              if isinstance(scores[m].get("total"), (int, float))]
    return {
        "exam_id": exam_id,
        "generated_at": meta.get("exam_time") or _now(),
        "meta": meta,                          # model/base_url/seed/candidates，绝不含 key
        "scores": scores,
        "questions": questions,
        "summary": {
            "modes": list(scores.keys()),
            "question_count": len(questions),
            "avg_total": round(sum(totals) / len(totals), 1) if totals else MISSING,
            "caught_total": sum(q["turing_caught"] for q in questions
                                if isinstance(q["turing_caught"], int)),
            "votes_total": sum(q["turing_votes"] for q in questions
                               if isinstance(q["turing_votes"], int)),
            "note": "逐题图灵判定来自 exam_answers.json（turing_caught/turing_votes，真实值）；"
                    "分项得分为汇总级（eval_last.json），逐题分项标「无」，不编造。",
        },
    }


def _assemble_fallback(eval_last, testset, replyset, meta) -> dict:
    """降级路径：exam_answers.json 不存在时，用 eval_last.samples + 考卷 cases 按位置拼。

    只有各模式前 10-12 条 samples，且无逐题图灵判定 / 逐题分项 —— 如实标「无」，
    并在 meta.source = "fallback" 里写明，绝不伪装成全量答卷。
    """
    meta = dict(meta or {})
    meta["source"] = "fallback"
    meta["fallback_reason"] = ("exam_answers.json 不存在，改用 eval_last.json samples "
                               "+ testset.json/replyset.json 按位置拼；samples 只存前 10-12 条，"
                               "逐题图灵判定/分项无数据，均标「无」")
    exam_id = meta.pop("exam_id", None) or "exam"
    if testset is None and TESTSET.exists():
        try:
            testset = _load_json(TESTSET)
        except Exception:
            testset = {}
    if replyset is None and REPLYSET.exists():
        try:
            replyset = _load_json(REPLYSET)
        except Exception:
            replyset = {}
    eval_last = eval_last or {}
    test_cases = (testset or {}).get("cases") or []
    reply_cases = (replyset or {}).get("cases") or []

    questions: list[dict] = []
    scores: dict = {}
    qid = 0
    for mode in ("comment", "post", "reply"):
        r = eval_last.get(mode)
        if not isinstance(r, dict):
            continue
        scores[mode] = _mode_scores(r)
        for i, ai in enumerate(r.get("samples") or []):
            qid += 1
            if mode == "comment":
                c = test_cases[i] if i < len(test_cases) else {}
                question = str(c.get("post") or MISSING)
                board = str(c.get("board") or MISSING)
                human = [str(x.get("text", "")).strip()
                         for x in (c.get("real_comments") or []) if str(x.get("text", "")).strip()]
                human = human or MISSING
                kind_text, is_ref, their = "", False, ""
            elif mode == "reply":
                c = reply_cases[i] if i < len(reply_cases) else {}
                question = str(c.get("post") or MISSING)
                their = str(c.get("their") or "")
                board = str(c.get("board") or MISSING)
                human = [str(x).strip() for x in (c.get("real_replies") or []) if str(x).strip()]
                human = human or MISSING
                kind_text, is_ref = "", False
            else:  # post
                board = POST_BOARDS[i % len(POST_BOARDS)] if POST_BOARDS else MISSING
                question = "（开放式发帖：按板块生成）板块：" + board
                human, their = MISSING, ""
                kind_text = "真实帖子作风格参照（开放式发帖无唯一标准答案）"
                is_ref = True
            questions.append({
                "qid": qid, "mode": mode, "board": board, "question": question,
                "their": their, "human": human,
                "human_kind": "reference" if is_ref else "standard",
                "human_kind_text": kind_text,
                "ai": str(ai) if ai else MISSING,
                "turing_caught": MISSING, "turing_votes": MISSING,
                "verdict": MISSING, "subscore": MISSING,
                "note": (REF_MARK + kind_text) if is_ref else "",
            })
    return {
        "exam_id": exam_id,
        "generated_at": _now(),
        "meta": meta,
        "scores": scores or _scores_from_answers(questions),
        "questions": questions,
        "summary": {
            "modes": list(scores.keys()),
            "question_count": len(questions),
            "avg_total": MISSING, "caught_total": 0, "votes_total": 0,
            "note": "降级路径（source=fallback）：只有 samples 前 10-12 条，"
                    "逐题图灵判定/分项无数据，标「无」。请升级 eval_run.py 让它落盘 exam_answers.json。",
        },
    }


def result_from_answers_file(path, eval_last_path=None) -> dict | None:
    """读一份已有 exam_answers*.json 组装 result（不重跑考试）。

    供 ``--from`` CLI 与 WebUI「导出上次答卷」在服务重启后兜底使用。
    默认不带 eval_last（图灵得分由逐题现算、分项标「无」），避免混入别的轮次汇总。
    """
    p = Path(path)
    if not p.exists():
        return None
    payload = _load_json(p)
    eval_last = None
    if eval_last_path and Path(eval_last_path).exists():
        try:
            eval_last = _load_json(Path(eval_last_path))
        except Exception:
            eval_last = None
    meta = _meta_from_payload(
        payload, mode="from_file",
        source=f"--from {p.name}（直接读取已有答卷，未重跑考试）")
    return sanitize(assemble_exam(answers_payload=payload,
                                  eval_last=eval_last, meta=meta))


# ---------------------------------------------------------------- 跑考试
def run_exam(mode: str = "all", n: int = 0, candidates: int = 1,
             llm_override: dict | None = None, progress=None) -> dict:
    """后台执行入口。

    ``llm_override`` 支持 ``{base|base_url, key|api_key, model}``；
    ``progress(pct:int, text:str)`` 为可选进度回调。
    返回组装好的 result（**绝不含 key**）。
    """
    def tick(pct, text):
        if callable(progress):
            try:
                progress(int(pct), str(text))
            except Exception:
                pass

    ov = dict(llm_override or {})
    base = str(ov.get("base") or ov.get("base_url") or "")
    key = str(ov.get("key") or ov.get("api_key") or "")
    model = str(ov.get("model") or "")

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    if base:
        env["SZLIKE_LLM_BASE"] = base
    if key:
        env["SZLIKE_LLM_KEY"] = key          # 只在子进程环境，绝不落盘/日志/结果
    if model:
        env["SZLIKE_LLM_MODEL"] = model

    cmd = [sys.executable, str(EVAL_RUN),
           "--mode", str(mode), "--n", str(int(n)),
           "--candidates", str(int(candidates))]

    # 启动前删掉上一轮逐题答卷，避免本轮失败时导出到陈旧数据
    try:
        ANSWERS_FILE.unlink(missing_ok=True)
    except OSError as e:
        tick(4, f"清理旧答卷失败（忽略）：{e}")

    tick(5, "启动评测子进程 eval_run.py")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    proc = subprocess.Popen(
        cmd, cwd=str(BOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        creationflags=flags, bufsize=1,
    )

    pct = 8
    lines: list[str] = []
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.rstrip("\n")
        # 安全兜底：万一子进程把 key 回显出来，立刻抹掉
        if key and len(key) >= 8:
            line = line.replace(key, "***")
        lines.append(line)
        if line.strip():
            pct = min(90, pct + 1)
            tick(pct, line.strip()[:200])
    proc.wait()
    if proc.returncode != 0:
        tail = "\n".join(lines[-8:])[-500:]
        raise RuntimeError(f"eval_run.py 退出码 {proc.returncode}：{tail}")

    tick(92, "评测完成，读取逐题答卷…")
    # 优先 exam_answers.json；缺失时 assemble_exam 自动回退到 eval_last+testset 老路径
    # 并在结果里标注 source="fallback"，不再直接报错中断。
    def _read(p):
        try:
            return _load_json(p) if Path(p).exists() else None
        except Exception:
            return None
    answers_payload = _read(ANSWERS_FILE)
    eval_last = _read(EVAL_LAST)
    testset = _read(TESTSET)
    replyset = _read(REPLYSET)

    meta = _meta_from_payload(
        answers_payload or {}, mode=str(mode), requested_n=int(n),
        source=("exam_answers.json（逐题） + eval_last.json（汇总）"
                if answers_payload else "fallback"))
    if not answers_payload:
        tick(93, "exam_answers.json 缺失，降级到 eval_last+testset 旧路径（source=fallback）")
    result = assemble_exam(answers_payload=answers_payload, eval_last=eval_last,
                           testset=testset, replyset=replyset, meta=meta)

    # 最后一道保险：结果里绝不出现 key / 任何敏感字段
    secrets: list = []
    _scrub_collect(result, secrets)
    if key:
        secrets.append(key)
    result = _scrub(result, secrets)
    tick(100, "完成")
    return result


# ---------------------------------------------------------------- 导出
def _question_for_export(q: dict) -> tuple[str, str]:
    """返回 (题目文本, 人类答案文本)，post 的风格参照会显式标注。"""
    qtext = q.get("question", MISSING)
    if q.get("their"):
        qtext = f"{qtext}\n【对方说】{q['their']}"
    human = _human_text(q.get("human", MISSING))
    if q.get("human_kind") == "reference":
        human = REF_MARK + human
    return qtext, human


def _q_public(q: dict) -> dict:
    """单题的公开字段（白名单，天然不含任何凭据）。"""
    d = {
        "题号": q.get("qid", MISSING),
        "模式": q.get("mode", MISSING),
        "板块": q.get("board", MISSING),
        "试卷题目": q.get("question", MISSING),
        "对方原句": q.get("their", ""),
        "人类标准答案性质": ("风格参照（非唯一标准答案）"
                        if q.get("human_kind") == "reference" else "标准答案（真人原句）"),
        "人类标准答案": q.get("human", MISSING),
        "AI答案": q.get("ai", MISSING),
        "图灵被抓次数": q.get("turing_caught", MISSING),
        "投票数": q.get("turing_votes", MISSING),
        "图灵裁判判定": q.get("verdict", MISSING),
        "分项得分": q.get("subscore", MISSING),
        "备注": q.get("note", ""),
    }
    mi = _norm_model_input(q.get("model_input"))
    if mi:
        d["模型原输入"] = mi
    return d


def _exam_paper_json(clean: dict, secrets: list) -> bytes:
    """试卷.json：question + human_answers（不含 AI 答案/判定/分数/凭据）。"""
    qs = clean.get("questions") or []
    questions = []
    for q in qs:
        is_ref = q.get("human_kind") == "reference"
        item = {
            "题号": q.get("qid"),
            "模式": q.get("mode"),
            "板块": q.get("board"),
            "试卷题目": q.get("question", MISSING),
            "对方原句": q.get("their", ""),
            # human_answers 照常导出，用 human_answers_kind 原文注明性质
            "人类标准答案": q.get("human", MISSING),
        }
        if is_ref:
            item["人类标准答案性质"] = "真实帖子风格参照（开放式发帖无唯一标准答案）"
            item["human_answers_kind"] = (q.get("human_kind_text")
                                          or "真实帖子作风格参照，非唯一标准答案")
            item["风格参照答案"] = q.get("human", MISSING)   # 同一份内容另列，便于机读
            item["说明"] = "该行 human_answers 为风格参照，不是本题唯一标准答案"
        else:
            item["人类标准答案性质"] = "标准答案（真人原句）"
        questions.append(item)
    payload = {
        "说明": "试卷：题目（真实帖子正文 / 板块）与人类标准答案（真人原句）。全量 N 题，不截断。"
                "post 模式为开放式发帖，human_answers 照常导出，但每行用"
                "「人类标准答案性质 / human_answers_kind」注明是真实帖子风格参照、非唯一标准答案。"
                "不含 AI 答案、判定、分数，也不含任何密钥或账号凭据。",
        "exam_id": clean.get("exam_id"),
        "generated_at": clean.get("generated_at"),
        "questions": questions,
    }
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    return _finalize_bytes(data, secrets)


def _answer_sheet_json(clean: dict, secrets: list) -> bytes:
    """答卷.json：ai_answer + turing_* + 分数（不含凭据）；
    含 model_input 的题另附「模型原输入」{system, user}，其余题不带该键。
    """
    qs = clean.get("questions") or []
    answers = []
    for q in qs:
        item = {
            "题号": q.get("qid"), "模式": q.get("mode"),
            "AI答案": q.get("ai", MISSING),
            "图灵被抓次数": q.get("turing_caught", MISSING),
            "投票数": q.get("turing_votes", MISSING),
            "图灵裁判判定": q.get("verdict", MISSING),
            "分项得分": q.get("subscore", MISSING),
            "人类标准答案性质": ("风格参照（非唯一标准答案）"
                            if q.get("human_kind") == "reference" else "标准答案（真人原句）"),
            "备注": q.get("note", ""),
        }
        mi = _norm_model_input(q.get("model_input"))
        if mi:
            item["模型原输入"] = mi
        answers.append(item)
    payload = {
        "说明": "答卷：AI 答案、逐题图灵判定（turing_caught/turing_votes，来自 exam_answers.json）、"
                "汇总分数。逐题分项无数据标「无」。含 model_input 的题另附「模型原输入」"
                "(system/user，供调试向量用)。不含任何密钥或账号凭据。",
        "exam_id": clean.get("exam_id"),
        "generated_at": clean.get("generated_at"),
        "scores": clean.get("scores") or {},
        "answers": answers,
    }
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    return _finalize_bytes(data, secrets)


def _export_json(clean: dict, secrets: list) -> bytes:
    qs = clean.get("questions") or []
    payload = {
        "说明": "拟人化考试答卷（含人类标准答案 vs AI 答案逐题对比 + 逐题图灵判定）。"
                "不含任何密钥或账号凭据。",
        "exam_id": clean.get("exam_id"),
        "generated_at": clean.get("generated_at"),
        "meta": clean.get("meta") or {},
        "scores": clean.get("scores") or {},
        "summary": clean.get("summary") or {},
        "answers": [_q_public(q) for q in qs],
    }
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    return _finalize_bytes(data, secrets)


def _caught_cell(q: dict) -> str:
    """「是否被抓」列：turing_caught + "/" + turing_votes 真实值。"""
    c, v = q.get("turing_caught"), q.get("turing_votes")
    if isinstance(c, int) and isinstance(v, int):
        return f"{c}/{v}"
    return MISSING


def _export_csv(clean: dict, secrets: list) -> bytes:
    qs = clean.get("questions") or []            # 全量 N 题，不截断
    # 仅当本批次至少一题带 model_input 时才追加「模型原输入」列；
    # 全批次都没有时表头严格保持原有 6 列不变。
    has_mi = any(_norm_model_input(q.get("model_input")) for q in qs)
    buf = io.StringIO()
    w = csv.writer(buf)
    header = ["题号", "模式", "题目", "人类标准答案", "AI答案", "是否被抓"]
    if has_mi:
        header.append("模型原输入")
    w.writerow(header)
    for q in qs:
        qtext, human = _question_for_export(q)
        row = [
            q.get("qid", MISSING), q.get("mode", MISSING), qtext, human,
            q.get("ai", MISSING), _caught_cell(q),
        ]
        if has_mi:
            mi = _norm_model_input(q.get("model_input"))
            # system 与 user 用 ⏎ 拼成单个单元格；引号/逗号/换行交给 csv.writer 正确转义。
            row.append(f"{mi.get('system', '')} ⏎ {mi.get('user', '')}" if mi else "")
        w.writerow(row)
    data = ("\ufeff" + buf.getvalue()).encode("utf-8")   # BOM 方便 Excel 打开中文
    return _finalize_bytes(data, secrets)


def _export_md(clean: dict, secrets: list) -> bytes:
    scores = clean.get("scores") or {}
    meta = clean.get("meta") or {}
    qs = clean.get("questions") or []
    L: list[str] = []
    L.append("# 拟人化考试成绩单")
    L.append("")
    L.append(f"- 生成时间：{clean.get('generated_at', MISSING)}")
    L.append(f"- 考试编号：{clean.get('exam_id', MISSING)}")
    L.append(f"- 考试参数：模式 `{meta.get('mode', MISSING)}` · "
             f"样本数 `{meta.get('n', MISSING)}` · 候选数 `{meta.get('candidates', MISSING)}`"
             f" · 种子 `{meta.get('seed', MISSING)}`")
    L.append(f"- 当前模型：`{meta.get('model', MISSING)}` @ `{meta.get('base_url', MISSING)}`")
    L.append(f"- 数据来源：`{meta.get('source', MISSING)}`")
    L.append("")
    L.append("> 本成绩单及所有导出文件均不含密钥与账号凭据。")
    L.append("> 逐题图灵判定来自 exam_answers.json（turing_caught/turing_votes，真实值）；"
             "分项得分/总分为汇总级（eval_last.json）。")
    L.append("> post 模式为开放式发帖，无唯一标准答案：human_answers 是 4 条真实帖子作"
             "**风格参照**（非标准答案），已在试卷.json / 对比.csv 中明确标注。")
    L.append("")
    L.append("## 汇总表（满分 100）")
    L.append("")
    L.append("| 模式 | 总分 | 图灵(40) | 被抓率 | 分布/题材(25) | 同质/形式(20) | AI味(15) | 样本量 |")
    L.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for mode in ("comment", "post", "reply"):
        s = scores.get(mode)
        if not s:
            continue
        cr = s.get("catch_rate")
        crs = f"{cr * 100:.1f}%" if isinstance(cr, (int, float)) else MISSING
        p = s.get("parts") or {}
        L.append("| {m} | {t} | {a} | {cr} | {b} | {c} | {d} | {n} |".format(
            m=mode, t=s.get("total", MISSING),
            a=(p.get("turing") or {}).get("v", MISSING), cr=crs,
            b=(p.get("mid") or {}).get("v", MISSING),
            c=(p.get("third") or {}).get("v", MISSING),
            d=(p.get("flavor") or {}).get("v", MISSING), n=s.get("n", MISSING),
        ))
    L.append("")
    L.append("## 图灵测试对照表")
    L.append("")
    L.append("| 模式 | 图灵得分(40) | 被抓率 | 判定 |")
    L.append("| --- | --- | --- | --- |")
    for mode in ("comment", "post", "reply"):
        s = scores.get(mode)
        if not s:
            continue
        cr = s.get("catch_rate")
        crs = f"{cr * 100:.1f}%" if isinstance(cr, (int, float)) else MISSING
        if isinstance(cr, (int, float)):
            verdict = "通过（较难识破）" if cr < 0.25 else "待改进（易被识破）"
        else:
            verdict = MISSING
        L.append(f"| {mode} | {s.get('turing', MISSING)} | {crs} | {verdict} |")
    L.append("")
    L.append("> 被抓率越低越拟人；图灵得分 = (1 − 被抓率) × 40。")
    L.append("")
    L.append("## 逐题对比（人类标准答案 vs AI 答案）")
    L.append("")
    # 全量 N 题都在 对比.csv / 试卷.json / 答卷.json 里；md 只放前 50 条保持可读，
    # 不截断到旧版的 12 条。全量 N 题一个不少。
    md_n = min(50, len(qs))
    L.append(f"> 本表展示前 {md_n} / {len(qs)} 题；**全量 {len(qs)} 题**见 对比.csv / 试卷.json / 答卷.json。")
    L.append("")
    L.append("| 题号 | 模式 | 试卷题目 | 人类标准答案 | AI 答案 | 图灵判定 | 被抓/票 |")
    L.append("| --- | --- | --- | --- | --- | --- | --- |")
    for q in qs[:md_n]:
        qtext, human = _question_for_export(q)
        c, v = q.get("turing_caught", MISSING), q.get("turing_votes", MISSING)
        cv = f"{c}/{v}" if isinstance(c, int) and isinstance(v, int) else MISSING
        L.append("| {i} | {m} | {q} | {h} | {a} | {vd} | {cv} |".format(
            i=q.get("qid", MISSING), m=q.get("mode", MISSING),
            q=_md_cell(qtext), h=_md_cell(human),
            a=_md_cell(q.get("ai", MISSING)),
            vd=_md_cell(q.get("verdict", MISSING)), cv=cv,
        ))
    L.append("")
    L.append(f"> 全量 {len(qs)} 题（不截断）在 对比.csv / 试卷.json / 答卷.json；表格内长文本仅单元格截断。")
    L.append("> post 行「人类标准答案」列前缀【风格参照·非唯一标准答案】者为真实帖子风格参照。")
    L.append("")
    # 模型原输入：逐题用 <details> 折叠，避免原始 prompt 把正文撑爆（仅本表展示范围内）。
    mi_rows = [q for q in qs[:md_n] if _norm_model_input(q.get("model_input"))]
    if mi_rows:
        L.append("### 模型原输入（调试向量用，默认折叠）")
        L.append("")
        L.append("> 仅列出本表展示范围内含原输入的题；全量见 答卷.json / 对比.csv。")
        L.append("")
        for q in mi_rows:
            mi = _norm_model_input(q.get("model_input")) or {}
            L.append(f"<details><summary>题号 {q.get('qid')} · {q.get('mode')} · 模型原输入</summary>")
            L.append("")
            L.append("**system**")
            L.append("")
            L.append(f"<pre>{html.escape(mi.get('system', ''))}</pre>")
            L.append("")
            L.append("**user**")
            L.append("")
            L.append(f"<pre>{html.escape(mi.get('user', ''))}</pre>")
            L.append("")
            L.append("</details>")
            L.append("")
    data = "\n".join(L).encode("utf-8")
    return _finalize_bytes(data, secrets)


def _export_zip(clean: dict, secrets: list) -> bytes:
    files = {
        "试卷.json": _exam_paper_json(clean, secrets),
        "答卷.json": _answer_sheet_json(clean, secrets),
        "对比.csv": _export_csv(clean, secrets),
        "成绩单.md": _export_md(clean, secrets),
        "说明.txt": ("拟人化考试导出包（全量 N 题，不截断）\n"
                     "试卷.json   全量：question + 人类标准答案（post 为风格参照并逐行注明）\n"
                     "答卷.json   全量：AI 答案 + 逐题图灵判定(turing_caught/turing_votes) + 汇总分数\n"
                     "对比.csv    全量：题号/模式/题目/人类标准答案/AI答案/是否被抓(caught/votes)\n"
                     "成绩单.md   汇总表 + 图灵对照表 + 逐题并排对比(前 50 题，全量见 csv)\n"
                     "所有文件均不含密钥或账号凭据。\n").encode("utf-8"),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, _finalize_bytes(data, secrets))
    return buf.getvalue()


def export_exam(result: dict, fmt: str = "zip") -> bytes:
    """导出为 json / csv / md / zip，返回 bytes。所有导出不含 key / 凭据。"""
    fmt = (fmt or "zip").lower()
    if fmt not in EXTS:
        raise ValueError(f"不支持的导出格式：{fmt}（可选 json/csv/md/zip）")
    clean, secrets = _safe(result or {})
    if fmt == "json":
        return _export_json(clean, secrets)
    if fmt == "csv":
        return _export_csv(clean, secrets)
    if fmt == "md":
        return _export_md(clean, secrets)
    return _export_zip(clean, secrets)


# ---------------------------------------------------------------- 离线自测
def mock_result(n: int = 6) -> dict:
    """按 exam_answers.json 结构造假数据，``n`` 可放大到 260 验证不截断。

    含 canary 假 key，验证导出脱敏。6 个模板循环铺满 n 题。
    """
    templates = [
        {"mode": "comment", "board": "苏大迷惑大赏",
         "question": "有没有人能找我约拍，我拍的真的还可以[流泪]",
         "human_answers": ["大王！我圣诞节可以约你嘛[期待]我想出片！", "泪目，光影绝了！",
                           "周黑鸭去拍烤鸭"],
         "ai_answer": "你试过往小红书发组图再带苏大定位吗", "turing_votes": 2, "turing_caught": 2},
        {"mode": "comment", "board": "今天吃什么", "question": "求鼠鼠们推荐好吃的外卖",
         "human_answers": ["食其家 [期待]", "麦当劳"],
         "ai_answer": "试试东吴面馆的焖肉面，加个蛋", "turing_votes": 2, "turing_caught": 0},
        {"mode": "post", "board": "",
         "question": "（开放式发帖：按抽到的真实话题写一条帖子）",
         "human_answers": ["不知道怎么了，总是失眠，有没有解决办法啊", "求周六唱k搭子",
                           "问问苏大天赐庄哪里可以线上家教", "求问近代史纲要老师哪个好"],
         "human_answers_kind": "reference_posts（真实帖子，作为风格参照而非唯一标准答案）",
         "ai_answer": "并排骑车聊天挡着后面一溜人，真服了", "turing_votes": 2, "turing_caught": 0},
        {"mode": "post", "board": "",
         "question": "（开放式发帖：按抽到的真实话题写一条帖子）",
         "human_answers": ["真实帖子参照一", "真实帖子参照二"],
         "human_answers_kind": "reference_posts（真实帖子，作为风格参照而非唯一标准答案）",
         "ai_answer": "独墅湖二期教学楼晚上那条狗又来了", "turing_votes": 2, "turing_caught": 1},
        {"mode": "reply", "board": "", "question": "苏大26阳澄湖校区选课求助",
         "their": "请问点名是课前点嘛？",
         "human_answers": ["这个我记不太清了耶", "请问点名是课前点嘛？"],
         "ai_answer": "点名一般是课前，不过有的老师会中途再点一次", "turing_votes": 0, "turing_caught": 0},
        {"mode": "reply", "board": "", "question": "出一台380的kindle", "their": "成色怎么样",
         "human_answers": ["九成新，无磕碰", "380的，你也是这个价吗"],
         "ai_answer": "九成新没磕碰，380 自取", "turing_votes": 2, "turing_caught": 1},
    ]
    n = max(1, int(n))
    answers = []
    for i in range(n):
        a = json.loads(json.dumps(templates[i % len(templates)], ensure_ascii=False))
        if n > len(templates):
            a["question"] = a["question"] + f"（第{i + 1}题）"
            a["ai_answer"] = a["ai_answer"] + f"（第{i + 1}题）"
        answers.append(a)
    payload = {
        "generated_at": "2026-11-06 18:00:00", "seed": 20261105, "candidates": 1,
        "model": "mock-model", "base_url": "https://mock.local/v1",
        "n": n, "answers": answers,
    }
    eval_last = {
        "comment": {"total": 78.6, "turing": 26.4, "catch_rate": 0.341,
                    "dist": 20.3, "dup": 17.4, "flavor": 14.6, "n": n},
        "post": {"total": 86.7, "turing": 33.7, "catch_rate": 0.158,
                 "topic": 21.0, "form": 17.0, "flavor": 15.0, "n": n},
        "reply": {"total": 82.5, "turing": 28.7, "catch_rate": 0.283,
                  "dist": 19.3, "dup": 19.5, "flavor": 15.0, "n": n},
    }
    meta = _meta_from_payload(payload, mode="all", source=f"mock（假数据 {n} 题）")
    result = assemble_exam(answers_payload=payload, eval_last=eval_last, meta=meta)
    # 把假 key 放进敏感字段 + 普通文本，双重验证：字段剔除 + 值级脱敏
    result["_secrets"] = [CANARY]
    result["meta"]["api_key"] = CANARY
    result["questions"][0]["ai"] = result["questions"][0]["ai"] + " " + CANARY
    result["questions"][0]["human"] = list(result["questions"][0]["human"]) + [CANARY]
    return result


def _self_test(export_path: str, mock_n: int = 6) -> None:
    print(f"== exam.py --mock 离线自测（不调用 LLM，{mock_n} 题）==")
    result = mock_result(mock_n)
    n = len(result["questions"])
    print(f"  题数 = {n}（source={result['meta'].get('source')}）")
    assert n == mock_n
    for fmt in ("json", "csv", "md", "zip"):
        data = export_exam(result, fmt)
        assert isinstance(data, (bytes, bytearray)), fmt
        print(f"  export fmt={fmt:4s} -> {len(data)} bytes")

    path = Path(export_path) if export_path else (
        Path(tempfile.gettempdir()) / "exam_mock.zip")
    path.write_bytes(export_exam(result, "zip"))
    print(f"\n  已写出：{path}")
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        print("  zip 条目：")
        for name in names:
            print(f"    - {name}  ({z.getinfo(name).file_size} bytes)")

        required = ["试卷.json", "答卷.json", "对比.csv", "成绩单.md"]
        for name in required:
            assert name in names, f"缺文件 {name}"

        csv_text = z.read("对比.csv").decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(csv_text)))
        header = ",".join(rows[0])
        print(f"\n  对比.csv 表头：{header}")
        assert rows[0] == ["题号", "模式", "题目", "人类标准答案", "AI答案", "是否被抓"], \
            f"表头不符: {rows[0]}"
        print(f"  对比.csv 数据行数：{len(rows) - 1}（应为 {n}，不截断）")
        assert len(rows) - 1 == n, "对比.csv 题目数被截断"
        print("  前 3 行（列名=值，人类标准答案内部用 ` | ` 分隔多条）：")
        for r in rows[1:4]:
            print("    " + _row_preview(r))
        # 是否被抓 = 真实 caught/votes，不再「无」
        assert rows[1][5] == "2/2", f"首题是否被抓非真实值: {rows[1][5]}"
        assert all(r[5] != "无" for r in rows[1:]), "存在『是否被抓=无』"
        assert "大王！我圣诞节可以约你嘛" in csv_text, "缺真人标准答案实际内容"
        assert "你试过往小红书发组图" in csv_text, "缺 AI 答案实际内容"
        assert REF_MARK in csv_text, "post 风格参照未标注"

        paper = json.loads(z.read("试卷.json"))
        sheet = json.loads(z.read("答卷.json"))
        print(f"  试卷.json 题数={len(paper['questions'])}  答卷.json 题数={len(sheet['answers'])}")
        assert len(paper["questions"]) == n and len(sheet["answers"]) == n, "试卷/答卷截断"
        assert paper["questions"][0]["试卷题目"], "试卷缺 question"
        assert "人类标准答案" in paper["questions"][0], "试卷缺 human_answers"
        assert "AI答案" in sheet["answers"][0], "答卷缺 ai_answer"
        assert "图灵被抓次数" in sheet["answers"][0] and "投票数" in sheet["answers"][0]
        post_paper = [x for x in paper["questions"] if x["模式"] == "post"][0]
        assert isinstance(post_paper["人类标准答案"], list), "post 未照常导出 human_answers"
        assert "风格参照" in post_paper["人类标准答案性质"], "post 未标注风格参照性质"
        assert post_paper.get("human_answers_kind"), "post 缺 human_answers_kind 原文"
        print("  试卷.json / 答卷.json 结构：OK（post 照常导出并逐行注明风格参照）")

        md = z.read("成绩单.md").decode("utf-8")
        assert f"全量 {n} 题" in md, "成绩单未注明全量题数"
        assert f"前 {min(50, n)} / {n} 题" in md, "成绩单未注明前 50 条"

        blob = b"".join(z.read(name) for name in names)
        assert CANARY.encode("utf-8") not in blob, "导出里泄漏了假 key！"
        assert b'"api_key"' not in blob, "导出里出现了敏感字段名"
        assert b"sk-" not in blob, "导出里出现了 sk- 假 key 前缀"

    print("  ✓ 四个必需文件齐全（另含 说明.txt）")
    print("  ✓ 对比.csv 表头：题号,模式,题目,人类标准答案,AI答案,是否被抓")
    print(f"  ✓ 是否被抓为真实 caught/votes；{n} 题全量不截断；人类标准答案与 AI 答案并排")
    print("  ✓ post 照常导出 human_answers 并用 human_answers_kind 注明风格参照")
    print("  ✓ 全包不含 sk- / 临时 key / 敏感字段 / 账号凭据")
    for fmt in ("json", "csv", "md"):
        assert CANARY.encode("utf-8") not in export_exam(result, fmt), fmt
    print("  ✓ json/csv/md 单格式导出同样不含 key")
    print("\n== 自测通过 ==")


CSV_COLS = ["题号", "模式", "题目", "人类标准答案", "AI答案", "是否被抓"]


def _row_preview(r: list[str]) -> str:
    """带列名的行预览，避免真人答案里的 ` | ` 与列分隔符混淆。"""
    return "  ".join(f"{lab}={val[:46]}" for lab, val in zip(CSV_COLS, r))


def _inspect_zip(path: Path) -> None:
    """验收用：打印 zip 条目与 对比.csv 前 3 行，证明逐题真实值 + 并排对比。"""
    print(f"\n--- 导出包 {path} ---")
    with zipfile.ZipFile(path) as z:
        print("zip 条目：")
        for i in z.infolist():
            print(f"  {i.filename:14s} {i.file_size:7d} bytes")
        rows = list(csv.reader(io.StringIO(z.read("对比.csv").decode("utf-8-sig"))))
        print(f"\n对比.csv 表头：{','.join(rows[0])}")
        print("对比.csv 前 3 行（列名=值，人类标准答案内部用 ` | ` 分隔多条）：")
        for r in rows[1:4]:
            print("  " + _row_preview(r))
        print(f"\n共 {len(rows) - 1} 题（不截断）；是否被抓列示例：{rows[1][5]}")


def main() -> None:
    ap = argparse.ArgumentParser(description="拟人化考试：组装答卷并导出（key 不落盘）")
    ap.add_argument("--mock", action="store_true", help="假数据离线自测，不调用 LLM")
    ap.add_argument("--mock-n", type=int, default=6,
                    help="--mock 假数据题数，用 260 验证不截断（默认 6）")
    ap.add_argument("--from", dest="from_file", default="",
                    help="直接读一份 exam_answers*.json 导出，不重跑考试")
    ap.add_argument("--export", default="", help="导出文件写出的路径")
    ap.add_argument("--mode", default="all",
                    choices=["all", "both", "comment", "post", "reply"])
    ap.add_argument("--n", type=int, default=0, help="样本数，0=用考卷默认量")
    ap.add_argument("--candidates", type=int, default=1)
    ap.add_argument("--fmt", default=None, choices=["json", "csv", "md", "zip"],
                    help="导出格式；不给则按 --export 后缀推断，默认 zip")
    args = ap.parse_args()

    if args.mock:
        _self_test(args.export, args.mock_n)
        return

    fmt = args.fmt or _fmt_from_path(args.export) or "zip"

    if args.from_file:
        result = result_from_answers_file(args.from_file)
        if not result:
            raise SystemExit(f"找不到答卷文件：{args.from_file}")
        out = args.export or str(Path(tempfile.gettempdir()) / ("exam" + EXTS[fmt]))
        Path(out).write_bytes(export_exam(result, fmt))
        print(f"已从 {args.from_file} 组装 {len(result['questions'])} 题 -> 导出 {out}")
        if fmt == "zip":
            _inspect_zip(Path(out))
        return

    out = args.export or str(Path(tempfile.gettempdir()) / ("exam" + EXTS[fmt]))
    result = run_exam(args.mode, args.n, args.candidates,
                      progress=lambda p, t: print(f"  [{p:3d}%] {t}", flush=True))
    Path(out).write_bytes(export_exam(result, fmt))
    print(f"已导出：{out}")


if __name__ == "__main__":
    main()
