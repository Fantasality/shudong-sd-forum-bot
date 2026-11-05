"""知识库分析器：把 kb/raw.db 里的原始帖子/评论压成结构化统计。

    python kb_analyze.py

产物：
    kb/stats.json          # 全量统计，供合库与前端用
    kb/samples.json        # 各板块代表性文本（已脱敏），供 LLM 提炼
    kb/stats_report.md     # 人读版体检报告
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
DB = ROOT / "kb" / "raw.db"
OUT = ROOT / "kb"

CJK = re.compile(r"[\u4e00-\u9fff]+")
BRACKET = re.compile(r"\[([^\[\]]{1,8})\]")
URL = re.compile(r"https?://\S+|www\.\S+")
AT = re.compile(r"@\S{1,20}")
LONGNUM = re.compile(r"\d{5,}")
BUILDING = re.compile(r"[A-Za-z]?\d{1,2}\s*号?[栋楼舍幢]|宿舍楼\S{0,4}|[一二三四五六七八九十]食堂")
TEACHER = re.compile(r"[\u4e00-\u9fff]{1,3}(?:老师|辅导员|教授|讲师)")

# 停用词：功能词、平台高频套话
STOP = set("""我们 你们 他们 自己 这个 那个 就是 什么 怎么 可以 没有 不是 现在 还是 因为 所以
但是 如果 真的 已经 一个 一下 有点 感觉 觉得 可能 应该 知道 喜欢 时候 东西 大家 有人 多少
这种 那种 一次 一直 一起 一样 不会 不能 不要 不过 而且 然后 其实 这里 那里 这么 那么 怎样
为什么 什么样 有没有 是不是 能不能 要不要 的话 了吗 了吧 了呢 我的 你的 他的 她的 它的
今天 明天 昨天 现在 刚才 下午 上午 晚上 早上 中午 同学 学校 苏州 大学 苏大 校区""".split())

POS_WORDS = ("开心 快乐 高兴 喜欢 舒服 期待 惊喜 感动 温柔 治愈 好吃 好看 顺利 幸运 值得 感谢 "
             "nice 好耶 太好了 爽 棒 爱了 可爱 暖和").split()
NEG_WORDS = ("难受 难过 累 烦 崩溃 破防 焦虑 emo 抑郁 想哭 哭了 痛苦 恶心 讨厌 无语 绝望 孤独 "
             "失眠 摆烂 麻了 寄 完蛋 挂了 裂开 心累 内耗 压力 疲惫 委屈 生气 怒 恶心 想死 死了").split()
MEME_SEED = ("鼠 鼠鼠 本鼠 鼠洞 洞 uu 宝宝 家人 姐妹 哥们 大佬 学长 学姐 学弟 学妹 卷 卷王 摆烂 躺平 "
             "破防 绷不住 麻了 寄 润 赢 蹲 置臀 蹲一个 酸了 柠檬 好家伙 离谱 离大谱 逆天 抽象 "
             "属实 属实是 救命 笑死 笑不活 xswl hhh 233 呜呜 啊啊 卧槽 我超 嗯嗯 懂了 学到了 "
             "同问 同求 蹲蹲 求问 有没有 推 安利 避雷 踩雷 真香 破费 烧钱 血亏 赚 白嫖 挂 挂科 "
             "满绩 绩点 保研 考研 四六级 体测 早八 晚课 抢课 选课 转专业 辅修 双学位 军训 军训 "
             "食堂 外卖 快递 校车 通勤 断电 断网 洗澡 热水 空调 图书馆 自习 通宵 熬夜 早睡 "
             "独墅湖 天赐庄 阳澄湖 本部 东区 西区 北区 南区 猫 猫猫 狗 松鼠 银杏 桂花 樱花").split()


def scrub(t: str) -> str:
    """去掉具体人、老师、楼栋、联系方式——这些不该进知识库。"""
    if not t:
        return ""
    t = URL.sub("", t)
    t = AT.sub("", t)
    t = LONGNUM.sub("", t)
    t = BUILDING.sub("某处", t)
    t = TEACHER.sub("某老师", t)
    t = re.sub(r"(微信|qq|QQ|vx|VX|v信)\s*[:：]?\s*\S{0,15}", "", t)
    return re.sub(r"\s+", " ", t).strip()


def load():
    conn = sqlite3.connect(DB)
    posts = [dict(zip(("id", "sub_id", "sub", "topic", "content", "created_at",
                       "like", "comments", "anon", "day"), r))
             for r in conn.execute(
                 "SELECT id,sub_id,sub_name,topic_name,content,created_at,like_count,"
                 "comment_count,anonymous,day FROM posts")]
    cmts = [dict(zip(("id", "post_id", "content", "created_at", "like", "anon", "day", "is_sub"), r))
            for r in conn.execute(
                "SELECT id,post_id,content,created_at,like_count,anonymous,day,is_sub FROM comments")]
    conn.close()
    return posts, cmts


def parse_dt(s: str):
    try:
        return datetime.fromisoformat(s.replace("Z", "+08:00"))
    except Exception:
        return None


def ngrams(texts, ns=(2, 3, 4), top=500):
    """CJK n-gram 词频。用「整段切 CJK 再滑窗」的方式，够抓梗词。"""
    cnt = Counter()
    for t in texts:
        for seg in CJK.findall(t):
            L = len(seg)
            for n in ns:
                for i in range(L - n + 1):
                    g = seg[i:i + n]
                    if g in STOP:
                        continue
                    cnt[g] += 1
    # 去掉被更长高频词完全覆盖的短词（鼠鼠 覆盖 鼠）
    items = [(g, c) for g, c in cnt.items() if c >= 3]
    items.sort(key=lambda x: -x[1])
    keep = []
    for g, c in items:
        if any(g != h and g in h and c <= hc * 1.15 for h, hc in keep):
            continue
        keep.append((g, c))
        if len(keep) >= top:
            break
    return keep


def pct(x, n):
    return round(100.0 * x / max(1, n), 1)


def main() -> None:
    posts, cmts = load()
    OUT.mkdir(exist_ok=True)
    P = [p for p in posts if p["content"]]
    C = [c for c in cmts if c["content"]]
    print(f"载入 帖子 {len(P)} 评论 {len(C)}")

    # ---------- 长度 ----------
    def lens(rows):
        v = sorted(len(r["content"]) for r in rows)
        if not v:
            return {}
        q = lambda p: v[min(len(v) - 1, int(len(v) * p))]
        return {"n": len(v), "mean": round(sum(v) / len(v), 1), "p10": q(.1), "p50": q(.5),
                "p90": q(.9), "max": v[-1]}

    # ---------- 板块画像 ----------
    boards = defaultdict(lambda: {"n": 0, "lens": [], "likes": 0, "cmts": 0, "q": 0, "anon": 0})
    for p in P:
        b = boards[p["sub"]]
        b["n"] += 1
        b["lens"].append(len(p["content"]))
        b["likes"] += p["like"]
        b["cmts"] += p["comments"]
        b["anon"] += p["anon"]
        if "?" in p["content"] or "？" in p["content"]:
            b["q"] += 1
    board_stats = {}
    for k, v in sorted(boards.items(), key=lambda x: -x[1]["n"]):
        L = sorted(v["lens"])
        board_stats[k] = {
            "posts": v["n"], "share": pct(v["n"], len(P)),
            "len_median": L[len(L) // 2] if L else 0,
            "avg_like": round(v["likes"] / v["n"], 1),
            "avg_comment": round(v["cmts"] / v["n"], 1),
            "question_ratio": pct(v["q"], v["n"]),
            "anon_ratio": pct(v["anon"], v["n"]),
        }

    # ---------- 时间节律 ----------
    hour = Counter()
    weekday = Counter()
    month = Counter()
    daycnt = Counter()
    for p in P:
        dt = parse_dt(p["created_at"])
        if not dt:
            continue
        hour[dt.hour] += 1
        weekday[dt.weekday()] += 1
        month[dt.strftime("%Y-%m")] += 1
        daycnt[p["day"]] += 1
    hours = [hour.get(h, 0) for h in range(24)]
    nights = sum(hours[0:6])
    work = sum(hours[8:18])
    eve = sum(hours[18:24])

    # ---------- 表情标记 ----------
    br = Counter()
    for p in P:
        for m in BRACKET.findall(p["content"]):
            br[m] += 1
    for c in C:
        for m in BRACKET.findall(c["content"]):
            br[m] += 1

    # ---------- 标点与语气 ----------
    allp = "\n".join(p["content"] for p in P)
    allc = "\n".join(c["content"] for c in C)
    punct = {
        "省略号……": allp.count("……") + allp.count("..."),
        "波浪号～": allp.count("～") + allp.count("~"),
        "连续感叹!!": len(re.findall(r"[!！]{2,}", allp)),
        "连续问号??": len(re.findall(r"[?？]{2,}", allp)),
        "hhh": len(re.findall(r"[hH]{3,}", allp)),
        "233": allp.count("233"),
        "呜呜": allp.count("呜呜"),
        "啊啊": allp.count("啊啊"),
        "括号补充（": allp.count("（"),
    }
    c_punct = {
        "省略号": allc.count("……") + allc.count("..."),
        "hhh": len(re.findall(r"[hH]{3,}", allc)),
        "hh": len(re.findall(r"[hH]{2}", allc)),
    }

    # ---------- 开场与结尾 ----------
    openers = Counter(p["content"][:2] for p in P if len(p["content"]) > 2)
    enders = Counter(p["content"][-2:] for p in P if len(p["content"]) > 2)
    c_open = Counter(c["content"][:2] for c in C if len(c["content"]) > 2)

    # ---------- 情绪 ----------
    pos = sum(allp.count(w) for w in POS_WORDS)
    neg = sum(allp.count(w) for w in NEG_WORDS)
    meme_hits = {w: allp.count(w) for w in set(MEME_SEED) if allp.count(w) >= 3}

    # ---------- n-gram ----------
    ng_p = ngrams([p["content"] for p in P], top=420)
    ng_c = ngrams([c["content"] for c in C], top=260)

    # ---------- 代表样本 ----------
    def pick(rows, k, by="like"):
        rs = sorted(rows, key=lambda r: -(r[by] or 0))[:k]
        return [scrub(r["content"])[:260] for r in rs if r["content"]]

    samples = {"boards": {}, "top_liked": pick(P, 40), "top_commented": pick(P, 25, "comments")}
    for b in board_stats:
        rows = [p for p in P if p["sub"] == b]
        hot = pick(rows, 12)
        # 均匀抽 8 条覆盖不同月份，避免只有最近
        rows_sorted = sorted(rows, key=lambda r: r["created_at"])
        step = max(1, len(rows_sorted) // 8)
        spread = [scrub(r["content"])[:260] for r in rows_sorted[::step][:8]]
        samples["boards"][b] = {"hot": hot, "spread": spread, "stats": board_stats[b]}
    samples["comments"] = {
        "top": [scrub(c["content"])[:160] for c in sorted(C, key=lambda r: -(r["like"] or 0))[:120]],
        "spread": [scrub(c["content"])[:160] for c in C[::max(1, len(C) // 120)][:120]],
    }

    stats = {
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "corpus": {"posts": len(P), "comments": len(C),
                   "span": [min(p["day"] for p in P if p["day"]), max(p["day"] for p in P if p["day"])]},
        "length": {"post": lens(P), "comment": lens(C)},
        "boards": board_stats,
        "rhythm": {"hour": hours, "weekday": [weekday.get(i, 0) for i in range(7)],
                   "month": dict(sorted(month.items())),
                   "night_share": pct(nights, len(P)), "work_share": pct(work, len(P)),
                   "eve_share": pct(eve, len(P)), "active_days": len(daycnt),
                   "per_day_avg": round(len(P) / max(1, len(daycnt)), 1)},
        "stickers": dict(br.most_common(60)),
        "punct": punct, "comment_punct": c_punct,
        "openers": dict(openers.most_common(25)), "enders": dict(enders.most_common(25)),
        "comment_openers": dict(c_open.most_common(20)),
        "emotion": {"pos_hits": pos, "neg_hits": neg, "ratio": round(pos / max(1, neg), 2),
                    "top_meme_words": dict(sorted(meme_hits.items(), key=lambda x: -x[1])[:60])},
        "ngram_post": ng_p, "ngram_comment": ng_c,
    }

    (OUT / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "samples.json").write_text(json.dumps(samples, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---------- 人读报告 ----------
    def bar(v, mx, w=36):
        return "█" * max(0, round(v / max(1, mx) * w))

    mx = max(hours) or 1
    lines = ["# 鼠洞语料体检", "", f"语料：{len(P)} 帖 / {len(C)} 评论，"
             f"{stats['corpus']['span'][0]} → {stats['corpus']['span'][1]}", "",
             "## 发帖时段", "```"]
    for h in range(24):
        lines.append(f"{h:02d} {hours[h]:>5} {bar(hours[h], mx)}")
    lines += ["```", "", f"夜间(0-6) {pct(nights, len(P))}% · 白天(8-18) {pct(work, len(P))}% "
              f"· 晚上(18-24) {pct(eve, len(P))}%", "", "## 板块", ""]
    for k, v in board_stats.items():
        lines.append(f"- **{k}** {v['posts']} 帖（{v['share']}%）· 中位 {v['len_median']} 字 · "
                     f"均 {v['avg_like']} 赞 / {v['avg_comment']} 评 · 提问 {v['question_ratio']}%")
    lines += ["", "## 表情标记 Top20", "",
              " · ".join(f"[{k}]×{v}" for k, v in br.most_common(20)), "",
              "## 高频词 Top60", "", " · ".join(f"{g}×{c}" for g, c in ng_p[:60]), "",
              "## 评论高频词 Top40", "", " · ".join(f"{g}×{c}" for g, c in ng_c[:40]), "",
              "## 情绪", f"正向词 {pos} 次 / 负向词 {neg} 次 = {round(pos/max(1,neg),2)}"]
    (OUT / "stats_report.md").write_text("\n".join(lines), encoding="utf-8")
    print("已写出 kb/stats.json / samples.json / stats_report.md")
    print(f"时段分布 夜间{pct(nights,len(P))}% 白天{pct(work,len(P))}% 晚上{pct(eve,len(P))}%")
    print("表情 Top10:", br.most_common(10))
    print("词 Top20:", [g for g, _ in ng_p[:20]])


if __name__ == "__main__":
    main()
