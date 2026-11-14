# -*- coding: utf-8 -*-
"""A4 真机冒烟：AddListenChat -> Start -> SendMsg -> 回调收到新消息。"""

from __future__ import annotations

import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from handwechat import MessageListener, TextMessage, WeChat
from handwechat.errors import HWError


def main() -> int:
    who = sys.argv[1] if len(sys.argv) > 1 else "文件传输助手"
    my_names = sys.argv[2].split(",") if len(sys.argv) > 2 else []
    stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    text = f"HandWeChat A4 listen test {stamp}"
    state_path = Path(__file__).resolve().parent.parent / "data" / "a4_smoke_state.json"
    received: list[tuple[str, TextMessage]] = []
    received_event = threading.Event()
    listener: MessageListener | None = None

    def on_message(chat_name: str, msg) -> None:
        print(
            f"callback: {chat_name!r} seq={msg.extra.get('listen_seq')} "
            f"sender={msg.sender!r} content={msg.content[:80]!r}"
        )
        if isinstance(msg, TextMessage) and msg.content == text:
            received.append((chat_name, msg))
            received_event.set()

    def on_error(context: str, exc: Exception) -> None:
        print(f"listener error: {context}: {exc!r}")

    try:
        wx = WeChat()
        print(f"attach pid={wx.pid} hwnd=0x{wx.hwnd:X} online={wx.IsOnline()}")

        listener = MessageListener(
            wx,
            poll_interval=1.0,
            my_names=my_names,
            state_path=state_path,
            on_message=on_message,
            on_error=on_error,
        )
        session = listener.AddListenChat(who)
        print(f"AddListenChat: {session!r}")
        print(f"listening: {listener.GetAllListenChat()}")

        listener.Start()
        time.sleep(1.2)
        sent = wx.SendMsg(text, who=who)
        print(f"SendMsg verify: {sent!r}")
        if not isinstance(sent, TextMessage):
            print("smoke failed: SendMsg could not verify its own text")
            return 3

        if not received_event.wait(15.0):
            print("smoke failed: listener did not receive the new message")
            return 4

        chat_name, msg = received[0]
        if chat_name != who or msg.extra.get("listen_seq") is None:
            print("smoke failed: callback metadata was invalid")
            return 5

        print("smoke passed: new message emitted once through callback")
        return 0
    except HWError as exc:
        print(f"handwechat error: {exc}")
        return 1
    finally:
        try:
            if listener:
                listener.Stop()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
