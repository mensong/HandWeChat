# -*- coding: utf-8 -*-
"""消息轨迹：最近若干条消息经过应用的记录，供管理页下钻。"""

from __future__ import annotations

from collections import deque

MAX_ITEMS = 200
MAX_RECENT = 200
_MAX_CONTENT = 2000


def clip(text: str, limit: int = _MAX_CONTENT) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    return value[:limit] + "…"


class MessageLog:
    """内存环形缓冲；网关重启后清空，不落盘。"""

    def __init__(self, max_items: int = MAX_ITEMS) -> None:
        self._items: deque[dict] = deque(maxlen=max(1, max_items))

    def add(self, record: dict) -> None:
        self._items.append(record)

    def get(self, trace_id: str) -> dict | None:
        for record in reversed(self._items):
            if record.get("id") == trace_id:
                return record
        return None

    def recent(self, limit: int = 50) -> list[dict]:
        size = max(1, min(int(limit), MAX_RECENT))
        return [
            _summary(record) for record in list(self._items)[-size:][::-1]
        ]

    def __len__(self) -> int:
        return len(self._items)


def _summary(record: dict) -> dict:
    reply = record.get("reply") or {}
    return {
        "id": record.get("id"),
        "message_id": record.get("message_id"),
        "received_at": record.get("received_at"),
        "recorded_at": record.get("recorded_at"),
        "sender": record.get("sender"),
        "chat": record.get("chat"),
        "content": record.get("content"),
        "final_content": record.get("final_content"),
        "consumed_by": record.get("consumed_by"),
        "reason": record.get("reason"),
        "elapsed_ms": record.get("elapsed_ms"),
        "tried": record.get("tried") or [],
        "steps": len(record.get("steps") or []),
        "reply_parts": reply.get("parts", 0),
    }
