# -*- coding: utf-8 -*-
"""不依赖真实微信的 M2 HTTP API 冒烟测试。"""

from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dataclasses import dataclass
from fastapi.testclient import TestClient

import handwechat.api.service as api_service
from handwechat.api.app import create_app
from handwechat.api.config import ApiConfig
from handwechat.api.service import ApiService

class FakeRuntime:
    def __init__(self) -> None:
        self.started = False
        self.shutdown_count = 0

    @property
    def listener_running(self) -> bool:
        return False

    @property
    def send_queue_size(self) -> int:
        return 0

    def start_send_queue(self) -> None:
        self.started = True

    def start_listener(self) -> None:
        pass

    def stop_listener(self) -> None:
        pass

    def shutdown(self) -> None:
        self.shutdown_count += 1


class FakeWeChat:
    def __init__(self) -> None:
        self.online = True

    def IsOnline(self) -> bool:
        return self.online

    @property
    def pid(self) -> int:
        return 12345

    @property
    def hwnd(self) -> int:
        return 0xE30D24

    def GetSession(self, max_sessions: int = 100):
        @dataclass
        class Session:
            name: str
            type: str
            is_new: bool
            unread_count: int
            preview: str
            time_raw: str

        return [
            Session(
                name="888 种田大队",
                type="group",
                is_new=True,
                unread_count=2,
                preview="还有不要钱的？",
                time_raw="12:00",
            )
        ]


def main() -> None:
    api_service.get_process_version = lambda pid: api_service.REQUIRED_WECHAT_VERSION
    wx = FakeWeChat()
    runtime = FakeRuntime()
    service = ApiService(
        ApiConfig(),
        wechat_factory=lambda: wx,
        runtime_factory=lambda instance, **kwargs: runtime,
    )

    with TestClient(create_app(config=ApiConfig(), service=service)) as client:
        health = client.get("/api/v1/health")
        assert health.status_code == 200
        payload = health.json()
        assert payload["status"] == "ok"
        assert payload["online"] is True
        assert payload["version_baseline_matched"] is True

        attach = client.post("/api/v1/attach")
        assert attach.status_code == 200
        assert attach.json()["wechat_hwnd"] == "0xe30d24"

        sessions = client.get("/api/v1/sessions")
        assert sessions.status_code == 200
        item = sessions.json()["items"][0]
        assert item["name"] == "888 种田大队"
        assert item["type"] == "group"
        assert item["unread"] is True

        wx.online = False
        offline = client.get("/api/v1/sessions")
        assert offline.status_code == 409
        assert offline.json()["code"] == "WECHAT_OFFLINE"

    assert runtime.started is True
    assert runtime.shutdown_count >= 2
    print("M2 API smoke: ok")


if __name__ == "__main__":
    main()
