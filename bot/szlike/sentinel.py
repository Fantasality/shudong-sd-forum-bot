"""哨兵：盯着主账号的收藏，看到新收藏就派**一个**号去评论。

2026-11-05 复盘改版
------------------
真人连着发了三帖点名这批号：
  「都是默认id头像，只在近几天发了两三条帖子，且语气和逻辑都怪怪的」
  「评论别人几乎也是一起来的」
  「怎么还有人机互评，这几个号所有的帖子和评论全都密集在一天或两天内」

原来 dispatch() 一次派 **4 个号**、间隔 15-35 秒评同一条真人帖，
而且 make_ctx 传 manual=True 跳过 Guard、直接 ops.create_comment 绕过
claims 占坑与 antirepeat 撞车检查，评论还是另一套独立 prompt 生成的。
这就是「一窝蜂 / 人机互评」最直接的技术来源。

现在：默认整条链路关闭（config.json 的 sentinel.enabled=false）。
即便手动开启，也强制单号，且必须走和线上完全相同的准入：
  本方帖不互评 → claim_post 抢全局唯一 owner → ctx.allow()（冷启动+全局配额）
  → build_comment_prompt 同一条生成链路 → antirepeat / guard.text_ok
  → 发成功后 mark + remember_text，失败释放坑。
一条真人帖永远只会出现一个本方账号。
"""
from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime
from pathlib import Path

from . import antirepeat, ops
from . import tasks as T
from .brain import Brain
from .client import Client, SzlikeError
from .config import account_config
from .guard import Guard
from .store import Store
from .tasks import Ctx

log = logging.getLogger("szlike.sentinel")

BASE = Path(__file__).resolve().parent.parent
STATE_PATH = BASE / "sentinel_state.json"
ROSTER_PATH = BASE / "accounts_roster.json"

BATCH = 18          # 每轮盯最新多少条帖子（别名上限实测 20 安全）

Q_FEED = """
query Q($n: Int!) { posts(filter: {schoolId: "U2Nob29sOjIy"}, first: $n) {
  edges { node { id content createdAt commentCount topic { name } } } } }
"""

Q_POST = """
query Q($id: ID!) { post(id: $id) { id content commentCount
  creator { id username } topic { name } subtopic { name } } }
"""

# 兜底不再用「蹲个后续 / 同问 / 帮顶」这类复读机话术，
# 真人原话就是「还有复读机」。改从真实语料里抽 <=12 字的短句。
_SHORT_CACHE: list[str] | None = None


def _fallback_line() -> str:
    global _SHORT_CACHE
    if _SHORT_CACHE is None:
        try:
            d = json.loads((BASE / "kb" / "short_replies.json").read_text(encoding="utf-8"))
            pool = [x["t"] for k in ("plain", "react")
                    for x in (d.get(k) or [])
                    if isinstance(x, dict) and x.get("t")]
            _SHORT_CACHE = [x for x in pool if 2 <= len(x) <= 12] or ["加一"]
        except Exception:
            _SHORT_CACHE = ["加一"]
    return random.choice(_SHORT_CACHE)


PERSONA_KEYWORDS = {
    "food": ["食堂", "饭", "菜", "好吃", "外卖", "奶茶", "夜宵", "零食", "吃", "餐", "面"],
    "kaoyan": ["考研", "保研", "复试", "自习", "图书馆", "绩点", "复习", "期末", "考试", "四级", "六级"],
    "sport": ["跑步", "健身", "球场", "体育", "篮球", "足球", "操场", "体测", "运动", "骑车"],
    "acg": ["动漫", "番", "手办", "游戏", "原神", "cos", "二次元", "漫画", "动画",
            "周边", "吧唧", "立牌", "谷子", "痛包", "挂件", "徽章", "出谷", "收谷", "偶像", "演唱会"],
    "photo": ["拍照", "摄影", "相机", "晚霞", "风景", "照片", "拍"],
    "nerd": ["课", "作业", "实验", "老师", "专业", "报告", "绩点", "题"],
    "grind": ["作业", "ddl", "复习", "考试", "卷", "绩点", "赶"],
    "warm": ["失物", "求助", "找人", "急", "谢谢", "难受", "emo", "哭", "帮"],
    "crush": ["恋爱", "表白", "喜欢", "对象", "分手", "暗恋", "约"],
    "night": ["失眠", "凌晨", "熬夜", "睡不着"],
    "artsy": ["设计", "画", "展", "手工", "海报", "拍立得"],
    "slacker": ["懒", "躺", "摆烂", "逃课", "不想", "困"],
    "lone": ["一个人", "孤独", "社恐", "没人", "独来独往"],
    "cool": ["音乐", "乐队", "演出", "live", "歌", "吉他"],
    "shy": ["尴尬", "社死", "不敢", "害羞"],
    "zest": ["玩", "出游", "周末", "逛", "聚会", "爬"],
    "obsess": ["上瘾", "沉迷", "停不下来", "刷"],
    "fan": ["追星", "偶像", "粉丝", "应援"],
}


def _load() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(data: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_PATH)


def make_ctx(cfg: dict, name: str) -> Ctx:
    d = account_config(cfg, str(BASE), name)
    acc = d["_account"]
    client = Client(d["app_id"], d["school_id"], acc["tokens"],
                    account_path=str(BASE / f"account_{acc['name']}.json"))
    store = Store(str(BASE / "state.db"), ns="" if acc["name"] == "main" else acc["name"])
    brain = Brain(d.get("llm"))
    guard = Guard(d.get("guard"), store)
    # manual=False：哨兵不再绕过静默时段、配额、冷启动与全局跨账号配额
    return Ctx(client, store, brain, guard, d, manual=False)


def _roster_persona() -> dict[str, str]:
    try:
        data = json.loads(ROSTER_PATH.read_text(encoding="utf-8"))
        return {x["key"]: (x.get("persona") or "") for x in data}
    except Exception:
        return {}


def pick_targets(cfg: dict, post: dict, count: int, exclude: set[str]) -> list[str]:
    """按帖子内容挑性格最对口的一个号（count 只会是 1）。"""
    personas = _roster_persona()
    text = ((post.get("content") or "") + " " +
            ((post.get("topic") or {}).get("name") or "")).lower()
    scored = []
    for key, acc in (cfg.get("accounts") or {}).items():
        if not acc.get("enabled") or key in exclude or key in ("bot", "main"):
            continue
        p = acc.get("persona") or personas.get(key, "")
        score = sum(text.count(k) for k in PERSONA_KEYWORDS.get(p, []))
        scored.append((score, random.random(), key))
    scored.sort(reverse=True)
    if scored and scored[0][0] > 0:
        return [k for s, _, k in scored if s > 0][:1]
    if not scored:
        return []
    return [random.choice(scored)[2]]


def _line(ctx: Ctx, post: dict, prev: list[str]) -> str:
    """和线上 random_comment 走同一条生成链路，不再另起一套 prompt。"""
    text = (post.get("content") or "").strip()[:300]
    board = (post.get("topic") or {}).get("name") or "校园生活"
    try:
        spec = T.build_comment_prompt(ctx, text, board, prev=prev,
                                      post_id=(post.get("id") or ""))
        if spec.get("early"):
            return spec["early"]
        if spec.get("enabled"):
            out = (ctx.brain.chat(spec["system"], spec["user"], max_tokens=120) or "").strip()
            if out:
                out = T.erase_hedges(out)
                out = T.trim_periods(out)
                out = T.erase_ai_flourish(out)
                out = T.fix_period_rate(out)
                return out.split("\n")[0][:60]
    except Exception as e:
        log.warning("生成评论失败：%s", e)
    return _fallback_line()


def dispatch(cfg: dict, post_id: str, count: int = 1,
             exclude: set[str] | None = None) -> list[dict]:
    """派**一个**号去评论指定帖子。硬上限 1，不受调用方 count 影响。"""
    scfg = cfg.get("sentinel") or {}
    if not scfg.get("enabled"):
        log.info("哨兵默认关闭（2026-11-05 举报复盘），跳过派单 %s", post_id)
        return []

    exclude = set(exclude or ())
    master = scfg.get("watch_account") or "bot"
    exclude.add(master)

    try:
        mctx = make_ctx(cfg, master)
        post = mctx.client.gql(Q_POST, {"id": post_id}, "Q")["post"]
    except Exception as e:
        log.warning("读帖失败，取消派单：%s", e)
        return []

    # ① 本方账号发的帖不互评
    our_ids = {v.get("user_id") for v in (cfg.get("accounts") or {}).values()
               if isinstance(v, dict) and v.get("user_id")}
    cid = (post.get("creator") or {}).get("id")
    if cid and cid in our_ids:
        log.info("目标帖是本方账号发的，跳过（禁止互评）")
        return []

    # ② 全局唯一 owner：一条真人帖永久只允许一个本方账号出面
    if not mctx.store.claim_post(post_id, "sentinel"):
        log.info("这条帖已被 %s 占，跳过", mctx.store.post_owner(post_id) or "别的号")
        return []

    names = pick_targets(cfg, post, 1, exclude)
    if not names:
        mctx.store.release_post(post_id)
        return []
    name = names[0]

    try:
        ctx = make_ctx(cfg, name)
        # ③ 冷启动 + 全局跨账号配额（原来 manual=True 直接跳过）
        ok, why = ctx.allow("comment")
        if not ok:
            log.info("[准入] %s 被拒：%s", name, why)
            mctx.store.release_post(post_id)
            return []

        # ④ 同帖已有评论，交给撞车检查
        prev: list[str] = []
        try:
            d = ops.list_comments(ctx.client, post_id, "LATEST")
            prev = [(e["node"].get("content") or "")[:120]
                    for e in (d.get("edges") or [])]
        except Exception:
            pass

        line = _line(ctx, post, prev)
        recent = ctx.store.recent_texts("comment", 20)
        if antirepeat.check(line, same_post=prev, recent=recent):
            line = _fallback_line()
        tok, _ = ctx.guard.text_ok(line)
        if not tok or not line.strip():
            mctx.store.release_post(post_id)
            return []

        node = ops.create_comment(ctx.client, post_id, line, is_anonymous=False)
        ctx.mark("comment", post_id, line)
        ctx.store.remember_text("comment", line)
        log.info("[%s] 已评论 %s：%s", name, node.get("id"), line[:24])
        sent = [{"account": name, "persona": ctx.persona,
                 "comment": node.get("id"), "content": line}]
    except SzlikeError as e:
        log.warning("[%s] 评论失败：%s", name, e)
        mctx.store.release_post(post_id)
        return []
    except Exception as e:
        log.warning("[%s] 异常：%s", name, e)
        mctx.store.release_post(post_id)
        return []

    state = _load()
    trig = state.get("triggers") or []
    trig.insert(0, {
        "at": datetime.now().isoformat(timespec="seconds"),
        "post": post_id,
        "content": (post.get("content") or "")[:40],
        "sent": sent,          # 长度恒为 1
    })
    state["triggers"] = trig[:30]
    _save(state)
    return sent


def _mark_flags(ctx: Ctx, ids: list[str]) -> dict[str, bool]:
    """一次性查一批帖子的收藏状态（别名上限实测 20）。"""
    out: dict[str, bool] = {}
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        parts = " ".join(f'p{j}: post(id:"{pid}"){{ id viewerHasMarked }}' for j, pid in enumerate(chunk))
        try:
            d = ctx.client.gql("query Q{" + parts + "}", {}, "Q")
            for j, pid in enumerate(chunk):
                node = d.get(f"p{j}") or {}
                out[pid] = bool(node.get("viewerHasMarked"))
        except SzlikeError as e:
            log.warning("收藏状态查询失败：%s", e)
        if i + BATCH < len(ids):
            time.sleep(2)
    return out


def watch(cfg: dict, count: int = 1, quiet: bool = False) -> list[dict]:
    """轮询最新帖子，发现新收藏就派一个号。首轮只建基线，不触发。"""
    scfg = cfg.get("sentinel") or {}
    if not scfg.get("enabled"):
        return []
    master = scfg.get("watch_account") or "bot"
    state = _load()
    try:
        ctx = make_ctx(cfg, master)
        feed = ctx.client.gql(Q_FEED, {"n": BATCH}, "Q")["posts"]["edges"]
    except Exception as e:
        log.warning("读广场失败：%s", e)
        return []

    ids = [e["node"]["id"] for e in feed]
    if not ids:
        return []
    flags = _mark_flags(ctx, ids)
    true_ids = [pid for pid, v in flags.items() if v]

    known = set(state.get("known_marks") or [])
    first_run = not state.get("known_marks")

    if first_run:
        state["known_marks"] = true_ids
        state["baseline_at"] = datetime.now().isoformat(timespec="seconds")
        _save(state)
        log.info("哨兵基线：%d 条收藏（%s）", len(true_ids), ", ".join(true_ids))
        return []

    fresh = [pid for pid in true_ids if pid not in known]
    state["known_marks"] = sorted(set(true_ids) | (known & set(ids)))
    _save(state)

    if not fresh:
        if not quiet:
            log.info("本轮无新收藏（盯 %d 帖，%d 条已收藏）", len(ids), len(true_ids))
        return []

    out: list[dict] = []
    for pid in fresh[:1]:
        log.info("发现新收藏 %s，派一个号", pid)
        out.extend(dispatch(cfg, pid, 1, exclude={master}))
    return out
