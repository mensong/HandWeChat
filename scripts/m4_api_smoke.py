# -*- coding: utf-8 -*-
"""不依赖真实微信的 M4 发送任务 API 冒烟测试。"""

from __future__ import annotations

import sys
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
            ok = session != "失败会话"
            self.runtime.kwargs["on_send_task_completed"](
                task_id,
                session,
                text,
                ok,
                "" if ok else "发送后未回读到消息",
            )

        threading.Thread(target=run, daemon=True).start()
        return task_id


class FakeRuntime:
    def __init__(self, instance, **kwargs) -> None:
        self.kwargs = kwargs
        self.send_queue = FakeSendQueue(self)
        self.started_send_queue = False
        self.stopped = False

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


def main() -> None:
    api_service.get_process_version = lambda pid: api_service.REQUIRED_WECHAT_VERSION
    runtime = None

    def runtime_factory(instance, **kwargs):
        nonlocal runtime
        runtime = FakeRuntime(instance, **kwargs)
        return runtime

    service = ApiService(
        ApiConfig(),
        wechat_factory=lambda: FakeWeChat(),
        runtime_factory=runtime_factory,
    )

    with TestClient(create_app(config=ApiConfig(), service=service)) as client:
        assert runtime is not None
        assert runtime.started_send_queue is True

        queued = client.post(
            "/api/v1/send",
            json={"session": "文件传输助手", "text": "M4 API smoke"},
        )
        assert queued.status_code == 202, queued.text
        queued_payload = queued.json()
        assert queued_payload["status"] == "queued"
        assert queued_payload["task_id"].startswith("send_")

        success = wait_task(client, queued_payload["task_id"])
        assert success["session"] == "文件传输助手"
        assert success["text"] == "M4 API smoke"
        assert success["completed_at"] is not None
        assert success["error_code"] is None
        assert success["error_message"] is None

        failed_queued = client.post(
            "/api/v1/send",
            json={"session": "失败会话", "text": "M4 failed smoke"},
        )
        assert failed_queued.status_code == 202, failed_queued.text
        failed = wait_task(client, failed_queued.json()["task_id"])
        assert failed["status"] == "failed"
        assert failed["error_code"] == "SEND_VERIFY_FAILED"
        assert failed["error_message"] == "发送后未回读到消息"

        missing = client.get("/api/v1/send/send_not_exists")
        assert missing.status_code == 404, missing.text
        assert missing.json()["code"] == "SEND_TASK_NOT_FOUND"

        invalid = client.post(
            "/api/v1/send",
            json={"session": "文件传输助手", "text": ""},
        )
        assert invalid.status_code == 422, invalid.text
        assert invalid.json()["code"] == "INVALID_REQUEST"

    assert runtime is not None and runtime.stopped is True
    print("M4 API smoke: ok")


if __name__ == "__main__":
    main()
