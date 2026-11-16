# -*- coding: utf-8 -*-
"""Shared OCR window-region calibration storage."""

from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


RatioBox = tuple[float, float, float, float]


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
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "input_x_ratio": self.input_x_ratio,
            "input_y_ratio": self.input_y_ratio,
            "title_box": list(self.title_box),
            "chat_box": list(self.chat_box),
            "updated_at": self.updated_at or _now_text(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "OcrCalibration":
        if not isinstance(data, dict):
            raise ValueError("OCR 标定文件必须是 JSON 对象")
        return cls(
            input_x_ratio=_ratio(data.get("input_x_ratio"), "input_x_ratio"),
            input_y_ratio=_ratio(data.get("input_y_ratio"), "input_y_ratio"),
            title_box=_ratio_box(data.get("title_box"), "title_box"),
            chat_box=_ratio_box(data.get("chat_box"), "chat_box"),
            updated_at=str(data.get("updated_at") or ""),
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
