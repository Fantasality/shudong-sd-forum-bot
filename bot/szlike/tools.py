"""让模型自己决定「要不要查一下」。

DeepSeek 的 API 只支持**客户端执行**的 function calling（官方文档原文：
"Currently, only functions are supported as a tool"），没有服务端联网搜索。
所以工具在本地实现，模型只负责发起调用：

    search_campus(query)  查校区/食堂/学院/专业（本地结构化库）
    fetch_url(url)        抓一个公开网页（只读，限 http/https，去掉标签）

用在「回复里带具体事实」的场合：对方问哪个食堂几点开、某个学院在哪个校区，
模型先搜再答，而不是凭记忆编。查不到时如实说不知道——这比编一个更像人。
"""
from __future__ import annotations

import http.client
import json
import re
import ssl
from urllib.parse import urlsplit

from . import facts

MAX_FETCH = 1800


def schemas() -> list[dict]:
    return [
        {"type": "function", "function": {
            "name": "search_campus",
            "description": "查询苏州大学的校区、食堂、学院、专业信息。"
                           "不确定某个食堂属于哪个校区、某个专业属于哪个学院时调用。",
            "parameters": {"type": "object",
                           "properties": {"query": {"type": "string",
                                                    "description": "关键词，如「六食堂」「未来校区 专业」"}},
                           "required": ["query"]}}},
        {"type": "function", "function": {
            "name": "web_search",
            "description": "联网搜索，用于本地库没有的信息（校历、通知、外部事实）。",
            "parameters": {"type": "object",
                           "properties": {"query": {"type": "string"}},
                           "required": ["query"]}}},
        {"type": "function", "function": {
            "name": "fetch_url",
            "description": "抓取一个公开网页的正文纯文本，用于核对官网/公告信息。",
            "parameters": {"type": "object",
                           "properties": {"url": {"type": "string"}},
                           "required": ["url"]}}},
    ]


_BLOCK_HOST = re.compile(r"(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|0\.0\.0\.0)")


def _strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    return re.sub(r"\s+", " ", text).strip()


def fetch(url: str) -> str:
    try:
        u = urlsplit(url)
    except Exception:
        return "URL 无效"
    if u.scheme not in ("http", "https") or not u.hostname or _BLOCK_HOST.search(u.hostname):
        return "只允许抓取公开的 http/https 页面"
    try:
        conn = (http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=8,
                                            context=ssl.create_default_context())
                if u.scheme == "https" else http.client.HTTPConnection(u.hostname, u.port or 80, timeout=8))
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        conn.request("GET", path, headers={"User-Agent": "Mozilla/5.0 (compatible; suda-bot/1.0)"})
        resp = conn.getresponse()
        raw = resp.read(400_000)
        conn.close()
        ctype = (resp.getheader("content-type") or "").lower()
        if "html" not in ctype and "text" not in ctype:
            return f"不是文本页面（{ctype or '未知'}）"
        text = _strip_html(raw.decode("utf-8", "replace"))
        return text[:MAX_FETCH] or "页面没有可读正文"
    except Exception as e:
        return f"抓取失败：{type(e).__name__}: {str(e)[:80]}"


def run(name: str, args: dict) -> str:
    if name == "search_campus":
        return facts.search(str(args.get("query") or ""), k=3)
    if name == "web_search":
        from . import web
        return web.search(str(args.get("query") or ""), k=3)
    if name == "fetch_url":
        return fetch(str(args.get("url") or ""))
    return f"没有这个工具：{name}"


def parse_args(raw: str) -> dict:
    try:
        d = json.loads(raw or "{}")
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}
