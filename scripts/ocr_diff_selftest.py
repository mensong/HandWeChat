# -*- coding: utf-8 -*-
"""OCR 新消息 diff 自检：尾部序列对齐的采纳范围（不依赖微信窗口）。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from handwechat.models import MessageAttr
from handwechat.ocr_channel import (
    OcrMessage,
    _find_tail_alignment,
    _message_signature,
    _select_pending,
)


def F(text: str) -> OcrMessage:
    return OcrMessage(attr=MessageAttr.FRIEND, text=text, sender="对方")


def S(text: str) -> OcrMessage:
    return OcrMessage(attr=MessageAttr.SELF, text=text, sender="我")


def T(text: str) -> OcrMessage:
    return OcrMessage(attr=MessageAttr.TIME, text=text, sender="")


def Y(text: str) -> OcrMessage:
    return OcrMessage(attr=MessageAttr.SYSTEM, text=text, sender="")


def sig(messages: list[OcrMessage]) -> tuple[str, ...]:
    return tuple(_message_signature(message) for message in messages)


FAILURES: list[str] = []


def check(
    name: str,
    previous: list[OcrMessage],
    current: list[OcrMessage],
    *,
    last_emitted: str | None = None,
    texts: tuple[str, ...],
    match: str | None = None,
    advance: bool | None = None,
) -> None:
    if last_emitted is None:
        last_emitted = _message_signature(previous[-1]) if previous else ""
    pending, state, moves, _length = _select_pending(
        sig(previous), current, last_emitted
    )
    got = tuple(message.text for message in pending)
    problems = []
    if got != texts:
        problems.append(f"pending={got!r} 期望 {texts!r}")
    if match is not None and state != match:
        problems.append(f"match={state} 期望 {match}")
    if advance is not None and moves is not advance:
        problems.append(f"advance={moves} 期望 {advance}")
    if any(message.attr == MessageAttr.SELF for message in pending):
        problems.append("上报了机器人消息")
    if problems:
        FAILURES.append(f"{name}: " + "; ".join(problems))
        print(f"[FAIL] {name}")
    else:
        print(f"[PASS] {name}")


def main() -> int:
    check("画面不变", [F("a")], [F("a")], texts=(), match="same", advance=True)
    check("新增一条", [F("a")], [F("a"), F("b")], texts=("b",), match="aligned")
    check(
        "一帧新增三条",
        [F("a")],
        [F("a"), F("b"), F("c"), F("d")],
        texts=("b", "c", "d"),
        match="aligned",
    )
    check(
        "机器人回复停在底部、画面重绘",
        [F("a"), S("回复")],
        [F("a"), S("回复")],
        texts=(),
        match="same",
    )
    check(
        "机器人回复后收到新消息",
        [F("a"), S("回复")],
        [F("a"), S("回复"), F("b")],
        texts=("b",),
        match="aligned",
    )
    check(
        "重复同一句（中间有机器人）",
        [F("a"), S("回复")],
        [F("a"), S("回复"), F("a")],
        texts=("a",),
        match="aligned",
    )
    check(
        "重复同一句（中间无机器人）",
        [F("x"), F("a")],
        [F("x"), F("a"), F("a")],
        texts=("a",),
        match="aligned",
    )
    check(
        "对方消息后跟系统提示",
        [F("a")],
        [F("a"), F("b"), Y("拍了拍")],
        texts=("b", "拍了拍"),
        match="aligned",
    )
    check(
        "时间戳与系统提示保持顺序",
        [F("a")],
        [F("a"), T("12:30"), F("b"), Y("拍了拍")],
        texts=("12:30", "b", "拍了拍"),
        match="aligned",
    )
    check(
        "顶部滚出、底部新增",
        [F("a"), F("b"), F("c")],
        [F("b"), F("c"), F("d")],
        texts=("d",),
        match="aligned",
    )
    check(
        "底部误读走锚点兜底",
        [F("a"), S("回复")],
        [F("a"), S("回复误读"), F("新")],
        last_emitted=_message_signature(F("a")),
        texts=("新",),
        match="fallback_anchor",
        advance=True,
    )
    check(
        "锚点滚出且无可对上消息，保持旧锚点",
        [F("已滚出")],
        [F("b")],
        last_emitted=_message_signature(F("b")),
        texts=(),
        match="fallback_anchor",
        advance=False,
    )
    check(
        "彻底对不上时取最新一条",
        [S("只有机器人")],
        [F("a")],
        last_emitted=_message_signature(F("z")),
        texts=("a",),
        match="fallback_latest",
        advance=True,
    )
    check(
        "锚点之后的机器人消息不上报",
        [F("a")],
        [F("a"), S("机器人")],
        texts=(),
        match="same",
    )

    alignment = _find_tail_alignment(("a", "b", "c"), ("a", "b", "c", "d"))
    if alignment != (0, 3):
        FAILURES.append(f"最长后缀对齐: {alignment} 期望 (0, 3)")
        print("[FAIL] 最长后缀对齐")
    else:
        print("[PASS] 最长后缀对齐")

    repeated = _find_tail_alignment(("a", "b"), ("a", "b", "a", "b"))
    if repeated != (2, 2):
        FAILURES.append(f"重复段取最后一次出现: {repeated} 期望 (2, 2)")
        print("[FAIL] 重复段取最后一次出现")
    else:
        print("[PASS] 重复段取最后一次出现")

    print()
    if FAILURES:
        print(f"共 {len(FAILURES)} 项失败：")
        for failure in FAILURES:
            print(f"- {failure}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
