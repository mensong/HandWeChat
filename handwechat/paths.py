# -*- coding: utf-8 -*-
"""Application and resource paths for source and packaged execution."""

from __future__ import annotations

import sys
from pathlib import Path


def app_dir() -> Path:
    """Return the directory that contains writable application data."""

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def app_data_dir() -> Path:
    return app_dir() / "data"


def app_data_path(name: str) -> Path:
    return app_data_dir() / name


def app_path(path: str | Path) -> Path:
    """Resolve a configured path relative to the application directory."""

    value = Path(path)
    return value if value.is_absolute() else app_dir() / value


def resource_base() -> Path:
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass).resolve()
        return Path(sys.executable).resolve().parent
    return app_dir()


def resource_path(relative: str) -> Path:
    return resource_base() / relative


def ocr_models_dir() -> Path:
    return resource_path("ocr_models")
