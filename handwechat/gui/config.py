# -*- coding: utf-8 -*-
"""GUI 配置持久化。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict


@dataclass
class SessionConfig:
    name: str
    alias: str = ""


@dataclass
class GuiConfig:
    my_names: list[str] = field(default_factory=list)
    sessions: Dict[str, SessionConfig] = field(default_factory=dict)
    poll_interval: float = 2.0
    max_send_per_minute: int = 20

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "my_names": list(self.my_names),
            "sessions": {
                name: {"alias": item.alias}
                for name, item in self.sessions.items()
            },
            "poll_interval": self.poll_interval,
            "max_send_per_minute": self.max_send_per_minute,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GuiConfig":
        sessions = {
            name: SessionConfig(name=name, alias=str(item.get("alias") or ""))
            for name, item in (data.get("sessions") or {}).items()
        }
        return cls(
            my_names=list(data.get("my_names") or []),
            sessions=sessions,
            poll_interval=float(data.get("poll_interval") or 2.0),
            max_send_per_minute=int(data.get("max_send_per_minute") or 20),
        )

    @classmethod
    def load(cls, path: str | Path) -> "GuiConfig":
        path = Path(path)
        if not path.exists():
            return cls()
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(path)
