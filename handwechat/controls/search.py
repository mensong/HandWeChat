# -*- coding: utf-8 -*-
"""全局搜索：Ctrl+F -> 粘贴目标名 -> 点选精确 search_item。"""

from __future__ import annotations

from typing import Optional

import uiautomation as uia

from handwechat import selectors as sel


class SearchBox:
    """独立封装搜索弹窗，避免 ChatWith 里堆 UI 细节。"""

    def __init__(self, main_control: uia.Control) -> None:
        self._main = main_control

    def open(self, target: str, timeout: float = 4.0) -> Optional[uia.Control]:
        """打开并选中精确匹配的搜索结果；失败时关闭弹窗并返回 None。"""

        self._main.SendKeys("{Ctrl}f", waitTime=0.4)
        popover = self._popover(timeout)
        if popover is None:
            return None

        try:
            uia.SetClipboardText(target)
            uia.SendKeys("{Ctrl}v", waitTime=0.5)
            exact = self._find_result(popover, target)
            if exact is not None:
                exact.Click(simulateMove=False, waitTime=0.2)
                return exact
            return None
        finally:
            self.close()

    def _popover(self, timeout: float) -> Optional[uia.Control]:
        ctrl = uia.WindowControl(
            ClassName=sel.SEARCH_POPOVER_CLASS,
            Name="Weixin",
            searchDepth=2,
        )
        return ctrl if ctrl.Exists(timeout, 0.2) else None

    def _find_result(self, popover: uia.Control, target: str) -> Optional[uia.Control]:
        """查找目标搜索结果。

        4.1.15.56 的结果 AutomationId 会带类型段，例如
        ``search_item_function_文件传输助手``，因此这里按前缀而不是完整值匹配。
        """

        for control, _depth in uia.WalkControl(popover, includeTop=False, maxDepth=20):
            if not isinstance(control, uia.ListItemControl):
                continue
            automation_id = control.AutomationId or ""
            if (
                automation_id.startswith(sel.SEARCH_ITEM_ID_PREFIX)
                and control.Name == target
            ):
                return control

        for control, _depth in uia.WalkControl(popover, includeTop=False, maxDepth=20):
            if isinstance(control, uia.ListItemControl) and control.Name == target:
                return control
        return None

    def close(self) -> None:
        popover = self._popover(0.2)
        if popover is not None:
            uia.SendKeys("{Esc}", waitTime=0.2)
