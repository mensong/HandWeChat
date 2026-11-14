# -*- coding: utf-8 -*-
"""A2 真机冒烟：ChatWith -> GetAllMessage，打印 sender/type/hash 校验。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from handwechat import WeChat
from handwechat.errors import HWError
from handwechat.models import SessionType, SystemMessage, TextMessage


def main() -> int:
    who = sys.argv[1] if len(sys.argv) > 1 else "群聊"
    my_names = sys.argv[2].split(",") if len(sys.argv) > 2 else []

    try:
        wx = WeChat()
        print(f"attach pid={wx.pid} hwnd=0x{wx.hwnd:X} online={wx.IsOnline()}")
        session = wx.ChatWith(who)
        if session is None:
            print(f"ChatWith failed: {who!r}")
            return 2
        print(f"ChatWith: {session!r}")

        messages = wx.GetAllMessage(my_names=my_names)
        print(f"GetAllMessage: {len(messages)} messages")
        for msg in messages:
            print(
                f"  {msg.index:02d} {msg.attr:6s}/{msg.type:6s} "
                f"sender={msg.sender!r} content={msg.content[:40]!r} "
                f"hash={msg.hash[:10]}"
            )

        human = [m for m in messages if isinstance(m, TextMessage)]
        if not human:
            print("smoke warning: no TextMessage in loaded history")
        if session.type not in (SessionType.FRIEND, SessionType.GROUP):
            print("smoke warning: session type missing")
        if any(
            not m.hash
            for m in messages
            if not isinstance(m, SystemMessage)
        ):
            print("smoke warning: message hash missing")
        return 0
    except HWError as exc:
        print(f"handwechat error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
