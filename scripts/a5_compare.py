# -*- coding: utf-8 -*-
"""A5 对照验收：用 wxauto4 校验 handwechat 读到同一当前会话消息。"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from handwechat import WeChat as HandWeChat


def normalize_sender(value: object) -> str:
    return str(value or "").strip().lower()


def handwe_key(msg) -> tuple[str, str, str, str]:
    return (
        str(msg.type),
        str(msg.attr),
        normalize_sender(msg.sender),
        str(msg.content),
    )


def wxauto4_key(msg) -> tuple[str, str, str, str]:
    return (
        str(getattr(msg, "type", "")),
        str(getattr(msg, "attr", "")),
        normalize_sender(getattr(msg, "sender", "")),
        str(getattr(msg, "content", "")),
    )


def find_by_content(msgs, key_fn, content: str):
    return next((msg for msg in msgs if key_fn(msg)[3] == content), None)


def open_target(wx: HandWeChat, target: str) -> None:
    current = wx.CurrentChat()
    if current == target:
        return

    if wx.ChatWith(target, timeout=6.0) is None:
        raise RuntimeError(f"HandWeChat 无法打开目标会话：{target!r}")
    current = wx.CurrentChat()
    if current != target:
        raise RuntimeError(
            f"目标会话校验失败：期望 {target!r}，实际 {current!r}"
        )


def pick_anchor(msgs):
    for msg in reversed(msgs):
        if str(getattr(msg, "type", "")) == "text" and str(getattr(msg, "content", "")):
            return msg
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("target", nargs="?", default="文件传输助手")
    args = parser.parse_args()

    target = args.target
    hw = HandWeChat()
    print(
        f"handwechat attached: pid={hw.pid} hwnd=0x{hw.hwnd:X} "
        f"online={hw.IsOnline()}"
    )
    open_target(hw, target)
    print(f"handwechat current chat: {hw.CurrentChat()!r}")

    import wxauto4

    wx4 = wxauto4.WeChat(ads=False, resize=False)
    info = wx4.ChatInfo()
    print(f"wxauto4 attached: online={wx4.IsOnline()} current_chat={info!r}")
    if info.get("chat_name") != target:
        raise RuntimeError(
            f"wxauto4 当前会话不一致：期望 {target!r}，实际 {info.get('chat_name')!r}"
        )

    wx4_msg = None
    deadline = time.time() + 10.0
    while time.time() < deadline:
        wx4_msgs = wx4.GetAllMessage()
        wx4_msg = pick_anchor(wx4_msgs)
        if wx4_msg is not None:
            break
        time.sleep(0.5)

    if wx4_msg is None:
        raise RuntimeError("wxauto4 没有可用的文本消息锚点")

    anchor_content = str(getattr(wx4_msg, "content", ""))
    hw_msg = None
    deadline = time.time() + 10.0
    while time.time() < deadline:
        hw_msg = find_by_content(hw.GetAllMessage(), handwe_key, anchor_content)
        if hw_msg is not None:
            break
        time.sleep(0.5)
    if hw_msg is None:
        raise RuntimeError("HandWeChat 未读到 wxauto4 的锚点消息")

    hw_key = handwe_key(hw_msg)
    wx4_key = wxauto4_key(wx4_msg)
    print(f"handwechat message: {hw_key!r}")
    print(f"wxauto4 message:    {wx4_key!r}")
    if hw_key != wx4_key:
        raise RuntimeError("两个实现读到的测试消息不一致")

    print("A5 passed: handwechat and wxauto4 read the same message")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
