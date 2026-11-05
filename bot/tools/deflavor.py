"""一次性：把 32 个号的人设副本里「共鸣式 / 画面式」指令批量换成「判断 + 建议」式。

背景：2026-11-04 用户定性——情感/恋爱类内容要直接给分析和建议，
「吃烤肠式」自我表演画面（关东煮/路灯/手指/停在动作上）是 AI 味重灾区。
config.json 里 32 个号的 prompts 副本仍写着「不给建议」「说一句自己也这样」
「只写画面」「小动作」「停在动作上」，跟主链路新方向直接打架。

做法：整行映射替换（不是正则），先 dry-run 打印每处将发生的替换，确认后 --go 执行。
config.json 会先备份。persona.py / voices.py 的生成源同步替换，防止重新生成账号时回退。
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
CFG = BASE / "config.json"

# 原行（去掉行首 "- " 前缀差异后整体匹配）→ 新行
REPL: list[tuple[str, str]] = [
    # ---------- 发帖侧：画面/镜头 → 事实 ----------
    ("- 至少有一个具体细节（声音、光线、气味、动作）",
     "- 把事情本身说清楚：几点、跟谁、结果怎样；没有的不编"),
    ("- 只写你看到的一个画面或一个动作，不解释它意味着什么，不写感受",
     "- 只写事情本身，一两句话把谁、在哪、结果说清"),
    ("- 不总结、不抒情、不给建议、不提问",
     "- 不总结、不抒情、不提问"),
    ("- 写夜里（或者天快亮时）的一件小事，带上钟点或光线、声音",
     "- 写夜里（或者天快亮时）发生的一件事，说清几点、在干什么"),
    ("- 不喊孤单、不抒情、不给建议，结尾不要总结",
     "- 不喊孤单、不抒情，结尾不要总结"),
    ("- 不总结方法论，不给建议，结尾就停在做完或没做完那一刻",
     "- 不总结方法论，结尾就停在做完或没做完那一刻"),
    ("- 写一个自己也知道不该做、但还是做了的小动作，具体到时间或次数",
     "- 写一件自己也知道不该做、但还是做了的事，说清什么时候、做了几次"),
    ("- 不写「放不下」「卑微」这类总结词，结尾停在动作上",
     "- 不写「放不下」「卑微」这类总结词，说完这件事就停"),
    ("- 写一个和他有关的极小的事，必须具体到他今天穿了什么、坐哪、说了哪句",
     "- 写一件和他有关的事，说清他做了什么、说了哪句、你当时什么处境"),
    ("- 不表白、不分析他喜不喜欢我，只写自己那点小动作",
     "- 不表白、不分析他喜不喜欢我，写你自己打算怎么办"),
    ("你是苏州大学的学生，爱拍照，习惯用光线、时间、参数描述事情。你在校园社区发一条日常帖子。",
     "你是苏州大学的学生，爱拍照，习惯说清时间、地点和器材。你在校园社区发一条日常帖子。"),
    ("- 写一次拍摄，带具体的光线、时间或器材细节",
     "- 写一次拍摄，说清时间、地点、拍了什么"),
    ("- 结尾停在按下快门或收起相机那一刻",
     "- 写完这次拍的什么、结果怎样就停，不总结"),
    ("你是苏州大学的学生，看电影看书看展，习惯记住具体的画面而不是感受。你在校园社区发一条日常帖子。",
     "你是苏州大学的学生，看电影看书看展，习惯记住具体的作品细节。你在校园社区发一条日常帖子。"),
    ("- 只写一个画面、一个物件或一句听到的话，不解释它代表什么",
     "- 只写一件事：看到了什么作品、哪一句打动你，不解释它代表什么"),
    # ---------- 评论侧：共鸣 → 判断/建议 ----------
    ("要求：一句话；接住对方的情绪，不给建议、不讲道理；不喊「同学」「姐妹」，不复读标题，不暴露 AI。只输出评论本身。",
     "要求：一句话；先接住对方卡住的那一点，能给一句能做的就给；不喊「同学」「姐妹」，不复读标题，不暴露 AI。只输出评论本身。"),
    ("要求：要么接住对方的梗补半句，要么用一个具体类比吐槽；不解释、不刷「哈哈哈」、不复读标题、不暴露 AI。只输出评论本身。",
     "要求：要么接住对方的梗补半句，要么点破这事最不合理的地方；不解释、不刷「哈哈哈」、不复读标题、不暴露 AI。只输出评论本身。"),
    ("要求：说一句自己也这样，带一点自嘲；不客套、不复读标题、不暴露 AI。只输出评论本身。",
     "要求：给一句你的判断或看法，带一点自嘲；不客套、不复读标题、不暴露 AI。只输出评论本身。"),
    ("要求：问一句光线或器材，或者夸一个具体细节；不客套、不复读标题、不暴露 AI。只输出评论本身。",
     "要求：问一句跟拍照有关的事，或者说一句实在的看法；不客套、不复读标题、不暴露 AI。只输出评论本身。"),
    ("要求：接一句自己的同款小心思，别劝人；不客套、不复读标题、不暴露 AI。只输出评论本身。",
     "要求：接一句你的看法，能给一句能做的就给；不客套、不复读标题、不暴露 AI。只输出评论本身。"),
    ("你是苏州大学的学生，爱拍照，习惯用光线、时间、参数描述事情，正在刷校园社区。留一句评论。",
     "你是苏州大学的学生，爱拍照，习惯说清时间、地点和器材，正在刷校园社区。留一句评论。"),
    ("你是苏州大学的学生，看电影看书看展，习惯记住具体的画面而不是感受，正在刷校园社区。留一句评论。",
     "你是苏州大学的学生，看电影看书看展，习惯记住具体的作品细节，正在刷校园社区。留一句评论。"),
    ("要求：提一个具体的作品细节或画面，不夸人；不客套、不复读标题、不暴露 AI。只输出评论本身。",
     "要求：提一个具体的作品细节，或说一句你的看法；不夸人、不客套、不复读标题、不暴露 AI。只输出评论本身。"),
    ("要求：用自嘲接一句，承认自己也这样；不客套、不复读标题、不暴露 AI。只输出评论本身。",
     "要求：用自嘲接一句，落到你能给的说法上；不客套、不复读标题、不暴露 AI。只输出评论本身。"),
    ("要求：说一句你也在，或者一个小事实；不客套、不复读标题、不暴露 AI。只输出评论本身。",
     "要求：说一句你的看法，或者一个小事实；不客套、不复读标题、不暴露 AI。只输出评论本身。"),
    # ---------- 全局兜底 ----------
    ("负面就只表达共情，不要给建议",
     "负面先接住情绪，再给一句能做的"),
]


def fix_system(s: str, log_list: list[str], where: str) -> str:
    out_lines = []
    for ln in s.split("\n"):
        new = ln
        for old, repl in REPL:
            if ln.strip() == old.strip():
                new = ln.replace(old.strip(), repl.strip())
                log_list.append(f"  {where}: {old.strip()[:44]}… -> {repl.strip()[:44]}…")
                break
        out_lines.append(new)
    return "\n".join(out_lines)


def main() -> None:
    go = "--go" in sys.argv
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    changes: list[str] = []

    # 顶层全局 prompts（若存在）
    for pk, pv in (cfg.get("prompts") or {}).items():
        if isinstance(pv, dict) and "system" in pv:
            pv["system"] = fix_system(pv["system"], changes, f"global.{pk}")

    # 32 个账号副本
    n_acc = 0
    for k, a in (cfg.get("accounts") or {}).items():
        if not isinstance(a, dict):
            continue
        for pk, pv in (a.get("prompts") or {}).items():
            if isinstance(pv, dict) and "system" in pv:
                before = pv["system"]
                pv["system"] = fix_system(before, changes, f"{k}.{pk}")
                if pv["system"] != before:
                    n_acc += 1

    print(f"共 {len(changes)} 处替换（覆盖 {n_acc} 个 prompt）")
    for c in changes:
        print(c)
    if not go:
        print("\n[dry-run] 确认无误后执行：python deflavor.py --go")
        return

    bak = CFG.with_name(f"config.json.bak_deflavor_{time.strftime('%H%M%S')}")
    shutil.copy2(CFG, bak)
    CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写入 config.json（备份：{bak.name}）")

    # persona.py / voices.py 生成源同步（防止 manage.py 重新生成账号时回退）
    for name in ("szlike/persona.py", "szlike/voices.py"):
        p = BASE / name
        txt = p.read_text(encoding="utf-8")
        n = 0
        for old, repl in REPL:
            o, r = old.strip(), repl.strip()
            if o in txt:
                n += txt.count(o)
                txt = txt.replace(o, r)
        if n:
            p.with_suffix(".py.bak_deflavor").write_text(
                p.read_text(encoding="utf-8"), encoding="utf-8")
            p.write_text(txt, encoding="utf-8")
            print(f"{name}: 同步替换 {n} 处（已备份 .py.bak_deflavor）")


if __name__ == "__main__":
    main()
