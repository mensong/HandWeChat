# -*- coding: utf-8 -*-
"""HTTP API service for the current WeChat chat."""

from __future__ import annotations

import io
import logging
import os
import threading
import ctypes
import time
import uuid
from ctypes import wintypes
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from PIL import Image

from handwechat.api.config import (
    CONFIG_FIELDS,
    ApiConfig,
    config_field_specs,
    save_api_config,
)
from handwechat.api.message_buffer import BufferedMessage, MessageBuffer
from handwechat.api.send_tasks import SendTask, SendTaskStore
from handwechat.errors import HWError, WeChatOfflineError
from handwechat.ocr_calibration import (
    OcrCalibration,
    load_ocr_calibration,
    save_ocr_calibration,
)
from handwechat.paths import app_data_path, app_path
from handwechat.sending import MAX_IMAGE_BYTES, SendPayload


class SendTaskNotFoundError(HWError):
    """发送任务不存在或已被清理。"""


class OcrChannelNotRunningError(HWError):
    """OCR 通道未启动。"""


class LogNotFoundError(HWError):
    """日志文件不存在或无法读取。"""


class ListenerRunningError(HWError):
    """OCR 监听运行中，不允许修改配置。"""

    status = 409
    code = "LISTENER_RUNNING"


class InvalidConfigError(HWError):
    """API 配置校验失败。"""

    status = 400
    code = "INVALID_CONFIG"


class InvalidCalibrationError(HWError):
    """OCR 标定校验失败。"""

    status = 400
    code = "INVALID_CALIBRATION"


class CalibrationCaptureNotFoundError(HWError):
    """标定截图不存在或已失效。"""

    status = 404
    code = "CAPTURE_NOT_FOUND"


class CaptureBlockedError(HWError):
    """微信窗口被其他窗口遮挡，无法截图。"""

    status = 409
    code = "CAPTURE_BLOCKED"


class InvalidImageError(HWError):
    """图片无法识别或格式不支持。"""

    status = 400
    code = "INVALID_IMAGE"


class ImageTooLargeError(InvalidImageError):
    """图片超过大小上限。"""

    status = 413
    code = "IMAGE_TOO_LARGE"


_SEND_CACHE_NAME = "send_cache"
_SEND_CACHE_TTL_SECONDS = 600
_RAW_IMAGE_SUFFIX = {"PNG": ".png", "JPEG": ".jpg", "BMP": ".bmp", "GIF": ".gif"}


def _prune_send_cache(directory: Path, ttl_seconds: int) -> None:
    cutoff = time.time() - ttl_seconds
    try:
        for item in directory.iterdir():
            if item.is_file() and item.stat().st_mtime < cutoff:
                item.unlink(missing_ok=True)
    except OSError:
        pass


def _save_image(data: bytes, directory: Path) -> Path:
    """校验图片并落盘；GIF 等原始格式原样保存，其他格式转 PNG。"""

    try:
        with Image.open(io.BytesIO(data)) as image:
            image_format = (image.format or "").upper()
            if image_format in _RAW_IMAGE_SUFFIX:
                image.verify()
                path = directory / f"{uuid.uuid4().hex}{_RAW_IMAGE_SUFFIX[image_format]}"
                path.write_bytes(data)
                return path
            image.load()
            converted = image.convert(
                "RGBA" if image.mode in ("RGBA", "LA", "P") else "RGB"
            )
    except Exception as exc:
        raise InvalidImageError("无法识别的图片内容") from exc

    path = directory / f"{uuid.uuid4().hex}.png"
    try:
        converted.save(path, format="PNG")
    except OSError as exc:
        raise InvalidImageError(f"图片缓存写入失败：{exc}") from exc
    return path


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
        self._calibration_capture: Optional[dict] = None
        self._capture_lock = threading.Lock()

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

    def listener_running(self) -> bool:
        channel = self.ocr_channel
        return bool(channel is not None and channel.running)

    def _require_listener_stopped(self) -> None:
        if self.listener_running():
            raise ListenerRunningError("OCR 监听运行中，请先停止监听再修改配置")

    # -- 配置 ---------------------------------------------------------------

    def get_config(self) -> dict:
        path = app_data_path("api_config.json")
        exists = path.exists()
        values = ApiConfig()
        error = None
        if exists:
            try:
                values = ApiConfig.load(path)
            except (OSError, ValueError) as exc:
                error = f"配置文件读取失败，当前显示默认值：{exc}"
        return {
            "path": str(path),
            "exists": exists,
            "error": error,
            "values": values.to_dict(),
            "defaults": ApiConfig().to_dict(),
            "fields": config_field_specs(),
        }

    def save_config(self, values: dict) -> dict:
        self._require_listener_stopped()
        path = app_data_path("api_config.json")
        before = asdict(self.config)
        try:
            config = save_api_config(path, values)
        except ValueError as exc:
            raise InvalidConfigError(str(exc)) from exc
        saved_values = config.to_dict()
        config.log_dir = str(app_path(config.log_dir))
        self.config.__dict__.update(asdict(config))
        after = asdict(config)
        restart_required = any(
            field.restart_required and before.get(field.key) != after.get(field.key)
            for field in CONFIG_FIELDS
        )
        return {
            "saved": True,
            "path": str(path),
            "values": saved_values,
            "restart_required": restart_required,
        }

    # -- OCR 区域标定 -------------------------------------------------------

    def get_ocr_calibration(self) -> dict:
        path = app_data_path("ocr_calibration.json")
        exists = path.exists()
        error = None
        try:
            calibration = load_ocr_calibration(path)
        except (OSError, ValueError) as exc:
            calibration = OcrCalibration()
            error = f"标定文件读取失败，当前显示默认值：{exc}"
        return {
            "path": str(path),
            "exists": exists,
            "error": error,
            "calibration": calibration.to_dict(),
            "defaults": OcrCalibration().to_dict(),
        }

    def capture_ocr_screenshot(self) -> dict:
        self._require_listener_stopped()
        from handwechat.ocr_capture import (
            WindowCoveredError,
            capture_wechat_screenshot,
        )

        try:
            capture = capture_wechat_screenshot()
        except WindowCoveredError as exc:
            raise CaptureBlockedError(str(exc)) from exc
        except RuntimeError as exc:
            raise WeChatOfflineError(str(exc)) from exc
        buffer = io.BytesIO()
        capture.image.save(buffer, format="PNG")
        capture_id = uuid.uuid4().hex
        with self._capture_lock:
            self._calibration_capture = {
                "id": capture_id,
                "image": capture.image,
                "png": buffer.getvalue(),
                "window_width": capture.window_width,
                "window_height": capture.window_height,
                "maximized": capture.maximized,
            }
        return {
            "capture_id": capture_id,
            "width": capture.image.width,
            "height": capture.image.height,
            "window_width": capture.window_width,
            "window_height": capture.window_height,
            "maximized": capture.maximized,
            "image_url": f"/api/v1/config/ocr_calibration/capture/{capture_id}",
        }

    def _require_capture(self, capture_id: str) -> dict:
        with self._capture_lock:
            entry = self._calibration_capture
        if entry is None or entry["id"] != capture_id:
            raise CalibrationCaptureNotFoundError("标定截图已失效，请重新截图")
        return entry

    def capture_image(self, capture_id: str) -> bytes:
        return self._require_capture(capture_id)["png"]

    def sample_ocr_color(
        self, capture_id: str, x_ratio: float, y_ratio: float
    ) -> dict:
        entry = self._require_capture(capture_id)
        image = entry["image"]
        px = min(max(0, int(round(x_ratio * image.width))), image.width - 1)
        py = min(max(0, int(round(y_ratio * image.height))), image.height - 1)
        color = image.getpixel((px, py))
        if not isinstance(color, tuple) or len(color) < 3:
            raise InvalidCalibrationError("无法读取该点颜色，请重新点击")
        return {
            "rgb": [int(color[0]), int(color[1]), int(color[2])],
            "x": px,
            "y": py,
        }

    def save_ocr_calibration(
        self,
        capture_id: str,
        *,
        title_box: list[float],
        chat_box: list[float],
        input_x_ratio: float,
        input_y_ratio: float,
        palette: dict,
    ) -> dict:
        self._require_listener_stopped()
        entry = self._require_capture(capture_id)
        if not palette:
            raise InvalidCalibrationError("缺少三种底色的取色结果")
        payload = {
            "version": 3,
            "input_x_ratio": input_x_ratio,
            "input_y_ratio": input_y_ratio,
            "title_box": list(title_box),
            "chat_box": list(chat_box),
            "window_size": [entry["window_width"], entry["window_height"]],
            "window_maximized": entry["maximized"],
            "palette": palette,
        }
        try:
            calibration = OcrCalibration.from_dict(payload)
        except ValueError as exc:
            raise InvalidCalibrationError(str(exc)) from exc
        if calibration.palette is not None:
            problem = calibration.palette.distance_problem()
            if problem:
                raise InvalidCalibrationError(problem)
        path = app_data_path("ocr_calibration.json")
        try:
            save_ocr_calibration(calibration, path)
        except OSError as exc:
            raise InvalidCalibrationError(f"标定文件写入失败：{exc}") from exc
        return {
            "saved": True,
            "path": str(path),
            "calibration": calibration.to_dict(),
        }

    def send(self, text: str) -> SendTask:
        channel = self._require_ocr_channel()
        payload = SendPayload(text=text)
        task = self.send_tasks.create(payload.display_text())
        channel.send(payload, task_id=task.task_id)
        return task

    def send_image(
        self,
        data: bytes,
        *,
        filename: str = "",
        text: str = "",
        image_name: str = "",
    ) -> SendTask:
        channel = self._require_ocr_channel()
        if not data:
            raise InvalidImageError("图片内容为空")
        if len(data) > MAX_IMAGE_BYTES:
            raise ImageTooLargeError(
                f"图片超过 {MAX_IMAGE_BYTES // (1024 * 1024)}MB 上限"
            )
        directory = app_data_path(_SEND_CACHE_NAME)
        directory.mkdir(parents=True, exist_ok=True)
        _prune_send_cache(directory, _SEND_CACHE_TTL_SECONDS)
        image_path = _save_image(data, directory)
        display_name = (
            Path(image_name).name or Path(filename).name or image_path.name
        )
        payload = SendPayload(
            text=text,
            image_path=image_path,
            image_name=display_name,
        )
        task = self.send_tasks.create(payload.display_text())
        channel.send(payload, task_id=task.task_id)
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
            # 一次启动绑定一个会话：重启后不允许把上个会话的消息带出来。
            self.message_buffer.clear()
            channel = factory(
                poll_interval=self.config.ocr_poll_interval,
                max_send_per_minute=self.config.ocr_max_send_per_minute,
                history_size=self.config.ocr_history_size,
                state_path=app_data_path("ocr_listener_state.json"),
                input_x_ratio=calibration.input_x_ratio,
                input_y_ratio=calibration.input_y_ratio,
                title_box=calibration.title_box,
                chat_box=calibration.chat_box,
                window_size=calibration.window_size,
                window_maximized=calibration.window_maximized,
                palette=calibration.palette,
                on_message=self._on_ocr_message,
                on_error=self._on_ocr_error,
                on_send_started=self._on_ocr_send_started,
                on_send_completed=self._on_ocr_send_completed,
            )
            try:
                # start() 会阻塞到首帧识别出会话名；失败会抛 OcrBindError。
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
