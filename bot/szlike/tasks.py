"""四类自动化任务。每个函数返回 (是否执行, 说明)。

多账号
------
每个账号一个 Ctx，各自独立的 client / store 命名空间 / 人设 prompt / 配额。
任务函数本身不关心"现在是谁"，只从 ctx 取。

随机性
------
选题和选帖都走 `persona` 模块：种子来自 `time.time_ns()`，
抽到的场景、板块、写法、目标帖都会写进日志，便于复盘。
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import (antirepeat, dense, events, facts, human, kb, longform, mood,
               ops, persona, quality, rag, topic)

log = logging.getLogger("szlike.tasks")

# 话题类型 → 写法。同质化不只出在「写什么」，也出在「用同一种方式写」：
# 七条帖子全是「今天看到一件小事」的抒情见闻，话题再散也像一个号写的。
KIND_HINT = {
    # 以前这里写的是「写你今天亲眼看到的一件事，你只是路过」——一句话把 32 个号
    # 全变成了监控摄像头：每条都是「几点 + 在哪 + 看见谁做了什么」。
    # 真人发帖绝大多数是「我今天干了什么 / 我卡在哪」，旁观别人反而是少数。
    "见闻": "写你自己今天亲身经历的一件事：你去做什么、中间出了什么岔子、最后怎么样了。"
            "不要写你站在旁边看见别人做什么——你不是摄像头",
    "吐槽": "先把事实摆清楚（谁、哪里、多少钱、多久），再补一句你的态度，别上纲上线",
    "堵": "写清发生了什么、你卡在哪、最后怎么样了，别堆动作和场景",
    "吃": "写菜名、价格和味道，别编吃饭时的肢体小动作",
    "问": "真的问一个问题，给一两句背景，像在群里随口问，别写成求助模板",
    "恋爱": "写清这件事本身和你的处境（对方做了什么、你现在不知道该怎么办），"
            "不表白、不写微动作，不猜测对方心思",
}

# 仅当 config 里对应的 prompts.<key> 缺失时兜底用
DEFAULT_PROMPTS: dict[str, dict] = {
    "post": {
        "system": "你是苏州大学的学生，在校园社区发一条日常帖子。把一件事说清楚："
        "发生了什么、跟谁有关、结果怎样。只保留跟这件事有关的事实，"
        "没有就不编。不要营销、不要提问求关注、不要暴露 AI。只输出帖子正文。",
        "user": "现在是 {time}。\n发在「{topic}」板块。\n这次写这个场景：{angle}\n写法：{style}\n{seed}",
    },
    "reply": {
        "system": "你在大学社区回复别人给你的评论。用一句自然、有温度的话回应，"
        "不要客套开头，不要说教，不要暴露自己是 AI。只输出回复本身。",
        "user": "帖子：{post}\n对方说：{their}",
    },
    "comment": {
        "system": "你是苏州大学的学生，正在浏览校园社区。写一句真实、轻松、像真人随口说的话。"
        "不要说教、不要复读标题、不要暴露自己是 AI。只输出评论本身。",
        "user": "帖子：{post}\n写法：{style}",
    },
    "like": {
        "system": "判断这条帖子是否值得点赞。只回答 YES 或 NO。广告、引战、纯水、求关注答 NO。",
        "user": "{post}",
    },
}


def render(tpl: str, **kw) -> str:
    """把 {placeholder} 替换掉。不用 str.format，避免 prompt 里的花括号报错。"""
    out = tpl or ""
    for k, v in kw.items():
        out = out.replace("{" + k + "}", str(v if v is not None else ""))
    return out


class Ctx:
    def __init__(self, client, store, brain, guard, cfg: dict, manual: bool = False):
        self.client = client
        self.store = store
        self.brain = brain
        self.guard = guard
        self.cfg = cfg
        self.school_id = cfg["school_id"]
        self.me = cfg.get("my_user_id") or ""
        self.dry_run = bool(cfg.get("dry_run", True))
        self.anon = bool(cfg.get("is_anonymous", False))
        self.prompts = cfg.get("prompts") or {}
        self.manual = manual
        acc = cfg.get("_account") or {}
        self.account = acc.get("name") or "main"
        self.persona = acc.get("persona") or "warm"
        self.label = acc.get("label") or self.account
        # 固定身份：专业、宿舍、常去的地方。七个号如果只有语气不同，读起来还是同一个人
        self.profile = acc.get("profile") or ""
        self.campus = acc.get("campus") or ""
        self.address = acc.get("address") or ""
        # 自己所有号的 user_id，避免三个号互相点赞/评论
        self.siblings = {
            (v or {}).get("user_id") for v in (cfg.get("accounts") or {}).values()
            if isinstance(v, dict) and v.get("user_id")
        }

    # ---------- prompt ----------

    def prompt(self, key: str, **kw) -> tuple[str, str, bool]:
        """返回 (system, user, 该 prompt 是否启用)。缺失时用内置兜底。

        知识库在 system 末尾追加，属于「常识参考」，不覆盖用户自己写的指令。
        """
        p = self.prompts.get(key) or DEFAULT_PROMPTS.get(key) or {}
        enabled = bool(p.get("enabled", True))
        system = render(p.get("system", ""), **kw)
        user = render(p.get("user", ""), **kw)
        system = self._with_kb(key, system, kw.get("board"), kw.get("scene"),
                               kw.get("sample") or kw.get("post") or "",
                               kw.get("post_id"),
                               prefer_advice=bool(kw.get("long_text"))
                               and key in ("comment", "reply"))
        if kw.get("length"):
            # 长度指令放 system 末尾：塞在 user 里会被「像真人随口说一句」压过去
            system = f"{system}\n\n{kw['length']}"
        if kw.get("hint"):
            # 表情提示反过来必须放 user，放 system 会变成六成评论都挂表情
            user = f"{user}\n\n{kw['hint']}"
        if key in ("post", "comment", "reply"):
            if kw.get("long_text"):
                # 长文反过来：语料里 ≥300 字的帖子 76% 有句号（p50=3、p90=12），
                # ≥100 字的评论 58% 有。但人设 prompt 里普遍写着「不用句号」，
                # 不删掉这条，模型会写出一整段三百字全是逗号的东西。
                system = re.sub(r"\n?[^\n]*不用句号[^\n]*", "", system)
                system = (f"{system}\n\n这段比较长，必须用句号断句（平均两三句一个），"
                          "分成两三段，每段之间空一行。不要写成一大坨。")
            else:
                # 短内容：语料里只有 9.2% 的帖子出现句号，模型几乎每句都收句号，
                # 是一眼能看出来的机器味。但也不能全禁——仍有 8% 的评论带句号，
                # 一刀切会让「句号率」这个指标直接归零，同样是破绽。
                system = (f"{system}\n\n断句用逗号或者直接换行，别每句都收句号；"
                          "只有一句话特别长、不加句号会读串的时候才用。")
            if self.profile:
                # 稳定身份，不主动说出来，只影响「会注意到什么」
                system = (f"你的固定身份（别自我介绍，也别提这些设定）：{self.profile}\n\n"
                          f"{system}")
        if key in ("comment", "reply"):
            # 人设越强越容易自说自话：追星号会在食堂排队帖下聊签售，摄影号会问「你坐哪拍的」。
            # 先接对方说的那件事，再带自己的角度；帖子里没写的信息不许假设。
            system = (f"{system}\n\n先接住对方这条帖子/这句话里说的具体事，再讲你的角度。"
                      "帖子里没提到的信息不要凭空假设，看不懂就只说看懂的。")
        return system, user, enabled

    def _with_kb(self, key: str, system: str, board: str | None,
                 scene: str | None = None, sample: str = "",
                 post_id: str | None = None, prefer_advice: bool = False) -> str:
        """system 末尾追加两块：社区常识 + 同类真实示范。都只作语感参考。"""
        if key not in ("post", "comment", "reply"):
            return system          # like 这类分类任务不注入，免得污染判断
        parts: list[str] = []
        # 评论/回复要快，预算砍半；发帖可以多给点
        scale = 0.55 if key in ("comment", "reply") else 1.0
        kcfg = self.cfg.get("kb") or {}
        if kcfg.get("enabled", True):
            try:
                parts.append(kb.render(
                    key, persona=self.persona, board=board, scene=scene,
                    budget=int((kcfg.get("budget") or 800) * scale),
                    campus=self.campus,
                    path=kcfg.get("path") or "kb/knowledge.json",
                ))
            except Exception as e:  # 知识库坏了不能拖垮发帖
                log.warning("知识库注入失败：%s", e)
        rcfg = self.cfg.get("rag") or {}
        if rcfg.get("enabled", True) and sample:
            shots = int(rcfg.get("shots") or 3)
            if key in ("comment", "reply"):
                shots = max(2, shots - 1)
            try:
                blk = rag.prompt_block(
                    key, sample, board=board, k=shots,
                    budget=int((rcfg.get("budget") or 520) * scale),
                    cfg=rcfg, post_id=post_id, prefer_advice=prefer_advice,
                )
                if blk:
                    parts.append(blk)
            except Exception as e:
                log.warning("示范检索失败：%s", e)
        parts = [p for p in parts if p]
        return f"{system}\n\n" + "\n\n".join(parts) if parts else system

    # ---------- 记录 ----------

    def mark(self, kind: str, target: str, meta: str = "") -> None:
        """干跑的记录加 dry: 前缀，不占真实配额，但仍能防重复。"""
        if self.dry_run:
            kind = "dry:" + kind
        self.store.mark(kind, target, meta)

    def pick(self, kind: str, target: str, meta: str = "") -> None:
        """选题记录：不区分干跑，永远记真实值。"""
        self.store.mark("pick:" + kind, target, meta)

    def saw_pick(self, kind: str, limit: int = 10) -> set[str]:
        return {r["target"] for r in self.store.list_actions("pick:" + kind, limit)}

    def seen(self, kind: str, target: str) -> bool:
        return self.store.seen(kind, target) or self.store.seen("dry:" + kind, target)

    def mine(self, creator_id: str | None) -> bool:
        return bool(creator_id) and (creator_id == self.me or creator_id in self.siblings)

    def rng(self, purpose: str) -> random.Random:
        return persona.rng(f"{self.account}:{purpose}")

    def log_action(self, kind: str, target: str, detail: str, dry: bool) -> None:
        tag = "[DRY]" if dry else "[REAL]"
        log.info("%s [%s] %s %s —— %s", tag, self.label, kind, target, detail)


# 32 个号分布在四个校区，最容易被本校人看出来的破绽是「未来校区的号写庄里图书馆」。
# 每行是「本校区不该出现的词」，生成后硬查一遍。
CAMPUS_DENY = quality.CAMPUS_DENY


def campus_violation(text: str, campus: str) -> tuple[bool, str]:
    bad = quality.check_campus(text, campus)
    try:
        bad += facts.verify(text, campus)
    except Exception:
        pass
    if bad:
        return True, (f"这条里的事实不对（{bad[0]}）。你在{campus}校区，"
                      "把地点和院系改成本校区的，不确定就别写具体名字。")
    return False, ""


def ensure_sticker(text: str, pl: dict, scene: str = "") -> str:
    """该带表情的条目，模型没带就补一个。

    语料里 23 字以上的评论 33% 带表情，而 prompt 里写「必须带」模型只有一半时候听。
    光靠提示词，实测表情率 13%，真值 27%——差一倍，是能看出来的破绽。
    这里在文本末尾补，位置就是结尾，跟真人的习惯一致。
    """
    if not text or not pl.get("sticker"):
        return text
    if re.search(r"\[[^\]]{1,6}\]\s*$", text):
        return text
    try:
        from . import human as _h
        return text.rstrip() + _h._pick_sticker(scene)
    except Exception:
        return text


def _clean_content(text: str, lo: int = 12, hi: int = 220) -> str | None:
    t = (text or "").strip().strip('"“”')
    # 模型偶尔从半句对话开始，留下的收尾引号/逗号要清掉
    t = t.lstrip('”’"，,。、；;：:）) ')
    if not t:
        return None
    if len(t) > hi:
        seg = t[:hi]
        cut = -1
        for p in "。！？!?；;，,":
            cut = max(cut, seg.rfind(p))
        t = seg[: cut + 1] if cut >= lo else seg.rstrip("，,。. ")
    if len(t) < lo:
        return None
    return t


# ---------- 1. 定时发帖 ----------


def _post_anchor(ctx, plan: dict) -> str:
    """发帖前先逼出一个具体细节，再写正文。

    直接写正文时，模型爱写「今天心情不错」这种没有信息量的句子。先让它给出
    「几点、哪栋楼、几个人、花了多少钱、什么味道」中的一个，正文才有骨头——
    真人写帖子几乎都从这种小细节起手。
    """
    try:
        t = ctx.brain.chat(
            "你在回忆今天校园里的一个具体瞬间。只输出一个 20 字以内的细节短语，"
            "必须包含地点、时间、数量、价格、气味或声音中的至少一个。"
            "不要完整句子，不要解释，不要引号。",
            f"板块：{plan['subtopic_name']}　场景：{plan['angle']}　写法：{plan['style']}",
            max_tokens=48, temperature=1.15)
        return _clean_content(t or "", lo=3, hi=32) or ""
    except Exception:
        return ""


def scheduled_post(ctx: Ctx, hhmm: str) -> tuple[bool, str]:
    t0 = time.time()
    ok, why = ctx.guard.allow("post", manual=ctx.manual)
    if not ok:
        return False, why

    daykey = datetime.now().strftime("%Y-%m-%d") + " " + hhmm
    if ctx.seen("post_slot", daykey):
        return False, "该时间点今天已执行"

    tcfg = ctx.cfg["tasks"]["scheduled_post"]
    # 时间种子：场景 × 板块 × 写法
    plan = persona.pick_post_plan(ctx.cfg, ctx.store, ctx.account, ctx.persona)
    # 话题不再从写死的 angle 列表里抽，而是按系统时间在 7.8 万条真实帖里随机取一条，
    # 抽象成「场合｜事」再写。手写场景十几天就轮完，之后就开始重复（旧书页那两次）。
    tp = topic.plan(ctx.brain, ctx.account, ctx.persona)
    plan["kind"] = tp.get("kind") or "见闻"
    plan["seed_doc"] = tp.get("doc") or ""
    plan["seed_board"] = tp.get("board") or ""
    if tp.get("topic"):
        plan["angle"] = tp["topic"]
        for sid, (tname, tid, sname) in persona.subtopic_index(ctx.cfg).items():
            if sname == tp.get("board"):        # 话题出自哪个板块就发哪个板块
                plan.update(subtopic_id=sid, subtopic_name=sname,
                            topic_id=tid, topic_name=tname)
                break
    log.info(
        "[选题] %s 种子=%s 板块=%s 类型=%s 话题=%s 源帖=%s",
        ctx.label, plan["seed"], plan["subtopic_name"], plan["kind"], plan["angle"],
        (plan["seed_doc"] or "-")[-6:],
    )

    pool = [x for x in (tcfg.get("content_pool") or []) if x.strip()]
    content = None
    vec = None
    pl = human.plan("post", plan["subtopic_name"], plan["angle"], ctx.rng("shape"))
    if pool:
        # 内容池也按时间种子抽，避免永远发第一条
        content = ctx.rng("post-pool").choice(pool)
    else:
        anchor = ""      # 抓手会让开头变成半句散文，实测口语化版本不需要它
        kw = {**plan, "seed": ""}      # {seed} 原来是数字种子，会被模型当成电话号码写进正文
        # 长帖：语料里 ≥300 字的帖子只占 0.76%，天天写不现实，
        # 但一个号一个月出一两篇长文，人设会立体很多。只在「堵/恋爱」类话题上开门。
        lfp = None
        if plan.get("kind") in ("堵", "恋爱"):
            lfp = longform.plan_post(
                plan.get("angle") or "",
                {"mood": "情感" if plan["kind"] == "恋爱" else "孤独"},
                ctx.rng("lfp"))
        if lfp:
            pl = dict(pl, lo=lfp["lo"], hi=lfp["hi"], newline=True, longform="post")
            length_txt = longform.brief_post(lfp)
        else:
            length_txt = human.brief(pl)
        system, user, enabled = ctx.prompt(
            "post", topic=plan["subtopic_name"], board=plan["subtopic_name"],
            sample=f"{plan['subtopic_name']} {plan['angle']} {plan['style']}",
            length=length_txt, hint=human.sticker_hint(pl),
            long_text=bool(lfp), **kw
        )
        if anchor:
            user = (f"{user}\n\n【这次的抓手（不要照抄这句，把它写成一条完整的正文）】{anchor}")
        user = f"{user}\n\n【这条写成什么】{KIND_HINT.get(plan['kind'], KIND_HINT['见闻'])}"
        # 模型会自己编时间：傍晚发帖写「凌晨刷论坛」。时段对不上是最容易被本地人看出来的破绽
        user = (f"{user}\n\n【时间】现在是{plan['time']}，正文里出现的时间要落在这个时段附近，"
                "不要写凌晨、半夜、清早这些跟现在冲突的时段")
        if ctx.campus:
            user = (f"{user}\n\n【地点】你在{ctx.campus}校区（{ctx.address}），"
                    "正文里的地点必须是这个校区里能遇到的地方，不要写别的校区")
        try:
            ev = events.brief(ctx.campus)
            if ev:
                user = (f"{user}\n\n【今天】{ev}。"
                        "这段只是给你自己对时间的锚，不是正文素材：正文里不要写"
                        "「11月5日」「星期四」「11月5号」这种日期，也不要提"
                        "「今年几月」；要指时间就用「今天下午」「昨天晚上」这种日常说法。"
                        "天气只能用这里给的，今天没下雨就别出现雨、伞、淋湿、湿鞋。"
                        "不要复述通知原文、不要写通知编号")
        except Exception:
            pass
        if ctx.profile:
            # 话题是从全校语料里抽的，可能抽到医学/法学的场景。保留「事情的形状」，
            # 换成本专业对应的场景，别让光电的学生去上系统解剖学。
            user = (f"{user}\n\n【身份】{ctx.profile}。话题里的场景如果跟你专业不符，"
                    "换成你专业里同一类的事（别人写解剖课，你就写实验课），事情的结构保留")
        # 聊天群里怎么说话就怎么写：禁比喻/押韵/排比/金句，这是发帖最像 AI 的地方
        system = (f"{system}\n\n像在群里跟同学说话：不用比喻、不押韵、不排比、"
                  "不要在结尾写金句。可以啰嗦、可以有废话，把当时发生了什么说清楚就行。")
        if not enabled:
            return False, "发帖 prompt 已关闭，且内容池为空，没有文案可用"
        # 长帖要 600-800 字，320 token 会被截在半句上
        mtx = 320 if not lfp else min(2400, int(lfp["hi"] * 2.6))
        recent_open = ctx.store.recent_texts("post", 8) + human.shared_recent(40)
        food_recent = sum(1 for t in recent_open
                          if any(w in t for w in ("食堂", "吃", "外卖", "奶茶", "饭")))
        if recent_open and food_recent / len(recent_open) >= 0.25:
            user = (f"{user}\n\n【别写吃】最近大家发吃的太多了，这条不要写吃的，"
                    "也不要提食堂、外卖、奶茶")
        time_open = sum(1 for t in recent_open if human.opener(t, 2) in
                        ("早上", "上午", "中午", "下午", "傍晚", "晚上", "凌晨", "昨晚", "今天", "昨天"))
        if time_open >= max(3, len(recent_open) // 4):
            user = (f"{user}\n\n【开头】最近时间开头的帖子太多了，这条不要用时间开头，"
                    "直接从场景中间或一句话中间写起")
        # 「我路过、我看见」是比时间开头更深的同质化：换十个话题，句式还是同一个。
        witness_n = sum(1 for t in recent_open if human.witness_post(t))
        if witness_n >= 2:
            user = (f"{user}\n\n【别写旁观】最近已经有 {witness_n} 条是「我旁边看见谁做什么」了。"
                    "这条必须写你自己今天做了什么、卡在哪、最后怎么样了，"
                    "不要写你站在旁边看别人。")
        scene_n = sum(1 for t in recent_open if human.scene_open(t))
        if scene_n >= 3:
            user = (f"{user}\n\n【别交代时间地点】最近有 {scene_n} 条都是「几点、在哪」开场。"
                    "这条不要写时间和地点，直接从你在干什么、卡在哪写起。")
        # 模型对「不要用时间开头」的服从率只有三成，这里再加一道后处理
        allow_time = random.random() < 0.25 and time_open < max(3, len(recent_open) // 3)
        if not allow_time:
            user = (f"{user}\n\n【开头】不要用「中午/早上/下午/晚上/今天/昨天」这类时间词开头，"
                    "直接从事情本身写起")
        lo_c, hi_c = max(4, pl["lo"] - 4), (400 if not lfp else 1400)
        raw0 = ctx.brain.chat(system, user, max_tokens=mtx)
        # finish_reason=length 时正文会停在一个字中间（「亮了一中」「伞都快撑不」），
        # 这种断句读起来像人写的，但其实是残句，必须当成不合格重写。
        truncated = ctx.brain.last_finish == "length"
        content = _clean_content(raw0, lo=lo_c, hi=hi_c)
        # 一条帖子代表这个号的全部形象：客服味、复读自己、把抓手当正文，三种都要重写
        recent_posts = ctx.store.recent_texts("post", 20) + human.shared_recent(200)
        for _ in range(2):
            bad, why = human.needs_rewrite(content or "")
            if not bad and content and anchor and human.repeats([anchor], content, thr=0.7):
                bad, why = True, "不要把抓手那句话直接当正文，展开成一条有头有尾的帖子。"
            if not bad and content and not human.ends_ok(content):
                bad, why = True, "上一句断在半句。重写一条完整的，把话说完。"
            if not bad and truncated:
                bad, why = True, "上一句被长度上限截断在半句。重写一条更短的，必须把话说完。"
                truncated = False
            if not bad and content and len(content) > pl["hi"] + 16:
                bad, why = True, (f"这条太长。重新写一条 {pl['lo']}-{pl['hi']} 字的，"
                                  "只保留一件有结果的事，不要在原句上删字。")
            if not bad and content and re.match(r"^(这破|不是我说|真服了|笑死|无语了|家人们)", content):
                bad, why = True, "这不是发帖的口吻，是在评论别人的帖子。写你自己今天的事。"
            if not bad and content:
                # 旁观结构：主角不是「我」，是「我旁边那个人」。这是发帖最像脚本的地方。
                wp = human.witness_post(content)
                if wp:
                    bad, why = True, (wp + "改成写你自己：你去做什么、中间出了什么岔子、"
                                      "最后怎么样了。不要写你站在旁边看见别人做什么。")
            if not bad and content:
                bad, why = campus_violation(content, ctx.campus)
            if not bad and content:
                lb = quality.board_leak(content)
                if lb:
                    bad, why = True, f"正文开头是板块名「{lb}」，删掉它，直接写事情。"
            if not bad and content and not human.repeats(recent_posts, content):
                # 开头撞车比内容重复更显眼：连着几条都用「今天下午」开头，一眼就是同一个人
                op = human.opener(content, 3)
                same = sum(1 for t in recent_posts if op and human.opener(t, 3) == op)
                if same >= 2:
                    bad, why = True, (f"最近已经有 {same} 条帖子用「{op}」开头了，"
                                      "换个开头，直接从场景中间写起。")
                else:
                    break
            fix = why or "和你之前发过的帖子太像了，换个具体场景。"
            raw1 = ctx.brain.chat(system, f"{user}\n\n{fix}", max_tokens=mtx)
            truncated = ctx.brain.last_finish == "length"
            content = _clean_content(raw1, lo=lo_c, hi=hi_c)
        # 重写两轮还超长，最后按标点收一次，收不住就保留（宁可略长也不要断句）
        if content and len(content) > pl["hi"] + 12:
            seg = content[: pl["hi"] + 6]
            cut = max((seg.rfind(x) for x in "。！？!?"), default=-1)
            if cut >= max(4, pl["lo"]):
                content = seg[: cut + 1]
        # 重写两轮还是断在半句，就退回到最后一个标点，宁可短
        if content and not human.ends_ok(content):
            cut = max(content.rfind(x) for x in "。！？!?，,；;")
            if cut >= max(4, pl["lo"] - 4):
                content = content[: cut + 1].rstrip("，,；;")
        if not allow_time and content:
            content = human.strip_time_opener(content)
        content = human.shape(content or "", pl)

        # 语义查重：3-gram 挡不住「旧纸霉味」vs「霉味冲鼻」，向量能。
        # 和源帖 >0.85 就是换词复述；和近期发过的 >0.72 就是同质化（实测同题 0.66-0.77）。
        vec = None
        try:
            vec = dense.encode(content)
            near_text, near = topic.closest(vec)
            over = topic.seed_overlap(vec, plan["seed_doc"])
            if over >= 0.85 or near >= 0.72:
                fix = ("这条基本是把参考话题换个说法重写，换一件完全不同的事，"
                       "只保留「场合」，事情本身要换。"
                       if over >= 0.85 else
                       f"这条和最近发过的「{near_text[:18]}」太像了，换个场景重写。")
                log.info("[查重] %s over=%.2f near=%.2f → 重写", ctx.label, over, near)
                again = _clean_content(
                    ctx.brain.chat(system, f"{user}\n\n{fix}", max_tokens=mtx),
                    lo=lo_c, hi=hi_c)
                if again:
                    truncated = ctx.brain.last_finish == "length"
                    content = human.shape(again, pl)
                    vec = dense.encode(content)
        except Exception as e:
            log.warning("语义查重跳过：%s", e)

        # ---------- 出厂检验 ----------
        # 同一套 prompt 跑 20 条，好的很好、坏的不能用，靠继续调提示词只会来回摆。
        # 这里给每条打一次分，不过线的先重写一次，还不过线就压进待审队列不发。
        qres = quality.score(content or "", pl, campus=ctx.campus, recent=recent_posts)
        rq = ctx.cfg.get("review") or {}
        min_score = int(rq.get("min_score") or 80)
        # 不是「总分不够」才重写。断在半句、跨校区这类硬伤，95 分也要重写：
        # 六项里只有一项失分，但那一项就是最刺眼的地方。
        if quality.verdict(qres, min_score) != "publish":
            fix = quality.brief(qres)
            log.info("[评分] %s %d 分（%s）→ %s：%s", ctx.label, qres["total"],
                     ",".join(f"{k}{v}" for k, v in qres["parts"].items()),
                     quality.verdict(qres, min_score), fix[:40])
            again = _clean_content(
                ctx.brain.chat(system, f"{user}\n\n【这条不及格，按这个改】{fix}",
                               max_tokens=mtx),
                lo=lo_c, hi=hi_c)
            if again:
                again = human.shape(again, pl)
                q2 = quality.score(again, pl, campus=ctx.campus, recent=recent_posts)
                if q2["total"] >= qres["total"]:
                    content, qres = again, q2
                    try:
                        vec = dense.encode(content)
                    except Exception:
                        pass
            log.info("[评分] %s 重写后 %d 分（%s）", ctx.label, qres["total"],
                     quality.verdict(qres, min_score))
        plan["_score"] = qres["total"]
        plan["_score_detail"] = qres["parts"]
        plan["_fixes"] = qres["fixes"]
        plan["_verdict"] = quality.verdict(qres, min_score)

    if not content:
        return False, "内容生成失败（LLM 没返回可用正文）"

    # ---------- 闸门 ----------
    rq = ctx.cfg.get("review") or {}
    mode = (rq.get("mode") or "off").lower()
    min_score = int(rq.get("min_score") or 80)
    score_v = int(plan.get("_score") or 0)
    qd = plan.get("_score_detail") or {}
    force_queue = ctx.account in (rq.get("queue_accounts") or [])
    verdict = plan.get("_verdict") or "queue"
    if mode != "off" and (mode == "queue" or force_queue or verdict != "publish"):
        if ctx.dry_run:
            return True, (f"[干跑·待审] {score_v} 分 {verdict} "
                          f"{','.join(f'{k}{v}' for k, v in qd.items())}：{content[:40]}")
        did = ctx.store.draft_add(
            ctx.account, content, label=ctx.label, campus=ctx.campus,
            score=score_v, detail=json.dumps(qd, ensure_ascii=False),
            fixes="；".join(plan.get("_fixes") or []),
            topic=plan.get("angle", ""), board=plan.get("subtopic_name", ""),
            subtopic_id=plan.get("subtopic_id", ""), topic_id=plan.get("topic_id", ""),
            form=pl.get("form", ""), seed_doc=plan.get("seed_doc", ""),
        )
        ctx.log_action("post_queue", daykey, content, True)
        ctx.mark("post_slot", daykey, f"queued:{did}")
        if score_v < min_score:
            why_q = f"评分 {score_v} 未达 {min_score}"
        elif force_queue:
            why_q = f"{score_v} 分，但这个号在灰度审核名单里"
        else:
            why_q = f"{score_v} 分，判定为 {verdict}"
        return True, (f"{why_q}（{','.join(f'{k}{v}' for k, v in qd.items())}），"
                      f"已进待审队列 #{did}")
    log.info("[评分] %s %d 分，直接发布", ctx.label, score_v)

    if ctx.dry_run:
        ctx.log_action("post", daykey, content, True)
        ctx.mark("post_slot", daykey, "dry")
        ctx.pick("angle", plan["angle"], plan["subtopic_name"])
        ctx.pick("topic", plan["subtopic_id"], plan["subtopic_name"])
        return True, (f"[干跑 {score_v}分·{plan.get('_verdict','')}] 拟发"
                      f"「{plan['subtopic_name']}」：{content}（{int((time.time()-t0)*1000)}ms）")

    res = ops.create_post(
        ctx.client,
        content=content,
        school_id=ctx.school_id,
        topic_id=plan["topic_id"],
        subtopic_id=plan["subtopic_id"],
        is_anonymous=ctx.anon,
    )
    ctx.mark("post", res["id"], content[:80])
    ctx.mark("post_slot", daykey, res["id"])
    ctx.store.remember_text("post", content)
    human.remember(content, ctx.account)
    if vec is not None:
        topic.remember_vec(content, vec, ctx.account)
    ctx.pick("angle", plan["angle"], plan["subtopic_name"])
    ctx.pick("topic", plan["subtopic_id"], plan["subtopic_name"])
    return True, f"已发到「{plan['subtopic_name']}」{res['id']}（{int((time.time()-t0)*1000)}ms）"


def publish_draft(ctx: Ctx, d: dict) -> tuple[bool, str]:
    """把审核通过的草稿真正发出去。

    闸门只负责「卡住不合格的」，发布仍然走同一条 ops.create_post 和同一套记账，
    这样配额、去重、跨号共享记忆都不用另写一份。
    """
    content = (d.get("content") or "").strip()
    if not content:
        return False, "草稿为空"
    if not d.get("subtopic_id") or not d.get("topic_id"):
        return False, "这条草稿没绑定板块（抽话题时没匹配上），只能重新生成"
    ok, why = ctx.guard.allow("post", manual=ctx.manual)
    if not ok:
        return False, why
    try:
        res = ops.create_post(
            ctx.client, content=content, school_id=ctx.school_id,
            topic_id=d["topic_id"], subtopic_id=d["subtopic_id"],
            is_anonymous=ctx.anon,
        )
    except Exception as e:
        return False, f"发布失败：{e}"
    ctx.mark("post", res["id"], content[:80])
    ctx.mark("post_slot", datetime.now().strftime("%Y-%m-%d %H:%M"), res["id"])
    ctx.store.remember_text("post", content)
    human.remember(content, ctx.account)
    try:
        topic.remember_vec(content, dense.encode(content), ctx.account)
    except Exception:
        pass
    ctx.store.draft_decide(d["id"], "published", by="daemon",
                           published_id=res["id"], note="审核通过后自动发布")
    return True, f"已发到「{d.get('board','')}」{res['id']}"


def due_drafts(ctx: Ctx, limit: int = 1) -> list[dict]:
    """本号审核通过、还没发的草稿。按通过时间排队，先通过的先发。"""
    rows = ctx.store.draft_list("approved", limit=40)
    return [d for d in rows if d.get("account") == ctx.account][:limit]


# ---------- 2. 主动回复评论 ----------


def reply_comments(ctx: Ctx, max_n: int = 3) -> tuple[bool, str]:
    t0 = time.time()
    data = ops.latest_replies(ctx.client)
    tasks = []

    for e in (data.get("userComments") or {}).get("edges") or []:
        n = e["node"]
        if ctx.mine((n.get("creator") or {}).get("id")):
            continue
        tasks.append(
            {
                "key": "reply_comment:" + n["id"],
                "kind": "comment",
                "comment_id": n["id"],
                "reply_to": None,
                "their": n.get("content", ""),
                "post": (n.get("post") or {}).get("content", ""),
            }
        )

    for e in (data.get("userSubcomments") or {}).get("edges") or []:
        n = e["node"]
        if ctx.mine((n.get("creator") or {}).get("id")):
            continue
        cid = (n.get("comment") or {}).get("id")
        if not cid:
            continue
        tasks.append(
            {
                "key": "reply_sub:" + n["id"],
                "kind": "subcomment",
                "comment_id": cid,
                "reply_to": n["id"],
                "their": n.get("content", ""),
                "post": (n.get("comment") or {}).get("content", ""),
            }
        )

    tasks = [t for t in tasks if not ctx.seen("reply", t["key"])]
    if not tasks:
        return False, "没有新的待回复"

    tasks = tasks[: max(1, max_n)]
    jobs, plans = [], []

    def _build_reply(t):
        scene = rag.classify(t["their"], None)
        # 回复是「接着对方那句话往下说」，情绪要从对方这句话里读，
        # 所以姿态用 their 判，不用原帖——对方可能是在自嘲，原帖却是崩溃稿
        mp = mood.plan(t["their"], None)
        pl = human.plan("comment", None, scene, ctx.rng("shape"))
        if mp["long"]:
            pl = dict(pl, lo=30, hi=70)
        system, user, enabled = ctx.prompt(
            "reply", post=t["post"][:200], their=t["their"][:200], style="",
            scene=scene, sample=t["their"][:160], length=human.brief(pl),
            hint=human.sticker_hint(pl),
        )
        if enabled:
            system = f"{system}\n\n{mood.brief(mp)}"
        return pl, system, user, enabled

    with ThreadPoolExecutor(max_workers=min(3, len(tasks))) as ex:
        built = list(ex.map(_build_reply, tasks))
    for pl, system, user, enabled in built:
        plans.append(pl)
        jobs.append({"system": system, "user": user,
                     "max_tokens": max(20, min(160, int(pl["hi"] * 2)))} if enabled else None)

    # 并行生成，别一条一条等
    real_jobs = [j for j in jobs if j]
    got = ctx.brain.chat_many(real_jobs, workers=4) if real_jobs else []
    it = iter(got)
    texts = [next(it, None) if job else None for job in jobs]

    # 对方问的是具体事实（食堂几点开、哪个学院在哪个校区）时，先让模型自己查再答。
    # 查不到就照实说不知道——编一个具体名字比说不知道更像机器人。
    for i, t in enumerate(tasks):
        if not facts.need_lookup(t["their"]):
            continue
        system, user, _en = ctx.prompt(
            "reply", post=t["post"][:200], their=t["their"][:200], style="",
            scene=plans[i]["scene"], sample=t["their"][:160],
            length=human.brief(plans[i]), hint=human.sticker_hint(plans[i]),
        )
        # 先把本地查到的硬事实塞进 prompt（确定性），再放出工具让它自己补查
        hit = facts.search(t["their"], k=2)
        if hit and "没有这条" not in hit:
            user = (f"{user}\n\n【本地查到的资料，用得上就用，不确定就别写具体名字】\n{hit}")
        got = ctx.brain.chat_tools(system, user,
                                   max_tokens=max(30, int(plans[i]["hi"] * 2)))
        if got:
            texts[i] = got
            log.info("[查证] %s 对方问的是具体事实，查过再答", ctx.label)

    # 回复是「对方刚跟我说了一句话」，最忌客服腔，同样只重写不合格的。
    # 楼中楼也会成套出现（三个号分别在三个楼里回「那加你吧」「加个微信呗」），
    # 所以这里过一遍反复读：动作配额 + 口水词配额。
    recent_r = ctx.store.recent_texts("reply", 30) + human.shared_recent(60)
    redo: list[tuple[int, str]] = []
    for i, (t, pl) in enumerate(zip(texts, plans)):
        cand = _clean_content(t or "", lo=1, hi=max(50, pl["hi"] * 2))
        bad, why = human.needs_rewrite(cand or "")
        if not bad and cand:
            dup = antirepeat.check(cand, recent=recent_r)
            if dup:
                bad, why = True, dup
        if bad:
            redo.append((i, why))
    if redo:
        rjobs = []
        for i, why in redo:
            system, user, enabled = ctx.prompt(
                "reply", post=tasks[i]["post"][:200], their=tasks[i]["their"][:200],
                style="", scene=plans[i]["scene"], sample=tasks[i]["their"][:160],
                length=human.brief(plans[i]), hint=human.sticker_hint(plans[i]),
            )
            rjobs.append({"system": system, "user": f"{user}\n\n{why}",
                          "max_tokens": max(20, min(160, int(plans[i]["hi"] * 2)))} if enabled else None)
        got2 = ctx.brain.chat_many([j for j in rjobs if j], workers=4) if rjobs else []
        for (i, _), t2 in zip(redo, got2):
            if t2:
                texts[i] = t2

    done = 0
    for t, job, pl, text in zip(tasks, jobs, plans, texts):
        if not text:
            text = ctx.brain.pick_fallback(recent=recent_r)
        text = _clean_content(text, lo=1, hi=max(50, pl["hi"] * 2)) or ctx.brain.pick_fallback(recent=recent_r)
        text = human.shape(text, pl)
        if ctx.guard.similar("reply", text):
            text = ctx.brain.pick_fallback(recent=recent_r)

        ok, why = ctx.guard.allow("reply", manual=ctx.manual)
        if not ok:
            return done > 0, why

        if ctx.dry_run:
            ctx.log_action("reply", t["key"], text, True)
            ctx.mark("reply", t["key"], "dry")
            done += 1
            continue

        ctx.guard.sleep_jitter(0 if ctx.manual else 0.6)
        ops.create_subcomment(
            ctx.client,
            comment_id=t["comment_id"],
            content=text,
            reply_to_id=t["reply_to"],
            is_anonymous=ctx.anon,
        )
        ctx.mark("reply", t["key"], text)
        ctx.store.remember_text("reply", text)
        human.remember(text, ctx.account)
        done += 1

    return done > 0, f"回复 {done} 条（{int((time.time()-t0)*1000)}ms）"


# ---------- 3. 自动点赞 ----------


def autolike(ctx: Ctx, max_n: int = 3) -> tuple[bool, str]:
    t0 = time.time()
    tcfg = ctx.cfg["tasks"]["autolike"]
    pool = ops.feed_posts(ctx.client, ctx.school_id, pages=1)
    cands = []
    for n in pool:
        if ctx.mine((n.get("creator") or {}).get("id")):
            continue
        if n.get("viewerHasLiked"):
            continue
        if ctx.seen("like", n["id"]):
            continue
        ok, _ = ctx.guard.text_ok(n.get("content", ""))
        if not ok:
            continue
        # 可选：让 LLM 先判一下值不值得赞，避免赞到广告和引战
        if tcfg.get("llm_filter") and ctx.brain.enabled:
            system, user, enabled = ctx.prompt("like", post=n.get("content", "")[:300])
            if enabled:
                verdict = (ctx.brain.chat(system, user, max_tokens=8) or "").strip().upper()
                if not verdict.startswith("Y"):
                    log.info("[筛选] 跳过 %s：llm=%r", n["id"], verdict[:20])
                    continue
        cands.append(n)

    # 时间种子打乱 + 抽样，不同账号不会点同一批
    r = ctx.rng("like-target")
    r.shuffle(cands)
    cands = cands[: min(max_n, int(tcfg.get("max_per_run") or 3))]

    done = 0
    for n in cands:
        ok, why = ctx.guard.allow("like", manual=ctx.manual)
        if not ok:
            return done > 0, why
        if ctx.dry_run:
            ctx.log_action("like", n["id"], n["content"][:40], True)
            ctx.mark("like", n["id"], "dry")
            done += 1
            continue
        ctx.guard.sleep_jitter(0 if ctx.manual else 0.5)
        ops.add_like(ctx.client, n["id"])
        ctx.mark("like", n["id"], n["content"][:60])
        done += 1

    return done > 0, f"点赞 {done} 条（{int((time.time()-t0)*1000)}ms）"


# ---------- 4. 随机评论 ----------


def build_comment_prompt(ctx: Ctx, post_text: str, board: str,
                         prev: list[str] | None = None,
                         post_id: str = "") -> dict:
    """构建一条评论要发给模型的东西。

    抽成模块级函数是为了让离线评估跑**同一条路径**：评估脚本如果自己另拼一套
    prompt，测出来的分数跟线上没关系，改了也不知道变好还是变坏。

    prev 是这帖下面已经有人说过的话，会拼进 system 末尾防撞车。
    """
    prev = prev or []
    st = persona.pick_comment_style(ctx.persona)
    scene = rag.classify(post_text, board)
    mp = mood.plan(post_text, board)
    pl = human.plan("comment", board, scene, ctx.rng("shape"))
    # 偶尔就是一句「蹲」「顶」「？」，这种条目不值得调模型，也更像真人
    early = human.reaction(scene, post_text, board, rng=ctx.rng("react"))
    if early:
        return {"style": st, "board": board, "plan": pl, "system": "", "user": "",
                "enabled": False, "early": early, "mood": mp, "longform": None,
                "scene": scene}

    # 强情感帖才给超长档。语料里情绪帖下 60% 的评论超过 100 字，
    # 而全站只有 2.9%——长评论是强情感帖的配套，不是普遍现象。
    lf = longform.plan_comment(post_text, mp, ctx.rng("long"))
    if lf:
        pl = dict(pl, lo=lf["lo"], hi=lf["hi"], newline=True, longform=lf["kind"])
        length_txt = longform.brief_comment(lf)
    else:
        if mp["long"]:
            # 人家写了三百字，回五个字就是敷衍。长帖强制走长档
            pl = dict(pl, lo=38, hi=82)
        length_txt = human.brief(pl)

    system, user, enabled = ctx.prompt(
        "comment", post=post_text[:300], style=st, board=board,
        scene=scene, sample=post_text[:200], post_id=post_id,
        length=length_txt, hint=human.sticker_hint(pl),
        long_text=bool(lf),
    )
    if enabled:
        # 评论也得知道今天几号、下没下雨。不然随口一句「今天还下着雨」，
        # 或者把现在当成别的月份，本地人一眼看出是假的。
        try:
            ev = events.brief(ctx.campus, with_notice=False)
            if ev:
                user = (f"{user}\n\n【今天】{ev}。这只是背景，别把日期写进正文；"
                        "不要自己推月份；今天没下雨就别写雨、伞、淋湿。")
        except Exception:
            pass
        # 姿态放最后：前面是知识库和示范，通用约束在上面，这里是最具体的一条。
        # 长文例外：mood.brief 是「说一句你在」这种短评指令，会跟拆解/分析打架
        system = (f"{system}\n\n{longform.mood_note(mp)}" if lf
                  else f"{system}\n\n{mood.brief(mp)}")
        if prev:
            body = "\n".join(f"- {x}" for x in prev[:8])
            system = (
                f"{system}\n\n【这帖下面已经有人说过的】\n{body}\n"
                "上面这些角度、句式和开头都别再重复：别重复同一条建议，"
                "别套同一个句式，也别用同一句话起头。"
                "换一个别人没提到的角度，或者只说你自己会怎么做。"
            )
        # 评论最容易露馅的地方，是把一句随口接话写成小建议。
        # 「好处是…坏处是…」「先想清楚再行动」这种结构，真人在回帖里不写。
        # 「确实」「其实」这类填充词也是：真人回帖直接说事，不先垫一句认同。
        system = (f"{system}\n\n不要分析利弊、不要罗列理由、不要用「先…再…」"
                  "「建议你」「先想清楚」这类句式，不要给完整方案。"
                  "不要用「确实」「其实」「总的来说」「不得不说」这些垫词。"
                  "像在群里顺口接一句，说完就停。")
    return {"style": st, "board": board, "plan": pl, "system": system, "user": user,
            "enabled": enabled, "early": None, "mood": mp, "longform": lf,
            "scene": scene}


def random_comment(ctx: Ctx, max_n: int = 1) -> tuple[bool, str]:
    t0 = time.time()
    # 吃货号不主动评价别人。它的人设只关心吃什么、多少钱，在别人崩溃的帖子下面
    # 接一句「这价格不如去食堂」，比不说话更假。让它只发帖、只回自己楼里的。
    if (ctx.persona or "") == "food":
        return False, "吃货号不参与主动评论（人设只写吃的，评价情绪帖反而违和）"

    tcfg = ctx.cfg["tasks"]["random_comment"]
    max_hot = int(tcfg.get("skip_if_comments_over") or 20)
    max_min_age = float(tcfg.get("max_age_hours") or 72)

    # 抓两页，候选池更大，选中重复的概率更低
    pool = ops.feed_posts(ctx.client, ctx.school_id, pages=2)

    now = datetime.now(timezone.utc)
    cands = []
    for n in pool:
        if ctx.mine((n.get("creator") or {}).get("id")):
            continue
        if ctx.seen("comment", n["id"]):
            continue
        # 长文帖评论天然多，用同一个上限会把它们全筛掉，正好和「多关注长帖」相反
        longpost = len(n.get("content") or "") >= 120
        if int(n.get("commentCount") or 0) > (max_hot * 2 if longpost else max_hot):
            continue
        ok, _ = ctx.guard.text_ok(n.get("content", ""))
        if not ok:
            continue
        # 招工/代拿/按时报价的帖子不能当评论目标：机器人会一本正经去应聘
        if topic.is_ad(n.get("content", "")):
            continue
        created = n.get("createdAt") or ""
        try:
            cdt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if cdt.tzinfo is None:
                cdt = cdt.replace(tzinfo=timezone.utc)
            age_h = (now - cdt).total_seconds() / 3600
            if age_h > max_min_age:
                continue
        except Exception:
            pass
        cands.append(n)

    if not cands:
        return False, "候选池为空（都评论过或帖子太老）"

    # 长文帖优先：认真写一大段的帖子，回起来才有东西可接，
    # 而候选池是按新鲜度抽的，长帖很容易被短帖冲掉。
    for n in cands:
        n["_w"] = 1.0 + min(len(n.get("content") or ""), 400) / 220.0

    # 同一条新帖被几个号轮流刷是最明显的协同痕迹。
    # 以前只靠 recent_targets(1.5h) 事后避让，两个漏洞：
    #   ① 32 个 worker 是 32 个进程，同时启动时读到的是同一份旧快照，谁都没看到别人；
    #   ② 能避让的候选不足 2 条时直接退回原池，等于不避让。
    # 现在改成发之前先在 state.db 里原子占坑（主键唯一约束），抢不到就换帖。
    hot = human.recent_targets(6.0)
    cands = [n for n in cands if n["id"] not in hot]

    batch = persona.pick_targets(ctx.store, cands, min(len(cands), max(6, max_n * 4)), ctx.account)
    chosen = []
    for n in batch:
        if ctx.store.claim_target(n["id"], ctx.account, hours=6.0):
            chosen.append(n)
            if len(chosen) >= max_n:
                break
        else:
            log.info("[避让] %s 跳过 %s（%s 已占）", ctx.label, n["id"][-6:],
                     ctx.store.claim_owner(n["id"]) or "别的号")
    if not chosen:
        return False, "候选帖都被别的号占了（同帖 6 小时避让）"

    jobs, styles, plans, boards = [], [], [], []
    # 同帖已有评论。同一帖下面三个号都给「找宿管」这种事，光靠全局 3-gram 抓不到：
    # 换词不换事，重合度照样很低。要把这帖下面已经说过的话直接摆给模型看。
    same_post: dict[int, list[str]] = {}

    def _build(item):
        """每条帖子都要检索一次（编码 + 整库扫描 + 精排 ≈ 300ms），串行拼就是线性变慢。"""
        i, n = item
        # 先看这帖下面已经有人说了什么。这是治「几个号说同一件事」最直接的一条：
        # 不把已有评论摆给模型，它永远会重新想到「找宿管」。
        prev: list[str] = []
        try:
            cd = ops.list_comments(ctx.client, n["id"], order="LATEST")
            for e in (cd.get("edges") or []):
                c = ((e.get("node") or {}).get("content") or "").strip()
                if c and c not in prev:
                    prev.append(c[:100])
                if len(prev) >= 8:
                    break
        except Exception as e:
            log.debug("拉同帖评论失败 %s: %s", n["id"][-6:], e)
        same_post[i] = prev

        bd = ((n.get("subtopic") or {}).get("name") or (n.get("topic") or {}).get("name") or "")
        r = build_comment_prompt(ctx, n.get("content") or "", bd, prev, n.get("id") or "")
        mp = r["mood"]
        if r["longform"]:
            log.info("[长评] %s %s %s %d-%d字（%s）", ctx.label, (n.get("id") or "")[-6:],
                     r["longform"]["kind"], r["plan"]["lo"], r["plan"]["hi"], mp["mood"])
        log.info("[姿态] %s %s %s/%s %s", ctx.label, (n.get("id") or "")[-6:], mp["mood"],
                 mp["gender"], "长帖" if mp["long"] else "")
        return (r["style"], r["board"], r["plan"], r["system"], r["user"],
                r["enabled"], r["early"])

    with ThreadPoolExecutor(max_workers=min(4, len(chosen))) as ex:
        built = list(ex.map(_build, list(enumerate(chosen))))
    early_text: dict[int, str] = {}
    for i, (_st, _bd, _pl, system, user, enabled, early) in enumerate(built):
        styles.append(_st)
        boards.append(_bd)
        plans.append(_pl)
        if early:
            early_text[i] = early
            jobs.append(None)
            continue
        jobs.append({"system": system, "user": user,
                     "max_tokens": max(30, min(700, int(_pl["hi"] * 2.4)))} if enabled else None)

    real_jobs = [j for j in jobs if j]
    got = ctx.brain.chat_many(real_jobs, workers=4) if real_jobs else []
    it = iter(got)
    texts = [early_text[i] if i in early_text else (next(it, None) if jobs[i] else None)
             for i in range(len(jobs))]
    for i in early_text:
        log.info("[反应] %s 种子帖=%s → %s", ctx.label, chosen[i]["id"], early_text[i])

    # 客服味/自我复读 → 只把不合格的重写一轮，并且仍然并行
    recent = ctx.store.recent_texts("comment", 30) + human.shared_recent(120)
    redo: list[tuple[int, str]] = []
    for i, (t, pl) in enumerate(zip(texts, plans)):
        cand = _clean_content(t or "", lo=2, hi=max(60, pl["hi"] * 2))
        if cand:
            cand = ensure_sticker(cand, pl, plans[i].get("scene") or "")
        bad, why = human.needs_rewrite(cand or "")
        if not bad and cand:
            bad, why = campus_violation(cand, ctx.campus)
        if not bad and cand:
            wc = (quality.weather_clash(cand) or quality.fake_rain(cand)
                  or quality.month_clash(cand))
            if wc:
                bad, why = True, wc
        if not bad and cand and len(cand) > pl["hi"] + 12:
            bad, why = True, (f"太长了，压到 {pl['lo']}-{pl['hi']} 字，" +
                              ("删掉重复的解释，保留第一句判断和那条带原话的建议。"
                               if pl.get("longform") else
                               "一条评论只说一件事，别补第二句。"))
        if not bad and cand and pl.get("longform"):
            # 长评论出厂检查：有没有给真东西（判断/建议/原话），有没有画面堆砌
            gap = quality.advice_gap(cand)
            if gap:
                bad, why = True, gap
            elif quality.scene_porn_count(cand) >= 1:
                bad, why = True, ("删掉「关东煮/路灯/影子」这类自我表演式画面，"
                                  "改成对这件事的判断，补一条能照做的建议。")
        if not bad and cand:
            # 反复读检查：同帖动作撞车 / 同帖句式撞车 / 开头撞车 / 口水词超配额
            dup = antirepeat.check(cand, same_post=same_post.get(i), recent=recent)
            if dup:
                bad, why = True, dup
        if not bad and cand and human.repeats(recent, cand):
            bad, why = True, "和最近说过的话太像了，换个说法、换个开头。"
        if bad:
            redo.append((i, why))
    if redo:
        rjobs = []
        for i, why in redo:
            _p = plans[i]
            if _p.get("longform") in longform.COMMENT_KINDS:
                _len = longform.brief_comment(
                    {"kind": _p["longform"], **longform.COMMENT_KINDS[_p["longform"]]})
            else:
                _len = human.brief(_p)
            system, user, enabled = ctx.prompt(
                "comment", post=chosen[i]["content"][:300], style=styles[i],
                board=boards[i], scene=_p["scene"],
                sample=chosen[i]["content"][:200], post_id=chosen[i].get("id"),
                length=_len, hint=human.sticker_hint(_p),
                long_text=bool(_p.get("longform")),
            )
            if enabled:
                _mp = mood.plan(chosen[i]["content"], boards[i])
                system = (f"{system}\n\n{longform.mood_note(_mp)}" if _p.get("longform")
                          else f"{system}\n\n{mood.brief(_mp)}")
            rjobs.append({"system": system, "user": f"{user}\n\n{why}",
                          "max_tokens": max(30, min(700, int(plans[i]["hi"] * 2.4)))} if enabled else None)
        got2 = ctx.brain.chat_many([j for j in rjobs if j], workers=4) if rjobs else []
        for (i, _), t2 in zip(redo, got2):
            if t2:
                texts[i] = t2

    done = 0
    sent: set[str] = set()

    def _release_unsent() -> None:
        """没真发出去的把坑还回去，6 小时内别的号还能用这条帖。"""
        for x in chosen:
            if x["id"] not in sent:
                ctx.store.release_target(x["id"])

    for i, (n, job, st, pl, text) in enumerate(zip(chosen, jobs, styles, plans, texts)):
        if not text:
            text = ctx.brain.fallback()
        text = _clean_content(text, lo=2, hi=max(60, pl["hi"] * 2)) or ctx.brain.fallback()
        text = human.shape(text, pl)
        if ctx.guard.similar("comment", text, threshold=0.85):
            text = ctx.brain.pick_fallback(same_post.get(i), recent)

        ok, why = ctx.guard.allow("comment", manual=ctx.manual)
        if not ok:
            _release_unsent()
            return done > 0, why

        log.info(
            "[选帖] %s 种子帖=%s 评论数=%s 写法=%s → %s",
            ctx.label, n["id"], n.get("commentCount"), st, text,
        )

        if ctx.dry_run:
            ctx.log_action("comment", n["id"], text, True)
            ctx.mark("comment", n["id"], "dry")
            done += 1
            continue

        ctx.guard.sleep_jitter(0 if ctx.manual else 0.6)
        ops.create_comment(ctx.client, n["id"], text, is_anonymous=ctx.anon)
        ctx.mark("comment", n["id"], text)
        ctx.store.remember_text("comment", text)
        human.remember(text, ctx.account)
        human.remember_target(n["id"], ctx.account)
        sent.add(n["id"])
        done += 1

    _release_unsent()
    return done > 0, f"评论 {done} 条（{int((time.time()-t0)*1000)}ms）"
