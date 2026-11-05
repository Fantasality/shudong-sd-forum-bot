"""哨兵：盯着主人（橘子汽水）的收藏，看到新收藏就派一小撮「对口性格」的号去评论。

为什么不用 `userPosts(type: MARKED)`：实测列表有分钟级延迟（收藏后 60 秒仍刷不出来），
但 `post(id){viewerHasMarked}` 是实时的。所以改成盯「最新 N 条帖子的收藏状态」：
每轮 2 个请求，只有状态从 false 翻成 true 才触发。

不经过任何暗语通道，论坛上看不出任何指令痕迹——只是一条帖子底下多了 4 个正常评论。
"""
from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime
from pathlib import Path

from . import ops
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
  creator { username } topic { name } subtopic { name } } }
"""

FALLBACK = [
    "蹲个后续，有结果踢我一下。",
    "同问，我也想知道。",
    "帮顶，楼主说的挺真实的。",
    "看完了，这事得自己拿主意。",
    "有点意思，等个后续。",
]

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
    return Ctx(client, store, brain, guard, d, manual=True)


def _roster_persona() -> dict[str, str]:
    try:
        data = json.loads(ROSTER_PATH.read_text(encoding="utf-8"))
        return {x["key"]: (x.get("persona") or "") for x in data}
    except Exception:
        return {}


def pick_targets(cfg: dict, post: dict, count: int, exclude: set[str]) -> list[str]:
    """按帖子内容挑性格最对口的几个号。"""
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
        picked = [k for s, _, k in scored if s > 0][:count]
        if len(picked) < count:
            rest = [k for s, _, k in scored if k not in picked]
            random.shuffle(rest)
            picked += rest[:count - len(picked)]
        return picked[:count]
    random.shuffle(scored)
    return [k for _, _, k in scored[:count]]


def _line(ctx: Ctx, post: dict) -> str:
    text = (post.get("content") or "").strip()[:400]
    try:
        system = ("你是苏州大学的学生，正在刷校园论坛。看到这条帖子，写一句 12-30 字的评论："
                  "口语、有具体反应、符合你的人设，不要客套，不要总结，不要堆 emoji，"
                  "不要用拼音缩写或英文缩写。")
        user = (f"帖子：{text}\n板块：{(post.get('topic') or {}).get('name') or '校园生活'}"
                f"\n你的人设：{ctx.label}")
        out = (ctx.brain.chat(system, user, max_tokens=120) or "").strip()
        if out:
            return out.split("\n")[0][:60]
    except Exception as e:
        log.warning("生成评论失败：%s", e)
    return random.choice(FALLBACK)


def dispatch(cfg: dict, post_id: str, count: int = 4, exclude: set[str] | None = None) -> list[dict]:
    """派一小撮号去评论指定帖子。"""
    exclude = set(exclude or ())
    scfg = cfg.get("sentinel") or {}
    master = scfg.get("watch_account") or "bot"
    exclude.add(master)
    gap = scfg.get("gap") or [15, 35]

    try:
        mctx = make_ctx(cfg, master)
        post = mctx.client.gql(Q_POST, {"id": post_id}, "Q")["post"]
    except Exception as e:
        log.warning("读帖失败，取消派单：%s", e)
        return []

    names = pick_targets(cfg, post, max(1, min(count, 6)), exclude)
    if not names:
        return []

    sent = []
    for i, name in enumerate(names):
        try:
            ctx = make_ctx(cfg, name)
            line = _line(ctx, post)
            node = ops.create_comment(ctx.client, post_id, line, is_anonymous=False)
            log.info("[%s] 已评论 %s：%s", name, node.get("id"), line[:24])
            sent.append({"account": name, "persona": ctx.persona,
                         "comment": node.get("id"), "content": line})
        except SzlikeError as e:
            log.warning("[%s] 评论失败：%s", name, e)
        except Exception as e:
            log.warning("[%s] 异常：%s", name, e)
        if i < len(names) - 1:
            time.sleep(random.uniform(gap[0], gap[1]))

    state = _load()
    trig = state.get("triggers") or []
    trig.insert(0, {
        "at": datetime.now().isoformat(timespec="seconds"),
        "post": post_id,
        "content": (post.get("content") or "")[:40],
        "sent": sent,
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


def watch(cfg: dict, count: int = 4, quiet: bool = False) -> list[dict]:
    """轮询最新帖子，发现新收藏就派人。首轮只建基线，不触发。"""
    scfg = cfg.get("sentinel") or {}
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
    for pid in fresh[:2]:
        log.info("发现新收藏 %s，派人", pid)
        out.extend(dispatch(cfg, pid, count, exclude={master}))
        time.sleep(3)
    return out
