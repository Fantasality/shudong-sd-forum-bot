"""当日事件：天气 + 校内通知 + 校历，喂给发帖当背景。

为什么要这一层
--------------
32 个号如果每天都在写「食堂排队」「图书馆占座」，读起来像同一个模板。
真人发帖会带今天的东西：下雨了、降温了、教务处又调教学楼了、这周开始选课。
这些信息不该让模型凭记忆编，所以每天抓一次，缓存到 kb/events.json。

数据源（都无需 key）：
    天气     open-meteo.com（苏州坐标，免费）
    教务通知 jwc.suda.edu.cn 首页的带日期列表
    校园活动 www.suda.edu.cn 学术活动页

只用标题做背景，不让模型复述通知原文——复述必错，带一句「最近学校在调教学楼」就够。
"""
from __future__ import annotations

import http.client
import json
import re
import ssl
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
OUT = ROOT / "kb" / "events.json"

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
WEATHER_TXT = {
    0: "晴", 1: "晴间多云", 2: "多云", 3: "阴", 45: "有雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨", 61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨", 71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨", 81: "强阵雨", 82: "暴雨", 85: "阵雪", 86: "强阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷暴",
}


def _get(host: str, path: str, timeout: float = 8.0) -> str:
    conn = http.client.HTTPSConnection(host, 443, timeout=timeout,
                                       context=ssl.create_default_context())
    conn.request("GET", path, headers=UA)
    resp = conn.getresponse()
    raw = resp.read(500_000)
    conn.close()
    return raw.decode("utf-8", "replace")


def _weather() -> dict:
    try:
        d = json.loads(_get("api.open-meteo.com",
                            "/v1/forecast?latitude=31.30&longitude=120.59"
                            "&daily=temperature_2m_max,temperature_2m_min,"
                            "precipitation_probability_max,weathercode"
                            "&timezone=Asia%2FShanghai&forecast_days=2"))
        daily = d.get("daily") or {}
        out = []
        for i in range(len(daily.get("time") or [])):
            code = int((daily.get("weathercode") or [0])[i])
            out.append({
                "date": daily["time"][i],
                "tmax": daily["temperature_2m_max"][i],
                "tmin": daily["temperature_2m_min"][i],
                "rain": daily["precipitation_probability_max"][i],
                "code": code,
                "text": WEATHER_TXT.get(code, "说不准"),
            })
        return {"today": out[0] if out else {}, "tomorrow": out[1] if len(out) > 1 else {}}
    except Exception as e:
        return {"err": f"{type(e).__name__}: {str(e)[:60]}"}


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s or "")).strip()


def _notices() -> list[dict]:
    """教务处首页的通知，带日期。只留最近 21 天，最多 12 条。"""
    out: list[dict] = []
    try:
        html = _get("jwc.suda.edu.cn", "/")
    except Exception:
        return out
    now = time.time()
    for href, text in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
        t = _clean(text)
        m = re.match(r"(\d{2})/(\d{2})/(\d{4})\s*(.+)", t)
        if not m:
            continue
        mm, dd, yy, title = m.groups()
        try:
            ts = time.mktime(time.strptime(f"{yy}-{mm}-{dd}", "%Y-%m-%d"))
        except Exception:
            continue
        if (now - ts) / 86400 > 21:
            continue
        title = re.sub(r"^(关于|苏州大学关于)", "", title).strip()
        # 首页混着新闻稿，标题后面会跟正文，截到第一个句号/空格
        title = re.split(r"[。\s]{1,}", title)[0]
        if len(title) < 6:
            continue
        if not re.search(r"(通知|安排|调用|选课|考试|讲座|活动|报名|征集|调整|开放)", title):
            continue
        out.append({"title": title[:60], "date": f"{yy}-{mm}-{dd}"})
    # 去重、按时间倒序
    seen, uniq = set(), []
    for n in sorted(out, key=lambda x: x["date"], reverse=True):
        if n["title"] in seen:
            continue
        seen.add(n["title"])
        uniq.append(n)
    return uniq[:12]


def refresh() -> dict:
    d = {
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "weather": _weather(),
        "notices": _notices(),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    return d


_cache: dict = {"t": 0.0, "d": None}


def today(max_age: float = 3600) -> dict:
    """读缓存；超过 1 小时或跨天就重新抓。抓失败用旧数据。"""
    if _cache["d"] is not None and time.time() - _cache["t"] < max_age:
        return _cache["d"]
    try:
        if OUT.exists():
            d = json.loads(OUT.read_text(encoding="utf-8"))
            day = (d.get("fetched_at") or "")[:10]
            if day == time.strftime("%Y-%m-%d"):
                _cache["t"], _cache["d"] = time.time(), d
                return d
    except Exception:
        pass
    try:
        d = refresh()
    except Exception:
        d = _cache["d"] or {}
    _cache["t"], _cache["d"] = time.time(), d
    return d


def rain_now() -> bool:
    """今天是不是在下雨 / 大概率下雨。

    用来挡「今天下大雨，阳台上晒的被子」这种硬伤：天气是唯一一条
    所有人都能当场验证的事实，写错了整条帖子就废了。
    """
    w = (today().get("weather") or {}).get("today") or {}
    try:
        if float(w.get("rain") or 0) >= 55:
            return True
    except Exception:
        pass
    return "雨" in str(w.get("text") or "")


def brief(campus: str = "", with_notice: bool = True) -> str:
    """给 prompt 的一句话背景。"""
    d = today()
    w = (d.get("weather") or {}).get("today") or {}
    parts = []
    if w:
        parts.append(f"今天苏州{w.get('text')}，{w.get('tmin'):.0f}-{w.get('tmax'):.0f}℃，"
                     f"降水概率{w.get('rain')}%")
    if with_notice:
        ns = d.get("notices") or []
        if ns:
            # 只取标题里的动作，不复述原文
            parts.append("学校最近在说：" + "；".join(n["title"][:18] for n in ns[:2]))
    return "；".join(parts)


def search(query: str, k: int = 3) -> str:
    """在天气/通知里找，给工具用。"""
    d = today()
    hits = []
    q = query or ""
    for n in (d.get("notices") or []):
        if any(w and w in n["title"] for w in re.findall(r"[\u4e00-\u9fff]{2,}", q)):
            hits.append(f"{n['date']} {n['title']}")
    w = (d.get("weather") or {}).get("today") or {}
    if w and re.search(r"(天气|下雨|温度|几度|冷|热)", q):
        hits.insert(0, f"今天{w.get('text')}，{w.get('tmin'):.0f}-{w.get('tmax'):.0f}℃，"
                       f"降水概率{w.get('rain')}%")
    return "\n".join(hits[:k]) if hits else "本地没有相关的当日信息"
