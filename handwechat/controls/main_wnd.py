# -*- coding: utf-8 -*-
"""微信主窗口对象：附加、在线判断、可见性。"""

from __future__ import annotations

import ctypes
import time

import uiautomation as uia
from ctypes import wintypes

from handwechat import selectors as sel
from handwechat.errors import AttachError
from handwechat.uia.wrapper import ui_lock, wait_control

SPI_GETSCREENREADER = 0x1046
SW_RESTORE = 9


def screen_reader_enabled() -> bool:
    """读系统"屏幕阅读器"标志（微信 4.1.15.56 物化 mmui 树的前置条件）。"""
    value = wintypes.BOOL()
    ctypes.windll.user32.SystemParametersInfoW(
        SPI_GETSCREENREADER, 0, ctypes.byref(value), 0)
    return bool(value.value)


class MainWnd:
    """附加微信主窗口并校验 mmui 控件树已物化。"""

    def __init__(self, attach_timeout: float = 10.0) -> None:
        self._control: uia.WindowControl
        self.attach(attach_timeout)

    # -- 附加 --------------------------------------------------------------
    def attach(self, timeout: float = 10.0) -> None:
        with ui_lock:
            win = uia.WindowControl(
                ClassName=sel.MAIN_WINDOW_CLASS,
                Name=sel.MAIN_WINDOW_TITLE,
                searchDepth=1,
            )
            if win.Exists(0.5, 0.2) and win.GetChildren():
                self._control = win
                return

            login = uia.WindowControl(ClassName=sel.LOGIN_WINDOW_CLASS, searchDepth=1)
            if login.Exists(0.5, 0.2):
                raise AttachError("微信停在登录窗口，请先完成登录（扫码/确认）")

            win = uia.WindowControl(
                ClassName=sel.MAIN_WINDOW_CLASS,
                Name=sel.MAIN_WINDOW_TITLE,
                searchDepth=1,
            )
            if not win.Exists(timeout, 0.5):
                raise AttachError(
                    "未找到微信主窗口：微信未启动、未登录，或窗口标题不是"
                    f"「{sel.MAIN_WINDOW_TITLE}」"
                )
            # M0 教训：根节点类名恒为 mmui::MainWindow，不能用类名判断物化，
            # 必须确认子树真的出现了（4.1.15.56 需读屏标志 + 冷启动）
            deadline = time.time() + min(timeout, 5.0)
            children: list = []
            while time.time() < deadline:
                children = win.GetChildren()
                if children:
                    break
                time.sleep(0.3)
            if not children:
                raise AttachError(
                    "微信控件树未物化（根节点下没有子控件）。"
                    "原因：启动微信前系统「屏幕阅读器」标志未开启。"
                    "请用 scripts/start_wechat.py 启动微信后重试"
                )
            self._control = win

    @property
    def control(self) -> uia.WindowControl:
        return self._control

    @property
    def hwnd(self) -> int:
        return self._control.NativeWindowHandle

    @property
    def pid(self) -> int:
        return self._control.ProcessId

    # -- 状态 --------------------------------------------------------------
    def is_alive(self) -> bool:
        """窗口仍在且控件树仍有子节点（掉线/退出登录时消失）。"""
        with ui_lock:
            if not self._control.Exists(0.5, 0.2):
                return False
            return bool(self._control.GetChildren())

    def show(self) -> None:
        """把主窗口拉到前台（UI 操作前确保可见）。"""
        with ui_lock:
            ctypes.windll.user32.ShowWindow(self.hwnd, SW_RESTORE)
            self._control.SwitchToThisWindow()

    def recover_tree(self) -> bool:
        """恢复因窗口状态或 UIA 物化抖动而“收起”的控件树。"""

        with ui_lock:
            if not self._control.Exists(0.5, 0.2):
                return False
            if self._control.GetChildren():
                return True
            self.show()
            ctypes.windll.user32.SetForegroundWindow(self.hwnd)
            deadline = time.time() + 2.0
            while time.time() < deadline:
                if self._control.GetChildren():
                    return True
                time.sleep(0.2)
            try:
                self.attach(timeout=2.0)
                return bool(self._control.GetChildren())
            except AttachError:
                return False

    def session_list(self, timeout: float = 3.0):
        """定位会话列表控件，找不到返回 None。"""
        from handwechat.controls.session_list import SessionList

        ctrl = wait_control(
            uia.ListControl,
            self._control,
            timeout,
            20,
            ClassName=sel.SESSION_LIST_CLASS,
            AutomationId=sel.SESSION_LIST_ID,
        )
        return SessionList(ctrl) if ctrl is not None else None

    def chat_box(self, timeout: float = 3.0):
        """定位当前聊天页；空白页/未打开会话返回 None。"""

        from handwechat.controls.chat_box import ChatBox

        ctrl = wait_control(
            uia.GroupControl,
            self._control,
            timeout,
            25,
            ClassName="mmui::ChatMessagePage",
            AutomationId=sel.CHAT_MESSAGE_PAGE_ID,
        )
        return ChatBox(ctrl) if ctrl is not None else None
