"""知识库合成：把 stats.json + samples.json 交给 LLM，提炼成可注入的知识库。

    python kb_build.py            # 四个维度各问一次，合并写 kb/knowledge.json
    python kb_build.py --only memes

产物：
    kb/knowledge.json   运行期注入用
    kb/KNOWLEDGE.md     人读版，可在 Web 控制台查看
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from szlike.brain import Brain

KB = ROOT / "kb"


def brain() -> Brain:
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    return Brain(cfg.get("llm") or {})

SAFETY = """铁律（违反即作废）：
1. 只输出**群体层面的规律**，绝对不出现任何具体的人名、昵称、老师称呼、宿舍楼名、门牌号、QQ/微信号、手机号、学号、社团名、店名。
2. 不要复述任何一条原文，不要引用具体事件、具体日期、具体课程名、具体老师。
3. 不要写「某同学说」「有人发帖说」这类叙述，直接写结论。
4. 只输出 JSON，不要 markdown 代码块，不要解释。"""

SCHEMA_HINT = "字段值用简洁的中文短句，每条不超过 30 字；数组 6-12 条为宜。"


def pick_samples(samples: dict, boards: list[str], key: str, k: int) -> list[str]:
    out = []
    for b in boards:
        d = samples["boards"].get(b)
        if not d:
            continue
        out += d.get(key, [])[:k]
    return out


def brief_stats(stats: dict) -> str:
    r = stats["rhythm"]
    names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    wd = r["weekday"]
    tot = sum(wd) or 1
    lines = [f"发帖时段（0-23 点计数）：{r['hour']}",
             "各小时占比：" + " ".join(f"{h}点{round(100*v/tot,1)}%" for h, v in enumerate(r["hour"]) if v),
             "星期分布：" + " ".join(f"{n}{round(100*v/tot,1)}%" for n, v in zip(names, wd)),
             f"夜间0-6点 {r['night_share']}% · 白天8-18点 {r['work_share']}% · 晚18-24点 {r['eve_share']}%",
             f"覆盖 {r['active_days']} 天，日均 {r['per_day_avg']} 帖"]
    lines.append("按月：" + " ".join(f"{k}:{v}" for k, v in list(r["month"].items())[-14:]))
    lines.append("板块：" + "；".join(
        f"{k}({v['posts']}帖,中位{v['len_median']}字,均{v['avg_comment']}评,提问{v['question_ratio']}%)"
        for k, v in stats["boards"].items()))
    L = stats["length"]
    lines.append(f"帖长 p10/p50/p90 = {L['post']['p10']}/{L['post']['p50']}/{L['post']['p90']} 字；"
                 f"评论 p10/p50/p90 = {L['comment']['p10']}/{L['comment']['p50']}/{L['comment']['p90']} 字")
    lines.append("表情标记：" + " ".join(f"[{k}]x{v}" for k, v in list(stats["stickers"].items())[:45]))
    lines.append("标点：" + json.dumps(stats["punct"], ensure_ascii=False))
    lines.append("帖开头高频二字节：" + " ".join(f"{k}x{v}" for k, v in list(stats["openers"].items())[:18]))
    lines.append("帖结尾高频二字节：" + " ".join(f"{k}x{v}" for k, v in list(stats["enders"].items())[:18]))
    lines.append("评论开头：" + " ".join(f"{k}x{v}" for k, v in list(stats["comment_openers"].items())[:14]))
    return "\n".join(lines)


def ask(brain: Brain, name: str, system: str, user: str, max_tokens: int = 1800) -> dict:
    t0 = time.time()
    txt = brain.chat(system, user, max_tokens=max_tokens, temperature=0.35) or ""
    ms = int((time.time() - t0) * 1000)
    data = extract_json(txt)
    if data is None:
        raise SystemExit(f"[{name}] 返回不是 JSON（{ms}ms）：\n{txt[:600]}")
    print(f"  ✓ {name} {ms}ms  keys={list(data)[:8]}")
    return data


def extract_json(t: str):
    t = (t or "").strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        return json.loads(t[a:b + 1])
    except Exception:
        return None


# ---------------------------------------------------------------- 四个维度
def sec_campus(stats, samples) -> dict:
    sys_p = ("你在为「苏州大学校园社区」写一份群体画像，供内容创作者参考。"
             "根据给定的统计与匿名语料，归纳：校区与地理、通勤、生活结构、作息、学期节奏、"
             "食堂与周边饮食、宿舍与作息现象、校园空间使用（图书馆/自习/运动）、"
             "学生群体的构成与社交形态。" + SAFETY + SCHEMA_HINT)
    user = f"""统计：
{brief_stats(stats)}

校园生活类语料（已脱敏，仅供归纳，不要复述）：
{json.dumps(pick_samples(samples, ["抛抛帖子","求助提问箱","新生专栏","今天吃什么","打工电波"], "spread", 6), ensure_ascii=False)}

输出 JSON：
{{"campuses":["校区名+一句特征"],
 "geography":["地形/位置/通勤的规律，如跨校区距离感、校车、地铁、骑车"],
 "life_rhythm":["一天的作息规律，几点起、几点吃饭、几点自习、几点睡"],
 "term_calendar":["学期节奏：开学、期中、期末、考试周、体测、选课、假期前后的情绪与话题变化"],
 "food":["食堂与外卖的普遍情况，不点具体窗口名"],
 "space":["图书馆/自习/运动/夜宵等空间使用习惯"],
 "social":["社交形态：匿名倾向、找搭子、社恐、恋爱、宿舍关系"],
 "pain_points":["学生反复抱怨的结构性问题（不点具体人或楼）"]}}"""
    return ask(brain(), "campus", sys_p, user)


def sec_memes(stats, samples) -> dict:
    ng = [f"{g}×{c}" for g, c in stats["ngram_post"][:300]]
    ngc = [f"{g}×{c}" for g, c in stats["ngram_comment"][:160]]
    sys_p = ("你在为苏州大学校园社区整理「玩梗与黑话词典」。任务：从词频表和匿名语料里识别出"
             "真正属于该校/该社区的梗、自称、缩写、固定句式、表情标记用法。"
             "判断标准：高频 + 有特定含义 + 带情绪色彩。普通词不要收。"
             + SAFETY + SCHEMA_HINT)
    user = f"""帖子高频词：{" ".join(ng[:220])}

评论高频词：{" ".join(ngc[:120])}

表情标记（平台贴纸，方括号内为名称）：{json.dumps(stats['stickers'], ensure_ascii=False)}

高频梗词命中：{json.dumps(stats['emotion']['top_meme_words'], ensure_ascii=False)}

语料片段（已脱敏）：
{json.dumps(pick_samples(samples, ["苏大迷惑大赏","抛抛帖子","今天不开心","苏大神奇动物"], "hot", 5), ensure_ascii=False)}
{json.dumps(samples["comments"]["top"][:40], ensure_ascii=False)}

输出 JSON：
{{"self_ref":["自称与称呼，如鼠/鼠鼠/本鼠/uu 的用法与语境"],
 "lexicon":[{{"w":"词","meaning":"含义","usage":"什么场合用","vibe":"情绪色彩"}}],
 "patterns":["固定句式/半句话梗，如「X，我的X」「关于X，我的评价是」这类可套用的结构"],
 "sticker_usage":[{{"s":"[名称]","when":"什么情绪下用"}}],
 "abbrev":["缩写与拼音梗"],
 "overused":["用滥了、现在用会被嫌尬的表达"]}}"""
    return ask(brain(), "memes", sys_p, user)


def sec_voice(stats, samples) -> dict:
    L = stats["length"]
    sys_p = ("你在为苏州大学校园社区校准「说话方式」。根据统计与匿名语料，归纳发帖与评论的"
             "语言风格：句长、分句、语气、标点、表情、开场、收尾、人称、书面/口语比例、"
             "情绪表达方式、什么时候严肃什么时候插科打诨。" + SAFETY + SCHEMA_HINT)
    user = f"""长度：帖子中位 {L['post']['p50']} 字（p10 {L['post']['p10']} / p90 {L['post']['p90']}）；
评论中位 {L['comment']['p50']} 字（p10 {L['comment']['p10']} / p90 {L['comment']['p90']}）。
标点：{json.dumps(stats['punct'], ensure_ascii=False)}
评论标点：{json.dumps(stats['comment_punct'], ensure_ascii=False)}
帖子开头：{" ".join(f"{k}×{v}" for k, v in list(stats['openers'].items())[:20])}
帖子结尾：{" ".join(f"{k}×{v}" for k, v in list(stats['enders'].items())[:20])}
评论开头：{" ".join(f"{k}×{v}" for k, v in list(stats['comment_openers'].items())[:16])}
表情标记：{" ".join(f"[{k}]×{v}" for k, v in list(stats['stickers'].items())[:40])}

帖子语料：
{json.dumps(pick_samples(samples, ["抛抛帖子","今天不开心","女孩，女孩","苏大恋爱日常","求助提问箱"], "spread", 5), ensure_ascii=False)}

评论语料：
{json.dumps(samples["comments"]["spread"][:50], ensure_ascii=False)}

输出 JSON：
{{"post_voice":["发帖的语气特征，6-10 条"],
 "comment_voice":["评论的语气特征，6-10 条"],
 "sentence":["句式习惯：短句/长句、有没有主语、爱不爱分点、爱不爱括号补充"],
 "punctuation_rules":["标点与表情的具体用法规律"],
 "openers":["自然可信的开头写法（写成可替换的模板，不要原文）"],
 "enders":["自然可信的收尾写法"],
 "serious_vs_playful":["什么话题严肃、什么话题可以玩梗"],
 "do":["写的时候必须做到的"],
 "dont":["写的时候绝对不能出现的（含 AI 味、营销味、鸡汤味的具体表现）"]}}"""
    return ask(brain(), "voice", sys_p, user)


def sec_social(stats, samples) -> dict:
    sys_p = ("你在为苏州大学校园社区写「话题地图 + 情绪应对手册」。"
             "根据统计与匿名语料，归纳：各板块的真实用途与潜规则、常见情绪场景、"
             "以及在不同情绪场景下社区里人们通常怎么回应。" + SAFETY + SCHEMA_HINT)
    user = f"""板块统计：{json.dumps(stats['boards'], ensure_ascii=False)}
情绪：正向词 {stats['emotion']['pos_hits']} 次，负向词 {stats['emotion']['neg_hits']} 次，比值 {stats['emotion']['ratio']}。
负向高频词：{json.dumps(stats['emotion']['top_meme_words'], ensure_ascii=False)}

各板块语料：
{json.dumps({k: v['hot'][:6] for k, v in samples['boards'].items()}, ensure_ascii=False)}

评论语料：
{json.dumps(samples["comments"]["top"][:60], ensure_ascii=False)}

输出 JSON：
{{"board_playbook":[{{"board":"板块名","purpose":"真实用途","rules":"潜规则/氛围","post_hint":"在这发帖该写什么"}}],
 "emotion_map":[{{"scene":"情绪场景","state":"学生此时的状态","reply":"合适的回应方式"}}],
 "reply_playbook":[{{"type":"帖子类型(求助/吐槽/炫耀/分享/寻人/恋爱/失物)","how":"评论该怎么写","avoid":"别怎么写"}}],
 "hot_topics":["长期反复出现的话题，按热度排序 12 条"],
 "taboos":["社区里会招骂/删帖/冷场的内容类型"]}}"""
    return ask(brain(), "social", sys_p, user)


def md_from(kb: dict) -> str:
    s = kb.get("stats_summary") or {}
    L = []
    L.append("# 鼠洞社区知识库")
    L.append("")
    L.append(f"构建于 {kb.get('built_at','')} · 语料 {s.get('posts','?')} 帖 / {s.get('comments','?')} 评论"
             f"（{s.get('span','')}）")
    L.append("")
    c = kb.get("campus", {})
    for key, title in [("campuses", "校区"), ("geography", "地形与通勤"), ("life_rhythm", "作息"),
                       ("term_calendar", "学期节奏"), ("food", "吃饭"), ("space", "空间使用"),
                       ("social", "社交形态"), ("pain_points", "结构性吐槽")]:
        if c.get(key):
            L.append(f"## {title}")
            L += [f"- {x}" for x in c[key]]
            L.append("")
    m = kb.get("memes", {})
    if m.get("self_ref"):
        L += ["## 自称与称呼", ""] + [f"- {x}" for x in m["self_ref"]] + [""]
    if m.get("lexicon"):
        L += ["## 词典", "", "| 词 | 含义 | 用法 | 色彩 |", "|---|---|---|---|"]
        L += [f"| {x.get('w','')} | {x.get('meaning','')} | {x.get('usage','')} | {x.get('vibe','')} |"
              for x in m["lexicon"]]
        L.append("")
    if m.get("patterns"):
        L += ["## 句式梗", ""] + [f"- {x}" for x in m["patterns"]] + [""]
    if m.get("sticker_usage"):
        L += ["## 表情用法", ""] + [f"- {x.get('s','')}：{x.get('when','')}" for x in m["sticker_usage"]] + [""]
    if m.get("overused"):
        L += ["## 用滥的", ""] + [f"- {x}" for x in m["overused"]] + [""]
    v = kb.get("voice", {})
    for key, title in [("post_voice", "发帖语气"), ("comment_voice", "评论语气"), ("sentence", "句式"),
                       ("punctuation_rules", "标点与表情"), ("openers", "开头"),
                       ("enders", "收尾"), ("serious_vs_playful", "严肃与玩笑的边界"),
                       ("do", "必须"), ("dont", "禁止")]:
        if v.get(key):
            L.append(f"## {title}")
            L += [f"- {x}" for x in v[key]]
            L.append("")
    so = kb.get("social", {})
    if so.get("board_playbook"):
        L += ["## 板块手册", "", "| 板块 | 用途 | 氛围 | 写什么 |", "|---|---|---|---|"]
        L += [f"| {x.get('board','')} | {x.get('purpose','')} | {x.get('rules','')} | {x.get('post_hint','')} |"
              for x in so["board_playbook"]]
        L.append("")
    if so.get("emotion_map"):
        L += ["## 情绪场景", ""] + [f"- **{x.get('scene','')}**（{x.get('state','')}）→ {x.get('reply','')}"
                                 for x in so["emotion_map"]] + [""]
    if so.get("reply_playbook"):
        L += ["## 回复手册", ""] + [f"- **{x.get('type','')}**：{x.get('how','')} ✗ {x.get('avoid','')}"
                                 for x in so["reply_playbook"]] + [""]
    if so.get("hot_topics"):
        L += ["## 常青话题", "", " · ".join(so["hot_topics"]), ""]
    if so.get("taboos"):
        L += ["## 禁区", ""] + [f"- {x}" for x in so["taboos"]] + [""]
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, choices=["campus", "memes", "voice", "social"])
    args = ap.parse_args()

    stats = json.loads((KB / "stats.json").read_text(encoding="utf-8"))
    samples = json.loads((KB / "samples.json").read_text(encoding="utf-8"))
    path = KB / "knowledge.json"
    kb = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    kb.setdefault("version", 1)
    kb.setdefault("campus", {})
    kb.setdefault("memes", {})
    kb.setdefault("voice", {})
    kb.setdefault("social", {})

    todo = [args.only] if args.only else ["campus", "memes", "voice", "social"]
    fns = {"campus": sec_campus, "memes": sec_memes, "voice": sec_voice, "social": sec_social}
    for name in todo:
        print(f"[{name}] 生成中…")
        try:
            kb[name] = fns[name](stats, samples)
        except SystemExit as e:
            print("  失败：", e)
    kb["built_at"] = datetime.now().isoformat(timespec="seconds")
    kb["stats_summary"] = {
        "posts": stats["corpus"]["posts"], "comments": stats["corpus"]["comments"],
        "span": " → ".join(stats["corpus"]["span"]),
        "boards": {k: v["posts"] for k, v in stats["boards"].items()},
        "length": {"post_p50": stats["length"]["post"]["p50"],
                   "comment_p50": stats["length"]["comment"]["p50"]},
        "rhythm": {"night": stats["rhythm"]["night_share"],
                   "work": stats["rhythm"]["work_share"],
                   "eve": stats["rhythm"]["eve_share"]},
        "stickers_top": list(stats["stickers"].items())[:20],
    }
    path.write_text(json.dumps(kb, ensure_ascii=False, indent=1), encoding="utf-8")
    (KB / "KNOWLEDGE.md").write_text(md_from(kb), encoding="utf-8")
    print(f"已写 {path}（{path.stat().st_size//1024}KB）与 KNOWLEDGE.md")


if __name__ == "__main__":
    main()
