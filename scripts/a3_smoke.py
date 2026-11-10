# -*- coding: utf-8 -*-
"""A3 真机冒烟：SendMsg -> 回读自身文本消息。"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from handwechat import WeChat
from handwechat.errors import HWError
from handwechat.models import TextMessage


def main() -> int:
    who = sys.argv[1] if len(sys.argv) > 1 else "文件传输助手"
    stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    text = f"HandWeChat A3 test {stamp}"

    try:
        wx = WeChat()
        print(f"attach pid={wx.pid} hwnd=0x{wx.hwnd:X} online={wx.IsOnline()}")
        if who:
            session = wx.ChatWith(who)
            if session is None:
                print(f"ChatWith failed: {who!r}")
                return 2
            print(f"ChatWith: {session!r}")

        sent = wx.SendMsg(text, who=who)
        print(f"SendMsg verify: {sent!r}")
        if not isinstance(sent, TextMessage):
            print("smoke failed: sent message was not read back")
            return 3

        messages = wx.GetAllMessage()
        print(f"GetAllMessage: {len(messages)} messages")
        for msg in messages[-3:]:
            print(
                f"  {msg.index:02d} {msg.attr:6s}/{msg.type:6s} "
                f"sender={msg.sender!r} content={msg.content[:60]!r}"
            )
        if not any(
            isinstance(msg, TextMessage)
            and msg.attr == "self"
            and msg.content == text
            for msg in messages
        ):
            print("smoke failed: GetAllMessage did not contain the sent text")
            return 4
        return 0
    except HWError as exc:
        print(f"handwechat error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
