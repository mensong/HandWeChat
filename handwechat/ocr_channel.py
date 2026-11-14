# -*- coding: utf-8 -*-
"""Pure OCR WeChat channel with decoupled capture and parse queues."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime, timezone
import json
from pathlib import Path
import queue
import re
import threading
import time
from typing import Callable, Optional, Sequence

import numpy as np
from PIL import Image, ImageGrab

from handwechat.errors import HWError
from handwechat.models import BaseMessage, MessageAttr, MessageType, TextMessage
from handwechat.ocr_win import _offline_model_args
from handwechat.paths import ocr_models_dir
from handwechat.rate_limit import TokenBucketRateLimiter


SendCompletedCallback = Callable[[str, str, bool, str], None]
SendStartedCallback = Callable[[str, str], None]
MessageCallback = Callable[[BaseMessage], None]
ChatChangedCallback = Callable[[str], None]
ErrorCallback = Callable[[str, Exception], None]


class OcrEngineUnavailableError(HWError):
    """Raised when the bundled offline OCR models cannot be initialized."""


@dataclass(frozen=True)
class OcrLine:
    text: str
    x0: int
    y0: int
    x1: int
    y1: int
    score: float

    @property
    def center_x(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def center_y(self) -> float:
        return (self.y0 + self.y1) / 2.0

    @property
    def height(self) -> int:
        return max(1, self.y1 - self.y0)


@dataclass(frozen=True)
class OcrMessage:
    attr: str
    text: str
    sender: str


@dataclass(frozen=True)
class CapturedFrame:
    hwnd: int
    left: int
    top: int
    right: int
    bottom: int
    captured_at: float
    image: Image.Image
    signature: str

    @property
    def size(self) -> tuple[int, int]:
        return self.image.size


@dataclass(frozen=True)
class ParsedFrame:
    chat_title: str
    messages: tuple[OcrMessage, ...]
    captured_at: float


@dataclass
class OcrListenState:
    initialized: bool = False
    last_seq: int = 0
    recent_keys: list[str] = field(default_factory=list)
    self_texts: list[str] = field(default_factory=list)
    updated_at: str = ""

    def __post_init__(self) -> None:
        if self.recent_keys is None:
            self.recent_keys = []
        if self.self_texts is None:
            self.self_texts = []

    def to_dict(self) -> dict:
        return {
            "initialized": self.initialized,
            "last_seq": self.last_seq,
            "recent_keys": list(self.recent_keys),
            "self_texts": list(self.self_texts),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "OcrListenState":
        return cls(
            initialized=bool(data.get("initialized", False)),
            last_seq=int(data.get("last_seq") or 0),
            recent_keys=list(data.get("recent_keys") or []),
            self_texts=list(data.get("self_texts") or []),
            updated_at=str(data.get("updated_at") or ""),
        )


def _now_text() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", str(text)).strip().lower()


def _message_key(message: OcrMessage) -> str:
    return "\x1f".join((message.attr, _normalize_text(message.text)))


def _frame_signature(
    image: Image.Image,
    title_box: tuple[int, int, int, int],
    chat_box: tuple[int, int, int, int],
) -> str:
    """Hash only the regions that are fed into OCR.

    The hash covers pixels instead of a timestamp or window position, so moving
    the window does not trigger OCR again when the visible content is unchanged.
    """

    digest = hashlib.sha256()
    digest.update(f"{image.mode}:{image.width}x{image.height}".encode("ascii"))
    for box in (title_box, chat_box):
        crop = image.crop(box)
        digest.update(str(crop.size).encode("ascii"))
        digest.update(crop.tobytes())
    return digest.hexdigest()


def _overlap_length(previous_keys: Sequence[str], current_keys: Sequence[str]) -> int:
    """Return the overlap between the previous key tail and the current prefix.

    WeChat appends visible messages at the bottom and can scroll older messages
    out of view.  Therefore the overlapping history is the tail of the previous
    state and the prefix of the newly parsed frame.
    """

    max_length = min(len(previous_keys), len(current_keys))
    for length in range(max_length, 0, -1):
        if list(previous_keys[-length:]) == list(current_keys[:length]):
            return length
    return 0


_shared_paddle_reader: Optional[PaddleLayoutReader] = None
_shared_paddle_reader_lock = threading.Lock()


def get_paddle_layout_reader() -> PaddleLayoutReader:
    """Reuse one OCR engine across channel restarts."""

    global _shared_paddle_reader
    if _shared_paddle_reader is None:
        with _shared_paddle_reader_lock:
            if _shared_paddle_reader is None:
                _shared_paddle_reader = PaddleLayoutReader()
    return _shared_paddle_reader


class PaddleLayoutReader:
    """Thread-safe wrapper that preserves OCR coordinates."""

    def __init__(self) -> None:
        self._engine = None
        self._lock = threading.Lock()

    def _initialize(self):
        if self._engine is not None:
            return self._engine
        with self._lock:
            if self._engine is not None:
                return self._engine
            args = _offline_model_args()
            if not args:
                missing = ocr_models_dir()
                raise OcrEngineUnavailableError(
                    f"离线 OCR 模型不完整，请确认 {missing}/det,rec,cls 目录"
                )
            try:
                from paddleocr import PaddleOCR
            except (ImportError, OSError) as exc:
                raise OcrEngineUnavailableError(
                    f"无法加载 PaddleOCR：{type(exc).__name__}: {exc}"
                ) from exc
            self._engine = PaddleOCR(
                use_angle_cls=False,
                lang="ch",
                show_log=False,
                **args,
            )
            return self._engine

    def recognize_lines(self, image: Image.Image) -> list[OcrLine]:
        engine = self._initialize()
        rgb = image.convert("RGB")
        scale = _ocr_upscale_factor(rgb.size)
        if scale > 1.0:
            rgb = rgb.resize(
                (max(1, round(rgb.width * scale)), max(1, round(rgb.height * scale))),
                Image.Resampling.LANCZOS,
            )
        bgr = np.asarray(rgb)[:, :, ::-1]
        with self._lock:
            pages = engine.ocr(bgr, cls=False)

        lines: list[OcrLine] = []
        for page in pages or []:
            for item in page or []:
                try:
                    box, (text, score) = item
                    if not text or not str(text).strip():
                        continue
                    xs = [int(point[0] / scale) for point in box]
                    ys = [int(point[1] / scale) for point in box]
                    lines.append(
                        OcrLine(
                            text=str(text).strip(),
                            x0=min(xs),
                            y0=min(ys),
                            x1=max(xs),
                            y1=max(ys),
                            score=float(score),
                        )
                    )
                except (TypeError, ValueError, IndexError):
                    continue
        return sorted(lines, key=lambda line: (line.y0, line.x0))


def _find_wechat_window() -> tuple[int, tuple[int, int, int, int]] | None:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.SetProcessDPIAware()

    result: list[tuple[int, tuple[int, int, int, int]]] = []

    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value.strip()
        if title not in {"微信", "WeChat"}:
            return True

        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        process_path_buffer = ctypes.create_unicode_buffer(1024)
        process_handle = kernel32.OpenProcess(
            0x1000, False, pid.value
        )
        process_name = ""
        if process_handle:
            size = wintypes.DWORD(len(process_path_buffer))
            if kernel32.QueryFullProcessImageNameW(
                process_handle, 0, process_path_buffer, ctypes.byref(size)
            ):
                process_name = Path(process_path_buffer.value).name.lower()
            kernel32.CloseHandle(process_handle)
        if process_name != "weixin.exe":
            return True

        rect = wintypes.RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            result.append(
                (
                    int(hwnd),
                    (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)),
                )
            )
        return True

    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(enum_proc(callback), 0)
    if not result:
        return None
    return result[0]


def _crop_box(
    size: tuple[int, int],
    *,
    left: float,
    top: float,
    right: float = 1.0,
    bottom: float = 1.0,
) -> tuple[int, int, int, int]:
    width, height = size
    x0 = max(0, min(width - 1, int(width * left)))
    y0 = max(0, min(height - 1, int(height * top)))
    x1 = max(x0 + 1, min(width, int(width * right)))
    y1 = max(y0 + 1, min(height, int(height * bottom)))
    return x0, y0, x1, y1


def _ocr_upscale_factor(size: tuple[int, int]) -> float:
    """Small chat crops need a little enlargement for stable recall."""

    width = max(1, int(size[0]))
    if width >= 700:
        return 1.0
    return min(3.0, max(2.0, 700.0 / width))


class OcrSendQueue:
    """Serial queue for OCR send actions."""

    def __init__(
        self,
        sender: Callable[[str], bool],
        *,
        max_per_minute: int = 20,
        on_send_started: Optional[SendStartedCallback] = None,
        on_send_completed: Optional[SendCompletedCallback] = None,
    ) -> None:
        self._sender = sender
        self._rate_limiter = TokenBucketRateLimiter(max_per_minute)
        self._queue: "queue.Queue[tuple[str, str] | None]" = queue.Queue()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._on_send_started = on_send_started
        self._on_send_completed = on_send_completed

    def submit(self, text: str, *, task_id: str) -> None:
        self._queue.put((text, task_id))

    @property
    def size(self) -> int:
        return self._queue.qsize()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="handwechat-ocr-send",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop_event.set()
        self._queue.put(None)
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                break
            text, task_id = item
            if self._on_send_started:
                try:
                    self._on_send_started(task_id, text)
                except Exception:
                    pass
            try:
                self._rate_limiter.wait_one()
                ok = bool(self._sender(text))
                detail = "" if ok else "OCR 回读未找到刚发送的消息"
            except Exception as exc:
                ok = False
                detail = f"{type(exc).__name__}: {exc}"
            if self._on_send_completed:
                try:
                    self._on_send_completed(task_id, text, ok, detail)
                except Exception:
                    pass


class OcrChannel:
    """Current-chat OCR listener and clipboard sender for WeChat 4.x."""

    def __init__(
        self,
        *,
        poll_interval: float = 2.0,
        max_send_per_minute: int = 20,
        state_path: str | Path = "data/ocr_listener_state.json",
        recent_key_limit: int = 128,
        input_x_ratio: float = 0.50,
        input_y_ratio: float = 0.905,
        title_box: tuple[float, float, float, float] = (0.28, 0.03, 0.50, 0.10),
        chat_box: tuple[float, float, float, float] = (0.12, 0.10, 0.97, 0.88),
        on_message: Optional[MessageCallback] = None,
        on_chat_changed: Optional[ChatChangedCallback] = None,
        on_error: Optional[ErrorCallback] = None,
        on_send_started: Optional[SendStartedCallback] = None,
        on_send_completed: Optional[SendCompletedCallback] = None,
    ) -> None:
        self.poll_interval = max(0.5, float(poll_interval))
        self.max_send_per_minute = max(1, int(max_send_per_minute))
        self.recent_key_limit = max(16, int(recent_key_limit))
        self.input_x_ratio = min(0.99, max(0.01, float(input_x_ratio)))
        self.input_y_ratio = min(0.99, max(0.01, float(input_y_ratio)))
        self.title_box = title_box
        self.chat_box = chat_box
        self.state_path = Path(state_path)
        self._reader = get_paddle_layout_reader()
        self._capture_queue: "queue.Queue[CapturedFrame]" = queue.Queue(maxsize=8)
        self._parse_queue: "queue.Queue[ParsedFrame]" = queue.Queue(maxsize=8)
        self._state_lock = threading.RLock()
        self._status_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._capture_thread: Optional[threading.Thread] = None
        self._ocr_thread: Optional[threading.Thread] = None
        self._parse_thread: Optional[threading.Thread] = None
        self._message_callbacks: list[MessageCallback] = []
        self._chat_changed_callbacks: list[ChatChangedCallback] = []
        self._error_callbacks: list[ErrorCallback] = []
        if on_message:
            self._message_callbacks.append(on_message)
        if on_chat_changed:
            self._chat_changed_callbacks.append(on_chat_changed)
        if on_error:
            self._error_callbacks.append(on_error)

        self._state = OcrListenState()
        self._load_state()
        self._last_error: Optional[str] = None
        self._chat_title = ""
        self._chat_type: Optional[str] = None
        self._last_frame_signature: Optional[str] = None
        self._last_accepted_signature: Optional[str] = None
        self._ocr_skip_identical_count = 0
        self._running = False
        self._send_lock = threading.RLock()
        self.send_queue = OcrSendQueue(
            self.send_text,
            max_per_minute=self.max_send_per_minute,
            on_send_started=on_send_started,
            on_send_completed=on_send_completed,
        )

    @property
    def running(self) -> bool:
        return bool(
            self._running
            and self._capture_thread
            and self._capture_thread.is_alive()
            and self._ocr_thread
            and self._ocr_thread.is_alive()
            and self._parse_thread
            and self._parse_thread.is_alive()
        )

    @property
    def last_error(self) -> Optional[str]:
        with self._status_lock:
            return self._last_error

    @property
    def chat_title(self) -> str:
        with self._status_lock:
            return self._chat_title

    @property
    def chat_type(self) -> Optional[str]:
        with self._status_lock:
            return self._chat_type

    @property
    def capture_queue_size(self) -> int:
        return self._capture_queue.qsize()

    @property
    def parse_queue_size(self) -> int:
        return self._parse_queue.qsize()

    def status(self) -> dict:
        with self._status_lock:
            last_frame_signature = self._last_frame_signature
            last_accepted_signature = self._last_accepted_signature
            ocr_skip_identical_count = self._ocr_skip_identical_count

        return {
            "running": self.running,
            "chat_title": self.chat_title,
            "chat_type": self.chat_type,
            "capture_queue_size": self.capture_queue_size,
            "parse_queue_size": self.parse_queue_size,
            "send_queue_size": self.send_queue.size,
            "poll_interval": self.poll_interval,
            "max_send_per_minute": self.max_send_per_minute,
            "last_frame_signature": last_frame_signature,
            "last_accepted_signature": last_accepted_signature,
            "ocr_skip_identical_count": ocr_skip_identical_count,
            "last_error": self.last_error,
        }

    def start(self) -> None:
        if self.running:
            return

        # A persisted recent-key baseline belongs to a previous chat.  The
        # active chat is only known after the first OCR frame, so rebuild it.
        with self._state_lock:
            self._state.initialized = False
            self._state.recent_keys = []

        self._stop_event.clear()
        self._running = True
        self._capture_thread = threading.Thread(
            target=self._capture_loop,
            name="handwechat-ocr-capture",
            daemon=True,
        )
        self._ocr_thread = threading.Thread(
            target=self._ocr_loop,
            name="handwechat-ocr-engine",
            daemon=True,
        )
        self._parse_thread = threading.Thread(
            target=self._parse_loop,
            name="handwechat-ocr-parser",
            daemon=True,
        )
        self._capture_thread.start()
        self._ocr_thread.start()
        self._parse_thread.start()
        self.send_queue.start()

    def stop(self, timeout: float = 4.0) -> None:
        self._stop_event.set()
        self._running = False
        self.send_queue.stop(timeout=timeout)
        for thread in (self._capture_thread, self._ocr_thread, self._parse_thread):
            if thread and thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout)
        self._save_state()

    def send(self, text: str, *, task_id: str) -> None:
        self.send_queue.submit(text, task_id=task_id)

    def send_text(self, text: str, *, verify_timeout: float = 6.0) -> bool:
        """Paste text into the active chat, press Enter, then verify."""

        if not text:
            raise ValueError("发送内容不能为空")
        with self._send_lock:
            normalized = _normalize_text(text)
            with self._state_lock:
                self._state.self_texts = [
                    item for item in self._state.self_texts if item != normalized
                ]
            if not self._confirm_chat():
                raise RuntimeError(
                    "无法确认当前微信聊天，请先在微信中打开要发送的聊天"
                )
            self._click_input()
            self._clear_input()
            self._set_clipboard_text(text)
            self._paste_clipboard()
            time.sleep(0.15)
            self._press_enter()
            return self._wait_for_self_message(text, timeout=verify_timeout)

    def _capture_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                located = _find_wechat_window()
            except Exception as exc:
                self._report_error("OCR 窗口定位", exc)
                self._stop_event.wait(self.poll_interval)
                continue
            if located is None:
                self._set_status_error("未找到微信主窗口")
                self._stop_event.wait(self.poll_interval)
                continue
            hwnd, (left, top, right, bottom) = located
            if right <= left or bottom <= top:
                self._set_status_error("微信窗口尺寸无效")
                self._stop_event.wait(self.poll_interval)
                continue
            try:
                temporary_topmost = self._bring_window_to_front(hwnd)
                try:
                    image = ImageGrab.grab(
                        bbox=(left, top, right, bottom), all_screens=True
                    )
                finally:
                    if temporary_topmost:
                        self._release_window_front(hwnd)
                width, height = image.size
                title_box = _crop_box(
                    (width, height),
                    left=self.title_box[0],
                    top=self.title_box[1],
                    right=self.title_box[2],
                    bottom=self.title_box[3],
                )
                chat_box = _crop_box(
                    (width, height),
                    left=self.chat_box[0],
                    top=self.chat_box[1],
                    right=self.chat_box[2],
                    bottom=self.chat_box[3],
                )
                signature = _frame_signature(image, title_box, chat_box)
                self._set_last_frame_signature(signature)
                if signature == self._last_accepted_signature:
                    self._increment_ocr_skip_count()
                    self._stop_event.wait(self.poll_interval)
                    continue
                frame = CapturedFrame(
                    hwnd=hwnd,
                    left=left,
                    top=top,
                    right=right,
                    bottom=bottom,
                    captured_at=time.time(),
                    image=image,
                    signature=signature,
                )
                # A bounded put intentionally back-pressures capture when OCR is slow.
                while not self._stop_event.is_set():
                    try:
                        self._capture_queue.put(frame, timeout=0.5)
                        break
                    except queue.Full:
                        continue
            except Exception as exc:
                self._report_error("OCR 截屏", exc)
            self._stop_event.wait(self.poll_interval)

    def _ocr_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                frame = self._capture_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if frame.signature == self._last_accepted_signature:
                    self._increment_ocr_skip_count()
                    continue
                parsed = self._read_frame(frame)
                self._mark_signature_accepted(frame.signature)
                self._parse_queue.put(parsed)
            except Exception as exc:
                self._report_error("OCR 解析", exc)

    def _parse_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                frame = self._parse_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._consume_frame(frame)
            except Exception as exc:
                self._report_error("OCR 消息差异计算", exc)

    def _read_frame(self, frame: CapturedFrame) -> ParsedFrame:
        width, height = frame.image.size
        title_box = _crop_box((width, height), left=self.title_box[0], top=self.title_box[1], right=self.title_box[2], bottom=self.title_box[3])
        chat_box = _crop_box((width, height), left=self.chat_box[0], top=self.chat_box[1], right=self.chat_box[2], bottom=self.chat_box[3])
        title_lines = self._reader.recognize_lines(frame.image.crop(title_box))
        chat_lines = self._reader.recognize_lines(frame.image.crop(chat_box))
        chat_title = self._select_title(title_lines)
        # Every current chat is parsed with the group layout, because OCR has
        # no reliable signal for whether a private chat will show sender rows.
        chat_type = "group" if chat_title else None
        messages = self._parse_chat_lines(
            chat_lines,
            width=chat_box[2] - chat_box[0],
            is_group=True,
        )
        return ParsedFrame(
            chat_title=chat_title,
            messages=tuple(messages),
            captured_at=frame.captured_at,
        )

    def _set_last_frame_signature(self, signature: str) -> None:
        with self._status_lock:
            self._last_frame_signature = signature

    def _mark_signature_accepted(self, signature: str) -> None:
        with self._status_lock:
            self._last_accepted_signature = signature

    def _increment_ocr_skip_count(self) -> None:
        with self._status_lock:
            self._ocr_skip_identical_count += 1

    def _select_title(self, lines: Sequence[OcrLine]) -> str:
        if not lines:
            return ""
        candidates = [line for line in lines if line.text.strip()]
        if not candidates:
            return ""
        return max(candidates, key=lambda line: len(line.text)).text.strip()

    def _parse_chat_lines(
        self, lines: Sequence[OcrLine], *, width: int, is_group: bool = False
    ) -> list[OcrMessage]:
        normalized = self._dedupe_ocr_lines(lines)
        if not normalized:
            return []

        nickname_indexes = (
            self._find_group_nicknames(normalized) if is_group else set()
        )
        clusters: list[list[OcrLine]] = []
        nickname_by_cluster: dict[int, str] = {}

        current_cluster: list[OcrLine] = []
        pending_nickname = ""
        for index, line in enumerate(normalized):
            if self._is_timestamp_text(line.text):
                if current_cluster:
                    clusters.append(current_cluster)
                    if pending_nickname:
                        nickname_by_cluster[len(clusters) - 1] = pending_nickname
                    current_cluster = []
                    pending_nickname = ""
                pending_nickname = ""
                continue

            if index in nickname_indexes:
                if current_cluster:
                    clusters.append(current_cluster)
                    if pending_nickname:
                        nickname_by_cluster[len(clusters) - 1] = pending_nickname
                    current_cluster = []
                    pending_nickname = ""
                pending_nickname = re.sub(r"[:：]\s*$", "", line.text).strip()
                continue

            if current_cluster:
                last = current_cluster[-1]
                row_gap = line.y0 - last.y1
                heights = (line.height, last.height)
                same_bubble = (
                    row_gap >= 0
                    and row_gap <= max(24.0, 1.8 * max(heights))
                    and abs(line.x0 - last.x0) <= max(16.0, 1.2 * max(heights))
                )
                if same_bubble:
                    current_cluster.append(line)
                    continue

            if current_cluster:
                clusters.append(current_cluster)
                if pending_nickname:
                    nickname_by_cluster[len(clusters) - 1] = pending_nickname
                pending_nickname = ""
            current_cluster = [line]

        if current_cluster:
            clusters.append(current_cluster)
            if pending_nickname:
                nickname_by_cluster[len(clusters) - 1] = pending_nickname

        messages: list[OcrMessage] = []
        for index, cluster in enumerate(clusters):
            text = "\n".join(line.text for line in cluster).strip()
            if not text:
                continue
            center_ratio = (
                sum(line.center_x for line in cluster) / len(cluster)
            ) / max(1, width)
            attr = (
                MessageAttr.SELF
                if center_ratio >= 0.55
                else MessageAttr.FRIEND
            )
            sender = "我" if attr == MessageAttr.SELF else nickname_by_cluster.get(index, "对方")
            messages.append(OcrMessage(attr=attr, text=text, sender=sender))
        return messages

    def _dedupe_ocr_lines(self, lines: Sequence[OcrLine]) -> list[OcrLine]:
        """Remove duplicate OCR boxes without touching distinct repeated text."""

        normalized = sorted(lines, key=lambda line: (line.y0, line.x0))
        kept: list[OcrLine] = []
        for line in normalized:
            duplicate = False
            for prior in reversed(kept):
                vertical_distance = abs(line.center_y - prior.center_y)
                if vertical_distance > max(12.0, 1.5 * max(line.height, prior.height)):
                    continue
                if not self._same_ocr_row(line, prior):
                    continue
                if _normalize_text(line.text) != _normalize_text(prior.text):
                    continue
                if self._is_same_text_box(line, prior):
                    duplicate = True
                    break
            if not duplicate:
                kept.append(line)
        return kept

    def _is_same_text_box(self, line: OcrLine, other: OcrLine) -> bool:
        overlap = min(line.x1, other.x1) - max(line.x0, other.x0)
        if overlap <= 0:
            return False
        shorter_width = max(1, min(line.x1 - line.x0, other.x1 - other.x0))
        return overlap / shorter_width >= 0.70

    def _find_group_nicknames(self, lines: Sequence[OcrLine]) -> set[int]:
        """Find nickname rows before clustering so quoted rows are not split."""

        nickname_indexes: set[int] = set()
        previous_content_index: Optional[int] = None
        for index, line in enumerate(lines):
            if not self._is_nickname_text(line.text):
                continue

            for candidate_index in range(index + 1, len(lines)):
                candidate = lines[candidate_index]
                if self._same_ocr_row(line, candidate):
                    break
                if self._is_timestamp_text(candidate.text):
                    continue
                if self._is_nickname_above_bubble(line, candidate):
                    nickname_indexes.add(index)
                break

            if index not in nickname_indexes and previous_content_index is not None:
                previous = lines[previous_content_index]
                if self._is_nickname_after_bubble(line, previous):
                    nickname_indexes.add(index)
            if index not in nickname_indexes and not self._is_timestamp_text(line.text):
                previous_content_index = index
        return nickname_indexes

    def _is_nickname_text(self, text: str) -> bool:
        value = text.strip()
        return bool(value) and len(value) <= 32 and not self._is_timestamp_text(value)

    def _is_timestamp_text(self, text: str) -> bool:
        return bool(re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?|\d{4}", text.strip()))

    def _is_nickname_above_bubble(
        self, nickname: OcrLine, bubble_line: OcrLine
    ) -> bool:
        vertical_gap = bubble_line.y0 - nickname.y1
        height = max(nickname.height, bubble_line.height)
        if vertical_gap < 0 or vertical_gap > max(36.0, 2.5 * height):
            return False

        left_offset = bubble_line.x0 - nickname.x0
        if left_offset < 0.2 * height or left_offset > 1.5 * height:
            return False
        return nickname.height <= bubble_line.height + 8

    def _is_nickname_after_bubble(
        self, nickname: OcrLine, previous_bubble: OcrLine
    ) -> bool:
        """Treat a left-anchored short row after a bubble as a missing-text sender."""

        height = max(nickname.height, previous_bubble.height)
        vertical_gap = nickname.y0 - previous_bubble.y1
        if vertical_gap < 0 or vertical_gap > max(36.0, 2.5 * height):
            return False
        left_offset = previous_bubble.x0 - nickname.x0
        if left_offset < 0.2 * height or left_offset > 1.5 * height:
            return False
        return nickname.height <= previous_bubble.height + 8

    def _same_ocr_row(self, line: OcrLine, other: OcrLine) -> bool:
        overlap = min(line.y1, other.y1) - max(line.y0, other.y0)
        if overlap <= 0:
            return False
        overlap_ratio = overlap / min(line.height, other.height)
        if overlap_ratio < 0.55:
            return False

        horizontal_gap = max(0, max(line.x0, other.x0) - min(line.x1, other.x1))
        return horizontal_gap <= max(32.0, 2.5 * max(line.height, other.height))

    def _consume_frame(self, frame: ParsedFrame) -> None:
        with self._status_lock:
            previous_title = self._chat_title
            if frame.chat_title:
                self._chat_title = frame.chat_title
                self._chat_type = "group"
            title_changed = bool(frame.chat_title) and (
                self._chat_title != previous_title
            )

        if title_changed:
            self._emit_chat_changed(frame.chat_title)

        keys = [_message_key(message) for message in frame.messages]
        new_messages: list[OcrMessage] = []
        with self._state_lock:
            if title_changed:
                self._state.initialized = False
                self._state.recent_keys = []

            if not self._state.initialized:
                self._state.initialized = True
                self._state.recent_keys = keys[-self.recent_key_limit :]
                self._state.updated_at = _now_text()
                self._save_state_locked()
                return

            overlap_length = _overlap_length(self._state.recent_keys, keys)
            if overlap_length:
                new_messages = list(frame.messages[overlap_length:])
            else:
                new_messages = list(frame.messages)

            if new_messages:
                for message in new_messages:
                    self._state.last_seq += 1
                    base = TextMessage(
                        sender=message.sender,
                        content=message.text,
                        attr=message.attr,
                        type=MessageType.TEXT,
                        index=self._state.last_seq,
                    )
                    base.extra["listen_time"] = _now_text()
                    if message.attr == MessageAttr.SELF:
                        self._state.self_texts.append(_normalize_text(message.text))
                        self._state.self_texts = self._state.self_texts[-32:]
                    self._emit_message(base)
                self._state.recent_keys.extend(
                    _message_key(message) for message in new_messages
                )
                self._state.recent_keys = self._state.recent_keys[
                    -self.recent_key_limit :
                ]
                self._state.updated_at = _now_text()
                self._save_state_locked()

    def _emit_message(self, message: BaseMessage) -> None:
        for callback in tuple(self._message_callbacks):
            try:
                callback(message)
            except Exception as exc:
                self._report_error("OCR 消息回调", exc)

    def _emit_chat_changed(self, chat_title: str) -> None:
        for callback in tuple(self._chat_changed_callbacks):
            try:
                callback(chat_title)
            except Exception as exc:
                self._report_error("OCR 聊天切换回调", exc)

    def _report_error(self, context: str, exc: Exception) -> None:
        text = f"{context}: {type(exc).__name__}: {exc}"
        self._set_status_error(text)
        for callback in tuple(self._error_callbacks):
            try:
                callback(context, exc)
            except Exception:
                pass

    def _set_status_error(self, message: str) -> None:
        with self._status_lock:
            self._last_error = message

    def _load_state(self) -> None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            self._state = OcrListenState.from_dict(data)
        except (OSError, json.JSONDecodeError):
            self._state = OcrListenState()

    def _save_state(self) -> None:
        with self._state_lock:
            self._save_state_locked()

    def _save_state_locked(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(self._state.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _wait_for_self_message(self, text: str, *, timeout: float) -> bool:
        normalized = _normalize_text(text)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._state_lock:
                if normalized in self._state.self_texts:
                    return True
            time.sleep(0.2)
        return False

    def _confirm_chat(self, timeout: float = 6.0) -> bool:
        located = _find_wechat_window()
        if located is None:
            raise RuntimeError("未找到微信主窗口")
        hwnd, rect = located
        self._focus_window(hwnd)
        time.sleep(0.15)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            image = self._grab_window(rect)
            if image is None:
                return False
            width, height = image.size
            title_box = _crop_box(
                (width, height),
                left=self.title_box[0],
                top=self.title_box[1],
                right=self.title_box[2],
                bottom=self.title_box[3],
            )
            title_lines = self._reader.recognize_lines(image.crop(title_box))
            title = self._select_title(title_lines)
            if title:
                with self._status_lock:
                    self._chat_title = title
                    self._chat_type = "group"
                return True
            time.sleep(0.4)
        return False

    def _click_input(self) -> None:
        located = _find_wechat_window()
        if located is None:
            raise RuntimeError("未找到微信主窗口")
        hwnd, rect = located
        self._focus_window(hwnd)
        time.sleep(0.15)
        left, top, right, bottom = rect
        width = right - left
        height = bottom - top
        x = int(width * self.input_x_ratio)
        y = int(height * self.input_y_ratio)
        self._click_window_point(rect, x, y)

    def _clear_input(self) -> None:
        self._press_combo(0x11, 0x41)  # Ctrl+A
        self._tap(0x2E)  # Delete

    def _paste_clipboard(self) -> None:
        self._press_combo(0x11, 0x56)  # Ctrl+V

    def _press_enter(self) -> None:
        self._tap(0x0D)  # Enter

    def _grab_window(self, rect: tuple[int, int, int, int]) -> Image.Image | None:
        left, top, right, bottom = rect
        if right <= left or bottom <= top:
            return None
        return ImageGrab.grab(bbox=rect, all_screens=True)

    def _focus_window(self, hwnd: int) -> None:
        user32 = ctypes.windll.user32
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)

    def _bring_window_to_front(self, hwnd: int) -> bool:
        """Put WeChat above overlapping windows before reading screen pixels."""

        user32 = ctypes.windll.user32
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            time.sleep(0.08)
        user32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL
        if user32.GetWindowLongW(hwnd, -20) & 0x0008:  # WS_EX_TOPMOST
            return False
        return bool(
            user32.SetWindowPos(
                wintypes.HWND(hwnd),
                wintypes.HWND(-1),  # HWND_TOPMOST
                0,
                0,
                0,
                0,
                0x0001 | 0x0002 | 0x0010,
            )
        )

    def _release_window_front(self, hwnd: int) -> None:
        user32 = ctypes.windll.user32
        user32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL
        user32.SetWindowPos(
            wintypes.HWND(hwnd),
            wintypes.HWND(-2),  # HWND_NOTOPMOST
            0,
            0,
            0,
            0,
            0x0001 | 0x0002 | 0x0010,
        )

    def _click_window_point(
        self, rect: tuple[int, int, int, int], x: int, y: int
    ) -> None:
        left, top, _right, _bottom = rect
        user32 = ctypes.windll.user32
        user32.SetCursorPos(left + x, top + y)
        time.sleep(0.03)
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        user32.mouse_event(0x0004, 0, 0, 0, 0)

    def _set_clipboard_text(self, text: str) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002
        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL
        kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalFree.restype = wintypes.HGLOBAL
        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        user32.SetClipboardData.restype = wintypes.HANDLE
        encoded = text.encode("utf-16le") + b"\x00\x00"
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
        if not handle:
            raise OSError("GlobalAlloc failed")
        locked = kernel32.GlobalLock(handle)
        if not locked:
            kernel32.GlobalFree(handle)
            raise OSError("GlobalLock failed")
        try:
            ctypes.memmove(locked, encoded, len(encoded))
        finally:
            kernel32.GlobalUnlock(handle)

        if not user32.OpenClipboard(None):
            kernel32.GlobalFree(handle)
            raise OSError("OpenClipboard failed")
        try:
            user32.EmptyClipboard()
            if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                kernel32.GlobalFree(handle)
                raise OSError("SetClipboardData failed")
        finally:
            user32.CloseClipboard()

    def _press_combo(self, modifier: int, key: int) -> None:
        user32 = ctypes.windll.user32
        user32.keybd_event(modifier, 0, 0, 0)
        user32.keybd_event(key, 0, 0, 0)
        user32.keybd_event(key, 0, 2, 0)
        user32.keybd_event(modifier, 0, 2, 0)

    def _tap(self, key: int) -> None:
        user32 = ctypes.windll.user32
        user32.keybd_event(key, 0, 0, 0)
        user32.keybd_event(key, 0, 2, 0)
