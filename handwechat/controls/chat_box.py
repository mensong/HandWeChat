# -*- coding: utf-8 -*-
"""聊天区：标题栏、消息列表、气泡方向与消息解析。"""

from __future__ import annotations

import re
import time
from collections import Counter
from typing import Iterable, Optional, Sequence

import uiautomation as uia
from PIL import ImageGrab

from handwechat import selectors as sel
from handwechat.models import (
    BaseMessage,
    MessageAttr,
    OtherMessage,
    SessionType,
    SystemMessage,
    TextMessage,
    TimeMessage,
)
from handwechat.ocr_win import recognize_images

_TIME_RE = re.compile(
    r"(?:星期[一二三四五六日天]|\d{1,2}[:：]\d{2}|\d{4}[-/年]|\d{1,2}月\d{1,2}日)"
)
_ME_RE = re.compile(r"@([^\s@：:，,。]+)")
MAX_ANCHOR_SCROLL_ROUNDS = 12
ANCHOR_SCROLL_WAIT = 0.18
ANCHOR_RECOVERY_TIMEOUT = 2.5
BOTTOM_SCROLL_WAIT = 0.18


def _message_key(msg: BaseMessage) -> str:
    """与监听器保持同一把内容锚点键。"""

    return "\x1f".join((msg.type, msg.attr, msg.content))


def _find_suffix(keys: list[str], suffix: list[str]) -> tuple[int, int]:
    """返回 suffix 在 keys 中最早出现的起点和匹配长度。"""

    max_length = min(len(keys), len(suffix))
    for length in range(max_length, 0, -1):
        pattern = suffix[-length:]
        for start in range(0, len(keys) - length + 1):
            if keys[start : start + length] == pattern:
                return start, length
    return -1, 0


def _stitch_overlap(older_keys: list[str], newer_keys: list[str]) -> int:
    """返回 older 后缀与 newer 前缀的最长重叠长度。"""

    max_length = min(len(older_keys), len(newer_keys))
    for length in range(max_length, 0, -1):
        if older_keys[-length:] == newer_keys[:length]:
            return length
    return 0


class ChatBox:
    """包装当前打开的 mmui::ChatMessagePage。"""

    def __init__(self, control: uia.Control) -> None:
        self._control = control

    @property
    def control(self) -> uia.Control:
        return self._control

    @property
    def message_list(self) -> uia.ListControl:
        ctrl = uia.ListControl(
            self._control,
            ClassName="mmui::RecyclerListView",
            AutomationId=sel.MESSAGE_LIST_ID,
            searchDepth=20,
        )
        if not ctrl.Exists(2, 0.2):
            raise RuntimeError("找不到消息列表 chat_message_list，请重新打开会话")
        return ctrl

    def current_chat_name(self) -> Optional[str]:
        label = uia.TextControl(
            self._control,
            AutomationId=sel.CHAT_TITLE_LABEL_ID,
            searchDepth=25,
        )
        if not label.Exists(0.5, 0.2):
            return None
        return label.Name or None

    def send_text(self, msg: str, *, clear: bool = True) -> None:
        """把纯文本写入输入框并回车发送。

        粘贴比逐字模拟稳定，也不会被中文输入法改写；这里假设调用方已经
        处于目标会话。发送是否真的到达消息区，由 WeChat.SendMsg 回读校验。
        """

        if not msg:
            raise ValueError("msg 不能为空")

        edit = uia.EditControl(
            self._control,
            ClassName="mmui::ChatInputField",
            AutomationId=sel.CHAT_INPUT_FIELD_ID,
            searchDepth=20,
        )
        if not edit.Exists(2.0, 0.2):
            raise RuntimeError("找不到输入框 chat_input_field，请重新打开会话")

        edit.Click(simulateMove=False, waitTime=0.2)
        if clear:
            uia.SendKeys("{Ctrl}a", waitTime=0.1)
            uia.SendKeys("{Delete}", waitTime=0.1)

        uia.SetClipboardText(msg)
        uia.SendKeys("{Ctrl}v", waitTime=0.4)
        value = ""
        try:
            value = edit.GetValuePattern().Value or ""
        except Exception:
            value = ""
        if not value:
            raise RuntimeError("文本已粘贴但输入框仍为空，已取消回车发送")
        uia.SendKeys("{Enter}", waitTime=0.5)

    def wait_for_self_text(self, content: str, timeout: float = 4.0) -> Optional[TextMessage]:
        """回读消息区，确认指定内容已成为最新一条自身文本消息。"""

        deadline = time.time() + timeout
        while time.time() < deadline:
            for message in reversed(self.get_all_messages()):
                if (
                    isinstance(message, TextMessage)
                    and message.attr == MessageAttr.SELF
                    and message.content == content
                ):
                    return message
            time.sleep(0.2)
        return None

    def session_type(self) -> Optional[str]:
        """优先用标题栏类名判群；旧版标题后缀成员数仅作兜底。"""

        if uia.GroupControl(
            self._control,
            ClassName=sel.CHAT_TITLE_BAR_GROUP_CLASS,
            searchDepth=3,
        ).Exists(0.5, 0.2):
            return SessionType.GROUP
        if uia.GroupControl(
            self._control,
            ClassName=sel.CHAT_TITLE_BAR_FRIEND_CLASS,
            searchDepth=3,
        ).Exists(0.5, 0.2):
            return SessionType.FRIEND

        name = self.current_chat_name()
        if name and sel.GROUP_TITLE_RE.match(name):
            return SessionType.GROUP
        return SessionType.FRIEND

    def get_all_messages(
        self,
        *,
        my_names: Sequence[str] = (),
    ) -> list[BaseMessage]:
        """读取当前聊天页已加载的消息（按 UIA 列表顺序）。"""

        msg_list = self.message_list
        rect = msg_list.BoundingRectangle
        items = [
            item
            for item in msg_list.GetChildren()
            if isinstance(item, uia.ListItemControl)
        ]
        if not items:
            return []

        shot = None
        try:
            shot = ImageGrab.grab(
                bbox=(
                    int(rect.left),
                    int(rect.top),
                    int(rect.right),
                    int(rect.bottom),
                ),
                all_screens=True,
            )
        except OSError:
            shot = None
        background = _screen_background(shot)

        session_name = self.current_chat_name() or ""
        session_type = self.session_type()
        messages: list[BaseMessage] = []
        for index, item in enumerate(items):
            msg = self._parse_message(
                item=item,
                index=index,
                session_name=session_name,
                session_type=session_type,
                list_rect=rect,
                shot=shot,
                background=background,
            )
            messages.append(msg)
        self._mark_at_me(messages, my_names)
        self._resolve_group_senders(messages, rect, shot)
        return messages

    def read_messages_until_anchor(
        self,
        anchor_keys: Sequence[str],
        *,
        my_names: Sequence[str] = (),
        max_scroll_rounds: int = MAX_ANCHOR_SCROLL_ROUNDS,
        scroll_wait: float = ANCHOR_SCROLL_WAIT,
        timeout_seconds: float = ANCHOR_RECOVERY_TIMEOUT,
    ) -> Optional[list[BaseMessage]]:
        """向上回看消息窗口，直到重新找到旧锚点并返回锚点后的完整序列。

        UIA 的消息列表是虚拟化的。活跃群在两次轮询间可能把旧锚点挤出一屏。
        这里用单格滚动保留相邻快照，只有相邻快照存在重叠时才允许拼接，
        避免把中间不连续的内容误当成完整消息序列。
        """

        if not anchor_keys:
            return None

        anchors = list(anchor_keys)
        my_names = list(my_names)
        msg_list = self.message_list
        history = self.get_all_messages(my_names=my_names)
        history_keys = [_message_key(msg) for msg in history]
        previous_keys = history_keys
        found = _find_suffix(history_keys, anchors)[0] >= 0
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        stalled_rounds = 0

        try:
            for _ in range(max(0, max_scroll_rounds)):
                if found or time.monotonic() >= deadline:
                    break

                msg_list.WheelUp(wheelTimes=1, waitTime=max(0.02, scroll_wait))
                older = self.get_all_messages(my_names=my_names)
                older_keys = [_message_key(msg) for msg in older]
                if not older or older_keys == previous_keys:
                    # UIA 的单格滚轮偶尔不产生新快照，多等几轮再放弃。
                    stalled_rounds += 1
                    if stalled_rounds >= 3:
                        break
                    continue

                overlap = _stitch_overlap(older_keys, history_keys)
                if overlap == 0:
                    # 相邻快照不连续时无法安全重建完整顺序，交给调用方暴露缺口。
                    break

                stalled_rounds = 0
                history = older[: len(older) - overlap] + history
                history_keys = [_message_key(msg) for msg in history]
                found = _find_suffix(history_keys, anchors)[0] >= 0
                previous_keys = older_keys
        finally:
            self._scroll_message_list_to_bottom(msg_list, wait_time=scroll_wait)

        if not found:
            return None

        # 回溯期间新消息可能已经到达；滚回底部后尝试把最终可见尾部接上。
        final = self.get_all_messages(my_names=my_names)
        final_keys = [_message_key(msg) for msg in final]
        overlap = _stitch_overlap(history_keys, final_keys)
        if overlap:
            messages = history + final[overlap:]
        else:
            # 尾部不连续时不在本次强行拼接；下一轮会以 history 为锚点继续补齐。
            messages = history
        return messages

    def _scroll_message_list_to_bottom(
        self,
        msg_list: uia.ListControl,
        *,
        wait_time: float = BOTTOM_SCROLL_WAIT,
    ) -> None:
        """把虚拟化消息列表滚回底部，保证监听器下一轮仍然从最新消息读起。"""

        for _ in range(MAX_ANCHOR_SCROLL_ROUNDS):
            before = self._visible_fingerprints(msg_list)
            if msg_list.SetFocus() and msg_list.IsKeyboardFocusable:
                # 焦点在消息列表上按 End 是微信当前更稳定的“跳到最新页”方式。
                msg_list.SendKeys("{END}", interval=0.02, waitTime=max(0.02, wait_time))
            else:
                msg_list.WheelDown(wheelTimes=3, waitTime=max(0.02, wait_time))
            after = self._visible_fingerprints(msg_list)
            if before == after:
                break

    @staticmethod
    def _visible_fingerprints(msg_list: uia.ListControl) -> list[tuple[str, str]]:
        return [
            (item.ClassName or "", item.Name or "")
            for item in msg_list.GetChildren()
            if isinstance(item, uia.ListItemControl)
        ]

    def _parse_message(
        self,
        *,
        item: uia.ListItemControl,
        index: int,
        session_name: str,
        session_type: Optional[str],
        list_rect,
        shot,
        background,
    ) -> BaseMessage:
        class_name = item.ClassName or ""
        content = item.Name or ""
        rect = item.BoundingRectangle
        rect_tuple = (
            int(rect.left),
            int(rect.top),
            int(rect.right),
            int(rect.bottom),
        )
        runtime_id = "".join(str(v) for v in item.GetRuntimeId())

        if class_name == sel.MESSAGE_TIME_ITEM_CLASS and _TIME_RE.search(content):
            return TimeMessage(
                session_name=session_name,
                sender=MessageAttr.TIME,
                content=content,
                attr=MessageAttr.TIME,
                type=TimeMessage.type,
                index=index,
                runtime_id=runtime_id,
                rect=rect_tuple,
            )

        if class_name == sel.MESSAGE_TEXT_ITEM_CLASS:
            cls = TextMessage
        elif class_name == sel.MESSAGE_BUBBLE_ITEM_CLASS:
            # MVP 明确不解析文件/图片，这里保留 Name 并保守标记 other。
            cls = OtherMessage
        else:
            cls = SystemMessage

        parsed_content = content
        fallback_sender = None
        if session_type == SessionType.GROUP:
            sender, parsed_content, fallback_sender = _split_group_content(content)

        attr = self._detect_attr(
            item=item,
            list_rect=list_rect,
            shot=shot,
            background=background,
        )
        if attr != MessageAttr.SELF:
            sender = fallback_sender or self._friend_sender(
                session_name=session_name,
                session_type=session_type,
                content=parsed_content,
            )
        else:
            sender = "Self"
        return cls(
            session_name=session_name,
            sender=sender,
            content=parsed_content,
            attr=attr,
            type=cls.type,
            index=index,
            automation_id=item.AutomationId or "",
            runtime_id=runtime_id,
            rect=rect_tuple,
        )

    def _detect_attr(self, *, item, list_rect, shot, background) -> str:
        """用非背景像素的水平分布判断 self/friend。

        微信 4.x 气泡项本身占满整行，UIA 属性不带左右信息；
        截图分析不假设背景为白色，而是取消息区众数色。
        """

        if shot is None:
            return MessageAttr.FRIEND
        left, right = _content_columns(
            shot,
            item.BoundingRectangle,
            list_rect,
            background,
        )
        if left is None or right is None:
            return MessageAttr.FRIEND
        center_x = (left + right) / 2
        list_width = list_rect.right - list_rect.left
        if list_width <= 0:
            return MessageAttr.FRIEND
        ratio = (center_x - list_rect.left) / list_width
        return MessageAttr.SELF if ratio >= 0.55 else MessageAttr.FRIEND

    def _friend_sender(
        self,
        *,
        session_name: str,
        session_type: Optional[str],
        content: str,
    ) -> str:
        if session_type == SessionType.GROUP:
            return "unknown"
        return session_name

    def _mark_at_me(
        self, messages: Iterable[BaseMessage], my_names: Sequence[str]
    ) -> None:
        names = {name for name in my_names if name}
        if not names:
            return
        for msg in messages:
            if msg.type != "text":
                continue
            mentioned = {
                match.group(1) for match in _ME_RE.finditer(msg.content)
            }
            msg.is_at_me = bool(mentioned & names)

    def _resolve_group_senders(self, messages, list_rect, shot) -> None:
        """群聊 friend 文本用消息项顶部像素行还原发送者昵称。"""

        if shot is None:
            return
        targets = [
            msg
            for msg in messages
            if msg.type == "text"
            and msg.attr == MessageAttr.FRIEND
            and getattr(msg, "rect", None)
        ]
        if not targets:
            return

        images = {}
        for msg in targets:
            top = max(0, msg.rect[1] - int(list_rect.top))
            bottom = min(shot.height, top + 36)
            if bottom <= top:
                continue
            images[str(msg.index)] = shot.crop((0, top, shot.width, bottom))

        for key, text in recognize_images(images).items():
            sender = " ".join(text.split()).strip()
            if sender:
                msg = next(item for item in targets if str(item.index) == key)
                msg.sender = sender


def _screen_background(shot):
    if shot is None:
        return None
    return Counter(shot.getdata()).most_common(1)[0][0]


def _split_group_content(raw: str) -> tuple[str, str, Optional[str]]:
    """4.1.15.56 群文本消息暂按“昵称行 + 内容行”解析。"""

    if "\n" not in raw:
        return raw, raw, None
    sender, rest = raw.split("\n", 1)
    if not sender.strip():
        return raw, raw, None
    return sender.strip(), rest, sender.strip()


def _content_columns(shot, item_rect, list_rect, background):
    """返回 item 内非背景像素的最小/最大 X 坐标（屏幕坐标）。"""

    left = max(int(item_rect.left), int(list_rect.left))
    top = max(int(item_rect.top), int(list_rect.top))
    right = min(int(item_rect.right), int(list_rect.right))
    bottom = min(int(item_rect.bottom), int(list_rect.bottom))
    if right <= left or bottom <= top:
        return None, None

    shot_left, shot_top = shot.size
    x_positions: list[int] = []
    bg_r, bg_g, bg_b = background[:3]
    for y in range(top, bottom, 2):
        for x in range(left, right, 2):
            pixel = shot.getpixel((x - int(list_rect.left), y - int(list_rect.top)))
            if len(pixel) == 4:
                pixel = pixel[:3]
            if (
                abs(pixel[0] - bg_r) > 18
                or abs(pixel[1] - bg_g) > 18
                or abs(pixel[2] - bg_b) > 18
            ):
                x_positions.append(x)
    if not x_positions:
        return None, None
    return min(x_positions), max(x_positions)
