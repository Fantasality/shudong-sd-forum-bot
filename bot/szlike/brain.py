"""LLM 接口（OpenAI 兼容，默认 DeepSeek），零依赖。

性能要点
--------
1. **连接复用**：用 http.client 保持长连接（线程本地），省掉每次 TCP+TLS 握手，
   实测每轮省 150-400ms；短评论场景收益最明显。
2. **并行生成**：`chat_many()` 用线程池同时请求多条评论，N 条评论的等待时间
   从 N×t 压到接近 t。
3. **短输出**：评论默认 max_tokens 给到 64，模型不会拖着写。
"""
from __future__ import annotations

import http.client
import json
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

log = logging.getLogger("szlike.brain")

FALLBACK_COMMENTS = [
    "有道理，记下了",
    "确实是这么回事",
    "看到这条笑了下",
    "这事我不好说，你自己拿主意",
    "换我我也难受",
    "不是小事，值得较真",
    "听着就烦",
    "真没必要忍到这个份上",
    "这谁顶得住",
    "行，记下了",
    "你说的这个我倒没想到",
    "要是我也会这么想",
    "有点道理",
    "也就你能忍",
    "看完了，说不出啥安慰话",
    "算我一个",
    "嗯，是这样",
    "这就有点离谱了",
    "你还真能忍",
]


class Brain:
    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        self.base_url = (cfg.get("base_url") or "https://api.deepseek.com/v1").rstrip("/")
        self.api_key = cfg.get("api_key") or ""
        self.model = cfg.get("model") or "deepseek-chat"
        self.timeout = float(cfg.get("timeout") or 25)
        self.temperature = float(cfg.get("temperature") or 0.9)
        self.max_tokens = int(cfg.get("max_tokens") or 160)
        # 思考模式会占用 max_tokens，把几十字的短帖截断在半句上；社交文本不需要思考
        self.thinking = bool(cfg.get("thinking", False))
        self.enabled = bool(self.api_key)
        self._local = threading.local()
        u = urlsplit(self.base_url)
        self._https = u.scheme != "http"
        self._host = u.hostname or "api.deepseek.com"
        self._port = u.port or (443 if self._https else 80)
        self._prefix = (u.path or "").rstrip("/")
        self.last_ms = 0
        self.last_finish = ""
        self.calls = 0

    # ---------- 连接 ----------

    def _conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            cls = http.client.HTTPSConnection if self._https else http.client.HTTPConnection
            c = cls(self._host, self._port, timeout=self.timeout)
            self._local.conn = c
        return c

    def _drop(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
        self._local.conn = None

    def _post(self, payload: dict) -> dict:
        body = json.dumps(payload).encode()
        path = f"{self._prefix}/chat/completions"
        headers = {
            "content-type": "application/json",
            "authorization": "Bearer " + self.api_key,
            "accept": "application/json",
            "connection": "keep-alive",
        }
        last = None
        for attempt in (0, 1):
            try:
                c = self._conn()
                c.request("POST", path, body=body, headers=headers)
                resp = c.getresponse()
                raw = resp.read()
                if resp.status >= 400:
                    text = raw.decode("utf-8", "replace")[:300]
                    # 429/5xx 值得重连再试一次
                    if resp.status in (429, 500, 502, 503, 504) and attempt == 0:
                        last = f"HTTP {resp.status}: {text}"
                        self._drop()
                        time.sleep(0.6)
                        continue
                    raise BrainError(f"HTTP {resp.status}: {text}")
                return json.loads(raw.decode())
            except BrainError:
                raise
            except Exception as e:
                last = repr(e)
                self._drop()
                if attempt == 0:
                    time.sleep(0.25)
                    continue
                raise BrainError(last) from None
        raise BrainError(last or "unknown")

    # ---------- 对外 ----------

    def chat(self, system: str, user: str, max_tokens: int | None = None,
             temperature: float | None = None) -> str | None:
        if not self.enabled:
            return None
        t0 = time.time()
        try:
            data = self._post({
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": max_tokens or self.max_tokens,
                "temperature": self.temperature if temperature is None else temperature,
                "thinking": {"type": "enabled" if self.thinking else "disabled"},
                "stream": False,
            })
            text = (data["choices"][0]["message"]["content"] or "").strip()
            # 被 max_tokens 截断时，正文会停在一个字中间（「亮了一中」「撑不」）。
            # 这是最难自查的破绽：读起来像人写的，但句子是断的。
            self.last_finish = str(data["choices"][0].get("finish_reason") or "")
            ms = int((time.time() - t0) * 1000)
            self.last_ms = ms
            self.calls += 1
            if ms > 3000:
                log.warning("LLM 偏慢：%dms", ms)
            if not text:
                return None
            return clean(text)
        except Exception as e:
            log.error("LLM 调用失败（%dms）：%s", int((time.time() - t0) * 1000), e)
            return None

    def chat_tools(self, system: str, user: str, *, max_tokens: int | None = None,
                   temperature: float | None = None, max_rounds: int = 2) -> str | None:
        """带工具的对话：模型要查就查，查完再答。工具在本地执行（DeepSeek 不支持服务端搜索）。"""
        if not self.enabled:
            return None
        from . import tools
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": user}]
        t0 = time.time()
        for _ in range(max_rounds):
            try:
                data = self._post({
                    "model": self.model,
                    "messages": messages,
                    "tools": tools.schemas(),
                    "tool_choice": "auto",
                    "max_tokens": max_tokens or self.max_tokens,
                    "temperature": self.temperature if temperature is None else temperature,
                    "thinking": {"type": "enabled" if self.thinking else "disabled"},
                    "stream": False,
                })
            except Exception as e:
                log.error("工具调用失败：%s", e)
                return None
            msg = (data.get("choices") or [{}])[0].get("message") or {}
            calls = msg.get("tool_calls") or []
            if not calls:
                text = clean((msg.get("content") or "").strip())
                self.last_ms = int((time.time() - t0) * 1000)
                self.calls += 1
                return text or None
            messages.append({"role": "assistant", "content": msg.get("content") or "",
                             "tool_calls": calls})
            for c in calls:
                fn = (c.get("function") or {})
                result = tools.run(fn.get("name") or "", tools.parse_args(fn.get("arguments") or ""))
                messages.append({"role": "tool", "tool_call_id": c.get("id") or "",
                                 "content": str(result)[:2000]})
        # 用完轮次还没答，逼它直接回答
        try:
            data = self._post({"model": self.model, "messages": messages,
                               "max_tokens": max_tokens or self.max_tokens,
                               "temperature": self.temperature if temperature is None else temperature,
                               "stream": False})
            msg = (data.get("choices") or [{}])[0].get("message") or {}
            self.last_ms = int((time.time() - t0) * 1000)
            self.calls += 1
            return clean((msg.get("content") or "").strip()) or None
        except Exception:
            return None

    def chat_many(self, jobs: list[dict], workers: int = 4) -> list[str | None]:
        """并行跑多条。jobs: [{system,user,max_tokens?,temperature?}]"""
        if not self.enabled or not jobs:
            return [None] * len(jobs)
        if len(jobs) == 1:
            j = jobs[0]
            return [self.chat(j.get("system", ""), j.get("user", ""),
                              j.get("max_tokens"), j.get("temperature"))]
        out: list[str | None] = [None] * len(jobs)
        with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as ex:
            futs = {
                ex.submit(self.chat, j.get("system", ""), j.get("user", ""),
                          j.get("max_tokens"), j.get("temperature")): i
                for i, j in enumerate(jobs)
            }
            for f, i in futs.items():
                try:
                    out[i] = f.result()
                except Exception:
                    out[i] = None
        return out

    def warmup(self) -> None:
        """预热连接，第一次真实请求不用再握手。"""
        if not self.enabled:
            return
        try:
            self.chat("只回复一个字", "ping", max_tokens=1, temperature=0)
        except Exception:
            pass

    def fallback(self, kind: str = "comment") -> str:
        if kind == "reply":
            return random.choice(["看到啦，谢谢你说这些", "嗯，你这么说我清楚了", "好，我知道了"])
        return random.choice(FALLBACK_COMMENTS)

    def pick_fallback(self, same_post: list[str] | None = None,
                      recent: list[str] | None = None) -> str:
        """兜底也要躲重复。

        兜底是在「模型没生成出来」或「生成的和最近太像」时才走的，
        以前直接 random.choice 一个六条的小池子，32 个号一天下来必然撞车。
        这里先按反复读规则筛一遍，筛不出就退回全集。
        """
        from . import antirepeat
        pool = list(FALLBACK_COMMENTS)
        avoid = list(same_post or []) + list(recent or [])[-30:]
        if avoid:
            ok = [x for x in pool
                  if not antirepeat.check(x, same_post=same_post or [], recent=avoid)]
            if ok:
                pool = ok
        return random.choice(pool)

    def probe(self, system: str, user: str, max_tokens: int | None = None) -> dict:
        """给 Web UI 用：返回耗时 / 结果 / 报错，不抛异常。"""
        if not self.enabled:
            return {"ok": False, "error": "未配置 api_key", "text": ""}
        t0 = time.time()
        try:
            data = self._post({
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": max_tokens or self.max_tokens,
                "temperature": self.temperature,
                "stream": False,
            })
            text = clean((data["choices"][0]["message"]["content"] or "").strip())
            usage = data.get("usage") or {}
            return {
                "ok": True,
                "text": text,
                "ms": int((time.time() - t0) * 1000),
                "tokens": usage.get("total_tokens"),
                "model": data.get("model") or self.model,
            }
        except Exception as e:
            return {"ok": False, "error": str(e), "text": "",
                    "ms": int((time.time() - t0) * 1000)}


class BrainError(RuntimeError):
    pass


def clean(text: str) -> str:
    """去掉模型偶尔带出来的引号、前缀和 Markdown。"""
    t = (text or "").strip()
    if not t:
        return ""
    for _ in range(3):
        before = t
        if len(t) >= 2 and t[0] in "\"'“”「」" and t[-1] in "\"'“”「」":
            t = t[1:-1].strip()
        for p in ("帖子正文：", "帖子：", "评论：", "回复：", "正文：", "评论内容："):
            if t.startswith(p):
                t = t[len(p):].strip()
        t = t.strip("*`_# ").strip()
        if t == before:
            break
    return t.replace("\n\n", "\n")
