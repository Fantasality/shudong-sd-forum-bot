"""暗语指令通道（悄悄话）。

论坛没有私信接口，所以通道做成「暗桩帖 + 楼中楼」：
  1. 每个号在暗桩帖下留一条普通留言（锚点），看起来就是正常互动；
  2. 主机小号回复这条留言 = 下指令，内容全是日常口气；
  3. 号解析暗语 → 执行 → 在同一条楼中楼里回一句「办好了」。

两种信箱模式：
  - 暗桩帖模式（whisper.drop_post_id 有值）：只认那条帖子下的回复；
  - 自由投递（drop_post_id 为空）：认任意一条「我自己评论下面、主人发来的回复」，
    不需要全号去同一条帖子报到，脚印最小。

对外完全是一串正常的聊天回复，没有 bot 字样，也没有接口层面的异常行为。
"""
from __future__ import annotations

import base64
import json
import logging
import random
import re
import time
from datetime import datetime
from pathlib import Path

from . import ops
from .client import SzlikeError

log = logging.getLogger("szlike.whisper")

BASE = Path(__file__).resolve().parent.parent
SEEN_PATH = BASE / "whisper_seen.json"
PAUSE_PATH = BASE / "whisper_pause.json"

Q_INBOX = """
query Q($after: String) {
  viewer { user { userSubcomments(type: RECEIVED, first: 20, after: $after) {
    pageInfo { hasNextPage endCursor }
    edges { node {
      id content createdAt isAnonymous
      replyTo { id }
      creator { id username profile { nickname } }
      comment { id content post { id content } }
    } }
  } } }
}
"""

Q_POST_COMMENTS = """
query Q($pid: ID!) {
  post(id: $pid) { id content }
  comments(postId: $pid, first: 30, order: DEFAULT) {
    edges { node { id createdAt creator { id username } } }
  }
}
"""

# 看起来就是普通同学的一句话，放在任何帖子下面都不违和
ROLL_CALL_POOL = [
    "刚刷到，先码住。",
    "这事得看你自己怎么选。",
    "路过，蹲个后续。",
    "笑死，完全同意。",
    "有道理。",
    "早上在方塔吃的生煎，现在还撑。",
    "今天食堂的番茄炒蛋居然不酸。",
    "刚下课，来冒个泡。",
    "图书馆四楼今天人巨多。",
    "早安，今天也是被闹钟拽起来的一天。",
    "晚饭不知道吃啥，蹲个推荐。",
    "这楼不能空着，我来垫一层。",
    "刚睡醒，先留个脚印。",
    "在钟楼草坪晒太阳，路过留一句。",
    "晚课刚散，脑子已经下班了。",
]

ACK_POOL = {
    "post": ["写好了，发在{board}，你翻翻。", "发了，标题没敢太显眼。"],
    "comment": ["说了，你去看。", "回过了，语气拿捏着呢。"],
    "like": ["点过了。", "给了，顺手。"],
    "pause": ["行，我先歇着。", "收到，这两天不冒头。"],
    "resume": ["来了。", "好，这就出来。"],
    "report": ["在呢，一切正常。（ID:{uid}）", "在的，没掉线。（ID:{uid}）"],
    "rename": ["好，以后就叫这个。", "记住了。"],
    "unknown": ["没听懂，再说一遍？"],
}

# 暗语 → 指令（与 App 端 buildMsg 一一对应）
R_POST = re.compile(r"写(?:条|篇)关于[「\"“](?P<theme>[^」\"”]+)[」\"”]的帖子(?:[，,]?发到(?P<board>[^。！!]+))?")
R_RAID = re.compile(r"去\s*#?(?P<code>[0-9A-Za-z]{2,10})\s*底下叫上\s*(?P<n>\d+)?\s*个?人?说说话")
R_COMMENT = re.compile(r"去\s*#?(?P<code>[0-9A-Za-z]{2,10})\s*底下说句[「\"“](?P<content>[^」\"”]+)[」\"”]")
R_LIKE = re.compile(r"给\s*#?(?P<code>[0-9A-Za-z]{2,10})\s*捧个场")
R_PAUSE = re.compile(r"先歇会儿|歇两天|别冒头")
R_RESUME = re.compile(r"出来干活|出来吧")
R_REPORT = re.compile(r"报个到|报个平安")
R_RENAME = re.compile(r"以后叫你[「\"“](?P<name>[^」\"”]+)[」\"”]")


def code_to_id(code: str, kind: str = "Post") -> str:
    """App 里的帖代号（base36）还原成 GraphQL 全局 ID。"""
    code = str(code or "").strip().lstrip("#").upper()
    if not re.fullmatch(r"[0-9A-Z]{1,10}", code):
        return ""
    n = int(code, 36)
    if n <= 0:
        return ""
    return base64.b64encode(f"{kind}:{n}".encode()).decode().rstrip("=")


def parse(text: str) -> dict | None:
    t = (text or "").strip()
    if not t:
        return None
    m = R_POST.search(t)
    if m:
        return {"action": "post", "theme": m.group("theme").strip(),
                "board": (m.group("board") or "").strip()}
    m = R_RAID.search(t)
    if m:
        return {"action": "raid", "code": m.group("code").upper(),
                "count": int(m.group("n") or 0)}
    m = R_COMMENT.search(t)
    if m:
        return {"action": "comment", "code": m.group("code").upper(),
                "content": m.group("content").strip()}
    m = R_LIKE.search(t)
    if m:
        return {"action": "like", "code": m.group("code").upper()}
    m = R_RENAME.search(t)
    if m:
        return {"action": "rename", "name": m.group("name").strip()}
    if R_PAUSE.search(t):
        return {"action": "pause"}
    if R_RESUME.search(t):
        return {"action": "resume"}
    if R_REPORT.search(t):
        return {"action": "report"}
    return None


# ---------- 状态 ----------

def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def is_paused(account: str) -> bool:
    p = _load(PAUSE_PATH).get(account)
    if not p:
        return False
    until = p.get("until", 0)
    if until and time.time() > until:
        data = _load(PAUSE_PATH)
        data.pop(account, None)
        _save(PAUSE_PATH, data)
        return False
    return True


def _pause(account: str, minutes: int = 720) -> None:
    data = _load(PAUSE_PATH)
    data[account] = {"until": time.time() + minutes * 60,
                     "at": datetime.now().isoformat(timespec="seconds")}
    _save(PAUSE_PATH, data)


def _resume(account: str) -> None:
    data = _load(PAUSE_PATH)
    data.pop(account, None)
    _save(PAUSE_PATH, data)


def _seen(account: str) -> set[str]:
    return set(_load(SEEN_PATH).get(account) or [])


def _mark_seen(account: str, sid: str) -> None:
    data = _load(SEEN_PATH)
    ids = data.get(account) or []
    if sid not in ids:
        ids.append(sid)
    data[account] = ids[-400:]
    _save(SEEN_PATH, data)


# ---------- 主体 ----------

class Whisper:
    def __init__(self, ctx):
        self.ctx = ctx
        self.client = ctx.client
        cfg = ctx.cfg.get("whisper") or {}
        self.cfg = cfg
        self.drop = (cfg.get("drop_post_id") or "").strip()
        masters = set(cfg.get("master_user_ids") or [])
        if ctx.me:
            masters.add(ctx.me)
        self.masters = {m for m in masters if m}
        self.account = ctx.account
        self.now = datetime.now().strftime("%m-%d %H:%M")

    # ----- 锚点 -----
    def my_anchor(self) -> str:
        if not self.drop:
            return ""
        data = self.client.gql(Q_POST_COMMENTS, {"pid": self.drop}, "Q")
        me = self.ctx.me
        for e in (data.get("comments", {}).get("edges") or []):
            n = e["node"]
            if (n.get("creator") or {}).get("id") == me:
                return n["id"]
        return ""

    def roll_call(self, force: bool = False) -> str:
        """在暗桩帖下留一条普通留言当信箱；已有就返回它的 id。"""
        if not self.drop:
            log.warning("[%s] 没配暗桩帖，跳过报到", self.account)
            return ""
        anchor = self.my_anchor()
        if anchor and not force:
            return anchor
        text = random.choice(ROLL_CALL_POOL)
        try:
            node = ops.create_comment(self.client, self.drop, text, is_anonymous=False)
            log.info("[%s] 报到完成 %s", self.account, node.get("id"))
            return node.get("id", "")
        except SzlikeError as e:
            log.warning("[%s] 报到失败 %s", self.account, e)
            return ""

    # ----- 收件箱 -----
    def inbox(self) -> list[dict]:
        try:
            data = self.client.gql(Q_INBOX, {}, "Q")
        except SzlikeError as e:
            log.warning("[%s] 收件箱读取失败 %s", self.account, e)
            return []
        nodes = []
        user = ((data.get("viewer") or {}).get("user") or {})
        for e in ((user.get("userSubcomments") or {}).get("edges") or []):
            nodes.append(e["node"])
        return nodes

    def poll(self) -> list[dict]:
        if not (self.ctx.cfg.get("whisper") or {}).get("enabled", True):
            return []                     # 暗语通道已停用（代码保留）
        if not self.drop:
            return []
        seen = _seen(self.account)
        done = []
        for node in self.inbox():
            if node["id"] in seen:
                continue
            post = (((node.get("comment") or {}).get("post")) or {})
            if self.drop and post.get("id") != self.drop:
                continue
            creator = (node.get("creator") or {})
            if creator.get("id") not in self.masters:
                continue
            cmd = parse(node.get("content", ""))
            _mark_seen(self.account, node["id"])
            if not cmd:
                log.info("[%s] 暗语解析不了：%s", self.account, node.get("content", "")[:40])
                continue
            log.info("[%s] 收到指令 %s", self.account, cmd)
            ack = self.execute(cmd, node)
            if ack:
                self.reply(node, ack)
            done.append({"cmd": cmd, "ack": ack, "whisper": node.get("content")})
            time.sleep(random.uniform(2, 6))
        return done

    # ----- 执行 -----
    def execute(self, cmd: dict, node: dict) -> str:
        act = cmd["action"]
        try:
            if act == "post":
                return self._do_post(cmd)
            if act == "raid":
                from . import sentinel
                pid = code_to_id(cmd["code"])
                if not pid:
                    return "代号没看懂。"
                n = cmd.get("count") or (self.ctx.cfg.get("sentinel") or {}).get("squad", 4)
                sent = sentinel.dispatch(self.ctx.cfg, pid, int(n), exclude={self.account})
                return f"叫了 {len(sent)} 个人过去了。" if sent else "没叫动人，等等再试。"
            if act == "comment":
                pid = code_to_id(cmd["code"])
                if not pid:
                    return "那个代号我没看懂。"
                ops.create_comment(self.client, pid, cmd["content"], is_anonymous=False)
                return random.choice(ACK_POOL["comment"])
            if act == "like":
                pid = code_to_id(cmd["code"])
                if not pid:
                    return "代号不对。"
                ops.add_like(self.client, pid)
                return random.choice(ACK_POOL["like"])
            if act == "pause":
                _pause(self.account)
                return random.choice(ACK_POOL["pause"])
            if act == "resume":
                _resume(self.account)
                return random.choice(ACK_POOL["resume"])
            if act == "report":
                return random.choice(ACK_POOL["report"]).format(uid=self.ctx.me)
            if act == "rename":
                data = _load(BASE / "whisper_names.json")
                data[self.account] = cmd["name"]
                _save(BASE / "whisper_names.json", data)
                return random.choice(ACK_POOL["rename"]) + "（我叫" + cmd["name"] + "）"
        except SzlikeError as e:
            log.warning("[%s] 指令执行失败 %s", self.account, e)
            return "卡住了，等会儿再试。"
        return random.choice(ACK_POOL["unknown"])

    def _do_post(self, cmd: dict) -> str:
        theme = cmd.get("theme") or "今天的校园日常"
        topic_id, subtopic_id, board_name = self._board(cmd.get("board"))
        content = ""
        try:
            system = ("你是苏州大学的学生，在校园论坛发一条日常帖子。第一人称，口语，"
                      "60-120 字，不要标题，不要 emoji 堆砌，不要总结句。")
            user = f"主题：{theme}\n板块：{board_name}\n身份：{self.ctx.profile or '苏大学生'}"
            content = (self.ctx.brain.chat(system, user, max_tokens=400) or "").strip()
        except Exception as e:      # LLM 挂了也不能不发
            log.warning("[%s] 生成正文失败 %s", self.account, e)
        if not content:
            content = theme
        ops.create_post(self.client, content, self.ctx.school_id, topic_id, subtopic_id,
                        is_anonymous=False)
        return random.choice(ACK_POOL["post"]).format(board=board_name)

    def _board(self, board: str) -> tuple[str, str | None, str]:
        """把暗语里的板块名映射成 topic / subtopic id。"""
        topics = self.ctx.cfg.get("topics") or {}
        board = (board or "").strip()
        if board:
            for tname, t in topics.items():
                if board == tname:
                    return t["id"], None, tname
                for sname, sid in (t.get("subtopics") or {}).items():
                    if board == sname or board == tname + "·" + sname:
                        return t["id"], sid, sname
        tid = self.ctx.cfg.get("default_topic_id")
        sid = self.ctx.cfg.get("default_subtopic_id")
        name = "校园生活"
        for tname, t in topics.items():
            if t.get("id") == tid:
                name = tname
                for sname, sv in (t.get("subtopics") or {}).items():
                    if sv == sid:
                        name = sname
        return tid, sid, name

    def reply(self, node: dict, text: str) -> None:
        comment_id = (node.get("comment") or {}).get("id")
        if not comment_id:
            return
        try:
            ops.create_subcomment(self.client, comment_id, text, reply_to_id=node["id"])
            log.info("[%s] 已回执 %s", self.account, text[:24])
        except SzlikeError as e:
            log.warning("[%s] 回执失败 %s", self.account, e)


def run(ctx, rollcall: bool = False) -> list[dict]:
    if not (ctx.cfg.get("whisper") or {}).get("enabled", True):
        return []
    w = Whisper(ctx)
    if rollcall:
        w.roll_call()
    return w.poll()
