# -*- coding: utf-8 -*-
"""Fake-channel smoke test for the WebSocket message stream."""

from __future__ import annotations

import logging
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

import handwechat.ocr_channel as ocr_channel
from handwechat.api.app import create_app
from handwechat.api.config import ApiConfig
from handwechat.api.service import ApiService
from handwechat.models import MessageAttr, MessageType, TextMessage


class FakeOcrChannel:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.running_flag = False
        self.send_queue = SimpleNamespace(size=0)

    @property
    def running(self) -> bool:
        return self.running_flag

    @property
    def last_error(self):
        return None

    @property
    def chat_title(self) -> str:
        return "group"

    @property
    def chat_type(self):
        return "group"

    def start(self) -> None:
        self.running_flag = True

    def stop(self, timeout: float = 4.0) -> None:
        self.running_flag = False

    def status(self) -> dict:
        return {
            "running": self.running_flag,
            "chat_title": self.chat_title,
            "chat_type": self.chat_type,
            "send_queue_size": 0,
            "last_error": None,
        }

    def send(self, text: str, *, task_id: str | None = None) -> None:
        return None

    def emit_message(self, index: int, content: str, sender: str = "alice") -> None:
        self.kwargs["on_message"](
            TextMessage(
                sender=sender,
                content=content,
                attr=MessageAttr.FRIEND,
                type=MessageType.TEXT,
                index=index,
            )
        )


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def main() -> None:
    ocr_channel._find_wechat_window = lambda: (0xE30D24, None)
    channels: list[FakeOcrChannel] = []

    def channel_factory(**kwargs) -> FakeOcrChannel:
        channel = FakeOcrChannel(**kwargs)
        channels.append(channel)
        return channel

    with tempfile.TemporaryDirectory() as temp_dir:
        config = ApiConfig(log_dir=temp_dir, ws_ping_interval=0.3)
        service = ApiService(config, ocr_channel_factory=channel_factory)

        with TestClient(create_app(config=config, service=service)) as client:
            started = client.post("/api/v1/listener/start")
            assert started.status_code == 200, started.text
            channel = channels[0]

            # History emitted before the connection must never be replayed.
            channel.emit_message(1, "history")
            assert service.message_cursor == 1

            with client.websocket_connect("/api/v1/ws/messages") as ws:
                ready = ws.receive_json()
                assert ready["type"] == "ready", ready
                assert ready["cursor"] == 1, ready
                assert ready["buffer_size"] == 1, ready

                channel.emit_message(2, "first")
                frame = ws.receive_json()
                assert frame["type"] == "messages", frame
                assert len(frame["items"]) == 1, frame
                assert frame["items"][0]["content"] == "first", frame
                assert frame["items"][0]["cursor"] == 2, frame
                assert frame["next_cursor"] == 2, frame
                assert frame["has_more"] is False, frame

                # An idle connection receives a heartbeat, not messages.
                ping = ws.receive_json()
                assert ping["type"] == "ping", ping
                assert ping["ts"], ping

                # A second client starts from its own connect point.
                with client.websocket_connect("/api/v1/ws/messages") as ws2:
                    ready2 = ws2.receive_json()
                    assert ready2["type"] == "ready", ready2
                    assert ready2["cursor"] == 2, ready2

                    channel.emit_message(3, "shared")
                    frame1 = ws.receive_json()
                    frame2 = ws2.receive_json()
                    assert frame1["items"][0]["content"] == "shared", frame1
                    assert frame2["items"][0]["content"] == "shared", frame2
                    assert frame1["items"][0]["cursor"] == 3, frame1
                    assert frame2["items"][0]["cursor"] == 3, frame2

                # One client left; the other keeps receiving.
                channel.emit_message(4, "after close")
                frame = ws.receive_json()
                assert frame["items"][0]["content"] == "after close", frame

                # Client frames are accepted and ignored.
                ws.send_text('{"type":"pong"}')
                channel.emit_message(5, "still alive")
                frame = ws.receive_json()
                assert frame["items"][0]["content"] == "still alive", frame

            assert _wait_until(lambda: not service.message_buffer._subscribers)

            # The HTTP long-poll endpoint still returns the whole buffer.
            history = client.get("/api/v1/messages?cursor=0&limit=100&timeout=0")
            assert history.status_code == 200, history.text
            contents = [item["content"] for item in history.json()["items"]]
            assert contents == [
                "history",
                "first",
                "shared",
                "after close",
                "still alive",
            ], contents

            for logger_name in (
                "handwechat.api.access",
                "handwechat.api.send_tasks",
                "handwechat.api.ws",
                "handwechat.ocr",
            ):
                logger = logging.getLogger(logger_name)
                for handler in logger.handlers:
                    handler.close()
                logger.handlers.clear()

    print("M6 WS smoke: ok")


if __name__ == "__main__":
    main()
