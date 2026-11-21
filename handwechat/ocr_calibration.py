# -*- coding: utf-8 -*-
"""Shared OCR window-region calibration storage."""

from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


RatioBox = tuple[float, float, float, float]
WindowSize = tuple[int, int]
RgbColor = tuple[int, int, int]


def _now_text() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _ratio(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 不是有效数值：{value!r}") from exc
    if not 0.0 <= number <= 1.0:
        raise ValueError(f"{name} 必须在 0.0~1.0 范围内：{number}")
    return number


def _ratio_box(value: Any, name: str) -> RatioBox:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError(f"{name} 必须是 [left, top, right, bottom]")
    box = (
        _ratio(value[0], f"{name}.left"),
        _ratio(value[1], f"{name}.top"),
        _ratio(value[2], f"{name}.right"),
        _ratio(value[3], f"{name}.bottom"),
    )
    if box[0] >= box[2] or box[1] >= box[3]:
        raise ValueError(f"{name} 的 right/bottom 必须大于 left/top")
    return box


def _window_size(value: Any) -> WindowSize | None:
    """The calibrated physical window size, absent in older calibration files."""

    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError("window_size 必须是 [width, height]")
    try:
        width = int(value[0])
        height = int(value[1])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"window_size 不是有效整数：{value!r}") from exc
    if width <= 0 or height <= 0:
        raise ValueError(f"window_size 必须是正数：{value!r}")
    return width, height


def _rgb(value: Any, name: str) -> RgbColor:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{name} 必须是 [r, g, b]")
    try:
        channels = tuple(int(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 不是有效颜色：{value!r}") from exc
    if any(channel < 0 or channel > 255 for channel in channels):
        raise ValueError(f"{name} 的通道值必须在 0~255：{value!r}")
    return channels  # type: ignore[return-value]


def _rgb_distance(left: RgbColor, right: RgbColor) -> int:
    return max(abs(int(left[index]) - int(right[index])) for index in range(3))


@dataclass(frozen=True)
class OcrPalette:
    """Background colors picked by hand during OCR calibration."""

    page: RgbColor
    friend: RgbColor
    own: RgbColor

    def to_dict(self) -> dict:
        return {
            "page": list(self.page),
            "friend": list(self.friend),
            "self": list(self.own),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "OcrPalette":
        if not isinstance(data, dict):
            raise ValueError("palette 必须是 JSON 对象")
        return cls(
            page=_rgb(data.get("page"), "palette.page"),
            friend=_rgb(data.get("friend"), "palette.friend"),
            own=_rgb(data.get("self"), "palette.self"),
        )

    def distance_problem(self) -> str | None:
        """Return a human-readable reason when the colors are too close."""

        page_friend = _rgb_distance(self.page, self.friend)
        if page_friend < 8:
            return f"页面底色与对方气泡底色太接近（差 {page_friend}，至少需要 8）"
        for name, left, right in (
            ("页面底色", self.page, self.own),
            ("对方气泡底色", self.friend, self.own),
        ):
            distance = _rgb_distance(left, right)
            if distance < 30:
                return f"{name}与自己气泡底色太接近（差 {distance}，至少需要 30）"
        return None


@dataclass(frozen=True)
class OcrCalibration:
    input_x_ratio: float = 0.9352678571428571
    input_y_ratio: float = 0.8842592592592593
    title_box: RatioBox = (
        0.34486607142857145,
        0.05246913580246913,
        0.8370535714285714,
        0.11265432098765432,
    )
    chat_box: RatioBox = (
        0.41183035714285715,
        0.19135802469135801,
        0.9196428571428571,
        0.7654320987654321,
    )
    window_size: WindowSize | None = None
    window_maximized: bool = False
    updated_at: str = ""
    palette: OcrPalette | None = None

    def to_dict(self) -> dict:
        return {
            "version": 3,
            "input_x_ratio": self.input_x_ratio,
            "input_y_ratio": self.input_y_ratio,
            "title_box": list(self.title_box),
            "chat_box": list(self.chat_box),
            "window_size": list(self.window_size) if self.window_size else None,
            "window_maximized": bool(self.window_maximized),
            "updated_at": self.updated_at or _now_text(),
            "palette": self.palette.to_dict() if self.palette else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "OcrCalibration":
        if not isinstance(data, dict):
            raise ValueError("OCR 标定文件必须是 JSON 对象")
        palette_data = data.get("palette")
        return cls(
            input_x_ratio=_ratio(data.get("input_x_ratio"), "input_x_ratio"),
            input_y_ratio=_ratio(data.get("input_y_ratio"), "input_y_ratio"),
            title_box=_ratio_box(data.get("title_box"), "title_box"),
            chat_box=_ratio_box(data.get("chat_box"), "chat_box"),
            window_size=_window_size(data.get("window_size")),
            window_maximized=bool(data.get("window_maximized")),
            updated_at=str(data.get("updated_at") or ""),
            palette=OcrPalette.from_dict(palette_data) if palette_data else None,
        )


def load_ocr_calibration(path: str | Path) -> OcrCalibration:
    """Load a calibration file, returning built-in defaults when absent."""

    path = Path(path)
    if not path.exists():
        return OcrCalibration()
    data = json.loads(path.read_text(encoding="utf-8"))
    return OcrCalibration.from_dict(data)


def save_ocr_calibration(
    calibration: OcrCalibration,
    path: str | Path,
) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(calibration.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(path)
