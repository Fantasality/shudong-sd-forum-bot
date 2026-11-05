"""联网：Bing 搜索 + 网页抓取。

DeepSeek 的 API 没有服务端搜索，工具只能本地执行；这里补上「搜索」这一半。
不引第三方 key，直接请求 cn.bing.com 的结果页解析 b_algo 块，拿标题/摘要/链接。
搜不到就如实返回「没搜到」，不编。

使用场景：回复里被问到具体事实，本地校区库没有，就搜一下；发帖不用（发帖靠语料和当日事件）。
"""
from __future__ import annotations

import http.client
import re
import ssl
import time
from urllib.parse import quote, unquote

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120 Safari/537.36",
      "Accept-Language": "zh-CN,zh;q=0.9"}

_cache: dict[str, tuple[float, str]] = {}
TTL = 1800


def _get(host: str, path: str, timeout: float = 8.0) -> str:
    conn = http.client.HTTPSConnection(host, 443, timeout=timeout,
                                       context=ssl.create_default_context())
    conn.request("GET", path, headers=UA)
    resp = conn.getresponse()
    raw = resp.read(600_000)
    conn.close()
    return raw.decode("utf-8", "replace")


def _clean(s: str) -> str:
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s or "")
    s = re.sub(r"<[^>]+>", "", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"')
          .replace("&#39;", "'").replace("&lt;", "<").replace("&gt;", ">"))
    return re.sub(r"\s+", " ", s).strip()


def _fetch(host: str, path: str) -> str:
    try:
        return _get(host, path)
    except Exception:
        return ""


_JUNK = re.compile(r"(其他人还搜了|相关搜索|大家还在搜|地图|百科|下载|登录|注册)")


def _parse(html: str, k: int) -> list[str]:
    """从搜索结果页里抠标题 + 摘要。三家引擎的 DOM 不一样，用 h3 统一切。"""
    out, seen = [], set()
    for m in re.finditer(r"(?is)<h3[^>]*>(.*?)</h3>(.{0,400})", html):
        raw_title, tail = m.group(1), m.group(2)
        title = _clean(raw_title)
        if not title or len(title) < 6 or _JUNK.search(title):
            continue
        if title in seen:
            continue
        seen.add(title)
        snip = _clean(tail.split("<")[0])
        snip = re.sub(r"^\s*\d+\s*", "", snip)[:110]
        out.append(f"{title}｜{snip}" if snip else title)
        if len(out) >= k:
            break
    return out


def search(query: str, k: int = 3) -> str:
    """搜狗 → 360 → Bing 依次试。三家都不稳定，谁先给出可用结果就用谁。"""
    q = (query or "").strip()
    if not q:
        return "搜索词为空"
    key = q[:80]
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    qq = quote(q)
    results: list[str] = []
    for host, path in (("www.sogou.com", "/web?query=" + qq),
                       ("www.so.com", "/s?q=" + qq),
                       ("cn.bing.com", "/search?q=" + qq + "&setlang=zh-CN&cc=CN")):
        html = _fetch(host, path)
        if not html:
            continue
        results = _parse(html, k)
        if len(results) >= 2:
            break
    text = "\n".join(results) if results else "没搜到"
    _cache[key] = (time.time(), text)
    return text


def search_links(query: str, k: int = 3) -> list[tuple[str, str]]:
    """要链接的场合（比如抓详情页）用这个。"""
    try:
        html = _get("cn.bing.com", "/search?q=" + quote(query) + "&setlang=zh-CN&cc=CN")
    except Exception:
        return []
    out = []
    for b in re.findall(r'(?is)<li class="b_algo".*?</li>', html)[:k]:
        m = re.search(r'(?is)<h2[^>]*>\s*<a[^>]+href="([^"]+)"', b)
        t = re.search(r"(?is)<h2[^>]*>(.*?)</h2>", b)
        if m:
            url = m.group(1)
            if url.startswith("http") and "bing.com" not in url:
                out.append((unquote(url), _clean(t.group(1)) if t else ""))
    return out
