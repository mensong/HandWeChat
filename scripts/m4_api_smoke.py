# -*- coding: utf-8 -*-
"""不依赖真实微信的 M4 发送任务 API 冒烟测试。"""

from __future__ import annotations

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
    """Gate every send so the test can observe the queued state."""

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.running_flag = False
        self.stopped = False
        self.send_queue = SimpleNamespace(size=0)
        self.gates: dict[str, threading.Event] = {}

    @property
    def running(self) -> bool:
        return self.running_flag

    @property
    def last_error(self) -> None:
        return None

    @property
    def chat_title(self) -> str:
        return "文件传输助手"

    @property
    def chat_type(self) -> str | None:
        return "friend"

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
        gate = threading.Event()
        self.gates[task_id] = gate
        threading.Thread(
            target=self._deliver,
            args=(task_id, text, gate),
            daemon=True,
        ).start()

    def release(self, task_id: str) -> None:
        self.gates[task_id].set()

    def _deliver(
        self, task_id: str, text: str, gate: threading.Event
    ) -> None:
        gate.wait(3)
        ok = text != "M4 failure"
        self.kwargs["on_send_started"](task_id, text)
        self.kwargs["on_send_completed"](
            task_id,
            text,
            ok,
            "" if ok else "RuntimeError: 未找到微信主窗口",
        )


def wait_task(client: TestClient, task_id: str) -> dict:
    deadline = time.time() + 3
    last: dict = {}
    while time.time() < deadline:
        response = client.get(f"/api/v1/send/{task_id}")
        assert response.status_code == 200, response.text
        last = response.json()
        if last["status"] in ("success", "failed"):
            return last
        time.sleep(0.05)
    raise AssertionError(f"任务未在 3 秒内完成：{last}")


def close_loggers() -> None:
    for logger_name in (
        "handwechat.api.access",
        "handwechat.api.send_tasks",
        "handwechat.ocr",
    ):
        logger = logging.getLogger(logger_name)
        for handler in logger.handlers:
            handler.close()
        logger.handlers.clear()


def main() -> None:
    ocr_channel._find_wechat_window = lambda: (0xE30D24, (0, 0, 100, 100))

    with tempfile.TemporaryDirectory() as temp_dir:
        config = ApiConfig(log_dir=temp_dir)
        channels: list[FakeOcrChannel] = []

        def channel_factory(**kwargs) -> FakeOcrChannel:
            channel = FakeOcrChannel(**kwargs)
            channels.append(channel)
            return channel

        service = ApiService(config, ocr_channel_factory=channel_factory)

        with TestClient(create_app(config=config, service=service)) as client:
            not_running = client.post("/api/v1/send", json={"text": "M4 success"})
            assert not_running.status_code == 409, not_running.text
            assert not_running.json()["code"] == "OCR_CHANNEL_NOT_RUNNING"

            started = client.post("/api/v1/listener/start")
            assert started.status_code == 200, started.text
            assert len(channels) == 1
            channel = channels[0]

            queued = client.post("/api/v1/send", json={"text": "M4 success"})
            assert queued.status_code == 202, queued.text
            queued_payload = queued.json()
            assert queued_payload["status"] == "queued"
            success_task_id = queued_payload["task_id"]
            assert success_task_id.startswith("send_")

            pending = client.get(f"/api/v1/send/{success_task_id}")
            assert pending.status_code == 200, pending.text
            assert pending.json()["status"] == "queued"
            assert pending.json()["text"] == "M4 success"
            assert pending.json()["completed_at"] is None
            assert (
                client.get("/api/v1/health").json()["send_task_active"] == 1
            )

            channel.release(success_task_id)
            success = wait_task(client, success_task_id)
            assert success["status"] == "success"
            assert success["completed_at"] is not None
            assert success["error_code"] is None
            assert success["error_message"] is None

            failed_queued = client.post(
                "/api/v1/send", json={"text": "M4 failure"}
            )
            assert failed_queued.status_code == 202, failed_queued.text
            failed_task_id = failed_queued.json()["task_id"]
            channel.release(failed_task_id)

            failed = wait_task(client, failed_task_id)
            assert failed["status"] == "failed"
            assert failed["error_code"] == "SEND_FAILED"
            assert failed["error_message"] == "RuntimeError: 未找到微信主窗口"

            assert client.get("/api/v1/health").json()["send_task_active"] == 0

            missing = client.get("/api/v1/send/send_not_exists")
            assert missing.status_code == 404, missing.text
            assert missing.json()["code"] == "SEND_TASK_NOT_FOUND"

            invalid = client.post("/api/v1/send", json={"text": ""})
            assert invalid.status_code == 422, invalid.text
            assert invalid.json()["code"] == "INVALID_REQUEST"

        assert channel.stopped is True

        close_loggers()

    print("M4 API smoke: ok")


if __name__ == "__main__":
    main()
