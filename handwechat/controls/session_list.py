# -*- coding: utf-8 -*-
"""会话列表：枚举、滚动去重、格子文本解析。"""

from __future__ import annotations

from typing import List, Optional

import uiautomation as uia

from handwechat import selectors as sel
from handwechat.models import Session

MAX_SESSIONS = 100          # 方案约束：监听/枚举规模 ≤100
MAX_SCROLL_ROUNDS = 40      # 滚动轮数上限，防呆
SCROLL_WAIT = 0.35          # 每轮滚动后等 UIA 树刷新


def parse_cell_name(raw: str, automation_id: str) -> Session:
    """解析 ChatSessionCell 的多行 Name。

    4.1.15.56 实测格式（行序固定，行数可变）：
        会话名
        [已置顶]
        ([有人@我] )([N条] )最后一条预览   ← 标记可能单独成行，也可能带在预览行首
        时间
    """
    lines = [ln for ln in raw.split("\n") if ln.strip()]
    name = automation_id[len(sel.SESSION_CELL_ID_PREFIX):] or (lines[0] if lines else "")
    preview_lines: List[str] = []
    time_raw = ""
    pinned = False
    at_me = False
    unread = 0

    body = lines[1:] if lines else []
    if body:
        time_raw = body[-1].strip()
        body = body[:-1]
    for line in body:
        text = line.strip()
        if text == sel.CELL_PINNED_MARK:
            pinned = True
            continue
        if sel.CELL_AT_ME_MARK in text:
            at_me = True
            text = text.replace(sel.CELL_AT_ME_MARK, "").strip()
        m = sel.CELL_UNREAD_RE.match(text)
        if m:
            unread = int(m.group(1))
            text = text[m.end():].strip()
        if text:
            preview_lines.append(text)

    preview = "\n".join(preview_lines)
    return Session(
        name=name,
        automation_id=automation_id,
        preview=preview,
        time_raw=time_raw,
        unread_count=unread,
        is_at_me=at_me,
        is_pinned=pinned,
        is_new=unread > 0,
    )


class SessionList:
    """包一层 mmui::XTableView（id=session_list）。"""

    def __init__(self, control: uia.ListControl) -> None:
        self._control = control

    @property
    def control(self) -> uia.ListControl:
        return self._control

    def _visible_cells(self) -> List[uia.ListItemControl]:
        cells = []
        for c in self._control.GetChildren():
            if isinstance(c, uia.ListItemControl) and c.AutomationId:
                cells.append(c)
        return cells

    def enumerate_sessions(self, max_sessions: int = MAX_SESSIONS) -> List[Session]:
        """滚动收集全部会话，按 AutomationId 去重；结束滚回顶部。"""
        seen: dict = {}
        for _ in range(MAX_SCROLL_ROUNDS):
            for cell in self._visible_cells():
                aid = cell.AutomationId
                if aid not in seen:
                    seen[aid] = parse_cell_name(cell.Name or "", aid)
            if len(seen) >= max_sessions:
                break
            before = len(seen)
            self._control.WheelDown(wheelTimes=3, waitTime=SCROLL_WAIT)
            if len(self._visible_cells()) == 0:
                break
            after_ids = {c.AutomationId for c in self._visible_cells()}
            if after_ids.issubset(seen.keys()) and len(seen) == before:
                break  # 滚到底且没有新内容
        self.scroll_to_top()
        return list(seen.values())[:max_sessions]

    def scroll_to_top(self) -> None:
        for _ in range(MAX_SCROLL_ROUNDS):
            ids_before = {c.AutomationId for c in self._visible_cells()}
            self._control.WheelUp(wheelTimes=5, waitTime=SCROLL_WAIT)
            ids_after = {c.AutomationId for c in self._visible_cells()}
            if ids_after == ids_before:
                break

    def find_cell(self, name: str) -> Optional[uia.ListItemControl]:
        """按会话名找格子：先深搜 UIA，再滚动；找不到返回 None。"""
        target = sel.SESSION_CELL_ID_PREFIX + name
        direct = uia.ListItemControl(
            self._control,
            AutomationId=target,
            searchDepth=20,
        )
        if direct.Exists(0.4, 0.15):
            return direct
        for cell in self._visible_cells():
            if cell.AutomationId == target:
                return cell
        for _ in range(MAX_SCROLL_ROUNDS):
            self._control.WheelDown(wheelTimes=3, waitTime=SCROLL_WAIT)
            for cell in self._visible_cells():
                if cell.AutomationId == target:
                    return cell
            ids = {c.AutomationId for c in self._visible_cells()}
            if not ids:
                break
        self.scroll_to_top()
        return None
