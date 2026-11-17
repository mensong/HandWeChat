# -*- coding: utf-8 -*-
"""HTTP API service for the current WeChat chat."""

from __future__ import annotations

import logging
import os
import threading
import ctypes
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
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


class LogNotFoundError(HWError):
    """日志文件不存在或无法读取。"""


_LOG_SCAN_BYTES = 2 * 1024 * 1024
_LOG_MAX_LINES = 5000


def _tail_lines(
    path: Path, *, line_limit: int, keyword: str
) -> tuple[list[str], int, int, bool, int]:
    """Read the tail of a log file and optionally keep matching lines.

    Returns (lines, scanned_lines, matched_lines, truncated, size). Only the
    last 2 MB are read so a large log cannot exhaust memory.
    """

    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start = max(0, size - _LOG_SCAN_BYTES)
            handle.seek(start)
            raw = handle.read()
    except OSError as exc:
        raise LogNotFoundError(f"日志文件读取失败：{path.name}") from exc

    truncated = start > 0
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if truncated and lines:
        # The first line is very likely cut in half by the byte window.
        lines = lines[1:]
    scanned = len(lines)

    if keyword:
        needle = keyword.casefold()
        lines = [line for line in lines if needle in line.casefold()]
    matched = len(lines)
    return lines[-line_limit:], scanned, matched, truncated, size


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
        history_capacity = 0
        history_size = 0
        try:
            if channel is not None and channel.last_error:
                last_error = channel.last_error
            if channel is not None:
                channel_status = channel.status()
                history_capacity = int(channel_status.get("history_capacity") or 0)
                history_size = int(channel_status.get("history_size") or 0)
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
            "history_capacity": history_capacity,
            "history_size": history_size,
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

    @property
    def message_cursor(self) -> int:
        """Latest process-wide cursor; a WebSocket client starts from here."""

        return self.message_buffer.latest_cursor

    def subscribe_messages(self, callback: Callable[[], None]) -> None:
        self.message_buffer.subscribe(callback)

    def unsubscribe_messages(self, callback: Callable[[], None]) -> None:
        self.message_buffer.unsubscribe(callback)

    def list_logs(self) -> dict:
        log_dir = Path(self.config.log_dir).resolve()
        try:
            entries = sorted(log_dir.iterdir())
        except OSError:
            entries = []

        files = []
        for entry in entries:
            try:
                if not entry.is_file():
                    continue
                stat = entry.stat()
            except OSError:
                continue
            files.append(
                {
                    "name": entry.name,
                    "size": int(stat.st_size),
                    "updated_at": datetime.fromtimestamp(stat.st_mtime)
                    .astimezone()
                    .isoformat(timespec="seconds"),
                }
            )
        return {"log_dir": str(log_dir), "files": files}

    def read_log(self, name: str, *, lines: int = 500, keyword: str = "") -> dict:
        path = self._log_path(name)
        limit = max(1, min(int(lines), _LOG_MAX_LINES))
        rows, scanned, matched, truncated, size = _tail_lines(
            path,
            line_limit=limit,
            keyword=str(keyword).strip(),
        )
        return {
            "name": path.name,
            "size": size,
            "scanned_lines": scanned,
            "matched_lines": matched,
            "truncated": truncated,
            "lines": rows,
        }

    def _log_path(self, name: str) -> Path:
        log_dir = Path(self.config.log_dir).resolve()
        try:
            candidate = (log_dir / name).resolve()
        except OSError as exc:
            raise LogNotFoundError(f"日志文件不存在：{name!r}") from exc
        if candidate.parent != log_dir or not candidate.is_file():
            raise LogNotFoundError(f"日志文件不存在：{name!r}")
        return candidate

    def get_history(self) -> dict:
        channel = self.ocr_channel
        if channel is None:
            return {
                "running": False,
                "capacity": self.config.ocr_history_size,
                "size": 0,
                "items": [],
            }
        snapshot = channel.history_snapshot()
        snapshot["running"] = bool(channel.running)
        return snapshot

    def clear_history(self) -> dict:
        channel = self.ocr_channel
        if channel is None:
            return {"cleared": 0}
        return {"cleared": channel.clear_history()}

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
                history_size=self.config.ocr_history_size,
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
                "history_capacity": self.config.ocr_history_size,
                "history_size": 0,
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
