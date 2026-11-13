# -*- coding: utf-8 -*-
"""HTTP API service for the current WeChat chat."""

from __future__ import annotations

import logging
import threading
import ctypes
from ctypes import wintypes
from typing import Callable, Optional

from handwechat.api.config import ApiConfig
from handwechat.api.message_buffer import BufferedMessage, MessageBuffer
from handwechat.api.send_tasks import SendTask, SendTaskStore
from handwechat.errors import HWError, WeChatOfflineError
from handwechat.ocr_calibration import OcrCalibration, load_ocr_calibration
from handwechat.paths import app_data_path


class SendTaskNotFoundError(HWError):
    """发送任务不存在或已被清理。"""


class OcrChannelNotRunningError(HWError):
    """OCR 通道未启动。"""


class ApiService:
    """Own the API process's single current-chat OCR channel."""

    def __init__(
        self,
        config: Optional[ApiConfig] = None,
        *,
        message_buffer_factory: Callable[..., MessageBuffer] = MessageBuffer,
        send_task_store_factory: Callable[..., SendTaskStore] = SendTaskStore,
        ocr_channel_factory: Optional[Callable[..., object]] = None,
    ) -> None:
        self.config = config or ApiConfig()
        self._message_buffer_factory = message_buffer_factory
        self._send_task_store_factory = send_task_store_factory
        self._ocr_channel_factory = ocr_channel_factory
        self.message_buffer = message_buffer_factory(self.config.message_buffer_size)
        self.send_tasks = send_task_store_factory(
            self.config.send_task_retention_minutes
        )
        self._ocr_channel: Optional[object] = None
        self._ocr_lock = threading.RLock()
        self._last_error: Optional[str] = None

    @property
    def ocr_channel(self) -> Optional[object]:
        with self._ocr_lock:
            return self._ocr_channel

    @property
    def attached(self) -> bool:
        return self._find_wechat_window() is not None

    def health(self) -> dict:
        located = self._find_wechat_window()
        online = located is not None
        hwnd, pid = (None, None)
        if located is not None:
            hwnd, pid = located

        channel = self.ocr_channel
        last_error = self._last_error
        try:
            if channel is not None and channel.last_error:
                last_error = channel.last_error
        except Exception:
            pass

        listener_running = bool(channel is not None and channel.running)
        return {
            "status": "ok" if online else "offline",
            "wechat_pid": pid,
            "wechat_hwnd": f"{hwnd:#x}" if hwnd is not None else None,
            "wechat_version": "",
            "online": online,
            "listener_running": listener_running,
            "send_queue_size": channel.send_queue.size if channel else 0,
            "attached": online,
            "chat_title": channel.chat_title if channel else "",
            "chat_type": channel.chat_type if channel else None,
            "message_buffer_size": self.message_buffer.size,
            "send_task_active": self.send_tasks.active_count(),
            "poll_interval": self.config.ocr_poll_interval,
            "max_send_per_minute": self.config.ocr_max_send_per_minute,
            "last_error": last_error,
        }

    def attach(self) -> dict:
        hwnd, pid = self._require_wechat_window()
        self._last_error = None
        return {
            "status": "attached",
            "wechat_pid": pid,
            "wechat_hwnd": f"{hwnd:#x}",
        }

    def start_listener(self) -> None:
        self._require_wechat_window()
        self.start_ocr()

    def stop_listener(self) -> None:
        self.stop_ocr()

    def get_messages(
        self,
        cursor: int,
        *,
        limit: int = 100,
        timeout: float = 0.0,
    ) -> tuple[list[BufferedMessage], int, bool]:
        return self.message_buffer.get_after(cursor, limit=limit, timeout=timeout)

    def send(self, text: str) -> SendTask:
        channel = self._require_ocr_channel()
        task = self.send_tasks.create(text)
        channel.send(text, task_id=task.task_id)
        return task

    def get_send_task(self, task_id: str) -> SendTask:
        task = self.send_tasks.get(task_id)
        if task is None:
            raise SendTaskNotFoundError(f"发送任务不存在：{task_id!r}")
        return task

    def start_ocr(self) -> dict:
        with self._ocr_lock:
            channel = self._ocr_channel
            if channel is not None and channel.running:
                return channel.status()

            if channel is not None:
                channel.stop(timeout=4.0)
                self._ocr_channel = None

            factory = self._ocr_channel_factory
            if factory is None:
                from handwechat.ocr_channel import OcrChannel

                factory = OcrChannel

            calibration = self._load_ocr_calibration()
            channel = factory(
                poll_interval=self.config.ocr_poll_interval,
                max_send_per_minute=self.config.ocr_max_send_per_minute,
                state_path=app_data_path("ocr_listener_state.json"),
                input_x_ratio=calibration.input_x_ratio,
                input_y_ratio=calibration.input_y_ratio,
                title_box=calibration.title_box,
            chat_box=calibration.chat_box,
            on_message=self._on_ocr_message,
            on_chat_changed=self._on_ocr_chat_changed,
            on_error=self._on_ocr_error,
                on_send_started=self._on_ocr_send_started,
                on_send_completed=self._on_ocr_send_completed,
            )
            try:
                channel.start()
            except Exception:
                channel.stop(timeout=2.0)
                self._ocr_channel = None
                raise
            self._ocr_channel = channel
            return channel.status()

    def stop_ocr(self) -> dict:
        with self._ocr_lock:
            channel = self._ocr_channel
            if channel is None:
                return {"status": "stopped"}
            channel.stop(timeout=4.0)
            self._ocr_channel = None
            return {"status": "stopped"}

    def get_ocr_status(self) -> dict:
        channel = self.ocr_channel
        if channel is None:
            return {
                "running": False,
                "chat_title": "",
                "chat_type": None,
                "capture_queue_size": 0,
                "parse_queue_size": 0,
                "send_queue_size": 0,
                "poll_interval": self.config.ocr_poll_interval,
                "max_send_per_minute": self.config.ocr_max_send_per_minute,
                "last_frame_signature": None,
                "last_accepted_signature": None,
                "ocr_skip_identical_count": 0,
                "last_error": None,
            }
        return channel.status()

    def shutdown(self) -> None:
        with self._ocr_lock:
            channel = self._ocr_channel
            if channel is not None:
                channel.stop(timeout=4.0)
                self._ocr_channel = None

    @staticmethod
    def _load_ocr_calibration() -> OcrCalibration:
        path = app_data_path("ocr_calibration.json")
        try:
            return load_ocr_calibration(path)
        except (OSError, ValueError) as exc:
            logging.getLogger(__name__).warning(
                "OCR 标定文件读取失败，使用默认区域：%s", exc
            )
            return OcrCalibration()

    @staticmethod
    def _find_wechat_window():
        from handwechat.ocr_channel import _find_wechat_window

        located = _find_wechat_window()
        if located is None:
            return None
        hwnd, _rect = located
        pid = wintypes.DWORD(0)
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return hwnd, pid.value or None

    @classmethod
    def _require_wechat_window(cls):
        located = cls._find_wechat_window()
        if located is None:
            raise WeChatOfflineError("未找到微信主窗口，请确认微信已启动并登录")
        return located

    def _require_ocr_channel(self):
        channel = self.ocr_channel
        if channel is None or not channel.running:
            raise OcrChannelNotRunningError(
                "OCR 通道未启动，请先调用 POST /api/v1/listener/start"
            )
        return channel

    def _on_ocr_message(self, message) -> None:
        self.message_buffer.append(message)

    def _on_ocr_chat_changed(self, _chat_title: str) -> None:
        self.message_buffer.clear()

    def _on_ocr_error(self, context: str, exc: Exception) -> None:
        text = f"{context}: {type(exc).__name__}: {exc}"
        self._last_error = text
        logging.getLogger(__name__).warning(text)

    def _on_ocr_send_started(self, task_id: str, _text: str) -> None:
        self.send_tasks.mark_started(task_id)

    def _on_ocr_send_completed(
        self,
        task_id: str,
        _text: str,
        ok: bool,
        detail: str,
    ) -> None:
        self.send_tasks.mark_completed(task_id, ok=ok, detail=detail)
