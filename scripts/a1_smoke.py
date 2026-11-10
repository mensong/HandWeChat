# -*- coding: utf-8 -*-
"""A1 真机冒烟：附加 → IsOnline → GetSession，打印结果。"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from handwechat import WeChat
from handwechat.errors import HWError


def main() -> int:
    print("读屏标志:", WeChat.ScreenReaderEnabled())
    try:
        wx = WeChat()
    except HWError as e:
        print("附加失败:", e)
        return 1
    print(f"附加成功: pid={wx.pid} hwnd=0x{wx.hwnd:X}")
    print("IsOnline:", wx.IsOnline())

    t0 = time.time()
    sessions = wx.GetSession()
    print(f"GetSession: {len(sessions)} 个会话，耗时 {time.time() - t0:.1f}s")
    for s in sessions:
        print(" ", repr(s), f"time={s.time_raw!r}")
        print(f"    preview={s.preview[:40]!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
