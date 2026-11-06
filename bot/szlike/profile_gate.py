"""账号画像门禁：没设昵称/头像的号不许上线。

真人 2026-11-05 12:00 的原话：
  「为啥鼠洞出现了一批疑似人坤的账号呀，都是默认id头像，只在近几天发了两三条帖子」
第一条指纹不是文案，是账号本身。实测 33/33 个自动账号 `viewer.user.profile`
全部为 null（nickname 为空），对外显示的就是默认 ID + 默认头像。

这个必须做成启动门禁而不是配置提醒：只要有一个号还是默认画像，
它在论坛上就会把这批号整体串起来。改画像只能在小程序端人工完成，
代码这边唯一能做的正确事情是「不合格就不发」。
"""
from __future__ import annotations

from . import ops


def check(client) -> tuple[bool, str]:
    """返回 (是否允许上线, 拒绝原因)。"""
    try:
        v = ops.viewer(client)
    except Exception as e:
        return False, f"读不到账号资料，拒绝上线：{e}"
    user = v.get("user") or {}
    prof = user.get("profile")
    nick = (prof or {}).get("nickname") if isinstance(prof, dict) else ""
    if not (nick or "").strip():
        return False, ("未设置昵称/头像（对外显示默认 ID）——真人就是靠这条"
                       "一眼认出是一批号，请先在小程序里补齐账号资料")
    return True, ""
