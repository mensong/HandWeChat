# -*- coding: utf-8 -*-
"""数据模型。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Optional


class SessionType:
    """会话类型常量。A1 阶段格子层面无法区分，A2 打开会话读标题栏后回填。"""

    FRIEND = "friend"
    GROUP = "group"


class MessageAttr:
    """消息来源：self/self、friend/friend、system/system、time/time。"""

    SELF = "self"
    FRIEND = "friend"
    SYSTEM = "system"
    TIME = "time"


class MessageType:
    """MVP 消息内容类型；A2 只解析 text/system/time，其余保守标为 other。"""

    TEXT = "text"
    SYSTEM = "system"
    TIME = "time"
    OTHER = "other"


@dataclass
class Session:
    """会话列表中的一项（来源：mmui::ChatSessionCell）。"""

    name: str
    automation_id: str
    preview: str = ""
    time_raw: str = ""
    unread_count: int = 0
    is_at_me: bool = False
    is_pinned: bool = False
    is_new: bool = False
    type: Optional[str] = None  # SessionType.FRIEND / GROUP，未解析时 None
    extra: dict = field(default_factory=dict)

    def __repr__(self) -> str:
        flags = []
        if self.is_pinned:
            flags.append("置顶")
        if self.unread_count:
            flags.append(f"未读{self.unread_count}")
        if self.is_at_me:
            flags.append("@我")
        flag = f" ({','.join(flags)})" if flags else ""
        return f"<Session {self.name!r}{flag}>"


@dataclass
class BaseMessage:
    """A2 的最小消息模型；不持有 UIA 控件，避免窗口刷新后引用失效。"""

    session_name: str
    sender: str
    content: str
    attr: str
    type: str
    index: int
    automation_id: str = ""
    runtime_id: str = ""
    rect: Optional[tuple[int, int, int, int]] = None
    hash: str = ""
    is_at_me: bool = False
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.hash:
            self.hash = make_message_hash(
                self.session_name, self.sender, self.content, self.index
            )

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} {self.attr}/{self.type} "
            f"sender={self.sender!r} content={self.content[:30]!r} "
            f"at_me={self.is_at_me} index={self.index}>"
        )


class TextMessage(BaseMessage):
    type = MessageType.TEXT

    def __post_init__(self) -> None:
        self.type = MessageType.TEXT
        super().__post_init__()


class SystemMessage(BaseMessage):
    type = MessageType.SYSTEM

    def __post_init__(self) -> None:
        self.type = MessageType.SYSTEM
        super().__post_init__()


class TimeMessage(BaseMessage):
    type = MessageType.TIME

    def __post_init__(self) -> None:
        self.type = MessageType.TIME
        super().__post_init__()


class OtherMessage(BaseMessage):
    """非纯文本气泡（文件/图片等）的保守兜底类型。"""

    type = MessageType.OTHER

    def __post_init__(self) -> None:
        self.type = MessageType.OTHER
        super().__post_init__()


def make_message_hash(
    session_name: str, sender: str, content: str, index: int
) -> str:
    """按方案约定的“会话 + 发送人 + 内容 + 位置序号”生成去重键。"""

    raw = f"{session_name}\x1f{sender}\x1f{content}\x1f{index}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
