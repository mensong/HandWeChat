# -*- coding: utf-8 -*-
"""WeChat main-window screenshot capture shared by the GUI and HTTP API."""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from dataclasses import dataclass

from PIL import Image, ImageGrab


class WindowCoveredError(RuntimeError):
    """The WeChat window could not be raised above overlapping windows."""


_GWL_EXSTYLE = -20
_WS_EX_TOPMOST = 0x00000008
_GA_ROOT = 2
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_SHOWWINDOW = 0x0040
_RAISE_ATTEMPTS = 5
_RAISE_DELAY = 0.12


@dataclass(frozen=True)
class WeChatCapture:
    """One calibration screenshot plus the window state it was taken with."""

    image: Image.Image
    window_width: int
    window_height: int
    maximized: bool


def _user32():
    # 不声明 argtypes 时 ctypes 会把 HWND_TOPMOST(-1) 当 32 位 int 传入，
    # SetWindowPos 返回 0（错误 1400），置顶实际从未生效。
    user32 = ctypes.windll.user32
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = ctypes.c_long
    return user32


def _set_window_topmost(user32, hwnd: int, topmost: bool) -> bool:
    return bool(
        user32.SetWindowPos(
            wintypes.HWND(hwnd),
            wintypes.HWND(-1 if topmost else -2),  # HWND_TOPMOST / HWND_NOTOPMOST
            0,
            0,
            0,
            0,
            _SWP_NOSIZE | _SWP_NOMOVE | _SWP_SHOWWINDOW,
        )
    )


def _flush_dwm() -> None:
    try:
        ctypes.windll.dwmapi.DwmFlush()
    except Exception:
        pass


def _window_point_owner(user32, hwnd: int, rect) -> bool:
    """Whether the top-level window under the window center is our window."""

    point = wintypes.POINT(
        (rect.left + rect.right) // 2,
        (rect.top + rect.bottom) // 2,
    )
    under = user32.WindowFromPoint(point)
    if not under:
        return False
    return int(user32.GetAncestor(under, _GA_ROOT) or 0) == int(hwnd)


def _window_is_front(user32, hwnd: int) -> bool:
    rect = wintypes.RECT()
    if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        return False
    return _window_point_owner(user32, hwnd, rect)


def _raise_window(user32, hwnd: int) -> None:
    """Raise WeChat above overlapping windows and verify it really is on top."""

    for _attempt in range(_RAISE_ATTEMPTS):
        _set_window_topmost(user32, hwnd, True)
        user32.SetForegroundWindow(wintypes.HWND(hwnd))
        _flush_dwm()
        time.sleep(_RAISE_DELAY)
        if _window_is_front(user32, hwnd):
            return
    if not user32.IsWindow(wintypes.HWND(hwnd)):
        raise RuntimeError("微信窗口已关闭，请确认微信仍在运行")
    raise WindowCoveredError(
        "微信窗口被其他窗口遮挡，请先移开遮挡窗口后重新截图"
    )


def capture_wechat_screenshot() -> WeChatCapture:
    """Bring WeChat forward and capture it together with its window state."""

    from handwechat.ocr_channel import _find_wechat_window

    user32 = _user32()

    located = _find_wechat_window()
    if located is None:
        raise RuntimeError("未找到微信主窗口，请确认微信已启动并登录")
    hwnd, _rect = located
    was_topmost = bool(user32.GetWindowLongW(hwnd, _GWL_EXSTYLE) & _WS_EX_TOPMOST)
    if user32.IsIconic(hwnd):
        # Only a minimized window needs restoring: SW_RESTORE would also
        # un-maximize a maximized window and change the calibrated layout.
        user32.ShowWindow(hwnd, 9)
        time.sleep(0.15)

    try:
        _raise_window(user32, hwnd)

        located = _find_wechat_window()
        if located is None:
            raise RuntimeError("截图前未找到微信主窗口")
        _hwnd, rect = located
        image = ImageGrab.grab(bbox=rect, all_screens=True)
        if image.width <= 1 or image.height <= 1:
            raise RuntimeError("微信窗口截图尺寸无效")
        return WeChatCapture(
            image=image,
            window_width=int(rect[2] - rect[0]),
            window_height=int(rect[3] - rect[1]),
            maximized=bool(user32.IsZoomed(hwnd)),
        )
    finally:
        if not was_topmost:
            _set_window_topmost(user32, hwnd, False)
