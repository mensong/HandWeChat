# -*- coding: utf-8 -*-
"""Thread-safe in-memory send task store."""

from __future__ import annotations

import threading
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional


task_logger = logging.getLogger("handwechat.api.send_tasks")


class SendTaskStatus:
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


@dataclass
class SendTask:
    task_id: str
    text: str
    status: str
    submitted_at: str
    completed_at: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None


def _now_text() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class SendTaskStore:
    """Keep completed tasks for the configured retention window."""

    def __init__(self, retention_minutes: int = 30) -> None:
        self._retention_seconds = max(1, int(retention_minutes)) * 60
        self._tasks: dict[str, SendTask] = {}
        self._lock = threading.RLock()
        self._date = ""
        self._counter = 0

    def active_count(self) -> int:
        with self._lock:
            self._cleanup()
            return sum(
                1
                for task in self._tasks.values()
                if task.status in (SendTaskStatus.QUEUED, SendTaskStatus.RUNNING)
            )

    def create(self, text: str) -> SendTask:
        now = datetime.now(timezone.utc).astimezone()
        with self._lock:
            self._cleanup(now)
            date = now.strftime("%Y%m%d")
            if date != self._date:
                self._date = date
                self._counter = 0
            self._counter += 1
            task = SendTask(
                task_id=f"send_{date}_{self._counter:06d}",
                text=text,
                status=SendTaskStatus.QUEUED,
                submitted_at=_now_text(),
            )
            self._tasks[task.task_id] = task
            _log_task("created", task)
            return task

    def mark_started(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status != SendTaskStatus.QUEUED:
                return False
            task.status = SendTaskStatus.RUNNING
            _log_task("started", task)
            return True

    def mark_completed(self, task_id: str, *, ok: bool, detail: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status == SendTaskStatus.SUCCESS:
                return False
            if ok:
                task.status = SendTaskStatus.SUCCESS
                task.completed_at = _now_text()
                task.error_code = None
                task.error_message = None
            elif task.status != SendTaskStatus.FAILED:
                task.status = SendTaskStatus.FAILED
                task.completed_at = _now_text()
                task.error_code = "SEND_FAILED"
                task.error_message = detail or "发送失败"
            _log_task("completed" if ok else "failed", task)
            return True

    def get(self, task_id: str) -> Optional[SendTask]:
        with self._lock:
            self._cleanup()
            return self._tasks.get(task_id)

    def _cleanup(self, now: Optional[datetime] = None) -> None:
        now = now or datetime.now(timezone.utc).astimezone()
        cutoff = now.timestamp() - self._retention_seconds
        for task_id, task in list(self._tasks.items()):
            if task.status not in (SendTaskStatus.SUCCESS, SendTaskStatus.FAILED):
                continue
            try:
                completed_at = datetime.fromisoformat(task.completed_at or "")
            except ValueError:
                continue
            if completed_at.timestamp() < cutoff:
                self._tasks.pop(task_id, None)

    def fail_active(self, *, error_message: str) -> int:
        changed = 0
        with self._lock:
            for task in self._tasks.values():
                if task.status not in (SendTaskStatus.QUEUED, SendTaskStatus.RUNNING):
                    continue
                task.status = SendTaskStatus.FAILED
                task.completed_at = _now_text()
                task.error_code = "SEND_FAILED"
                task.error_message = error_message
                _log_task("failed", task)
                changed += 1
        return changed

    def to_dict(self, task: SendTask) -> dict:
        return asdict(task)


def _log_task(action: str, task: SendTask) -> None:
    payload = {
        "action": action,
        "task_id": task.task_id,
        "status": task.status,
        "error_code": task.error_code,
        "error_message": task.error_message,
    }
    task_logger.info(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
