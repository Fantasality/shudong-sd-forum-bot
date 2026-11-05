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

    def allow(self, kind: str, manual: bool = False) -> tuple[bool, str]:
        """kind 例如 comment / like / post / reply。

        manual=True 表示用户在图示上手动点了一次，跳过静默时段与配额，
        但仍然受黑名单和重复检测约束（那两条是内容质量，不是风控）。
        """
        if manual:
            return True, ""
        if self.in_quiet_hours():
            return False, "处于静默时段"
        dlimit = self.daily.get(kind)
        if dlimit is not None and self.store.count_today(kind) >= int(dlimit):
            return False, f"已达每日 {kind} 上限 {dlimit}"
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
