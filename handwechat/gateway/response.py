# -*- coding: utf-8 -*-
"""response 列表状态机：分配 id、应用编辑/删除、图片按 name 合并。"""

from __future__ import annotations

import base64
from dataclasses import dataclass

from handwechat.gateway.protocol import (
    MAX_RESPONSE_IMAGE_BYTES,
    MAX_RESPONSE_IMAGE_TOTAL_BYTES,
    MAX_RESPONSE_IMAGES,
    MAX_RESPONSE_ITEMS,
    MAX_RESPONSE_TEXT,
    ResponseImage,
    ResponseItem,
)


class ResponseError(ValueError):
    """response 编辑非法；调用方应忽略本次 response 改动。"""


@dataclass
class _StoredItem:
    id: str
    app: str
    edited_by: str | None
    text: str
    images: dict[str, bytes]


class ResponseState:
    """一次 dispatch 内的回复草稿；图片字节由网关持有。"""

    def __init__(
        self,
        *,
        max_items: int = MAX_RESPONSE_ITEMS,
        max_text: int = MAX_RESPONSE_TEXT,
        max_images: int = MAX_RESPONSE_IMAGES,
        max_image_bytes: int = MAX_RESPONSE_IMAGE_BYTES,
        max_total_bytes: int = MAX_RESPONSE_IMAGE_TOTAL_BYTES,
    ) -> None:
        self._items: list[_StoredItem] = []
        self._next_id = 1
        self._max_items = max(1, int(max_items))
        self._max_text = max(1, int(max_text))
        self._max_images = max(0, int(max_images))
        self._max_image_bytes = max(1, int(max_image_bytes))
        self._max_total_bytes = max(0, int(max_total_bytes))

    @property
    def count(self) -> int:
        return len(self._items)

    def snapshot(self) -> list[dict]:
        """offer 里下发的当前 response；图片只给 name/size。"""

        payload: list[dict] = []
        for item in self._items:
            entry = {
                "id": item.id,
                "app": item.app,
                "text": item.text,
                "images": [
                    {"name": name, "size": len(data)}
                    for name, data in item.images.items()
                ],
            }
            if item.edited_by:
                entry["edited_by"] = item.edited_by
            payload.append(entry)
        return payload

    def final_parts(self) -> list[dict]:
        """终止时按顺序展开成待发送的消息：每个 text 一条，每张图一条。"""

        parts: list[dict] = []
        for item in self._items:
            if item.text:
                parts.append({"text": item.text, "image": None})
            for name, data in item.images.items():
                parts.append(
                    {
                        "text": "",
                        "image": {
                            "name": name,
                            "data": base64.b64encode(data).decode("ascii"),
                        },
                    }
                )
        return parts

    def apply(
        self,
        app_id: str,
        can_rewrite: bool,
        incoming: tuple[ResponseItem, ...],
    ) -> None:
        """应用一次 decision 的 response 列表；非法时抛 ResponseError。"""

        current = {item.id: item for item in self._items}
        if not can_rewrite:
            current_ids = [item.id for item in self._items]
            incoming_ids = [item.id for item in incoming if item.id]
            if incoming_ids != current_ids:
                raise ResponseError(
                    "没有 can_rewrite 权限的应用只能原样保留已有项并追加新项"
                )
            seen_new = False
            for item in incoming:
                if not item.id:
                    seen_new = True
                elif seen_new:
                    raise ResponseError(
                        "没有 can_rewrite 权限的应用只能在末尾追加新项"
                    )

        seen_ids: set[str] = set()
        result: list[_StoredItem] = []
        next_index = self._next_id
        for item in incoming:
            if item.id:
                if item.id in seen_ids:
                    raise ResponseError(f"response id 重复：{item.id}")
                seen_ids.add(item.id)
                stored = current.get(item.id)
                if stored is None:
                    raise ResponseError(f"未知 response id：{item.id}")

                new_text = stored.text if item.text is None else item.text
                if len(new_text) > self._max_text:
                    raise ResponseError(
                        f"response 文本超过 {self._max_text} 字符上限"
                    )
                new_images = self._merge_images(stored.images, item.images)
                changed = (
                    (item.text is not None and item.text != stored.text)
                    or new_images != stored.images
                )
                if changed and not can_rewrite:
                    raise ResponseError(
                        "没有 can_rewrite 权限的应用不能修改已有项"
                    )
                result.append(
                    _StoredItem(
                        id=stored.id,
                        app=stored.app,
                        edited_by=app_id if changed else stored.edited_by,
                        text=new_text,
                        images=new_images,
                    )
                )
                continue

            text = "" if item.text is None else item.text
            if len(text) > self._max_text:
                raise ResponseError(
                    f"response 文本超过 {self._max_text} 字符上限"
                )
            images = self._new_images(item.images)
            if not text and not images:
                raise ResponseError("新增 response 项至少需要 text 或 images")
            item_id = f"r-{next_index}"
            next_index += 1
            result.append(
                _StoredItem(
                    id=item_id,
                    app=app_id,
                    edited_by=None,
                    text=text,
                    images=images,
                )
            )

        self._validate(result)
        self._items = result
        self._next_id = next_index

    def _merge_images(
        self,
        stored: dict[str, bytes],
        incoming: tuple[ResponseImage, ...] | None,
    ) -> dict[str, bytes]:
        if incoming is None:
            return dict(stored)
        result: dict[str, bytes] = {}
        for image in incoming:
            if image.name in result:
                raise ResponseError(f"response 项内图片名重复：{image.name}")
            if image.data is not None:
                result[image.name] = self._decode(image)
                continue
            if image.name not in stored:
                raise ResponseError(f"新增图片 {image.name} 缺少 data")
            result[image.name] = stored[image.name]
        return result

    def _new_images(
        self,
        incoming: tuple[ResponseImage, ...] | None,
    ) -> dict[str, bytes]:
        if incoming is None:
            return {}
        result: dict[str, bytes] = {}
        for image in incoming:
            if image.name in result:
                raise ResponseError(f"response 项内图片名重复：{image.name}")
            if image.data is None:
                raise ResponseError(f"新增图片 {image.name} 缺少 data")
            result[image.name] = self._decode(image)
        return result

    def _decode(self, image: ResponseImage) -> bytes:
        try:
            raw = base64.b64decode(image.data or "", validate=True)
        except Exception as exc:
            raise ResponseError(
                f"图片 {image.name} 的 data 不是合法 base64"
            ) from exc
        if len(raw) > self._max_image_bytes:
            raise ResponseError(
                f"图片 {image.name} 超过 "
                f"{self._max_image_bytes // 1024}KB 上限"
            )
        return raw

    def _validate(self, items: list[_StoredItem]) -> None:
        if len(items) > self._max_items:
            raise ResponseError(f"response 项数超过 {self._max_items} 上限")
        total_images = 0
        total_bytes = 0
        for item in items:
            if len(item.text) > self._max_text:
                raise ResponseError(
                    f"response 文本超过 {self._max_text} 字符上限"
                )
            for data in item.images.values():
                if len(data) > self._max_image_bytes:
                    raise ResponseError(
                        f"图片超过 {self._max_image_bytes // 1024}KB 上限"
                    )
                total_images += 1
                total_bytes += len(data)
        if total_images > self._max_images:
            raise ResponseError(
                f"图片数量超过 {self._max_images} 张上限"
            )
        if total_bytes > self._max_total_bytes:
            raise ResponseError(
                f"图片总大小超过 {self._max_total_bytes // 1024}KB 上限"
            )
