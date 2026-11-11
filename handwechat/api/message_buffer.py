# -*- coding: utf-8 -*-
"""API 内存消息缓冲区和长轮询支持。"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from handwechat.models import BaseMessage, MessageType


def _now_text() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class BufferedMessage:
    cursor: int
    id: str
    session: str
    session_type: Optional[str]
    sender: str
    content: str
    is_at_me: bool
    received_at: str


class MessageBuffer:
    """线程安全环形缓冲区；cursor 在服务进程生命周期内单调递增。"""

    def __init__(self, size: int = 5000) -> None:
        self._messages: deque[BufferedMessage] = deque(maxlen=max(1, int(size)))
        self._condition = threading.Condition()
        self._cursor = 0

    @property
    def size(self) -> int:
        return len(self._messages)

    @property
    def latest_cursor(self) -> int:
        with self._condition:
            return self._cursor

    def append(
        self,
        session: str,
        message: BaseMessage,
        session_type: Optional[str] = None,
    ) -> int:
        """只缓冲纯文本消息，并返回服务端全局游标。"""

        if message.type != MessageType.TEXT:
            return self.latest_cursor

        with self._condition:
            self._cursor += 1
            item = BufferedMessage(
                cursor=self._cursor,
                id=str(self._cursor),
                session=session,
                session_type=session_type,
                sender=message.sender,
                content=message.content,
                is_at_me=bool(message.is_at_me),
                received_at=str(message.extra.get("listen_time") or _now_text()),
            )
            self._messages.append(item)
            self._condition.notify_all()
            return self._cursor

    def get_after(
        self,
        cursor: int,
        *,
        limit: int = 100,
        timeout: float = 0.0,
    ) -> tuple[list[BufferedMessage], int, bool]:
        """返回 cursor 之后的消息；timeout 秒内没有新消息则立即返回。"""

        normalized_cursor = max(0, int(cursor))
        normalized_limit = max(1, int(limit))
        with self._condition:
            if timeout > 0:
                self._condition.wait_for(
                    lambda: bool(self._items_after_locked(normalized_cursor)),
                    timeout=timeout,
                )

            matching = self._items_after_locked(normalized_cursor)
            items = matching[:normalized_limit]
            has_more = len(matching) > normalized_limit
            next_cursor = items[-1].cursor if items else normalized_cursor
            return list(items), next_cursor, has_more

    def _items_after_locked(self, cursor: int) -> list[BufferedMessage]:
        return [item for item in self._messages if item.cursor > cursor]
