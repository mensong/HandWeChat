# -*- coding: utf-8 -*-
"""微信 UIA 关键控件自检。"""

from __future__ import annotations

from dataclasses import dataclass, field

import uiautomation as uia

from handwechat import selectors as sel
from handwechat.uia.wrapper import ui_lock, wait_control
from handwechat.version_info import get_process_version


@dataclass
class SelfCheckItem:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class SelfCheckResult:
    ok: bool = True
    items: list[SelfCheckItem] = field(default_factory=list)
    wechat_version: str = ""
    process_path: str = ""

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.items.append(SelfCheckItem(name=name, ok=ok, detail=detail))
        if not ok:
            self.ok = False

    def failed_texts(self) -> list[str]:
        return [
            f"{item.name}: {item.detail}"
            for item in self.items
            if not item.ok
        ]


def run_self_check(wx) -> SelfCheckResult:
    """不切换会话，只检查主窗口、会话列表和可选的当前聊天区控件。"""

    result = SelfCheckResult()
    result.wechat_version = get_process_version(wx.pid)
    result.process_path = _process_path(wx.pid)

    if not wx.IsOnline():
        result.add("主窗口", False, "窗口不可用或控件树未物化")
        return result
    result.add("主窗口", True, f"PID={wx.pid} HWND={wx.hwnd:#x}")

    session_list = None
    with ui_lock:
        session_list = wx._wnd.session_list(1.0)
    if session_list is None:
        result.add("会话列表", False, f"{sel.SESSION_LIST_CLASS}/{sel.SESSION_LIST_ID}")
    else:
        result.add("会话列表", True, "定位成功")

    with ui_lock:
        tabbar = wait_control(
            uia.ToolBarControl,
            wx._wnd.control,
            1.0,
            20,
            ClassName=sel.MAIN_TABBAR_CLASS,
        )
    if tabbar is None:
        result.add("导航栏", False, sel.MAIN_TABBAR_CLASS)
    else:
        result.add("导航栏", True, "定位成功")

    chat_box = None
    with ui_lock:
        chat_box = wx._wnd.chat_box(0.5)
    if chat_box is None:
        result.add("当前聊天区", True, "未打开会话，跳过")
    else:
        try:
            with ui_lock:
                name = chat_box.current_chat_name()
                _ = chat_box.message_list
            if name:
                result.add("当前聊天区", True, f"{name}")
            else:
                result.add("当前聊天区", False, "标题名称为空")
        except Exception as exc:
            result.add("当前聊天区", False, f"{type(exc).__name__}: {exc}")

    return result


def _process_path(pid: int) -> str:
    from handwechat.version_info import get_process_path

    return get_process_path(pid)
