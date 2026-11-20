# -*- coding: utf-8 -*-
"""发送载荷：一条消息里的文本、图片与展示文本。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

MAX_IMAGE_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class SendPayload:
    text: str = ""
    image_path: Path | None = None
    image_name: str = ""

    def display_text(self) -> str:
        if self.image_path is None:
            return self.text
        name = self.image_name or self.image_path.name
        return f"{self.text} [图片] {name}".strip()
