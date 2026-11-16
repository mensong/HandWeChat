# -*- coding: utf-8 -*-
"""Fake-channel smoke test for logging, diagnostics and send task status."""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

import handwechat.ocr_channel as ocr_channel
from handwechat.api.app import create_app
from handwechat.api.config import ApiConfig
from handwechat.api.service import ApiService


class FakeOcrChannel:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.running_flag = False
        self.stopped = False
        self.send_queue = SimpleNamespace(size=0)

    @property
    def running(self) -> bool:
        return self.running_flag

    @property
    def last_error(self) -> None:
        return None

    @property
    def chat_title(self) -> str:
        return "group"

    @property
    def chat_type(self) -> str | None:
        return "group"

    def start(self) -> None:
        self.running_flag = True

    def stop(self, timeout: float = 4.0) -> None:
        self.running_flag = False
        self.stopped = True

    def status(self) -> dict:
        return {
            "running": self.running_flag,
            "chat_title": self.chat_title,
            "chat_type": self.chat_type,
            "send_queue_size": self.send_queue.size,
            "last_error": self.last_error,
        }

    def send(self, text: str, *, task_id: str | None = None) -> None:
        def run() -> None:
            time.sleep(0.05)
            ok = text != "M5 failure"
            self.kwargs["on_send_started"](task_id, text)
            self.kwargs["on_send_completed"](
                task_id,
                text,
                ok,
                "" if ok else "send verification failed",
            )

        threading.Thread(target=run, daemon=True).start()

    def emit_error(self, context: str, exc: Exception) -> None:
        self.kwargs["on_error"](context, exc)


def wait_task(client: TestClient, task_id: str) -> dict:
    deadline = time.time() + 3
    while time.time() < deadline:
        response = client.get(f"/api/v1/send/{task_id}")
        assert response.status_code == 200, response.text
        payload = response.json()
        if payload["status"] in ("success", "failed"):
            return payload
        time.sleep(0.05)
    raise AssertionError("task did not finish in 3 seconds")


def read_json_lines(path: Path) -> list[dict]:
    return [
        json.loads(line.split(" ", 2)[2])
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def main() -> None:
    ocr_channel._find_wechat_window = lambda: (0xE30D24, None)

    with tempfile.TemporaryDirectory() as temp_dir:
        config = ApiConfig(
            log_dir=temp_dir,
            access_log_file="access.log",
            send_task_log_file="tasks.log",
            ocr_poll_interval=1.5,
            ocr_max_send_per_minute=7,
        )
        service = ApiService(config, ocr_channel_factory=FakeOcrChannel)

        with TestClient(create_app(config=config, service=service)) as client:
            health = client.get("/api/v1/health")
            assert health.status_code == 200, health.text
            payload = health.json()
            assert payload["attached"] is True
            assert payload["online"] is True
            assert payload["listener_running"] is False
            assert payload["message_buffer_size"] == 0
            assert payload["send_task_active"] == 0
            assert payload["last_error"] is None
            assert payload["poll_interval"] == config.ocr_poll_interval
            assert payload["max_send_per_minute"] == config.ocr_max_send_per_minute

            started = client.post("/api/v1/listener/start")
            assert started.status_code == 200, started.text
            assert started.json()["status"] == "running"

            channel = service.ocr_channel
            assert isinstance(channel, FakeOcrChannel)
            channel.emit_error("OCR smoke", RuntimeError("capture failed"))
            health_payload = client.get("/api/v1/health").json()
            assert (
                health_payload["last_error"]
                == "OCR smoke: RuntimeError: capture failed"
            )

            queued = client.post("/api/v1/send", json={"text": "M5 success"})
            assert queued.status_code == 202, queued.text
            success_task_id = queued.json()["task_id"]
            success = wait_task(client, success_task_id)
            assert success["status"] == "success"
            assert success["error_code"] is None

            failed_queued = client.post("/api/v1/send", json={"text": "M5 failure"})
            assert failed_queued.status_code == 202, failed_queued.text
            failed_task_id = failed_queued.json()["task_id"]
            failed = wait_task(client, failed_task_id)
            assert failed["status"] == "failed"
            assert failed["error_code"] == "SEND_FAILED"

            missing = client.get("/api/v1/send/send_not_exists")
            assert missing.status_code == 404, missing.text
            assert missing.json()["code"] == "SEND_TASK_NOT_FOUND"

            invalid = client.post("/api/v1/send", json={"text": ""})
            assert invalid.status_code == 422, invalid.text
            assert invalid.json()["code"] == "INVALID_REQUEST"

        access_entries = read_json_lines(Path(temp_dir) / "access.log")
        access_paths = {entry["path"] for entry in access_entries}
        assert "/api/v1/health" in access_paths
        assert "/api/v1/send" in access_paths
        assert any(entry["status"] == 404 for entry in access_entries)
        assert any(entry["status"] == 422 for entry in access_entries)

        task_entries = read_json_lines(Path(temp_dir) / "tasks.log")
        assert any(
            entry["action"] == "created" and entry["task_id"] == success_task_id
            for entry in task_entries
        )
        assert any(
            entry["status"] == "success" and entry["task_id"] == success_task_id
            for entry in task_entries
        )
        assert any(
            entry["status"] == "failed"
            and entry["task_id"] == failed_task_id
            and entry["error_code"] == "SEND_FAILED"
            for entry in task_entries
        )

        for logger_name in (
            "handwechat.api.access",
            "handwechat.api.send_tasks",
            "handwechat.ocr",
        ):
            logger = logging.getLogger(logger_name)
            for handler in logger.handlers:
                handler.close()
            logger.handlers.clear()

        assert isinstance(channel, FakeOcrChannel)
        assert channel.stopped is True

    print("M5 API smoke: ok")


if __name__ == "__main__":
    main()
