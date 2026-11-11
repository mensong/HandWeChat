# -*- coding: utf-8 -*-
"""M0 probe: count UIA children of the WeChat main window with a fresh
connection, foreground poke, and retry loop. Exit code 0 = children > 0."""
import ctypes
import sys
import time

import uiautomation as uia

WIN_CLASS = "mmui::MainWindow"
WIN_NAME = "微信"


def main() -> int:
    win = uia.WindowControl(ClassName=WIN_CLASS, Name=WIN_NAME, searchDepth=1)
    if not win.Exists(5, 0.5):
        print("main window not found")
        return 1
    hwnd = win.NativeWindowHandle
    print("root:", win.ClassName, repr(win.Name), "hwnd=0x%X" % hwnd)

    user32 = ctypes.windll.user32
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    OBJID_CLIENT = 0xFFFFFFFC
    user32.SendMessageTimeoutW(hwnd, 0x003D, OBJID_CLIENT, 0, 0x0002, 1000, None)  # WM_GETOBJECT

    deadline = time.time() + 10.0
    attempt = 0
    while True:
        attempt += 1
        try:
            kids = win.GetChildren()
        except Exception as e:
            kids = []
            print("  attempt %d: query error %s" % (attempt, e))
        if kids:
            print("children after %d attempts: %d" % (attempt, len(kids)))
            for c in kids[:10]:
                print("  ", c.ControlTypeName, repr(c.ClassName), repr(c.Name))
            return 0
        if time.time() >= deadline:
            print("children: 0 after %d attempts (10s)" % attempt)
            return 1
        time.sleep(0.5)


if __name__ == "__main__":
    sys.exit(main())
