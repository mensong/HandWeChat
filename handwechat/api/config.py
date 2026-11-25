# -*- coding: utf-8 -*-
"""HTTP API 配置。"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class ConfigField:
    """One editable api_config.json field, shared with the web console."""

    key: str
    label: str
    group: str
    kind: str
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    restart_required: bool = False
    hint: str = ""

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "group": self.group,
            "kind": self.kind,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "step": self.step,
            "restart_required": self.restart_required,
            "hint": self.hint,
        }


CONFIG_FIELDS: tuple[ConfigField, ...] = (
    ConfigField(
        "host",
        "监听地址",
        "网络",
        "text",
        restart_required=True,
        hint="重启 API 服务后生效",
    ),
    ConfigField(
        "port",
        "监听端口",
        "网络",
        "int",
        minimum=1,
        maximum=65535,
        restart_required=True,
        hint="重启 API 服务后生效",
    ),
    ConfigField(
        "message_buffer_size",
        "消息缓冲上限（条）",
        "消息",
        "int",
        minimum=1,
        restart_required=True,
        hint="重启 API 服务后生效",
    ),
    ConfigField(
        "send_task_retention_minutes",
        "发送任务保留（分钟）",
        "消息",
        "int",
        minimum=1,
        restart_required=True,
        hint="重启 API 服务后生效",
    ),
    ConfigField(
        "log_dir",
        "日志目录",
        "日志",
        "text",
        restart_required=True,
        hint="相对安装目录；重启 API 服务后生效",
    ),
    ConfigField(
        "access_log_file",
        "访问日志文件名",
        "日志",
        "filename",
        restart_required=True,
        hint="重启 API 服务后生效",
    ),
    ConfigField(
        "send_task_log_file",
        "发送任务日志文件名",
        "日志",
        "filename",
        restart_required=True,
        hint="重启 API 服务后生效",
    ),
    ConfigField(
        "ocr_log_file",
        "OCR 日志文件名",
        "日志",
        "filename",
        restart_required=True,
        hint="重启 API 服务后生效",
    ),
    ConfigField(
        "ocr_poll_interval",
        "截屏间隔（秒）",
        "OCR",
        "float",
        minimum=0.2,
        step=0.1,
        hint="下次启动 OCR 监听时生效",
    ),
    ConfigField(
        "ocr_max_send_per_minute",
        "每分钟发送上限",
        "OCR",
        "int",
        minimum=1,
        hint="下次启动 OCR 监听时生效",
    ),
    ConfigField(
        "ocr_history_size",
        "历史 OCR 消息容量",
        "OCR",
        "int",
        minimum=1,
        hint="下次启动 OCR 监听时生效",
    ),
    ConfigField(
        "ws_batch_limit",
        "WebSocket 单帧条数",
        "WebSocket",
        "int",
        minimum=1,
        hint="新连接生效",
    ),
    ConfigField(
        "ws_ping_interval",
        "WebSocket 心跳间隔（秒）",
        "WebSocket",
        "float",
        minimum=5.0,
        step=0.5,
        hint="新连接生效",
    ),
)

_FIELDS_BY_KEY = {field.key: field for field in CONFIG_FIELDS}


@dataclass
class ApiConfig:
    host: str = "0.0.0.0"
    port: int = 8000
    message_buffer_size: int = 5000
    send_task_retention_minutes: int = 30
    log_dir: str = "data/logs"
    access_log_file: str = "api_requests.log"
    send_task_log_file: str = "send_tasks.log"
    ocr_log_file: str = "ocr.log"
    ocr_poll_interval: float = 2.0
    ocr_max_send_per_minute: int = 20
    ocr_history_size: int = 10
    ws_batch_limit: int = 100
    ws_ping_interval: float = 20.0

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ApiConfig":
        return cls(
            host=str(data.get("host") or "0.0.0.0"),
            port=int(data.get("port") or 8000),
            message_buffer_size=max(
                1, int(data.get("message_buffer_size") or 5000)
            ),
            send_task_retention_minutes=max(
                1, int(data.get("send_task_retention_minutes") or 30)
            ),
            log_dir=str(data.get("log_dir") or "data/logs"),
            access_log_file=str(data.get("access_log_file") or "api_requests.log"),
            send_task_log_file=str(
                data.get("send_task_log_file") or "send_tasks.log"
            ),
            ocr_log_file=str(data.get("ocr_log_file") or "ocr.log"),
            ocr_poll_interval=max(
                0.2, float(data.get("ocr_poll_interval") or 2.0)
            ),
            ocr_max_send_per_minute=max(
                1, int(data.get("ocr_max_send_per_minute") or 20)
            ),
            ocr_history_size=max(
                1, int(data.get("ocr_history_size") or 10)
            ),
            ws_batch_limit=max(1, int(data.get("ws_batch_limit") or 100)),
            ws_ping_interval=max(5.0, float(data.get("ws_ping_interval") or 20.0)),
        )

    @classmethod
    def load(cls, path: str | Path) -> "ApiConfig":
        path = Path(path)
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_mapping(data)

    def to_dict(self) -> dict:
        return asdict(self)


def _number_text(value: Any) -> str:
    if isinstance(value, bool):
        raise ValueError
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    raise ValueError


def _coerce_int(value: Any, field: ConfigField) -> int:
    try:
        text = _number_text(value)
        if isinstance(value, float):
            if not value.is_integer():
                raise ValueError
            number = int(value)
        else:
            number = int(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field.key} 必须是整数：{value!r}") from exc
    _check_range(number, field)
    return number


def _coerce_float(value: Any, field: ConfigField) -> float:
    try:
        number = float(_number_text(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field.key} 必须是数值：{value!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field.key} 必须是有限数值：{value!r}")
    _check_range(number, field)
    return number


def _check_range(number: float, field: ConfigField) -> None:
    if field.minimum is not None and number < field.minimum:
        raise ValueError(f"{field.key} 不能小于 {field.minimum}：{number}")
    if field.maximum is not None and number > field.maximum:
        raise ValueError(f"{field.key} 不能大于 {field.maximum}：{number}")


def _coerce_text(value: Any, field: ConfigField) -> str:
    text = str(value if value is not None else "").strip()
    if not text:
        raise ValueError(f"{field.key} 不能为空")
    return text


def _coerce_filename(value: Any, field: ConfigField) -> str:
    text = _coerce_text(value, field)
    if text in (".", "..") or "/" in text or "\\" in text:
        raise ValueError(f"{field.key} 只能是文件名，不能包含路径：{text!r}")
    return text


_COERCERS = {
    "int": _coerce_int,
    "float": _coerce_float,
    "text": _coerce_text,
    "filename": _coerce_filename,
}


def validate_values(values: Mapping[str, Any]) -> dict:
    """Validate a full set of editable config values."""

    if not isinstance(values, Mapping):
        raise ValueError("配置必须是 JSON 对象")
    unknown = sorted(set(values) - set(_FIELDS_BY_KEY))
    if unknown:
        raise ValueError(f"未知配置项：{', '.join(unknown)}")
    validated: dict[str, Any] = {}
    for field in CONFIG_FIELDS:
        if field.key not in values:
            raise ValueError(f"缺少配置项：{field.key}")
        validated[field.key] = _COERCERS[field.kind](values[field.key], field)
    return validated


def save_api_config(path: str | Path, values: Mapping[str, Any]) -> ApiConfig:
    """Validate values, merge them into the file and keep unknown keys."""

    path = Path(path)
    validated = validate_values(values)
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = None
        if isinstance(raw, dict):
            existing = raw
    existing.update(validated)
    text = json.dumps(existing, ensure_ascii=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(text, encoding="utf-8")
    temp_path.replace(path)
    return ApiConfig.from_mapping(existing)


def config_field_specs() -> list[dict]:
    return [field.to_dict() for field in CONFIG_FIELDS]
