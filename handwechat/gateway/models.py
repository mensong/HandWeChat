# -*- coding: utf-8 -*-
"""网关内部数据模型：消息、置顶状态、应用决策。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import time
import uuid

from handwechat.gateway.protocol import Decision


class OfferTimeout(Exception):
    """应用在超时时间内没有返回 decision。"""


class OfferUnavailable(Exception):
    """应用不在线或连接已断开，本条消息无法询问它。"""


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _as_confidence(value) -> float:
    """0.0 是合法的低置信度，不能当成缺失值。"""

    if value is None:
        return 1.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 1.0


@dataclass(frozen=True)
class IncomingMessage:
    """上游推送的一条微信消息，dispatch_id 由网关分配。"""

    id: str
    cursor: int
    sender: str
    content: str
    received_at: str
    is_latest: bool
    confidence: float
    dispatch_id: str

    @classmethod
    def from_upstream(cls, item: dict) -> "IncomingMessage":
        return cls(
            id=str(item.get("id") or ""),
            cursor=int(item.get("cursor") or 0),
            sender=str(item.get("sender") or ""),
            content=str(item.get("content") or ""),
            received_at=str(item.get("received_at") or now_iso()),
            is_latest=bool(item.get("is_latest", False)),
            confidence=_as_confidence(item.get("confidence")),
            dispatch_id=uuid.uuid4().hex,
        )

    def to_payload(self) -> dict:
        return {
            "id": self.id,
            "cursor": self.cursor,
            "sender": self.sender,
            "content": self.content,
            "received_at": self.received_at,
            "is_latest": self.is_latest,
            "confidence": self.confidence,
        }


@dataclass
class PinState:
    """临时置顶：某个应用在一段时间内拥有最高优先级。"""

    app_id: str
    state: str | None
    hold_seconds: float
    expires_mono: float
    chat_title: str = ""

    def remaining(self, now: float | None = None) -> float:
        return max(0.0, self.expires_mono - (time.monotonic() if now is None else now))

    def to_payload(self, now: float | None = None) -> dict:
        expires_at = datetime.now(timezone.utc).astimezone() + timedelta(
            seconds=self.remaining(now)
        )
        return {
            "app_id": self.app_id,
            "state": self.state,
            "expires_at": expires_at.isoformat(timespec="seconds"),
            "hold_seconds": self.hold_seconds,
            "chat_title": self.chat_title,
        }


@dataclass
class DispatchOutcome:
    consumed_by: str | None = None
    tried: list[str] = field(default_factory=list)
    timed_out: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0
