# -*- coding: utf-8 -*-
"""不依赖真实微信的 M2 HTTP API 冒烟测试：健康检查、窗口附着与错误码。"""

from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

import handwechat.ocr_channel as ocr_channel
from handwechat.api.app import create_app
from handwechat.api.config import ApiConfig
from handwechat.api.service import ApiService


HWND = 0xE30D24


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
    with tempfile.TemporaryDirectory() as temp_dir:
        config = ApiConfig(log_dir=temp_dir)

        ocr_channel._find_wechat_window = lambda: None
        service = ApiService(config)

        with TestClient(create_app(config=config, service=service)) as client:
            health = client.get("/api/v1/health")
            assert health.status_code == 200, health.text
            payload = health.json()
            assert payload["status"] == "offline"
            assert payload["online"] is False
            assert payload["attached"] is False
            assert payload["listener_running"] is False
            assert payload["chat_title"] == ""
            assert payload["chat_type"] is None
            assert payload["send_task_active"] == 0

            attach = client.post("/api/v1/attach")
            assert attach.status_code == 409, attach.text
            assert attach.json()["code"] == "WECHAT_OFFLINE"

            start = client.post("/api/v1/listener/start")
            assert start.status_code == 409, start.text
            assert start.json()["code"] == "WECHAT_OFFLINE"

            console = client.get("/")
            assert console.status_code == 200
            assert "text/html" in console.headers["content-type"]

            missing = client.get("/api/v1/nope")
            assert missing.status_code == 404, missing.text
            assert missing.json()["code"] == "NOT_FOUND"

            wrong_method = client.get("/api/v1/send")
            assert wrong_method.status_code == 405, wrong_method.text
            assert wrong_method.json()["code"] == "METHOD_NOT_ALLOWED"

            invalid_cursor = client.get("/api/v1/messages?cursor=-2")
            assert invalid_cursor.status_code == 422, invalid_cursor.text
            assert invalid_cursor.json()["code"] == "INVALID_REQUEST"

            tail = client.get("/api/v1/messages?cursor=-1&limit=100")
            assert tail.status_code == 200, tail.text
            assert tail.json()["items"] == []
            assert tail.json()["next_cursor"] == 0

            ocr_channel._find_wechat_window = lambda: (HWND, (0, 0, 100, 100))

            health = client.get("/api/v1/health")
            assert health.status_code == 200, health.text
            payload = health.json()
            assert payload["status"] == "ok"
            assert payload["online"] is True
            assert payload["attached"] is True
            assert payload["wechat_hwnd"] == "0xe30d24"

            attach = client.post("/api/v1/attach")
            assert attach.status_code == 200, attach.text
            assert attach.json()["status"] == "attached"
            assert attach.json()["wechat_hwnd"] == "0xe30d24"

        close_loggers()

    print("M2 API smoke: ok")


if __name__ == "__main__":
    main()
