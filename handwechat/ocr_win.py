"""OCR 桥接：优先 PaddleOCR，Windows OCR 仅作降级。"""

from __future__ import annotations

import json
from threading import Lock
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path

import numpy as np
from PIL import Image


_SCRIPT_PATH = Path(__file__).with_name("ocr_win.ps1")
_paddle_engine = None
_paddle_lock = Lock()


def recognize_images(images: Mapping[str, Image.Image]) -> dict[str, str]:
    """识别若干图像，返回与传入 key 相同的结果映射。

    PaddleOCR 对中文界面文本更稳定；Windows OCR 保留给未安装 Paddle 的环境。
    任一失败都返回空映射，调用方保留原 sender。
    """

    global _paddle_engine
    if not images:
        return {}

    if _paddle_engine is not None:
        return _recognize_with_paddle(images)

    with _paddle_lock:
        if _paddle_engine is None:
            try:
                from paddleocr import PaddleOCR
            except (ImportError, OSError):
                _paddle_engine = False
            else:
                _paddle_engine = PaddleOCR(use_angle_cls=False, lang="ch", show_log=False)
        engine = _paddle_engine

    if engine is False:
        return _recognize_with_windows(images)
    return _recognize_with_paddle(images)


def _recognize_with_paddle(images: Mapping[str, Image.Image]) -> dict[str, str]:
    """用已加载的 PaddleOCR 识别；输入按 key 顺序返回。"""

    if _paddle_engine is False or _paddle_engine is None:
        return _recognize_with_windows(images)

    result: dict[str, str] = {}
    for key, image in images.items():
        try:
            rgb = image.convert("RGB")
            bgr = np.asarray(rgb)[:, :, ::-1]
            pages = _paddle_engine.ocr(bgr, cls=False)
            result[key] = _paddle_text(pages)
        except Exception:
            result[key] = ""
    return result


def _paddle_text(pages) -> str:
    """取 OCR 结果中按 Y 坐标排序后的首行。"""

    lines: list[tuple[int, str]] = []
    for page in pages or []:
        for line in page or []:
            try:
                box, (text, _score) = line
                top = min(int(point[1]) for point in box)
            except (TypeError, ValueError, IndexError):
                continue
            if text and text.strip():
                lines.append((top, text.strip()))
    if not lines:
        return ""
    return sorted(lines)[0][1]


def _recognize_with_windows(images: Mapping[str, Image.Image]) -> dict[str, str]:
    """Windows OCR 降级路径。"""

    with tempfile.TemporaryDirectory(prefix="handwechat_ocr_") as temp_dir:
        paths: list[str] = []
        for key, image in images.items():
            path = Path(temp_dir) / f"image_{len(paths):04d}.png"
            image.save(path, format="PNG")
            paths.append(str(path))

        try:
            completed = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(_SCRIPT_PATH),
                    *paths,
                ],
                capture_output=True,
                timeout=20,
                encoding="utf-8",
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {}

        if completed.returncode != 0:
            return {}

        try:
            raw = json.loads(completed.stdout)
        except (TypeError, json.JSONDecodeError):
            return {}

        raw_items = raw.items() if isinstance(raw, dict) else []
        by_path = {
            str(path): _first_line(str(value))
            for path, value in raw_items
        }

    return {
        key: by_path.get(path, "")
        for key, path in zip(images.keys(), paths)
    }


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")
