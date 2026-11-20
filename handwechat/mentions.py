# -*- coding: utf-8 -*-
"""`[@用户名]` 提及语法解析。"""

from __future__ import annotations

import re

_MENTION_PATTERN = re.compile(r"\[@([^\[\]\r\n]+)\]")

Segment = tuple[str, str]


def split_mentions(text: str) -> list[Segment]:
    """按 [@用户名] 切分文本，返回 ("text", 文本) 与 ("mention", 用户名) 段。

    [@] 这类空用户名、未闭合的 [@abc 都按普通文本处理。
    """

    segments: list[Segment] = []
    position = 0
    for match in _MENTION_PATTERN.finditer(text):
        name = match.group(1).strip()
        if not name:
            continue
        if match.start() > position:
            segments.append(("text", text[position : match.start()]))
        segments.append(("mention", name))
        position = match.end()
    if position < len(text):
        segments.append(("text", text[position:]))
    return segments
