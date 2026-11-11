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
from handwechat.version_info import get_process_ids_by_image_name

SPI_GETSCREENREADER = 0x1046
SW_RESTORE = 9
MONITOR_DEFAULTTONEAREST = 2
AUTOMATION_WINDOW_WIDTH = 1280
AUTOMATION_WINDOW_HEIGHT = 800
MIN_AUTOMATION_WINDOW_WIDTH = 1024
MIN_AUTOMATION_WINDOW_HEIGHT = 640


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


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
        self._automation_size: tuple[int, int] = (
            AUTOMATION_WINDOW_WIDTH,
            AUTOMATION_WINDOW_HEIGHT,
        )
        self.attach(attach_timeout)

    # -- 附加 --------------------------------------------------------------
    def attach(self, timeout: float = 10.0) -> None:
        with ui_lock:
            any_main = uia.WindowControl(
                ClassName=sel.MAIN_WINDOW_CLASS,
                searchDepth=1,
            )
            if any_main.Exists(0.5, 0.2):
                self._set_attached_control(any_main)
                if any_main.GetChildren():
                    return

                self.show()
                deadline = time.time() + min(timeout, 5.0)
                while time.time() < deadline:
                    if any_main.GetChildren():
                        self._ensure_window_size()
                        return
                    time.sleep(0.3)

                raise AttachError(
                    "微信主窗口存在，但控件树未物化。"
                    + self._diagnostic(
                        main_found=True,
                        main_title=any_main.Name,
                        main_child_count=0,
                    )
                )

            win = uia.WindowControl(
                ClassName=sel.MAIN_WINDOW_CLASS,
                Name=sel.MAIN_WINDOW_TITLE,
                searchDepth=1,
            )
            if win.Exists(0.5, 0.2) and win.GetChildren():
                self._set_attached_control(win)
                return

            login = uia.WindowControl(ClassName=sel.LOGIN_WINDOW_CLASS, searchDepth=1)
            if login.Exists(0.5, 0.2):
                raise AttachError(
                    "微信停在登录窗口，请先完成登录（扫码/确认）。"
                    + self._diagnostic(login_found=True)
                )

            win = uia.WindowControl(
                ClassName=sel.MAIN_WINDOW_CLASS,
                Name=sel.MAIN_WINDOW_TITLE,
                searchDepth=1,
            )
            if not win.Exists(timeout, 0.5):
                raise AttachError(
                    "未找到微信主窗口：微信未启动、未登录，或窗口标题不是"
                    f"「{sel.MAIN_WINDOW_TITLE}」"
                    + self._diagnostic()
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
                    + self._diagnostic(
                        main_found=True,
                        main_title=win.Name,
                        main_child_count=0,
                    )
                )
            self._set_attached_control(win)

    def _set_attached_control(self, control: uia.WindowControl) -> None:
        self._control = control
        self._automation_size = self._capture_automation_size()
        self._ensure_window_size()

    def _monitor_work_area(self, hwnd: int):
        monitor = ctypes.windll.user32.MonitorFromWindow(
            ctypes.c_void_p(hwnd),
            MONITOR_DEFAULTTONEAREST,
        )
        if not monitor:
            return None
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
        return info.rcWork

    def _capture_automation_size(self) -> tuple[int, int]:
        """附加时记录一次窗口尺寸；最小化/最大化状态先恢复成普通窗口。"""

        hwnd = self.hwnd
        if not hwnd:
            return AUTOMATION_WINDOW_WIDTH, AUTOMATION_WINDOW_HEIGHT

        user32 = ctypes.windll.user32
        if user32.IsIconic(hwnd) or user32.IsZoomed(hwnd):
            user32.ShowWindow(ctypes.c_void_p(hwnd), SW_RESTORE)
            time.sleep(0.1)

        rect = uia.GetWindowRect(hwnd)
        if rect is None:
            return AUTOMATION_WINDOW_WIDTH, AUTOMATION_WINDOW_HEIGHT

        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width <= 0 or height <= 0:
            return AUTOMATION_WINDOW_WIDTH, AUTOMATION_WINDOW_HEIGHT

        return (
            max(MIN_AUTOMATION_WINDOW_WIDTH, width),
            max(MIN_AUTOMATION_WINDOW_HEIGHT, height),
        )

    def _ensure_window_size(self) -> bool:
        """把主窗口锁定到附加时记录的自动化尺寸，并避免越过工作区。"""

        hwnd = self.hwnd
        if not hwnd:
            return False

        user32 = ctypes.windll.user32
        if user32.IsIconic(hwnd) or user32.IsZoomed(hwnd):
            user32.ShowWindow(ctypes.c_void_p(hwnd), SW_RESTORE)

        rect = uia.GetWindowRect(hwnd)
        if rect is None:
            return False

        captured_width, captured_height = self._automation_size

        work = self._monitor_work_area(hwnd)
        if work is not None:
            max_width = work.right - work.left
            max_height = work.bottom - work.top
        else:
            max_width = captured_width
            max_height = captured_height

        target_width = min(self._automation_size[0], max_width)
        target_height = min(self._automation_size[1], max_height)
        if work is not None:
            x = min(max(rect.left, work.left), max(work.left, work.right - target_width))
            y = min(max(rect.top, work.top), max(work.top, work.bottom - target_height))
        else:
            x = rect.left
            y = rect.top

        if (
            rect.right - rect.left == target_width
            and rect.bottom - rect.top == target_height
            and rect.left == x
            and rect.top == y
        ):
            return True

        return bool(
            uia.SetWindowPos(
                hwnd,
                uia.SWP.HWND_Top,
                x,
                y,
                target_width,
                target_height,
                uia.SWP.SWP_NoZOrder
                | uia.SWP.SWP_NoActivate
                | uia.SWP.SWP_NoOwnerZOrder,
            )
        )

    def _diagnostic(
        self,
        *,
        main_found: bool = False,
        main_title: str = "",
        main_child_count: int = 0,
        login_found: bool = False,
    ) -> str:
        process_count = len(get_process_ids_by_image_name("Weixin.exe"))
        return (
            "｜诊断："
            f"微信进程数={process_count}"
            f"，主窗口={'找到' if main_found else '未找到'}"
            f"，主窗口标题={main_title or '空'}"
            f"，主窗口子控件={main_child_count}"
            f"，登录窗口={'找到' if login_found else '未找到'}"
            f"，读屏标志={'开启' if screen_reader_enabled() else '关闭'}"
            "｜不会自动拉起新微信。"
        )

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
            self._ensure_window_size()
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
