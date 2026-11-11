# -*- coding: utf-8 -*-
"""不依赖真实微信的 M3 监听与消息缓冲冒烟测试。"""

from __future__ import annotations

import sys
from pathlib import Path
from dataclasses import dataclass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

import handwechat.api.service as api_service
from handwechat.api.app import create_app
from handwechat.api.config import ApiConfig
from handwechat.api.service import ApiService
from handwechat.models import MessageType, TextMessage


class FakeRuntime:
    def __init__(self, instance, **kwargs) -> None:
        self.kwargs = kwargs
        self.sessions = set()
        self.types = {}
        self.listener_running_state = False
        self.start_count = 0
        self.stop_count = 0
        self.started_send_queue = False

    @property
    def listener_running(self) -> bool:
        return self.listener_running_state

    @property
    def send_queue_size(self) -> int:
        return 0

    def start_send_queue(self) -> None:
        self.started_send_queue = True

    def add_listener_session(self, name: str):
        if name == "不存在":
            raise ValueError("找不到要监听的会话：'不存在'")
        self.sessions.add(name)
        self.types[name] = "group" if "大队" in name else "friend"

        @dataclass
        class Session:
            name: str
            type: str

        return Session(name=name, type=self.types[name])

    def remove_listener_session(self, name: str) -> bool:
        existed = name in self.sessions
        if existed:
            self.sessions.remove(name)
        return existed

    def list_listener_sessions(self) -> list[str]:
        return sorted(self.sessions)

    def listener_session_type(self, name: str):
        return self.types.get(name)

    def start_listener(self) -> None:
        self.start_count += 1
        self.listener_running_state = True

    def stop_listener(self) -> None:
        self.stop_count += 1
        self.listener_running_state = False

    def shutdown(self) -> None:
        self.stop_listener()

    def emit_message(self, session: str, message: TextMessage) -> None:
        self.kwargs["on_message"](session, message)


class FakeWeChat:
    def IsOnline(self) -> bool:
        return True

    @property
    def pid(self) -> int:
        return 12345

    @property
    def hwnd(self) -> int:
        return 0xE30D24


def main() -> None:
    api_service.get_process_version = lambda pid: api_service.REQUIRED_WECHAT_VERSION
    wx = FakeWeChat()
    runtime = None

    def runtime_factory(instance, **kwargs):
        nonlocal runtime
        runtime = FakeRuntime(instance, **kwargs)
        return runtime

    service = ApiService(
        ApiConfig(
            listener_sessions=["888 种田大队"],
            my_names=["机器人管理员"],
        ),
        wechat_factory=lambda: wx,
        runtime_factory=runtime_factory,
    )

    with TestClient(create_app(config=ApiConfig(), service=service)) as client:
        assert runtime is not None
        assert runtime.kwargs["my_names"] == ["机器人管理员"]
        assert runtime.sessions == {"888 种田大队"}
        assert runtime.listener_running_state is True

        sessions = client.get("/api/v1/listener/sessions")
        assert sessions.status_code == 200, sessions.text
        assert sessions.json()["items"][0]["name"] == "888 种田大队"
        assert sessions.json()["items"][0]["type"] == "group"

        add = client.post(
            "/api/v1/listener/sessions",
            json={"name": "文件传输助手"},
        )
        assert add.status_code == 200, add.text
        assert add.json()["status"] == "added"
        assert add.json()["name"] == "文件传输助手"

        missing = client.post(
            "/api/v1/listener/sessions",
            json={"name": "不存在"},
        )
        assert missing.status_code == 404, missing.text
        assert missing.json()["code"] == "LISTENER_SESSION_NOT_FOUND"

        start = client.post("/api/v1/listener/start")
        assert start.status_code == 200, start.text
        assert start.json()["status"] == "running"

        group_message = TextMessage(
            session_name="888 种田大队",
            sender="张三",
            content="@机器人管理员 收到",
            attr="friend",
            type=MessageType.TEXT,
            index=1,
            is_at_me=True,
        )
        runtime.emit_message("888 种田大队", group_message)

        private_message = TextMessage(
            session_name="文件传输助手",
            sender="文件传输助手",
            content="测试",
            attr="friend",
            type=MessageType.TEXT,
            index=1,
        )
        runtime.emit_message("文件传输助手", private_message)

        messages = client.get("/api/v1/messages?cursor=0&limit=1&timeout=0")
        assert messages.status_code == 200, messages.text
        payload = messages.json()
        assert len(payload["items"]) == 1
        assert payload["has_more"] is True
        assert payload["items"][0]["is_at_me"] is True
        assert payload["items"][0]["session_type"] == "group"
        cursor = payload["next_cursor"]

        messages = client.get(
            f"/api/v1/messages?cursor={cursor}&limit=100&timeout=0"
        )
        assert messages.status_code == 200, messages.text
        payload = messages.json()
        assert payload["items"][0]["session"] == "文件传输助手"
        assert payload["has_more"] is False

        stop = client.post("/api/v1/listener/stop")
        assert stop.status_code == 200, stop.text
        assert stop.json()["status"] == "stopped"

        remove = client.delete("/api/v1/listener/sessions/文件传输助手")
        assert remove.status_code == 200, remove.text
        assert remove.json()["status"] == "removed"

    assert runtime.started_send_queue is True
    assert runtime.stop_count >= 1
    print("M3 API smoke: ok")


if __name__ == "__main__":
    main()
