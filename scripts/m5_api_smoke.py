# -*- coding: utf-8 -*-
"""Fake-runtime smoke test for M5 logging, diagnostics and task status."""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

import handwechat.api.service as api_service
from handwechat.api.app import create_app
from handwechat.api.config import ApiConfig
from handwechat.api.service import ApiService


class FakeWeChat:
    def IsOnline(self) -> bool:
        return True

    @property
    def pid(self) -> int:
        return 12345

    @property
    def hwnd(self) -> int:
        return 0xE30D24


class FakeSendQueue:
    def __init__(self, runtime) -> None:
        self.runtime = runtime
        self.items: list[str] = []

    @property
    def size(self) -> int:
        return len(self.items)

    def submit(self, session: str, text: str, *, task_id: str | None = None) -> str:
        task_id = task_id or f"fake_{len(self.items) + 1}"
        self.items.append(task_id)

        def run() -> None:
            time.sleep(0.05)
            if task_id in self.items:
                self.items.remove(task_id)
            self.runtime.kwargs["on_send_started"](task_id, session, text)
            ok = session != "fail-session"
            self.runtime.kwargs["on_send_task_completed"](
                task_id,
                session,
                text,
                ok,
                "" if ok else "send verification failed",
            )

        threading.Thread(target=run, daemon=True).start()
        return task_id


class FakeRuntime:
    def __init__(self, instance, **kwargs) -> None:
        self.kwargs = kwargs
        self.send_queue = FakeSendQueue(self)
        self.started_send_queue = False
        self.stopped = False
        self.sessions = ["listen-1", "listen-2"]

    @property
    def listener_running(self) -> bool:
        return False

    @property
    def send_queue_size(self) -> int:
        return self.send_queue.size

    def start_send_queue(self) -> None:
        self.started_send_queue = True

    def start_listener(self) -> None:
        pass

    def stop_listener(self) -> None:
        pass

    def shutdown(self) -> None:
        self.stopped = True

    def list_listener_sessions(self) -> list[str]:
        return list(self.sessions)

    def listener_session_type(self, name: str) -> str | None:
        return "group" if name == "listen-1" else "friend"


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
    api_service.get_process_version = lambda pid: api_service.REQUIRED_WECHAT_VERSION
    runtime = None

    def runtime_factory(instance, **kwargs):
        nonlocal runtime
        runtime = FakeRuntime(instance, **kwargs)
        return runtime

    with tempfile.TemporaryDirectory() as temp_dir:
        config = ApiConfig(
            log_dir=temp_dir,
            access_log_file="access.log",
            send_task_log_file="tasks.log",
        )
        service = ApiService(
            config,
            wechat_factory=lambda: FakeWeChat(),
            runtime_factory=runtime_factory,
        )

        with TestClient(create_app(config=config, service=service)) as client:
            health = client.get("/api/v1/health")
            assert health.status_code == 200, health.text
            health_payload = health.json()
            assert health_payload["attached"] is True
            assert health_payload["listener_session_count"] == 2
            assert health_payload["message_buffer_size"] == 0
            assert health_payload["send_task_active"] == 0
            assert health_payload["last_error"] is None
            assert health_payload["poll_interval"] == config.poll_interval
            assert health_payload["max_send_per_minute"] == config.max_send_per_minute

            runtime.kwargs["on_health_changed"](False, "self check failed")
            health_payload = client.get("/api/v1/health").json()
            assert health_payload["last_error"] == "self check failed"
            runtime.kwargs["on_health_changed"](True, "")
            health_payload = client.get("/api/v1/health").json()
            assert health_payload["last_error"] is None

            queued = client.post(
                "/api/v1/send",
                json={"session": "file-transfer", "text": "M5 success"},
            )
            assert queued.status_code == 202, queued.text
            success = wait_task(client, queued.json()["task_id"])
            assert success["status"] == "success"

            failed_queued = client.post(
                "/api/v1/send",
                json={"session": "fail-session", "text": "M5 failure"},
            )
            assert failed_queued.status_code == 202, failed_queued.text
            failed = wait_task(client, failed_queued.json()["task_id"])
            assert failed["status"] == "failed"
            assert failed["error_code"] == "SEND_FAILED"

            missing = client.get("/api/v1/send/send_not_exists")
            assert missing.status_code == 404, missing.text
            assert missing.json()["code"] == "SEND_TASK_NOT_FOUND"

            invalid = client.post(
                "/api/v1/send",
                json={"session": "file-transfer", "text": ""},
            )
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
            entry["action"] == "created"
            and entry["session"] == "file-transfer"
            for entry in task_entries
        )
        assert any(entry["status"] == "success" for entry in task_entries)
        assert any(
            entry["status"] == "failed" and entry["error_code"] == "SEND_FAILED"
            for entry in task_entries
        )

        for logger_name in ("handwechat.api.access", "handwechat.api.send_tasks"):
            logger = logging.getLogger(logger_name)
            for handler in logger.handlers:
                handler.close()
            logger.handlers.clear()

    assert runtime is not None and runtime.stopped is True
    print("M5 API smoke: ok")


if __name__ == "__main__":
    main()
