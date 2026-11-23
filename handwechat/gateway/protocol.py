# -*- coding: utf-8 -*-
"""网关与消费端共享的线上协议类型，只依赖标准库。"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass

PROTOCOL_VERSION = 2

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_RESPONSE_IMAGE_BYTES = 1024 * 1024
MAX_RESPONSE_IMAGE_TOTAL_BYTES = 5 * 1024 * 1024
MAX_RESPONSE_IMAGES = 5
MAX_RESPONSE_ITEMS = 20
MAX_RESPONSE_TEXT = 2000
MAX_EXTRA_BYTES = 4096
MAX_IMAGE_NAME = 255

ACTION_REWRITE = "rewrite"
ACTION_CONSUME = "consume"


def parse_image(value) -> dict | None:
    """校验主动 send 帧的 {name, data} 图片字段，返回规范化后的字典。"""

    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("image 必须是对象")
    raw_name = value.get("name")
    if raw_name is None or str(raw_name).strip() == "":
        name = "image"
    else:
        name = _clean_image_name(raw_name)
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
    return {"name": name, "data": data}


@dataclass(frozen=True)
class ResponseImage:
    """response 项里的一张图片。

    offer 中只有 name/size 元信息；decision 中可带 data 表示新增或替换。
    """

    name: str
    data: str | None = None
    size: int = 0

    @classmethod
    def from_offer(cls, value) -> "ResponseImage":
        if not isinstance(value, dict):
            raise ValueError("response[].images 中的每一项必须是对象")
        name = _clean_image_name(value.get("name"))
        raw_size = value.get("size")
        try:
            size = 0 if raw_size is None else max(0, int(raw_size))
        except (TypeError, ValueError) as exc:
            raise ValueError("image.size 必须是整数") from exc
        return cls(name=name, size=size)

    @classmethod
    def from_decision(cls, value) -> "ResponseImage":
        if not isinstance(value, dict):
            raise ValueError("response[].images 中的每一项必须是对象")
        name = _clean_image_name(value.get("name"))
        data = value.get("data")
        if data is None:
            return cls(name=name)
        if not isinstance(data, str) or not data:
            raise ValueError("image.data 必须是非空 base64 字符串")
        try:
            raw = base64.b64decode(data, validate=True)
        except Exception as exc:
            raise ValueError(f"image.data 不是合法 base64：{exc}") from exc
        if len(raw) > MAX_RESPONSE_IMAGE_BYTES:
            raise ValueError(
                f"图片超过 {MAX_RESPONSE_IMAGE_BYTES // 1024}KB 上限"
            )
        return cls(name=name, data=data, size=len(raw))

    @classmethod
    def from_bytes(cls, name: str, data: bytes) -> "ResponseImage":
        name = _clean_image_name(name)
        if not isinstance(data, (bytes, bytearray)):
            raise ValueError("image data 必须是 bytes")
        raw = bytes(data)
        if len(raw) > MAX_RESPONSE_IMAGE_BYTES:
            raise ValueError(
                f"图片超过 {MAX_RESPONSE_IMAGE_BYTES // 1024}KB 上限"
            )
        return cls(
            name=name,
            data=base64.b64encode(raw).decode("ascii"),
            size=len(raw),
        )

    def to_offer_payload(self) -> dict:
        return {"name": self.name, "size": self.size}

    def to_decision_payload(self) -> dict:
        payload = {"name": self.name}
        if self.data is not None:
            payload["data"] = self.data
        return payload


@dataclass(frozen=True)
class ResponseItem:
    """response 列表中的一项：一条待发送的回复单元。

    id 由网关分配；decision 中新项不带 id。
    text/images 为 None 表示该字段不变，空值表示清空。
    """

    id: str = ""
    app: str = ""
    edited_by: str | None = None
    text: str | None = None
    images: tuple[ResponseImage, ...] | None = None

    @classmethod
    def from_offer(cls, value) -> "ResponseItem":
        if not isinstance(value, dict):
            raise ValueError("response 中的每一项必须是对象")
        item_id = str(value.get("id") or "").strip()
        if not item_id:
            raise ValueError("response[].id 不能为空")
        text = value.get("text")
        if text is not None and not isinstance(text, str):
            raise ValueError("response[].text 必须是字符串")
        images = None
        if value.get("images") is not None:
            raw_images = value.get("images")
            if not isinstance(raw_images, list):
                raise ValueError("response[].images 必须是数组")
            images = tuple(ResponseImage.from_offer(item) for item in raw_images)
        edited_by = value.get("edited_by")
        if edited_by is not None and not isinstance(edited_by, str):
            raise ValueError("response[].edited_by 必须是字符串")
        return cls(
            id=item_id,
            app=str(value.get("app") or ""),
            edited_by=edited_by or None,
            text=text,
            images=images,
        )

    @classmethod
    def from_decision(cls, value) -> "ResponseItem":
        if not isinstance(value, dict):
            raise ValueError("response 中的每一项必须是对象")
        item_id = str(value.get("id") or "").strip()
        text = None
        if "text" in value:
            text = value.get("text")
            if not isinstance(text, str):
                raise ValueError("response[].text 必须是字符串")
        images = None
        if "images" in value:
            raw_images = value.get("images")
            if not isinstance(raw_images, list):
                raise ValueError("response[].images 必须是数组")
            images = tuple(ResponseImage.from_decision(item) for item in raw_images)
        app = str(value.get("app") or "")
        edited_by = value.get("edited_by")
        if edited_by is not None and not isinstance(edited_by, str):
            raise ValueError("response[].edited_by 必须是字符串")
        return cls(
            id=item_id,
            app=app,
            edited_by=edited_by or None,
            text=text,
            images=images,
        )

    @classmethod
    def new(
        cls,
        *,
        text: str | None = None,
        images: tuple[ResponseImage, ...] | list[ResponseImage] | None = None,
    ) -> "ResponseItem":
        return cls(text=text, images=tuple(images) if images is not None else None)

    @classmethod
    def keep(cls, item_id: str) -> "ResponseItem":
        return cls(id=item_id)

    def to_offer_payload(self) -> dict:
        payload = {
            "id": self.id,
            "app": self.app,
            "text": self.text or "",
            "images": [
                image.to_offer_payload() for image in (self.images or ())
            ],
        }
        if self.edited_by:
            payload["edited_by"] = self.edited_by
        return payload

    def to_decision_payload(self) -> dict:
        payload: dict = {}
        if self.id:
            payload["id"] = self.id
        if self.text is not None:
            payload["text"] = self.text
        if self.images is not None:
            payload["images"] = [
                image.to_decision_payload() for image in self.images
            ]
        return payload


@dataclass(frozen=True)
class Decision:
    """应用对一条消息的返回。action 是 rewrite 或 consume。"""

    action: str
    content: str | None = None
    sender: str | None = None
    extra: dict | None = None
    response: tuple[ResponseItem, ...] | None = None
    pin: bool | None = None
    hold_seconds: float | None = None
    state: str | None = None

    @classmethod
    def parse(cls, payload: dict) -> "Decision":
        action = str(payload.get("action") or "").strip().lower()
        if action not in (ACTION_REWRITE, ACTION_CONSUME):
            raise ValueError(f"未知 action：{action!r}")

        content = payload.get("content")
        if content is not None and not isinstance(content, str):
            raise ValueError("content 必须是字符串")

        sender = payload.get("sender")
        if sender is not None and not isinstance(sender, str):
            raise ValueError("sender 必须是字符串")

        extra = payload.get("extra")
        if extra is not None:
            if not isinstance(extra, dict):
                raise ValueError("extra 必须是对象")
            try:
                encoded = json.dumps(extra, ensure_ascii=False).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise ValueError("extra 必须是可序列化的 JSON 对象") from exc
            if len(encoded) > MAX_EXTRA_BYTES:
                raise ValueError(f"extra 超过 {MAX_EXTRA_BYTES} 字节上限")

        response = None
        if "response" in payload:
            raw_response = payload.get("response")
            if not isinstance(raw_response, list):
                raise ValueError("response 必须是数组")
            response = tuple(
                ResponseItem.from_decision(item) for item in raw_response
            )

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
            content=content,
            sender=sender,
            extra=extra,
            response=response,
            pin=pin,
            hold_seconds=hold,
            state=state,
        )

    @classmethod
    def rewrite(
        cls,
        *,
        content: str | None = None,
        sender: str | None = None,
        extra: dict | None = None,
        response: tuple[ResponseItem, ...] | list[ResponseItem] | None = None,
    ) -> "Decision":
        return cls(
            action=ACTION_REWRITE,
            content=content,
            sender=sender,
            extra=extra,
            response=_coerce_response(response),
        )

    @classmethod
    def consume(
        cls,
        *,
        response: tuple[ResponseItem, ...] | list[ResponseItem] | None = None,
        pin: bool | None = None,
        hold_seconds: float | None = None,
        state: str | None = None,
    ) -> "Decision":
        return cls(
            action=ACTION_CONSUME,
            response=_coerce_response(response),
            pin=pin,
            hold_seconds=hold_seconds,
            state=state,
        )

    def to_payload(self) -> dict:
        payload: dict = {"action": self.action}
        if self.content is not None:
            payload["content"] = self.content
        if self.sender is not None:
            payload["sender"] = self.sender
        if self.extra is not None:
            payload["extra"] = self.extra
        if self.response is not None:
            payload["response"] = [
                item.to_decision_payload() for item in self.response
            ]
        if self.pin is not None:
            payload["pin"] = self.pin
        if self.hold_seconds is not None:
            payload["hold_seconds"] = self.hold_seconds
        if self.state is not None:
            payload["state"] = self.state
        return payload


def _coerce_response(value) -> tuple[ResponseItem, ...] | None:
    if value is None:
        return None
    if isinstance(value, ResponseItem):
        return (value,)
    if not isinstance(value, (list, tuple)):
        raise ValueError("response 必须是数组")
    items: list[ResponseItem] = []
    for item in value:
        if isinstance(item, ResponseItem):
            items.append(item)
        elif isinstance(item, dict):
            items.append(ResponseItem.from_decision(item))
        else:
            raise ValueError("response 中的每一项必须是对象")
    return tuple(items)


def _clean_image_name(value) -> str:
    if not isinstance(value, str):
        raise ValueError("image.name 必须是字符串")
    name = value.strip()
    if not name:
        raise ValueError("image.name 不能为空")
    if len(name) > MAX_IMAGE_NAME:
        raise ValueError(f"image.name 最长 {MAX_IMAGE_NAME} 个字符")
    return name
