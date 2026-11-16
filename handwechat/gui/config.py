# -*- coding: utf-8 -*-
"""GUI configuration persistence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class GuiConfig:
    poll_interval: float = 2.0
    max_send_per_minute: int = 20
    dedup_queue_size: int = 10

    def to_dict(self) -> dict:
        return {
            "version": 3,
            "poll_interval": self.poll_interval,
            "max_send_per_minute": self.max_send_per_minute,
            "dedup_queue_size": self.dedup_queue_size,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GuiConfig":
        return cls(
            poll_interval=float(data.get("poll_interval") or 2.0),
            max_send_per_minute=int(data.get("max_send_per_minute") or 20),
            dedup_queue_size=max(1, int(data.get("dedup_queue_size") or 10)),
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
