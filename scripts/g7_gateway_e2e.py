# -*- coding: utf-8 -*-
"""网关端到端冒烟：假上游 + 真网关 + 三个假应用。"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
from fastapi import FastAPI, WebSocket

from handwechat.gateway.api import create_app
from handwechat.gateway.client import GatewayClient, Offer
from handwechat.gateway.config import GatewayAppConfig, GatewayConfig


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def item(number: int, content: str) -> dict:
    return {
        "id": str(number),
        "cursor": number,
        "sender": "张三",
        "content": content,
        "received_at": "2026-11-17T18:00:00+08:00",
        "is_latest": True,
        "confidence": 1.0,
    }


def close_gateway_logging() -> None:
    import logging

    logger = logging.getLogger("handwechat.gateway")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def stage(text: str) -> None:
    print(f"[stage] {text}", flush=True)


class FakeUpstream:
    def __init__(self, port: int) -> None:
        self.port = port
        self.queue: asyncio.Queue[dict] = asyncio.Queue()
        self.sent: list[str] = []
        self.app = self._build()

    @property
    def http_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/api/v1/ws/messages"

    def _build(self) -> FastAPI:
        app = FastAPI()

        @app.websocket("/api/v1/ws/messages")
        async def ws(websocket: WebSocket) -> None:
            await websocket.accept()
            await websocket.send_json({"type": "ready", "buffer_size": 0})
            try:
                while True:
                    payload = await self.queue.get()
                    await websocket.send_json(payload)
            except Exception:
                return

        @app.post("/api/v1/send")
        async def send(body: dict) -> dict:
            self.sent.append(str(body.get("text") or ""))
            return {"task_id": f"t{len(self.sent)}", "status": "queued"}

        return app


async def wait_started(server: uvicorn.Server, name: str) -> None:
    for _ in range(200):
        if server.started:
            stage(f"{name} started")
            return
        await asyncio.sleep(0.05)
    raise RuntimeError(f"{name} 未能启动")


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


async def wait_upstream_connected(port: int) -> None:
    for _ in range(100):
        status = await asyncio.to_thread(
            _get_json, f"http://127.0.0.1:{port}/api/v1/gateway/status"
        )
        if status["upstream"]["connected"]:
            stage("gateway connected to upstream")
            return
        await asyncio.sleep(0.05)
    raise RuntimeError("网关未能连上假上游")


async def wait_sent(upstream: FakeUpstream, expected: list[str]) -> None:
    for _ in range(100):
        if upstream.sent == expected:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"实际发送内容不符：{upstream.sent}")


async def consumer(app_id: str, port: int, decide, observed: list, clients: dict) -> None:
    def on_offer(offer: Offer) -> dict:
        observed.append(
            {"app": app_id, "content": offer.content, "pinned": offer.pinned}
        )
        return decide(app_id, offer.content)

    client = GatewayClient(
        app_id=app_id,
        token="t",
        url=f"ws://127.0.0.1:{port}/api/v1/gateway/ws/app",
        on_offer=on_offer,
        reconnect_seconds=0.5,
    )
    clients[app_id] = client
    await client.run()


async def main() -> None:
    upstream_port = free_port()
    gateway_port = free_port()

    upstream = FakeUpstream(upstream_port)
    upstream_server = uvicorn.Server(
        uvicorn.Config(upstream.app, host="127.0.0.1", port=upstream_port, log_level="critical")
    )
    upstream_task = asyncio.create_task(upstream_server.serve())
    await wait_started(upstream_server, "fake-upstream")

    consumers: list[asyncio.Task] = []
    gateway_task: asyncio.Task | None = None
    gateway_server: uvicorn.Server | None = None
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            config = GatewayConfig(
                upstream_ws_url=upstream.ws_url,
                upstream_http_url=upstream.http_url,
                listen_host="127.0.0.1",
                listen_port=gateway_port,
                heartbeat_seconds=0,
                round_deadline_ms=3000,
                log_dir=tmp,
                apps=[
                    GatewayAppConfig(id="checkin", priority=10, token="t"),
                    GatewayAppConfig(id="game", priority=100, token="t", hold_seconds=60),
                    GatewayAppConfig(id="bot", priority=999, token="t"),
                ],
            )
            gateway_app = create_app(config=config)
            gateway_server = uvicorn.Server(
                uvicorn.Config(
                    gateway_app, host="127.0.0.1", port=gateway_port, log_level="critical"
                )
            )
            gateway_task = asyncio.create_task(gateway_server.serve())
            await wait_started(gateway_server, "gateway")
            await wait_upstream_connected(gateway_port)

            observed: list[dict] = []

            def decide(app_id: str, content: str) -> dict:
                if app_id == "game":
                    if content == "开始数字游戏":
                        return {
                            "action": "consume",
                            "pin": True,
                            "hold_seconds": 60,
                            "state": "playing",
                            "reply": "游戏开始",
                        }
                    return {"action": "skip"}
                if app_id == "checkin":
                    if content == "42":
                        return {"action": "consume", "reply": "签到成功"}
                    return {"action": "skip"}
                if app_id == "bot":
                    return {"action": "consume", "reply": "bot"}
                return {"action": "skip"}

            clients: dict[str, GatewayClient] = {}
            consumers = [
                asyncio.create_task(
                    consumer("checkin", gateway_port, decide, observed, clients)
                ),
                asyncio.create_task(
                    consumer("game", gateway_port, decide, observed, clients)
                ),
                asyncio.create_task(
                    consumer("bot", gateway_port, decide, observed, clients)
                ),
            ]
            await asyncio.sleep(0.3)
            stage("consumers connected")

            await upstream.queue.put({"type": "messages", "items": [item(1, "开始数字游戏")]})
            await wait_sent(upstream, ["游戏开始"])
            stage("message 1 consumed by game")

            await upstream.queue.put({"type": "messages", "items": [item(2, "42")]})
            await wait_sent(upstream, ["游戏开始", "签到成功"])
            stage("message 2: pinned game skipped, checkin consumed")

            await upstream.queue.put({"type": "messages", "items": [item(3, "你好")]})
            await wait_sent(upstream, ["游戏开始", "签到成功", "bot"])
            stage("message 3: fallback bot consumed")

            await clients["bot"].send("主动消息")
            await wait_sent(
                upstream, ["游戏开始", "签到成功", "bot", "主动消息"]
            )
            stage("proactive send works")

            assert {"app": "bot", "content": "开始数字游戏", "pinned": False} not in observed
            assert {"app": "bot", "content": "42", "pinned": False} not in observed
            assert {"app": "bot", "content": "你好", "pinned": False} in observed
            assert {"app": "game", "content": "42", "pinned": True} in observed

            status = await asyncio.to_thread(
                _get_json, f"http://127.0.0.1:{gateway_port}/api/v1/gateway/status"
            )
            assert status["pin"]["app_id"] == "game", status["pin"]
            assert status["stats"]["dispatched"] == 3, status["stats"]
            assert status["stats"]["consumed"] == 3, status["stats"]
            stage("status verified")
    finally:
        close_gateway_logging()
        for task in consumers:
            task.cancel()
        await asyncio.gather(*consumers, return_exceptions=True)
        if gateway_server is not None:
            gateway_server.should_exit = True
        if gateway_task is not None:
            gateway_task.cancel()
            await asyncio.gather(gateway_task, return_exceptions=True)
        upstream_server.should_exit = True
        upstream_task.cancel()
        await asyncio.gather(upstream_task, return_exceptions=True)

    print("PASS 端到端：顺序询问、首个消费停止、置顶优先与回落、网关代发", flush=True)


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(), timeout=60))
