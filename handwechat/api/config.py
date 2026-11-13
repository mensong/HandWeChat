# -*- coding: utf-8 -*-
"""HTTP API 配置。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class ApiConfig:
    host: str = "0.0.0.0"
    port: int = 8000
    message_buffer_size: int = 5000
    send_task_retention_minutes: int = 30
    log_dir: str = "data/logs"
    access_log_file: str = "api_requests.log"
    send_task_log_file: str = "send_tasks.log"
    ocr_poll_interval: float = 2.0
    ocr_max_send_per_minute: int = 20

    @classmethod
    def load(cls, path: str | Path) -> "ApiConfig":
        path = Path(path)
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
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
            ocr_poll_interval=max(
                0.5, float(data.get("ocr_poll_interval") or 2.0)
            ),
            ocr_max_send_per_minute=max(
                1, int(data.get("ocr_max_send_per_minute") or 20)
            ),
        )

    def to_dict(self) -> dict:
        return asdict(self)
