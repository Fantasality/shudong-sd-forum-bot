"""szlikeyou（鼠洞SD）GraphQL 客户端。

核心特性：
- 零第三方依赖，只用标准库
- accessToken 快过期时用 refreshToken 自动续期，并原子落盘
- 内置请求限速与抖动，避免被风控
- 401/403 自动强刷一次后重试
"""
from __future__ import annotations

import base64
import json
import logging
import os
import random
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("szlike.client")

ENDPOINT = "https://api.szlikeyou.com/graphql"
LOGIN_BASIC_Q = (
    "mutation L($u:String!,$p:String!){ login(input:{basicInput:{username:$u,password:$p}})"
    "{ accessToken refreshToken __typename } }"
)
REFRESH_Q = (
    "mutation R($rt:String!){ login(input:{refreshTokenInput:{refreshToken:$rt}})"
    "{ accessToken refreshToken __typename } }"
)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/144.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) "
    "NetType/WIFI MiniProgramEnv/Windows WindowsWechat/WMPF "
    "WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf2541f37) XWEB/25799"
)


class SzlikeError(RuntimeError):
    pass


class AuthError(SzlikeError):
    pass


class FileLock:
    """跨进程互斥锁（Windows / POSIX 通用）。

    refreshToken 每次刷新都会轮换，daemon 和 Web UI 如果同时刷新，
    后刷的那个会拿着已作废的 token 去请求，双方一起失效。必须串行。
    """

    def __init__(self, path: str, timeout: float = 15.0, stale: float = 30.0):
        self.path = path
        self.timeout = timeout
        self.stale = stale
        self.fd: int | None = None

    def __enter__(self) -> "FileLock":
        deadline = time.time() + self.timeout
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > self.stale:
                        os.unlink(self.path)   # 死进程留下的锁
                        continue
                except FileNotFoundError:
                    continue
                if time.time() > deadline:
                    raise SzlikeError("等待 token 锁超时，另一个进程可能卡住了") from None
                time.sleep(0.05)

    def __exit__(self, *exc) -> None:
        try:
            if self.fd is not None:
                os.close(self.fd)
        finally:
            self.fd = None
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass


def jwt_payload(token: str) -> dict:
    """解出 JWT payload（只读，不校验签名）。"""
    part = token.split(".")[1]
    part += "=" * (-len(part) % 4)
    return json.loads(base64.urlsafe_b64decode(part))


class TokenStore:
    """tokens.json 的读写。refreshToken 会轮换，必须持久化最新值。"""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        if not os.path.exists(path):
            raise SzlikeError(f"找不到 {path}，请先放好 tokens.json")
        with open(path, encoding="utf-8") as f:
            self.data = json.load(f)

    def save(self) -> None:
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)

    @property
    def access(self) -> str:
        return self.data["access_token"]

    @property
    def refresh(self) -> str:
        return self.data["refresh_token"]

    def access_left(self) -> float:
        exp = self.data.get("access_exp")
        if not exp:
            exp = jwt_payload(self.access)["exp"]
        return exp - time.time()

    def refresh_left(self) -> float:
        exp = self.data.get("refresh_exp")
        if not exp:
            exp = jwt_payload(self.refresh)["exp"]
        return exp - time.time()


class Client:
    def __init__(self, app_id: str, school_id: str, tokens_path: str = "tokens.json",
                 min_interval: float = 0.6, timeout: float = 25.0,
                 account_path: str | None = None):
        self.app_id = app_id
        self.school_id = school_id
        self.tokens = TokenStore(tokens_path)
        self.account_path = account_path
        self.min_interval = min_interval
        self.timeout = timeout
        self._last_call = 0.0
        self._last_refresh_ts = 0.0
        self._refresh_lock = threading.Lock()

    # ---------- 底层 ----------

    def _headers(self, with_auth: bool = True) -> dict:
        h = {
            "content-type": "application/json",
            "mp-app-id": self.app_id,
            "mp-school-id": self.school_id,
            "xweb_xhr": "1",
            "accept": "*/*",
            "accept-language": "zh-CN,zh;q=0.9",
            "user-agent": UA,
            "referer": f"https://servicewechat.com/{self.app_id}/16/page-frame.html",
        }
        if with_auth:
            h["authorization"] = "Bearer " + self.tokens.access
        return h

    def _throttle(self) -> None:
        gap = time.time() - self._last_call
        wait = self.min_interval - gap
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.25))
        self._last_call = time.time()

    def _post(self, query: str, variables: dict | None, operation: str,
              with_auth: bool = True) -> dict:
        self._throttle()
        body = json.dumps(
            {"operationName": operation, "variables": variables or {}, "query": query}
        ).encode()
        req = urllib.request.Request(
            ENDPOINT, data=body, method="POST", headers=self._headers(with_auth)
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raw = ""
            try:
                raw = e.read().decode()[:400]
            except Exception:
                pass
            raise SzlikeError(f"HTTP {e.code}: {raw}") from None
        except urllib.error.URLError as e:
            raise SzlikeError(f"网络错误: {e.reason}") from None

    # ---------- 续期 ----------

    def ensure_token(self, threshold: float = 21600) -> None:
        """accessToken 剩余不足 threshold 秒就续期（默认 6 小时）。"""
        if self.tokens.access_left() > threshold:
            return
        self.refresh_now()

    def _apply_login(self, lg: dict) -> None:
        self.tokens.data = {
            "access_token": lg["accessToken"],
            "refresh_token": lg["refreshToken"],
            "access_exp": jwt_payload(lg["accessToken"])["exp"],
            "refresh_exp": jwt_payload(lg["refreshToken"])["exp"],
            "updated_at": int(time.time()),
        }
        self.tokens.save()
        log.info(
            "token 已更新：access 剩 %.1fh，refresh 剩 %.1f 天",
            self.tokens.access_left() / 3600,
            self.tokens.refresh_left() / 86400,
        )

    def login_basic(self) -> bool:
        """用账号文件里的用户名/密码重新登录。

        非微信注册的号走这条路，refreshToken 到期也能自救，
        等于永久免托管。没有账号文件就返回 False。
        """
        if not self.account_path or not os.path.exists(self.account_path):
            return False
        try:
            with open(self.account_path, encoding="utf-8") as f:
                cred = json.load(f)
            u, p = cred.get("username"), cred.get("password")
            if not (u and p):
                return False
            data = self._post(LOGIN_BASIC_Q, {"u": u, "p": p}, "L")
            if data.get("errors"):
                log.error("密码登录被拒绝：%s",
                          json.dumps(data["errors"], ensure_ascii=False)[:300])
                return False
            self._apply_login(data["data"]["login"])
            log.info("已用用户名 %s 重新登录", u)
            return True
        except Exception as e:
            log.error("密码登录失败：%r", e)
            return False

    def refresh_now(self, force: bool = False) -> None:
        lock_path = self.tokens.path + ".lock"
        with FileLock(lock_path):
            # 抢到锁后先重读磁盘：可能别的进程刚刷完，我手上的已经作废了
            try:
                disk = TokenStore(self.tokens.path)
            except Exception:
                disk = None
            if disk and disk.refresh != self.tokens.refresh:
                log.info("检测到其他进程已刷新过 token，直接采用")
                self.tokens.data = disk.data
                return

            # 60 秒内刚刷过就不再刷，避免无谓轮换；force=True 无视此限制
            if not force and time.time() - self._last_refresh_ts < 60:
                return
            if self.tokens.refresh_left() <= 0:
                if self.login_basic():
                    return
                raise AuthError("refreshToken 也已过期，需要重新从小程序登录取新 token")
            self._last_refresh_ts = time.time()

            try:
                data = self._post(REFRESH_Q, {"rt": self.tokens.refresh}, "R")
            except SzlikeError as e:
                if self.login_basic():
                    return
                raise AuthError(f"续期请求失败：{e}") from None

            if "errors" in data:
                if self.login_basic():
                    return
                raise AuthError(f"续期被拒绝：{json.dumps(data['errors'], ensure_ascii=False)[:300]}")

            self._apply_login(data["data"]["login"])

    # ---------- 业务调用 ----------

    def gql(self, query: str, variables: dict | None = None, operation: str = "I") -> dict:
        """执行 GraphQL。自动续期 + 401 重试一次。"""
        self.ensure_token()
        try:
            data = self._post(query, variables, operation)
        except SzlikeError as e:
            msg = str(e)
            if "HTTP 401" in msg or "HTTP 403" in msg:
                log.warning("鉴权失败，强制续期后重试")
                self.refresh_now()
                data = self._post(query, variables, operation)
            else:
                raise

        if data.get("errors"):
            detail = json.dumps(data["errors"], ensure_ascii=False)
            raise SzlikeError(f"GraphQL 错误: {detail[:500]}")
        return data.get("data") or {}
