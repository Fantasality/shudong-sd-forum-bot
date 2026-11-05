"""校区事实库：食堂、学院、专业、地标的查询与「说错就拦」。

为什么需要
----------
32 个号分在四个校区，最容易露馅的不是语气，是事实：
未来校区（吴江）的号写「庄里图书馆」，天赐庄的号写「独墅湖七食堂」，
或者一个光电的学生跑去上系统解剖学。这些错本地人一眼就能看出来。

数据来自 `kb/campus_map.json`（苏州大学迎新网食堂分布、采购公告 21 个食堂清单、
2025 招生章程的学院分布、未来校区官网），不靠模型记忆。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
MAP = ROOT / "kb" / "campus_map.json"

_cache: dict = {"mtime": 0.0, "d": None}


def data() -> dict:
    try:
        mt = MAP.stat().st_mtime
    except OSError:
        return {}
    if _cache["d"] is not None and _cache["mtime"] == mt:
        return _cache["d"]
    try:
        d = json.loads(MAP.read_text(encoding="utf-8"))
    except Exception:
        return {}
    _cache["mtime"], _cache["d"] = mt, d
    return d


def canteens(campus: str | None = None) -> list[dict]:
    rows = (data().get("canteens") or [])
    return [c for c in rows if not campus or c.get("campus") == campus]


def colleges(campus: str | None = None) -> list[str]:
    d = data().get("colleges") or {}
    if campus:
        return list(d.get(campus) or [])
    return [x for v in d.values() for x in v]


def _norm(s: str) -> str:
    return re.sub(r"[\s（）()·、，,。]", "", s or "")


def find_canteen(word: str) -> dict | None:
    """按名字找食堂。「方塔」能命中「方塔食堂」，「六食堂」会命中多个（需校区消歧）。"""
    w = _norm(word)
    if not w:
        return None
    rows = canteens()
    for c in rows:
        if _norm(c["name"]) == w:
            return c
    cands = [c for c in rows if w in _norm(c["name"])]
    if len(cands) == 1:
        return cands[0]
    return None


def canteen_options(word: str) -> list[dict]:
    w = _norm(word)
    return [c for c in canteens() if w and w in _norm(c["name"])]


# 校区里常见的地点词，用来判断「有没有说到别的校区」
AREA_WORDS = {
    "天赐庄": ["本部", "东区", "北区"],
    "独墅湖": ["一期", "二期", "南区", "北区"],
    "阳澄湖": ["校区"],
    "未来": ["校区"],
}
FOREIGN_AREAS = {
    "天赐庄": ["一期", "二期"],
    "独墅湖": ["本部", "东区", "北区"],
    "阳澄湖": ["本部", "东区", "北区", "一期", "二期"],
    "未来": ["本部", "东区", "北区", "一期", "二期"],
}


def verify(text: str, campus: str) -> list[str]:
    """返回问题列表，空列表表示没查出硬伤。只查能确定对错的，不做语义判断。"""
    t = text or ""
    out: list[str] = []
    if not campus:
        return out
    # 1) 提到别的校区的食堂
    for c in canteens():
        if c["campus"] == campus:
            continue
        short = _norm(c["name"])
        # 「六食堂」这种短名容易误伤，只有带前缀时才判定
        if len(short) >= 4 and short in _norm(t):
            out.append(f"你在{campus}，却写了{c['campus']}的{c['name']}")
            break
    # 2) 提到别的校区的区域词
    for w in FOREIGN_AREAS.get(campus, []):
        if w in t:
            out.append(f"你在{campus}，却写了「{w}」")
            break
    # 3) 提到别的校区的学院
    mine = set(colleges(campus))
    for col in colleges():
        if col in mine:
            continue
        if col in t and len(col) >= 4:
            out.append(f"你不在{col}所在校区，却提到了{col}")
            break
    return out


def brief(campus: str, area: str | None = None, max_canteens: int = 9) -> str:
    """给 prompt 用的一小段事实：本校区全部食堂名 + 招牌菜 + 地标。

    食堂名要给全。原来只给前 5 个（天赐庄有 9 个），模型遇到没给到的就自己编，
    生成过「五食堂」这种清单里不存在的名字——而本校人一眼就知道没有这个食堂。
    """
    rows = canteens(campus)
    if not rows:
        return ""
    if area:
        picked = [c for c in rows if c.get("area") == area] or rows
    else:
        picked = rows
    names = []
    for c in picked[:max_canteens]:
        names.append(c["name"].replace("食堂", ""))
    sig = []
    for c in picked[:5]:
        for item in re.split(r"[、，]", c.get("signature") or ""):
            item = item.strip()
            if item and item not in sig:
                sig.append(item)
    marks = (data().get("campuses") or {}).get(campus, {}).get("landmarks") or []
    out = f"你所在校区：{campus}。食堂：{'、'.join(names)}。"
    if sig:
        out += f"常见菜：{'、'.join(sig[:6])}。"
    if marks:
        out += f"地标：{'、'.join(marks[:8])}。"
    return out


def _score(query: str, text: str) -> int:
    q = _norm(query)
    if not q:
        return 0
    s = 0
    for ch in set(q):
        if ch in text:
            s += 1
    for w in re.findall(r"[\u4e00-\u9fff]{2,}", q):
        if w in text:
            s += len(w) * 2
    return s


def search(query: str, k: int = 3) -> str:
    """本地检索：校区库 + 语料。给模型当工具用的返回文本。"""
    d = data()
    if not d:
        return "（本地库没建好）"
    hits: list[tuple[int, str]] = []
    for c in d.get("canteens") or []:
        txt = (f"{c['name']}（{c['campus']}·{c['area']}，{c['floors']}层）："
               f"{c['signature']}")
        s = _score(query, c["name"]) * 3 + _score(query, txt)
        if s:
            hits.append((s, txt))
    for camp, cols in (d.get("colleges") or {}).items():
        for col in cols:
            majors = (d.get("majors") or {}).get(col) or []
            txt = f"{col}（{camp}校区）：{'、'.join(majors[:8])}"
            s = _score(query, col) * 3 + _score(query, txt)
            if s:
                hits.append((s, txt))
    for camp, meta in (d.get("campuses") or {}).items():
        txt = (f"{camp}校区（{meta.get('address')}）：{('、'.join(meta.get('areas') or []))}"
               f"；地标 {'、'.join(meta.get('landmarks') or [])}")
        s = _score(query, camp) * 2 + _score(query, txt)
        if s:
            hits.append((s, txt))
    hits.sort(key=lambda x: -x[0])
    out = [h[1] for h in hits[:k]]
    return "\n".join(out) if out else "本地库里没有这条"


def need_lookup(text: str) -> str:
    """什么时候该主动查一下：问句 + 涉及具体地点/课程，或者提到食堂/校区/学院。"""
    t = text or ""
    if any(w in t for w in ("食堂", "校区", "学院", "专业", "几楼", "哪里", "怎么走", "几点")):
        return "考据"
    if re.search(r"(哪个|哪家|有没有|是不是|几点|多少|怎么)", t) and \
            any(w in t for w in ("课", "老师", "选课", "宿舍", "图书馆", "楼")):
        return "考据"
    return ""
