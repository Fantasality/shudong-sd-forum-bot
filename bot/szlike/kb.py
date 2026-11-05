"""知识库注入：把 kb/knowledge.json 压成一段短提示，拼到 system prompt 后面。

原则
----
1. **只给规律，不给原文**：知识库里不应有可复述的具体事件/人名/楼栋。
2. **每次抽样**：梗词、表情、场景每次随机取几条，避免生成物千篇一律。
3. **按任务裁剪**：发帖要多给板块与选题，评论要多给情绪应对，回复要短。
4. **硬预算**：默认 800 字，超了按句截断，绝不吃掉主体指令。
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
from pathlib import Path

_lock = threading.Lock()
_cache: dict = {"mtime": 0.0, "data": None, "path": None}

# 这些场景适合「只接情绪」，其它场景（食堂/交易/失物）要办正事
_EMOTIONAL = {"情绪低落", "学业焦虑", "考试受挫", "人际孤独", "家庭矛盾",
              "恋爱困惑", "身体不适", "被冒犯"}
# 场景 → 回复手册里的类型
_SCENE2TYPE = {
    "学业焦虑": "求助", "考试受挫": "求助", "求助实操": "求助", "人际孤独": "求助",
    "情绪低落": "吐槽", "被冒犯": "吐槽", "对校园管理不满": "吐槽", "兼职打工": "吐槽",
    "生活琐事": "分享", "轻松分享": "分享", "被善意治愈": "分享",
    "恋爱困惑": "恋爱", "失物": "失物", "二手交易": "分享", "家庭矛盾": "求助",
    "身体不适": "求助", "节日": "分享",
}


def load(path: str | Path = "kb/knowledge.json") -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    m = p.stat().st_mtime
    with _lock:
        if _cache["path"] == str(p) and _cache["mtime"] == m and _cache["data"] is not None:
            return _cache["data"]
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        _cache.update({"mtime": m, "data": data, "path": str(p)})
        return data


def _rng(salt: str) -> random.Random:
    return random.Random(int(time.time() * 1000) ^ (hash(salt) & 0xFFFFFFFF))


def _short(x, n=34) -> str:
    s = str(x or "").strip()
    return s if len(s) <= n else s[:n].rstrip("，。 ") + "…"


def _pick(r: random.Random, seq, k: int) -> list:
    seq = [x for x in (seq or []) if x]
    if not seq:
        return []
    return r.sample(seq, min(k, len(seq)))


def _match(rows: list[dict], key: str, want: str | None) -> dict | None:
    if not want:
        return None
    for row in rows or []:
        if isinstance(row, dict) and want and (want in str(row.get(key, "")) or str(row.get(key, "")) in want):
            return row
    return None


def render(kind: str = "comment", *, persona: str = "", board: str | None = None,
           scene: str | None = None, budget: int = 800, campus: str = "",
           path: str | Path = "kb/knowledge.json") -> str:
    """返回一段可直接拼进 system 的社区常识。kind: post / comment / reply / like。"""
    kb = load(path)
    if not kb:
        return ""
    r = _rng(f"{kind}:{persona}:{board}:{scene}:{time.time()}")
    lines: list[str] = []

    voice = kb.get("voice") or {}
    memes = kb.get("memes") or {}
    social = kb.get("social") or {}
    campus = kb.get("campus") or {}
    s = kb.get("stats_summary") or {}

    # ---------- 长度与语气 ----------
    if kind == "post":
        lines += _pick(r, voice.get("post_voice"), 3)
        lines += _pick(r, voice.get("sentence"), 2)
    elif kind == "reply":
        lines.append("回复要短，一两句，顺着对方的话说。")
        lines += _pick(r, voice.get("comment_voice"), 2)
    else:
        lines += _pick(r, voice.get("comment_voice"), 3)
        lines += _pick(r, voice.get("sentence"), 1)

    # ---------- 板块 ----------
    bp = _match(social.get("board_playbook"), "board", board)
    if bp:
        lines.append(f"当前板块「{bp.get('board','')}」：{_short(bp.get('purpose'),26)}；"
                     f"氛围{_short(bp.get('rules'),26)}")
        if kind == "post" and bp.get("post_hint"):
            lines.append(f"这板块适合写：{_short(bp.get('post_hint'), 40)}")

    # ---------- 梗 ----------
    lex = memes.get("lexicon") or []
    if lex:
        chosen = _pick(r, [x for x in lex if isinstance(x, dict)], 4)
        lines.append("可用梗：" + "；".join(
            f"{x.get('w','')}={_short(x.get('meaning'), 16)}" for x in chosen))
    pats = _pick(r, memes.get("patterns"), 2)
    if pats:
        lines.append("句式：" + " / ".join(_short(p, 30) for p in pats))

    # ---------- 表情 ----------
    st = memes.get("sticker_usage") or []
    if st:
        chosen = _pick(r, [x for x in st if isinstance(x, dict)], 3)
        lines.append("表情：" + "、".join(f"{x.get('s','')}{_short(x.get('when'), 14)}" for x in chosen))

    # ---------- 场景与情绪 ----------
    if kind in ("comment", "reply"):
        emap = social.get("emotion_map") or []
        em = _match(emap, "scene", scene) if scene else None
        if not em:
            em = _match(emap, "scene", board)
        if not em and not scene and emap:
            em = r.choice(emap)          # 没有场景信息时才随机，避免给错建议
        em = em or {}
        if em.get("reply"):
            lines.append(f"遇到「{_short(em.get('scene'), 12)}」这类：{_short(em.get('reply'), 40)}")
        elif scene and scene in _EMOTIONAL:
            lines.append("没现成场景可套，就直接回这件事本身，能给判断就给判断。")
        # 回复手册按场景对号入座，别随机串味
        want = _SCENE2TYPE.get(scene or "", "")
        plays = [x for x in (social.get("reply_playbook") or []) if isinstance(x, dict)]
        rp = next((x for x in plays if want and want in str(x.get("type", ""))), None)
        if rp is None and not want and plays:
            rp = r.choice(plays)
        rp = rp or {}
        if rp.get("how"):
            lines.append(f"{_short(rp.get('type'), 8)}类帖：{_short(rp.get('how'), 40)}")
        if scene:
            lines.append(f"判断这条属于「{scene}」场景。")

    # ---------- 校园感 ----------
    if kind == "post":
        for key in ("life_rhythm", "term_calendar", "food", "space"):
            v = _pick(r, campus.get(key), 1)
            if v:
                lines.append(_short(v[0], 46))

    # ---------- 本校区的硬事实（来自 campus_map.json，不是模型记忆）----------
    if campus:
        try:
            from . import facts
            fb = facts.brief(campus)
            if fb:
                lines.append(fb)
        except Exception:
            pass

    # ---------- 禁忌 ----------
    d = _pick(r, voice.get("dont"), 3)
    if d:
        lines.append("别这样写：" + "；".join(_short(x, 24) for x in d))
    ov = _pick(r, memes.get("overused"), 2)
    if ov:
        lines.append("这些已经用滥：" + "、".join(_short(x, 14) for x in ov))

    if not lines:
        return ""

    head = f"【鼠洞社区常识·{s.get('posts','')}帖/{s.get('comments','')}评论提炼，只作参考，不要复述】"
    body = head + "\n" + "\n".join("· " + x for x in lines if x)
    if len(body) > budget:
        cut = body[:budget]
        # 按行收敛，避免截断半句
        lines2 = cut.split("\n")
        if len(lines2) > 2:
            body = "\n".join(lines2[:-1])
        else:
            body = cut
    return body


def stats(path: str | Path = "kb/knowledge.json") -> dict:
    kb = load(path)
    return {
        "exists": bool(kb),
        "built_at": kb.get("built_at", ""),
        "summary": kb.get("stats_summary", {}),
        "sections": {k: len(v) if isinstance(v, (list, dict)) else 0
                     for k, v in kb.items() if k in ("campus", "memes", "voice", "social")},
    }
