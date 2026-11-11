# -*- coding: utf-8 -*-
"""GUI 与 handwechat 后台能力之间的线程桥接。"""

from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QThread, Signal

import uiautomation as uia

from handwechat.models import BaseMessage
from handwechat.runtime import WeChatRuntime
from handwechat.wechat import WeChat


class FunctionWorker(QThread):
    """执行一个耗时函数；成功/失败分别发信号。"""

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, func: Callable[[], Any], parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._func = func

    def run(self) -> None:
        with uia.UIAutomationInitializerInThread():
            try:
                self.succeeded.emit(self._func())
            except Exception as exc:
                self.failed.emit(f"{type(exc).__name__}: {exc}")


class ListenerBridge(QObject):
    """把运行时线程回调转成 GUI 主线程信号。"""

    message_received = Signal(str, object)
    listener_error = Signal(str, object)
    state_changed = Signal(bool)
    health_event = Signal(str, str)
    health_changed = Signal(bool, str)
    send_completed = Signal(str, str, bool, str)
    queue_changed = Signal(int)

    def emit_message(self, session: str, message: BaseMessage) -> None:
        self.message_received.emit(session, message)

    def emit_error(self, context: str, exc: Exception) -> None:
        self.listener_error.emit(context, exc)

    def emit_state(self, running: bool) -> None:
        self.state_changed.emit(running)

    def emit_health_event(self, kind: str, message: str) -> None:
        self.health_event.emit(kind, message)

    def emit_health_changed(self, ok: bool, detail: str) -> None:
        self.health_changed.emit(ok, detail)

    def emit_send_completed(self, session: str, text: str, ok: bool, detail: str) -> None:
        self.send_completed.emit(session, text, ok, detail)

    def emit_queue_changed(self, size: int) -> None:
        self.queue_changed.emit(size)


class WeChatController(QObject):
    """GUI 层的微信操作门面，屏蔽线程细节。"""

    def __init__(
        self,
        wx: WeChat,
        *,
        poll_interval: float = 2.0,
        my_names: Optional[list[str]] = None,
        max_send_per_minute: int = 20,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.wx = wx
        self.bridge = ListenerBridge(self)
        self.runtime = WeChatRuntime(
            wx,
            poll_interval=poll_interval,
            my_names=my_names,
            on_message=self.bridge.emit_message,
            on_error=self.bridge.emit_error,
            on_state_changed=self.bridge.emit_state,
            on_health_event=self.bridge.emit_health_event,
            on_health_changed=self.bridge.emit_health_changed,
            on_send_completed=self.bridge.emit_send_completed,
            on_queue_changed=self.bridge.emit_queue_changed,
        )
        self.listener = self.runtime.listener
        self.send_queue = self.runtime.send_queue
        self.health_monitor = self.runtime.health_monitor

    def start_listener(self) -> None:
        self.runtime.start_listener()

    def stop_listener(self) -> None:
        self.runtime.stop_listener()

    def start_send_queue(self) -> None:
        self.runtime.start_send_queue()

    def stop_send_queue(self) -> None:
        self.runtime.stop_send_queue()

    def shutdown(self) -> None:
        self.runtime.shutdown()
