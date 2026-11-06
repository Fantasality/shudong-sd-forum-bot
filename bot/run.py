"""鼠洞SD 自动化机器人 CLI。

用法：
  python run.py status             看所有账号、token、启停状态
  python run.py doctor [-a NAME]   体检某个账号
  python run.py keepalive          续期所有启用的账号
  python run.py feed               看当前查看账号的最新帖
  python run.py replies            看别人给我的评论/回复
  python run.py post -c "内容" [-a NAME]
  python run.py comment --id POSTID -c "内容"
  python run.py like --id POSTID
  python run.py once autolike [-a NAME]    跑一次某任务（不受配额/静默限制）
  python run.py daemon             常驻：所有 enabled 账号各一个线程并行跑
"""
from __future__ import annotations

import argparse
import atexit
import json
import logging
import os
import random
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from szlike import (ops, persona, profile_gate, sentinel as sentinel_mod,  # noqa: E402
                    whisper as whisper_mod)
from szlike.brain import Brain                         # noqa: E402
from szlike.client import Client, SzlikeError, jwt_payload  # noqa: E402
from szlike.config import (                            # noqa: E402
    account_config, accounts_index, load, resolve_account, save,
)
from szlike.guard import Guard                         # noqa: E402
from szlike.store import Store                         # noqa: E402
from szlike.tasks import (                             # noqa: E402
    Ctx, autolike, due_drafts, publish_draft, random_comment, reply_comments,
    scheduled_post,
)

CONFIG_PATH = BASE / "config.json"
STATE_PATH = BASE / "state.db"
LOG_DIR = BASE / "logs"
PID_PATH = BASE / "daemon.pid"
INTERVAL_TASKS = ("reply_comments", "autolike", "random_comment")


def setup_logging(verbose: bool = False) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "bot.log", encoding="utf-8"),
    ]
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(f"找不到 {CONFIG_PATH}")
    return load(str(CONFIG_PATH))


def build(cfg: dict, name: str | None = None, manual: bool = False):
    d = account_config(cfg, str(BASE), name)
    acc = d["_account"]
    if not Path(acc["tokens"]).exists():
        sys.exit(f"找不到账号凭据 {acc['tokens']}（账号 {acc['label']}）")
    acct_file = BASE / f"account_{acc['name']}.json"
    client = Client(d["app_id"], d["school_id"], acc["tokens"],
                    account_path=str(acct_file) if acct_file.exists() else None)
    store = Store(str(STATE_PATH), ns="" if acc["name"] == "main" else acc["name"])
    brain = Brain(d.get("llm"))
    guard = Guard(d.get("guard"), store)
    ctx = Ctx(client, store, brain, guard, d, manual=manual)
    return client, store, brain, guard, ctx


def fmt_left(sec: float) -> str:
    if sec <= 0:
        return "已过期"
    if sec > 86400:
        return f"{sec / 86400:.1f} 天"
    return f"{sec / 3600:.1f} 小时"


# ---------- 状态 ----------


def cmd_status() -> None:
    cfg = load_config()
    store = Store(str(STATE_PATH))
    beat = store.workers()
    print(f"{'账号':10s} {'启用':4s} {'人设':10s} {'access':>8s} {'refresh':>9s} "
          f"{'今日 发/评/赞':>14s}  标签")
    print("-" * 86)
    for a in accounts_index(cfg):
        line = [a["name"][:10], "ON" if a["enabled"] else "off", a["persona"] or "-"]
        tp = BASE / a["tokens"]
        left_a = left_r = "-"
        if tp.exists():
            try:
                c = Client(cfg["app_id"], cfg["school_id"], str(tp))
                left_a = fmt_left(c.tokens.access_left())
                left_r = fmt_left(c.tokens.refresh_left())
            except Exception as e:
                left_a = "ERR"
        s = Store(str(STATE_PATH), ns="" if a["name"] == "main" else a["name"])
        st = s.stats_today()
        line += [left_a.rjust(8), left_r.rjust(9),
                 f'{st["post"]}/{st["comment"]}/{st["like"]}'.rjust(14), a["label"]]
        print(" ".join(str(x) for x in line))
        w = beat.get(a["name"])
        if w:
            print(f"{'':10s}   worker: {w['status']} {w['ago']}s 前  {w['note'][:50]}")
    print()


def cmd_doctor(name: str | None = None) -> None:
    cfg = load_config()
    client, store, brain, guard, ctx = build(cfg, name)
    acc = ctx.cfg["_account"]

    print("=" * 62)
    print(f"账号：{acc['label']}（{acc['name']}）  persona={ctx.persona}")
    p = jwt_payload(client.tokens.access)
    print(f"  access  : 剩 {fmt_left(client.tokens.access_left())}  (uid={p.get('uid')})")
    print(f"  refresh : 剩 {fmt_left(client.tokens.refresh_left())}")

    print("\n续期能力")
    if Path(f"account_{acc['name']}.json").exists():
        ok = client.login_basic()
        print(f"  用户名密码登录：{'可用（refresh 过期也能自救）' if ok else '失败'}")
    else:
        print("  用户名密码登录：无账号文件（微信号只能靠 refreshToken）")

    print("\n账号信息")
    v = ops.viewer(client)
    me = v.get("user") or {}
    prof = me.get("profile") or {}
    print(f"  昵称: {prof.get('nickname') or '(空昵称)'}  user_id={me.get('id')}")

    print("\n板块")
    for t in ops.topics(client, cfg["school_id"]):
        subs = "、".join(s["node"]["name"] for s in t["subtopics"]["edges"])
        print(f"  {t['name']}: {subs}")

    print("\n最新 3 帖")
    for n in ops.feed_posts(client, cfg["school_id"], 1)[:3]:
        print(f"  {n['id']} 赞{n['likeCount']} 评{n['commentCount']} | {n['content'][:36]}")

    print("\n运行配置")
    print(f"  dry_run = {cfg.get('dry_run')}")
    print(f"  LLM     = {'已配置 ' + brain.model if brain.enabled else '未配置'}")
    print("=" * 62)


def cmd_keepalive(name: str | None = None) -> None:
    cfg = load_config()
    names = [name] if name else [a["name"] for a in accounts_index(cfg) if a["enabled"]]
    for n in names:
        try:
            d = account_config(cfg, str(BASE), n)
            acc = d["_account"]
            af = BASE / f"account_{n}.json"
            c = Client(d["app_id"], d["school_id"], acc["tokens"],
                       account_path=str(af) if af.exists() else None)
            c.refresh_now(force=True)
            print(f"{acc['label']}: access 剩 {fmt_left(c.tokens.access_left())}，"
                  f"refresh 剩 {fmt_left(c.tokens.refresh_left())}")
        except Exception as e:
            print(f"{n}: 失败 {e}")


def cmd_feed(name: str | None = None) -> None:
    cfg = load_config()
    client, *_ = build(cfg, name)
    for n in ops.feed_posts(client, cfg["school_id"], 2):
        like = "♥" if n["viewerHasLiked"] else " "
        print(f"{like} {n['id']}  {n['createdAt'][:16]}  赞{n['likeCount']:>3} 评{n['commentCount']:>2}")
        print(f"   {n['content'][:70]}")


def cmd_replies(name: str | None = None) -> None:
    cfg = load_config()
    client, *_ = build(cfg, name)
    d = ops.latest_replies(client)
    print("—— 收到的评论 ——")
    for e in (d.get("userComments") or {}).get("edges") or []:
        n = e["node"]
        print(f"  [{n['id']}] {(n.get('creator') or {}).get('profile', {}).get('nickname')}")
        print(f"    帖: {(n.get('post') or {}).get('content', '')[:50]}")
        print(f"    评: {n['content'][:50]}")
    print("—— 收到的回复 ——")
    for e in (d.get("userSubcomments") or {}).get("edges") or []:
        n = e["node"]
        print(f"  [{n['id']}] {(n.get('creator') or {}).get('profile', {}).get('nickname')}")
        print(f"    回: {n['content'][:50]}")


def cmd_sentinel(args) -> None:
    cfg = load_config()
    if not (cfg.get("sentinel") or {}).get("enabled", True):
        print("哨兵已在 config.sentinel.enabled 里关掉")
        return
    if args.raid:
        raw = args.raid.strip().lstrip("#")
        pid = raw if raw.startswith("UG9zd") else whisper_mod.code_to_id(raw)
        if not pid:
            sys.exit("帖代号不对")
        sent = sentinel_mod.dispatch(cfg, pid, args.count)
        for x in sent:
            print(f"  {x['account']}: {x['content'][:30]}")
        print(f"派出 {len(sent)} 个号")
        return

    if not args.watch:
        print("用法：run.py sentinel --watch [--loop 90] | --raid <代号> [--count 4]")
        return

    while True:
        got = sentinel_mod.watch(cfg, args.count)
        for x in got:
            print(f"  → {x['account']}: {x['content'][:30]}")
        if not args.loop:
            break
        time.sleep(args.loop)


def cmd_whisper(args) -> None:
    cfg = load_config()
    if not (cfg.get("whisper") or {}).get("enabled", True) and not getattr(args, "force", False):
        print("暗语通道已停用（config.whisper.enabled = false）。代码保留，要临时启用加 --force。")
        return
    if args.drop:
        raw = args.drop.strip().lstrip("#")
        pid = raw if raw.startswith("UG9zd") else whisper_mod.code_to_id(raw)
        if not pid:
            sys.exit("帖代号不对，应该是 App 里 # 后面那串")
        cur = load(str(CONFIG_PATH), with_defaults=False)
        cur.setdefault("whisper", {})["drop_post_id"] = pid
        save(str(CONFIG_PATH), cur)
        print(f"暗桩帖已设为 #{raw}  ({pid})")
        return

    if not (cfg.get("whisper") or {}).get("drop_post_id") and not args.rollcall:
        print("还没设暗桩帖。先在 App 里把一条帖子设为暗桩帖，复制代号，然后：")
        print("  python run.py whisper --drop <代号>")
        print("  python run.py whisper --rollcall")
        return

    names = [args.account] if args.account else [
        k for k, v in (cfg.get("accounts") or {}).items() if v.get("enabled")
    ]
    if not names:
        sys.exit("没有启用中的账号")

    while True:
        for name in names:
            try:
                _, _, _, _, ctx = build(cfg, name, manual=True)
                w = whisper_mod.Whisper(ctx)
                if args.rollcall:
                    anchor_id = w.roll_call(force=args.force)
                    print(f"[{name}] 报到 {anchor_id or '失败'}", flush=True)
                    if args.gap and name != names[-1]:
                        time.sleep(random.uniform(args.gap * 0.6, args.gap * 1.4))
                for item in w.poll():
                    print(f"[{name}] {item['cmd']} -> {item['ack']}")
            except SystemExit:
                raise
            except Exception as e:                     # 单个号出错不影响其他号
                print(f"[{name}] 出错：{e}")
        if not args.loop:
            break
        time.sleep(args.loop)


def cmd_post(args) -> None:
    cfg = load_config()
    client, store, _, _, ctx = build(cfg, args.account)
    if ctx.dry_run and not args.force:
        print("dry_run=true，加 --force 才会真的发出去")
        print(f"拟发：{args.content}")
        return
    res = ops.create_post(
        client, args.content, cfg["school_id"], args.topic or cfg["default_topic_id"],
        args.subtopic or cfg["default_subtopic_id"], cfg.get("is_anonymous", False),
    )
    print(f"已发帖 {res['id']}")


def cmd_comment(args) -> None:
    cfg = load_config()
    client, store, _, _, ctx = build(cfg, args.account)
    if ctx.dry_run and not args.force:
        print("dry_run=true，加 --force 才会真的发出去")
        return
    res = ops.create_comment(client, args.id, args.content, cfg.get("is_anonymous", False))
    print(f"已评论 {res['id']}")


def cmd_like(args) -> None:
    cfg = load_config()
    client, *_ = build(cfg, args.account)
    ops.add_like(client, args.id)
    print(f"已点赞 {args.id}")


TASKS = {
    "scheduled_post": None,
    "reply_comments": reply_comments,
    "autolike": autolike,
    "random_comment": random_comment,
}


def cmd_once(args) -> None:
    cfg = load_config()
    client, store, brain, guard, ctx = build(cfg, args.account, manual=True)
    name = args.task
    if name == "scheduled_post":
        ok, msg = scheduled_post(ctx, datetime.now().strftime("%H:%M"))
    elif name in TASKS and TASKS[name]:
        ok, msg = TASKS[name](ctx)
    else:
        sys.exit(f"未知任务 {name}，可选：{list(TASKS)}")
    print(("已执行" if ok else "未执行") + f"：{msg}")


# ---------- daemon ----------


class AccountWorker(threading.Thread):
    """一个账号一个线程：自己的 client / store / 调度 / 续期。"""

    def __init__(self, name: str, index: int = 0):
        super().__init__(name=f"acct-{name}", daemon=True)
        self.acct = name
        self.index = index
        self.stop_evt = threading.Event()
        self._brain = None
        self._brain_sig = None
        self._client = None
        self._store = Store(str(STATE_PATH), ns="" if name == "main" else name)
        self._next_due = {}
        self._last_token_check = 0.0
        self._last_slot = ""
        # 错峰：每个号启动时间不同，避免三个号同一秒动作
        self._start_at = time.time() + 5 + index * 11 + random.uniform(0, 6)

    def stop(self) -> None:
        self.stop_evt.set()

    # -- 内部 --

    def _beat(self, status: str, note: str = "") -> None:
        try:
            self._store.beat(self.acct, status, note)
        except Exception:
            pass

    def run(self) -> None:
        log = logging.getLogger(f"szlike.{self.acct}")
        log.info("[%s] worker 已启动，%.0fs 后开始", self.acct, self._start_at - time.time())
        while not self.stop_evt.is_set():
            try:
                if time.time() < self._start_at:
                    self._beat("starting", "错峰等待中")
                    self.stop_evt.wait(2)
                    continue

                cfg = load(str(CONFIG_PATH))
                d = account_config(cfg, str(BASE), self.acct)
                acc = d["_account"]
                if not acc["enabled"]:
                    self._beat("disabled", "账号未启用")
                    self.stop_evt.wait(10)
                    continue

                self._ensure_client(d)
                self._ensure_brain(d)
                guard = Guard(d.get("guard"), self._store)
                ctx = Ctx(self._client, self._store, self._brain, guard, d, manual=False)

                now = time.time()

                # 账号画像门禁：真人第一条指纹就是「都是默认 id 头像」。
                # 实测 33/33 个号 profile 全为 null，没设昵称的号一律不准上线。
                # 每小时查一次即可，别每轮都打 viewer。
                beh = cfg.get("behavior") or {}
                if beh.get("enabled", True) and beh.get("require_custom_nickname"):
                    if now - getattr(self, "_last_profile_check", 0) > 3600:
                        self._last_profile_check = now
                        ready, why = profile_gate.check(self._client)
                        if not ready:
                            self._beat("blocked", why)
                            log.warning("[%s] 账号画像门禁：%s", self.acct, why)
                            self.stop_evt.wait(300)
                            continue

                # 在线 L3 自检：最近 24h 出现「同帖多号」就静默。
                # 5 分钟爆发只记观察、不阻断：真人 48 条窗口 burst5_max 的 p90=6
                # （tools/graph_baseline.json 实测），几个号短时同时出现本身不是破绽。
                # 离线评估通过不等于运行时安全 —— 这是最后一道兜底，
                # 也是 2026-11-05 被真人翻两屏识破后补上的那一层。
                if beh.get("live_graph_gate"):
                    if now - getattr(self, "_last_graph_check", 0) > 600:
                        self._last_graph_check = now
                        try:
                            from szlike import grapheval as _ge
                            acts = [a for a in _ge.load_actions(str(STATE_PATH))
                                    if a["ts"] >= now - 86400
                                    and a["kind"] in ("comment", "reply")]
                            m = _ge.metrics(acts)
                            if m["burst5_max"] > 3:
                                log.warning("[%s] 图谱观察：5 分钟内 %s 个号"
                                            "（真人 p90=6，非阻断项）",
                                            self.acct, m["burst5_max"])
                            # 唯一不可妥协的是我们自己定的永久单 owner 策略
                            if m["accounts_per_post_max"] > 1:
                                why = (f"图谱自检未过：同帖最多 "
                                       f"{m['accounts_per_post_max']} 个号")
                                self._beat("blocked", why)
                                log.warning("[%s] %s", self.acct, why)
                                self.stop_evt.wait(300)
                                continue
                        except Exception as e:
                            log.warning("[%s] 图谱自检异常（放行但记账）：%s", self.acct, e)

                # 暗语通道：被「先歇会儿」按住时整个号静默
                if whisper_mod.is_paused(self.acct):
                    self._beat("paused", "暗语暂停中")
                    self.stop_evt.wait(20)
                    continue

                if now - getattr(self, "_last_whisper", 0) > 100:
                    self._last_whisper = now
                    try:
                        self._run(ctx, "whisper", lambda c=ctx: whisper_mod.run(c))
                    except Exception as e:
                        log.warning("暗语通道异常：%s", e)

                if now - self._last_token_check > 1800:
                    self._maintain_token(d)
                    self._last_token_check = now

                # 发布人工审核通过的草稿。放在定时发帖之前：人工点过头的优先，
                # 但和定时帖共用「每号每天 1 帖」的配额，所以不会一天冒两条。
                if now - getattr(self, "_last_review", 0) > 60:
                    self._last_review = now
                    try:
                        for dd in due_drafts(ctx, limit=1):
                            if ctx.store.count_today("post") > 0:
                                break
                            self._run(ctx, "publish_draft",
                                      lambda x=dd: publish_draft(ctx, x))
                    except Exception as e:
                        log.warning("待审队列检查失败：%s", e)

                # 定时发帖
                sp = d["tasks"].get("scheduled_post") or {}
                if sp.get("enabled"):
                    now_dt = datetime.now()
                    hhmm = now_dt.strftime("%H:%M")
                    slot = now_dt.strftime("%Y-%m-%d") + hhmm
                    if hhmm in self._due_times(now_dt, sp.get("times") or [])                             and slot != self._last_slot:
                        self._last_slot = slot
                        self._run(ctx, "scheduled_post", lambda: scheduled_post(ctx, hhmm))

                # 间隔任务
                for name in INTERVAL_TASKS:
                    c = d["tasks"].get(name) or {}
                    if not c.get("enabled"):
                        continue
                    due = self._next_due.get(name)
                    if due is None:
                        # 首次错峰 20-120s，并且不同号之间再错开
                        due = now + random.uniform(20, 120) + self.index * 15
                        self._next_due[name] = due
                        continue
                    if now < due:
                        continue
                    self._next_due[name] = now + max(120, int(c.get("interval") or 600))
                    self._run(ctx, name, lambda n=name: TASKS[n](ctx))

                nxt = min([v for v in self._next_due.values()] or [now])
                self._beat("running", f"下次任务 {max(0, int(nxt - time.time()))}s 后")
                self.stop_evt.wait(5)

            except Exception as e:
                logging.getLogger(f"szlike.{self.acct}").exception("循环异常：%r", e)
                self._beat("error", str(e)[:120])
                self.stop_evt.wait(20)

        self._beat("stopped", "worker 已退出")
        log.info("[%s] worker 退出", self.acct)

    def _due_times(self, dt, times: list[str]) -> list[str]:
        """把配置里的发帖时刻按「日期+账号」抖 ±12 分钟。

        32 个号如果精确地每 30 分钟发一条，列表页翻两屏就能看出是排班。
        抖动是当天固定的（同一天只触发一次），第二天换一组。
        """
        out = []
        for t in times:
            try:
                h, m = (int(x) for x in str(t).split(":")[:2])
            except Exception:
                continue
            base = h * 60 + m + random.Random(f"{self.acct}:{dt:%Y-%m-%d}:{t}").randint(-12, 12)
            base = max(0, min(23 * 60 + 59, base))
            out.append(f"{base // 60:02d}:{base % 60:02d}")
        return out

    def _run(self, ctx, name, fn) -> None:
        log = logging.getLogger(f"szlike.{self.acct}")
        t0 = time.time()
        try:
            # 任务约定返回 (ok, msg)，但暗语模块返回的是 list（收到的指令）。
            # 原来直接 `ok, msg = fn()`，空 list 解不出两个值，每 100 秒刷一条
            # 「暗语通道异常」——功能没问题，日志全是噪音。
            r = fn()
            if isinstance(r, tuple) and len(r) == 2:
                ok, msg = r
            else:
                ok, msg = True, (str(r)[:120] if r else "无动作")
            log.info("[%s] %s -> %s %s（%.1fs）", self.acct, name, ok, msg, time.time() - t0)
            self._beat("running", f"{name}: {msg[:80]}")
        except SzlikeError as e:
            log.error("[%s] %s 失败：%s", self.acct, name, e)
            self._beat("error", f"{name}: {e}"[:120])

    def _ensure_client(self, d: dict) -> None:
        acc = d["_account"]
        if self._client is None:
            af = BASE / f"account_{acc['name']}.json"
            self._client = Client(
                d["app_id"], d["school_id"], acc["tokens"],
                account_path=str(af) if af.exists() else None,
            )

    def _ensure_brain(self, d: dict) -> None:
        llm = d.get("llm") or {}
        sig = (llm.get("base_url"), llm.get("api_key"), llm.get("model"),
               llm.get("temperature"), llm.get("max_tokens"))
        if self._brain is None or sig != self._brain_sig:
            self._brain = Brain(llm)
            self._brain_sig = sig

    def _maintain_token(self, d: dict) -> None:
        log = logging.getLogger(f"szlike.{self.acct}")
        try:
            c = self._client
            # refresh 剩不到 3 天就用密码重登，把 30 天重新续满
            if c.tokens.refresh_left() < 3 * 86400 and c.account_path:
                if c.login_basic():
                    return
            c.ensure_token(threshold=21600)
        except Exception as e:
            log.error("[%s] token 维护失败：%s", self.acct, e)


def cmd_daemon(args) -> None:
    cfg = load_config()
    try:
        PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
        atexit.register(lambda: PID_PATH.unlink(missing_ok=True))
    except Exception:
        pass

    _warm_vectors()
    try:
        # 待审队列每天都会涨，启动时清一次过期的，别让队列无限堆
        n = Store(str(STATE_PATH)).draft_expire(
            int((cfg.get("review") or {}).get("expire_days") or 3))
        if n:
            logging.getLogger("szlike.daemon").info("清理过期待审草稿 %d 条", n)
    except Exception as e:
        logging.getLogger("szlike.daemon").warning("清理待审队列失败：%s", e)
    logging.getLogger("szlike.daemon").info(
        "daemon 启动，账号矩阵：%s",
        ", ".join(f"{a['name']}{'(on)' if a['enabled'] else '(off)'}"
                  for a in accounts_index(cfg)),
    )
    if cfg.get("dry_run"):
        logging.getLogger("szlike.daemon").warning("dry_run=true，所有写操作只记日志")

    workers: dict[str, AccountWorker] = {}
    try:
        while True:
            cfg = load(str(CONFIG_PATH))
            live = [a for a in accounts_index(cfg) if a["enabled"]]
            idx = {a["name"]: i for i, a in enumerate(live)}
            for a in accounts_index(cfg):
                name = a["name"]
                if a["enabled"] and name not in workers:
                    w = AccountWorker(name, idx.get(name, 0))
                    w.start()
                    workers[name] = w
                elif not a["enabled"] and name in workers:
                    workers[name].stop()
                    workers[name].join(timeout=5)
                    del workers[name]
            # 清理已退出/报错卡死的
            for name, w in list(workers.items()):
                if not w.is_alive():
                    del workers[name]
            time.sleep(10)
    except KeyboardInterrupt:
        logging.getLogger("szlike.daemon").info("收到中断，停止所有 worker")
    finally:
        for w in workers.values():
            w.stop()
        for w in workers.values():
            w.join(timeout=5)
        try:
            PID_PATH.unlink(missing_ok=True)
        except Exception:
            pass


def cmd_web(args) -> None:
    import server
    _warm_vectors()
    server.main(host=args.host, port=args.port)


def _warm_vectors() -> None:
    """把 358MB 向量库读进页缓存。冷读 400ms、热读 25ms，差的就是这一步。"""
    try:
        from szlike import dense
        if dense.prefetch():
            logging.getLogger("szlike").info("向量库已预热（%s）", dense.info().get("count"))
    except Exception as e:
        logging.getLogger("szlike").warning("向量预热跳过：%s", e)


def main() -> None:
    ap = argparse.ArgumentParser(description="鼠洞SD 自动化机器人（多账号）")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("-a", "--account", default=None, help="账号 key，默认用 config 里的查看账号")
    sub = ap.add_subparsers(dest="cmd", required=True)

    # 允许把 -a 写在子命令后面：run.py doctor -a warm
    acct = argparse.ArgumentParser(add_help=False)
    acct.add_argument("-a", "--account", dest="account_sub", default=None,
                      help="账号 key（也可以写在子命令前）")

    sub.add_parser("status")
    sub.add_parser("doctor", parents=[acct])
    sub.add_parser("keepalive", parents=[acct])
    sub.add_parser("feed", parents=[acct])
    sub.add_parser("replies", parents=[acct])
    pw = sub.add_parser("whisper", parents=[acct], help="暗语通道：收指令 / 报到 / 设置暗桩帖")
    pw.add_argument("--rollcall", action="store_true", help="在暗桩帖下留一条锚点留言")
    pw.add_argument("--force", action="store_true", help="锚点已存在也再留一条；暗语停用时强制执行")
    pw.add_argument("--drop", default="", help="设置暗桩帖（填 App 里的帖代号，如 3F9K）")
    pw.add_argument("--loop", type=int, default=0, help="常驻轮询间隔秒数")
    pw.add_argument("--gap", type=float, default=0, help="报到时每个号之间平均等几秒")
    ps = sub.add_parser("sentinel", help="哨兵：盯收藏/点赞，派一小撮号去评论")
    ps.add_argument("--watch", action="store_true", help="轮询主人的收藏")
    ps.add_argument("--loop", type=int, default=0, help="轮询间隔秒数")
    ps.add_argument("--raid", default="", help="立刻派人评论某帖（帖代号）")
    ps.add_argument("--count", type=int, default=4, help="派几个号")

    p = sub.add_parser("post", parents=[acct])
    p.add_argument("-c", "--content", required=True)
    p.add_argument("--topic")
    p.add_argument("--subtopic")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_post)

    p = sub.add_parser("comment", parents=[acct])
    p.add_argument("--id", required=True)
    p.add_argument("-c", "--content", required=True)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_comment)

    p = sub.add_parser("like", parents=[acct])
    p.add_argument("--id", required=True)
    p.set_defaults(func=cmd_like)

    p = sub.add_parser("once", parents=[acct])
    p.add_argument("task", choices=list(TASKS))
    p.set_defaults(func=cmd_once)

    p = sub.add_parser("daemon")
    p.set_defaults(func=cmd_daemon)

    p = sub.add_parser("web", help="启动本地 Web 控制台")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.set_defaults(func=cmd_web)

    args = ap.parse_args()
    if getattr(args, "account_sub", None):
        args.account = args.account_sub
    setup_logging(args.verbose)

    if args.cmd == "status":
        cmd_status()
    elif args.cmd == "doctor":
        cmd_doctor(args.account)
    elif args.cmd == "keepalive":
        cmd_keepalive(args.account)
    elif args.cmd == "feed":
        cmd_feed(args.account)
    elif args.cmd == "whisper":
        cmd_whisper(args)
    elif args.cmd == "sentinel":
        cmd_sentinel(args)
    elif args.cmd == "replies":
        cmd_replies(args.account)
    else:
        args.func(args)


if __name__ == "__main__":
    main()
