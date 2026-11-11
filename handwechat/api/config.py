# -*- coding: utf-8 -*-
"""HTTP API 配置。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class ApiConfig:
    host: str = "0.0.0.0"
    port: int = 8000
    max_send_per_minute: int = 20
    poll_interval: float = 2.0
    message_buffer_size: int = 5000
    max_sessions: int = 100
    send_task_retention_minutes: int = 30
    log_dir: str = "data/logs"
    access_log_file: str = "api_requests.log"
    send_task_log_file: str = "send_tasks.log"
    listener_sessions: list[str] = field(default_factory=list)
    my_names: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> "ApiConfig":
        path = Path(path)
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            host=str(data.get("host") or "0.0.0.0"),
            port=int(data.get("port") or 8000),
            max_send_per_minute=int(data.get("max_send_per_minute") or 20),
            poll_interval=max(0.2, float(data.get("poll_interval") or 2.0)),
            message_buffer_size=max(1, int(data.get("message_buffer_size") or 5000)),
            max_sessions=max(1, min(100, int(data.get("max_sessions") or 100))),
            send_task_retention_minutes=max(
                1, int(data.get("send_task_retention_minutes") or 30)
            ),
            log_dir=str(data.get("log_dir") or "data/logs"),
            access_log_file=str(data.get("access_log_file") or "api_requests.log"),
            send_task_log_file=str(
                data.get("send_task_log_file") or "send_tasks.log"
            ),
            listener_sessions=[
                str(name) for name in (data.get("listener_sessions") or []) if str(name)
            ],
            my_names=[
                str(name) for name in (data.get("my_names") or []) if str(name)
            ],
        )

    def to_dict(self) -> dict:
        return asdict(self)
