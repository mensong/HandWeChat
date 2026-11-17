# -*- coding: utf-8 -*-
"""In-memory buffer for messages from the current WeChat chat."""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from handwechat.models import BaseMessage, MessageType

logger = logging.getLogger(__name__)


def _now_text() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class BufferedMessage:
    cursor: int
    id: str
    sender: str
    content: str
    received_at: str
    is_latest: bool = False
    confidence: float = 1.0


class MessageBuffer:
    """Thread-safe ring buffer with a process-wide monotonic cursor."""

    def __init__(self, size: int = 5000) -> None:
        self._messages: deque[BufferedMessage] = deque(maxlen=max(1, int(size)))
        self._condition = threading.Condition()
        self._subscribers: list[Callable[[], None]] = []
        self._cursor = 0

    @property
    def size(self) -> int:
        return len(self._messages)

    @property
    def latest_cursor(self) -> int:
        with self._condition:
            return self._cursor

    def append(self, message: BaseMessage) -> int:
        """Buffer pure text messages and return the process-wide cursor."""

        if message.type != MessageType.TEXT:
            return self.latest_cursor

        with self._condition:
            self._cursor += 1
            item = BufferedMessage(
                cursor=self._cursor,
                id=str(self._cursor),
                sender=message.sender,
                content=message.content,
                received_at=str(message.extra.get("listen_time") or _now_text()),
                is_latest=bool(getattr(message, "is_latest", False)),
                confidence=float(getattr(message, "confidence", 1.0)),
            )
            self._messages.append(item)
            self._condition.notify_all()
            cursor = self._cursor
            subscribers = tuple(self._subscribers)

        for callback in subscribers:
            # Subscribers run on the OCR thread; an isolated failure must not
            # stop message ingestion.
            try:
                callback()
            except Exception:
                logger.exception("消息缓冲订阅回调执行失败")

        return cursor

    def subscribe(self, callback: Callable[[], None]) -> None:
        """Register a no-argument callback fired after each buffered message."""

        with self._condition:
            if callback not in self._subscribers:
                self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[], None]) -> None:
        with self._condition:
            try:
                self._subscribers.remove(callback)
            except ValueError:
                pass

    def clear(self) -> None:
        """Discard buffered messages while preserving the global cursor."""

        with self._condition:
            self._messages.clear()

    def get_after(
        self,
        cursor: int,
        *,
        limit: int = 100,
        timeout: float = 0.0,
    ) -> tuple[list[BufferedMessage], int, bool]:
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
