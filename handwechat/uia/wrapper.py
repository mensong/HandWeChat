# -*- coding: utf-8 -*-
"""uiautomation 薄封装：全局 UI 锁、带超时找控件。

微信 UI 单线程，所有 UI 操作经全局锁串行化（对应 wxauto4 的 uilock 思路）。
"""

from __future__ import annotations

import threading
from typing import Optional, Type

import uiautomation as uia

from handwechat.errors import ControlNotFoundError

ui_lock = threading.RLock()


def wait_control(
    ctrl_type: Type[uia.Control],
    parent: uia.Control,
    timeout: float,
    search_depth: int,
    **props,
) -> Optional[uia.Control]:
    """在 parent 内按属性找控件，找到返回实例，超时返回 None（不抛异常）。"""
    ctrl = ctrl_type(parent=parent, searchDepth=search_depth, **props)
    return ctrl if ctrl.Exists(timeout, 0.2) else None


def require_control(
    ctrl_type: Type[uia.Control],
    parent: uia.Control,
    timeout: float,
    search_depth: int,
    desc: str,
    **props,
) -> uia.Control:
    """同 wait_control，但超时抛 ControlNotFoundError（desc 说明找什么）。"""
    ctrl = wait_control(ctrl_type, parent, timeout, search_depth, **props)
    if ctrl is None:
        raise ControlNotFoundError(
            f"控件定位失败（{desc}）：微信可能已升级，"
            "请用 probe/m0_uia_dump.py 重新 dump 对照 selectors.py"
        )
    return ctrl
