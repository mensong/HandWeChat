# -*- coding: utf-8 -*-
"""Message models for the current WeChat chat."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Optional


class MessageAttr:
    SELF = "self"
    FRIEND = "friend"
    SYSTEM = "system"
    TIME = "time"


class MessageType:
    TEXT = "text"
    SYSTEM = "system"
    TIME = "time"
    OTHER = "other"


@dataclass
class BaseMessage:
    """A text-oriented message captured from the current chat."""

    sender: str
    content: str
    attr: str
    type: str
    index: int
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.hash = make_message_hash(self.sender, self.content, self.index)

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} {self.attr}/{self.type} "
            f"sender={self.sender!r} content={self.content[:30]!r} "
            f"index={self.index}>"
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
    type = MessageType.OTHER

    def __post_init__(self) -> None:
        self.type = MessageType.OTHER
        super().__post_init__()


def make_message_hash(sender: str, content: str, index: int) -> str:
    raw = f"{sender}\x1f{content}\x1f{index}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
