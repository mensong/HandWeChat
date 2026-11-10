# -*- coding: utf-8 -*-
"""GUI 与 handwechat 后台能力之间的线程桥接。"""

from __future__ import annotations

import queue
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QThread, Signal

import uiautomation as uia

from handwechat.listener import MessageListener
from handwechat.models import BaseMessage
from handwechat.health import HealthMonitor
from handwechat.rate_limit import TokenBucketRateLimiter
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


class SendWorker(QThread):
    """串行发送队列，避免发送动作并发争抢微信 UI。"""

    send_completed = Signal(str, str, bool, str)
    queue_changed = Signal(int)

    def __init__(
        self,
        wx: WeChat,
        parent: Optional[QObject] = None,
        *,
        max_per_minute: int = 20,
    ) -> None:
        super().__init__(parent)
        self._wx = wx
        self._rate_limiter = TokenBucketRateLimiter(max_per_minute)
        self._queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self._stop_requested = False

    def submit(self, session: str, text: str) -> None:
        self._queue.put((session, text))
        self.queue_changed.emit(self._queue.qsize())

    def set_rate_limit(self, max_per_minute: int) -> None:
        self._rate_limiter.set_max_per_minute(max_per_minute)

    def stop(self) -> None:
        self._stop_requested = True
        self._queue.put(("", ""))

    def run(self) -> None:
        with uia.UIAutomationInitializerInThread():
            while not self._stop_requested:
                try:
                    session, text = self._queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                if self._stop_requested and not session:
                    break
                self.queue_changed.emit(self._queue.qsize())
                try:
                    self._rate_limiter.wait_one()
                    message = self._wx.SendMsg(text, who=session)
                    ok = message is not None
                    detail = "" if ok else "发送后未回读到消息"
                except Exception as exc:
                    ok = False
                    detail = f"{type(exc).__name__}: {exc}"
                self.send_completed.emit(session, text, ok, detail)


class ListenerBridge(QObject):
    """把监听线程回调转成 GUI 主线程信号。"""

    message_received = Signal(str, object)
    listener_error = Signal(str, object)
    state_changed = Signal(bool)
    health_event = Signal(str, str)
    health_changed = Signal(bool, str)

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
        self.listener = MessageListener(
            wx,
            poll_interval=poll_interval,
            my_names=my_names,
            on_message=self.bridge.emit_message,
            on_error=self.bridge.emit_error,
        )
        self.send_worker = SendWorker(
            wx,
            self,
            max_per_minute=max_send_per_minute,
        )
        self.health_monitor = HealthMonitor(
            wx,
            interval=15.0,
            on_event=self.bridge.emit_health_event,
            on_health_changed=self.bridge.emit_health_changed,
        )

    def start_listener(self) -> None:
        self.listener.Start()
        if not self.health_monitor._thread or not self.health_monitor._thread.is_alive():
            self.health_monitor.Start()
        self.bridge.emit_state(True)

    def stop_listener(self) -> None:
        self.listener.Stop()
        self.health_monitor.Stop()
        self.bridge.emit_state(False)

    def start_send_worker(self) -> None:
        if not self.send_worker.isRunning():
            self.send_worker.start()

    def stop_send_worker(self) -> None:
        self.send_worker.stop()
        self.send_worker.wait(3000)

    def shutdown(self) -> None:
        self.stop_listener()
        self.stop_send_worker()

        self.health_monitor.Stop()
        self.bridge.emit_state(False)
