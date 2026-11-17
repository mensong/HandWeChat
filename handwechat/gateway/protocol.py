# -*- coding: utf-8 -*-
"""网关与消费端共享的线上协议类型，只依赖标准库。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Decision:
    """应用对一条消息的返回。action 是唯一决定是否消费的字段。"""

    action: str
    reply: str | None = None
    pin: bool | None = None
    hold_seconds: float | None = None
    state: str | None = None

    @classmethod
    def parse(cls, payload: dict) -> "Decision":
        action = str(payload.get("action") or "").strip().lower()
        if action not in ("consume", "skip"):
            raise ValueError(f"未知 action：{action!r}")

        reply = payload.get("reply")
        if reply is not None and not isinstance(reply, str):
            raise ValueError("reply 必须是字符串")

        pin = payload.get("pin")
        if pin is not None and not isinstance(pin, bool):
            raise ValueError("pin 必须是布尔值")

        hold = payload.get("hold_seconds")
        if hold is not None:
            if isinstance(hold, bool):
                raise ValueError("hold_seconds 必须是数字")
            hold = float(hold)
            if hold < 0:
                raise ValueError("hold_seconds 不能为负数")

        state = payload.get("state")
        if state is not None and not isinstance(state, str):
            raise ValueError("state 必须是字符串")

        return cls(
            action=action,
            reply=reply or None,
            pin=pin,
            hold_seconds=hold,
            state=state,
        )

    @classmethod
    def skip(cls) -> "Decision":
        return cls(action="skip")

    @classmethod
    def consume(
        cls,
        reply: str | None = None,
        *,
        pin: bool | None = None,
        hold_seconds: float | None = None,
        state: str | None = None,
    ) -> "Decision":
        return cls(
            action="consume",
            reply=reply or None,
            pin=pin,
            hold_seconds=hold_seconds,
            state=state,
        )
