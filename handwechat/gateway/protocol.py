# -*- coding: utf-8 -*-
"""网关与消费端共享的线上协议类型，只依赖标准库。"""

from __future__ import annotations

import base64
from dataclasses import dataclass

MAX_IMAGE_BYTES = 10 * 1024 * 1024


def parse_image(value) -> dict | None:
    """校验 {name, data} 图片字段，返回规范化后的字典。"""

    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("image 必须是对象")
    name = str(value.get("name") or "").strip()
    data = value.get("data")
    if not isinstance(data, str) or not data:
        raise ValueError("image.data 必须是非空 base64 字符串")
    try:
        raw = base64.b64decode(data, validate=True)
    except Exception as exc:
        raise ValueError(f"image.data 不是合法 base64：{exc}") from exc
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError(
            f"图片超过 {MAX_IMAGE_BYTES // (1024 * 1024)}MB 上限"
        )
    return {"name": name or "image", "data": data}


@dataclass(frozen=True)
class Decision:
    """应用对一条消息的返回。action 是唯一决定是否消费的字段。"""

    action: str
    reply: str | None = None
    reply_image: dict | None = None
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

        reply_image = parse_image(payload.get("reply_image"))

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
            reply_image=reply_image,
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
        image: bytes | None = None,
        image_name: str = "image",
        pin: bool | None = None,
        hold_seconds: float | None = None,
        state: str | None = None,
    ) -> "Decision":
        reply_image = None
        if image is not None:
            reply_image = {
                "name": image_name or "image",
                "data": base64.b64encode(image).decode("ascii"),
            }
        return cls(
            action="consume",
            reply=reply or None,
            reply_image=reply_image,
            pin=pin,
            hold_seconds=hold_seconds,
            state=state,
        )
