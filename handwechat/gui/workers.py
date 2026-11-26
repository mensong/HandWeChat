# -*- coding: utf-8 -*-
"""Qt thread bridge for the current-chat OCR channel."""

from __future__ import annotations

from typing import Any, Callable, Optional
from pathlib import Path
import uuid

from PySide6.QtCore import QObject, QThread, Signal

from handwechat.models import BaseMessage
from handwechat.ocr_calibration import load_ocr_calibration
from handwechat.ocr_channel import OcrChannel, configure_ocr_logging
from handwechat.paths import app_data_path
from handwechat.runtime import WeChatRuntime
from handwechat.sending import SendPayload


class FunctionWorker(QThread):
    """Run one function and emit success or failure."""

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, func: Callable[[], Any], parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._func = func

    def run(self) -> None:
        try:
            self.succeeded.emit(self._func())
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class ListenerBridge(QObject):
    """Translate OCR callbacks into main-thread Qt signals."""

    message_received = Signal(object)
    listener_error = Signal(str, object)
    state_changed = Signal(bool)
    send_started = Signal(str, str)
    send_completed = Signal(str, str, bool, str)

    def emit_message(self, message: BaseMessage) -> None:
        self.message_received.emit(message)

    def emit_error(self, context: str, exc: Exception) -> None:
        self.listener_error.emit(context, exc)

    def emit_state(self, running: bool) -> None:
        self.state_changed.emit(running)

    def emit_send_started(self, task_id: str, text: str) -> None:
        self.send_started.emit(task_id, text)

    def emit_send_completed(
        self, task_id: str, text: str, ok: bool, detail: str
    ) -> None:
        self.send_completed.emit(task_id, text, ok, detail)


class WeChatController(QObject):
    """GUI-facing facade over one current-chat OCR channel."""

    def __init__(
        self,
        *,
        poll_interval: float = 2.0,
        max_send_per_minute: int = 20,
        history_size: int = 10,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.bridge = ListenerBridge(self)
        configure_ocr_logging(app_data_path("logs") / "ocr.log")
        calibration = load_ocr_calibration(
            app_data_path("ocr_calibration.json")
        )
        self.channel = OcrChannel(
            poll_interval=poll_interval,
            max_send_per_minute=max_send_per_minute,
            history_size=history_size,
            state_path=app_data_path("ocr_listener_state.json"),
            input_x_ratio=calibration.input_x_ratio,
            input_y_ratio=calibration.input_y_ratio,
            title_box=calibration.title_box,
            chat_box=calibration.chat_box,
            window_size=calibration.window_size,
            window_maximized=calibration.window_maximized,
            palette=calibration.palette,
            on_message=self.bridge.emit_message,
            on_error=self.bridge.emit_error,
            on_send_started=self.bridge.emit_send_started,
            on_send_completed=self.bridge.emit_send_completed,
        )
        self.runtime = WeChatRuntime(
            self.channel,
            on_state_changed=self.bridge.emit_state,
        )

    @property
    def active_chat(self) -> str:
        return self.channel.chat_title

    @property
    def active_chat_type(self) -> Optional[str]:
        return self.channel.chat_type

    def status(self) -> dict:
        return self.channel.status()

    def start_listener(self) -> None:
        # Calibration may have been saved after this controller was created.
        calibration = load_ocr_calibration(
            app_data_path("ocr_calibration.json")
        )
        self.channel.set_palette(calibration.palette)
        self.runtime.start_listener()

    def stop_listener(self) -> None:
        self.runtime.stop_listener()

    def send(self, text: str, image_path: str | None = None) -> str:
        task_id = f"gui_{uuid.uuid4().hex}"
        path = Path(image_path) if image_path else None
        payload = SendPayload(
            text=text,
            image_path=path,
            image_name=path.name if path else "",
        )
        self.channel.send(payload, task_id=task_id)
        return task_id

    def shutdown(self) -> None:
        self.runtime.shutdown()
