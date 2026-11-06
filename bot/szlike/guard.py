"""风控闸门：静默时段、配额、关键词、随机抖动。

所有写操作都必须先过这一层。想调松就改 config.json 里的 guard 段。
"""
from __future__ import annotations

import logging
import random
import time

from .store import Store

log = logging.getLogger("szlike.guard")


class Guard:
    def __init__(self, cfg: dict, store: Store):
        self.cfg = cfg or {}
        self.store = store
        self.quiet = tuple(self.cfg.get("quiet_hours") or [0, 7])
        self.blacklist = [w for w in (self.cfg.get("skip_keywords") or []) if w]
        self.daily = self.cfg.get("daily_limits") or {}
        self.hourly = self.cfg.get("hourly_limits") or {}
        self.jitter = self.cfg.get("jitter_seconds") or [20, 90]
        # 账号图谱层约束。2026-11-05 真人举报「默认头像 / 一两天内密集发帖 /
        # 评论几乎是一起来的」，这三条都不是文本问题，原来的 per-account 配额挡不住。
        self.behavior = self.cfg.get("behavior") or {}

    # ---------- 时间 ----------

    def in_quiet_hours(self) -> bool:
        h = time.localtime().tm_hour
        start, end = self.quiet
        if start <= end:
            return start <= h < end
        return h >= start or h < end

    def sleep_jitter(self, scale: float = 1.0) -> None:
        """写操作前的随机停顿。手动触发（scale=0）时直接跳过，避免点一下等一分钟。"""
        if scale <= 0:
            return
        lo, hi = self.jitter
        time.sleep(max(0.0, random.uniform(lo, hi) * scale))

    # ---------- 准入 ----------

    def allow(self, kind: str, manual: bool = False, *, account: str = "",
              behavior: dict | None = None) -> tuple[bool, str]:
        """kind 例如 comment / like / post / reply。

        两条**前置硬闸**在 manual 之前，且不看单个账号：
          ① 账号冷启动：新号在 min_account_age_days 内只允许 like；
          ② 全局跨账号配额：33 个号各自「每天 3 帖」= 全局 99 帖，
             真人看到的是整个论坛的密度，不是某一个号的配额。
        以前 sentinel 用 manual=True 直接跳过全部风控，一次派 4 个号评同一帖，
        所以这两条必须放在 manual 分支之前才拦得住。
        """
        b = behavior or self.behavior
        age = None
        min_d = 0.0
        ramp = 0.0
        if b.get("enabled", True):
            if account:
                age = self.store.account_age_days(account)
                min_d = float(b.get("min_account_age_days") or 0)
                ramp = float(b.get("ramp_days") or 0)
                if age < min_d:
                    allowed = set(b.get("new_account_allowed_kinds") or [])
                    if kind not in allowed:
                        return False, (f"账号登记仅 {age:.1f} 天，冷启动 {min_d:.0f} 天内"
                                       f"禁止 {kind}（只允许 {'/'.join(sorted(allowed)) or '无'}）")
            gl = (b.get("global_limits") or {}).get(kind)
            if gl is not None and self.store.global_count_today(kind) >= int(gl):
                return False, f"全账号今日 {kind} 已达上限 {gl}"
            gh = (b.get("global_hourly") or {}).get(kind)
            if gh is not None and self.store.global_count_last_hour(kind) >= int(gh):
                return False, f"全账号近 1 小时 {kind} 已达上限 {gh}"

        if manual:
            return True, ""
        if self.in_quiet_hours():
            return False, "处于静默时段"
        dlimit = self.daily.get(kind)
        if dlimit is not None:
            lim = int(dlimit)
            # 冷启动结束后再花 ramp_days 线性放开，避免第 8 天突然全速发车
            if age is not None and ramp > 0 and min_d <= age < min_d + ramp:
                lim = max(1, int(lim * (age - min_d) / ramp))
            if self.store.count_today(kind) >= lim:
                return False, f"已达每日 {kind} 上限 {lim}"
        hlimit = self.hourly.get(kind)
        if hlimit is not None and self.store.count_last_hour(kind) >= int(hlimit):
            return False, f"已达每小时 {kind} 上限 {hlimit}"
        return True, ""

    def text_ok(self, text: str) -> tuple[bool, str]:
        """检查帖子内容是否适合被自动互动。"""
        t = (text or "").strip()
        if len(t) < 6:
            return False, "内容过短"
        if len(t) > 800:
            return False, "内容过长"
        for w in self.blacklist:
            if w in t:
                return False, f"命中黑名单词「{w}」"
        return True, ""

    def similar(self, kind: str, text: str, threshold: float = 0.75) -> bool:
        """和最近发过的内容做粗粒度相似度比对，防复读。"""
        if not text:
            return False
        a = set(text)
        for old in self.store.recent_texts(kind, 30):
            b = set(old)
            if not a or not b:
                continue
            inter = len(a & b)
            union = len(a | b)
            if union and inter / union >= threshold:
                return True
        return False
