# -*- coding: utf-8 -*-
"""Fake-channel smoke test for listener lifecycle and the message buffer."""

from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

import handwechat.ocr_channel as ocr_channel
from handwechat.api.app import create_app
from handwechat.api.config import ApiConfig
from handwechat.api.service import ApiService
from handwechat.models import MessageAttr, MessageType, OtherMessage, TextMessage


class FakeOcrChannel:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.running_flag = False
        self.stopped = False
        self.sent: list[tuple[str, str | None]] = []
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
        self.sent.append((text, task_id))

    def emit_message(self, message) -> None:
        self.kwargs["on_message"](message)

    def emit_chat_changed(self, title: str) -> None:
        self.kwargs["on_chat_changed"](title)

    def emit_error(self, context: str, exc: Exception) -> None:
        self.kwargs["on_error"](context, exc)


def main() -> None:
    ocr_channel._find_wechat_window = lambda: (0xE30D24, None)
    channels: list[FakeOcrChannel] = []

    def channel_factory(**kwargs) -> FakeOcrChannel:
        channel = FakeOcrChannel(**kwargs)
        channels.append(channel)
        return channel

    with tempfile.TemporaryDirectory() as temp_dir:
        config = ApiConfig(log_dir=temp_dir)
        service = ApiService(config, ocr_channel_factory=channel_factory)

        with TestClient(create_app(config=config, service=service)) as client:
            health = client.get("/api/v1/health")
            assert health.status_code == 200, health.text
            payload = health.json()
            assert payload["attached"] is True
            assert payload["online"] is True
            assert payload["listener_running"] is False
            assert payload["message_buffer_size"] == 0
            assert payload["last_error"] is None
            assert payload["poll_interval"] == config.ocr_poll_interval
            assert payload["max_send_per_minute"] == config.ocr_max_send_per_minute

            send_before = client.post("/api/v1/send", json={"text": "hello"})
            assert send_before.status_code == 409, send_before.text
            assert send_before.json()["code"] == "OCR_CHANNEL_NOT_RUNNING"

            started = client.post("/api/v1/listener/start")
            assert started.status_code == 200, started.text
            assert started.json()["status"] == "running"
            assert len(channels) == 1
            channel = channels[0]
            assert channel.running is True
            assert channel.kwargs["poll_interval"] == config.ocr_poll_interval
            assert (
                channel.kwargs["max_send_per_minute"]
                == config.ocr_max_send_per_minute
            )
            assert callable(channel.kwargs["on_message"])

            channel.emit_message(
                TextMessage(
                    sender="alice",
                    content="hello",
                    attr=MessageAttr.FRIEND,
                    type=MessageType.TEXT,
                    index=1,
                )
            )
            channel.emit_message(
                OtherMessage(
                    sender="system",
                    content="[image]",
                    attr=MessageAttr.FRIEND,
                    type=MessageType.OTHER,
                    index=2,
                )
            )
            channel.emit_message(
                TextMessage(
                    sender="me",
                    content="hi",
                    attr=MessageAttr.SELF,
                    type=MessageType.TEXT,
                    index=3,
                )
            )

            messages = client.get("/api/v1/messages?cursor=0&limit=1&timeout=0")
            assert messages.status_code == 200, messages.text
            payload = messages.json()
            assert len(payload["items"]) == 1
            assert payload["has_more"] is True
            first = payload["items"][0]
            assert first["sender"] == "alice"
            assert first["content"] == "hello"
            assert "is_at_me" not in first
            cursor = payload["next_cursor"]

            rest = client.get(
                f"/api/v1/messages?cursor={cursor}&limit=100&timeout=0"
            )
            assert rest.status_code == 200, rest.text
            payload = rest.json()
            assert len(payload["items"]) == 1
            assert payload["items"][0]["sender"] == "me"
            assert payload["items"][0]["content"] == "hi"
            assert payload["has_more"] is False

            channel.emit_chat_changed("group")
            cleared = client.get("/api/v1/messages?cursor=0&limit=100&timeout=0")
            assert cleared.status_code == 200, cleared.text
            assert cleared.json()["items"] == []

            channel.emit_message(
                TextMessage(
                    sender="bob",
                    content="after switch",
                    attr=MessageAttr.FRIEND,
                    type=MessageType.TEXT,
                    index=4,
                )
            )
            after_switch = client.get(
                "/api/v1/messages?cursor=0&limit=100&timeout=0"
            )
            assert after_switch.status_code == 200, after_switch.text
            payload = after_switch.json()
            assert len(payload["items"]) == 1
            assert payload["items"][0]["cursor"] == 3

            channel.emit_error("OCR test", ValueError("boom"))
            health = client.get("/api/v1/health")
            assert health.status_code == 200, health.text
            assert health.json()["last_error"] == "OCR test: ValueError: boom"

            stopped = client.post("/api/v1/listener/stop")
            assert stopped.status_code == 200, stopped.text
            assert stopped.json()["status"] == "stopped"
            assert channel.stopped is True
            assert service.ocr_channel is None

            restarted = client.post("/api/v1/listener/start")
            assert restarted.status_code == 200, restarted.text
            assert len(channels) == 2
            assert channels[1].running is True

            attach = client.post("/api/v1/attach")
            assert attach.status_code == 200, attach.text
            assert attach.json()["status"] == "attached"
            assert attach.json()["wechat_hwnd"] == "0xe30d24"

        assert channels[-1].stopped is True

        for logger_name in ("handwechat.api.access", "handwechat.api.send_tasks"):
            logger = logging.getLogger(logger_name)
            for handler in logger.handlers:
                handler.close()
            logger.handlers.clear()

    print("M3 API smoke: ok")


if __name__ == "__main__":
    main()
