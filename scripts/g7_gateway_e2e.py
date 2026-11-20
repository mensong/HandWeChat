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
        self.health = {
            "status": "ok",
            "online": True,
            "listener_running": True,
            "attached": True,
            "chat_title": "群聊A",
            "chat_type": "group",
        }
        self.tasks: dict[str, dict] = {}
        self.task_seq = 0
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

        @app.get("/api/v1/health")
        async def health() -> dict:
            return self.health

        @app.post("/api/v1/send")
        async def send(body: dict) -> dict:
            text = str(body.get("text") or "")
            self.sent.append(text)
            self.task_seq += 1
            task_id = f"t{self.task_seq}"
            self.tasks[task_id] = {
                "task_id": task_id,
                "status": "queued",
                "text": text,
            }
            asyncio.get_running_loop().create_task(self._complete(task_id))
            return {"task_id": task_id, "status": "queued"}

        @app.get("/api/v1/send/{task_id}")
        async def send_task(task_id: str) -> dict:
            return self.tasks.get(
                task_id,
                {
                    "task_id": task_id,
                    "status": "failed",
                    "error_code": "SEND_TASK_NOT_FOUND",
                },
            )

        return app

    async def _complete(self, task_id: str) -> None:
        await asyncio.sleep(0.2)
        task = self.tasks.get(task_id)
        if task is not None:
            task["status"] = "success"


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


async def wait_health_ready(port: int) -> None:
    for _ in range(100):
        status = await asyncio.to_thread(
            _get_json, f"http://127.0.0.1:{port}/api/v1/gateway/status"
        )
        if status.get("context", {}).get("health_ok"):
            stage("gateway health ready")
            return
        await asyncio.sleep(0.05)
    raise RuntimeError("网关未能读取上游健康状态")


async def wait_until(predicate, label: str, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.05)):
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"等待超时：{label}")


async def wait_sent(upstream: FakeUpstream, expected: list[str]) -> None:
    await wait_until(lambda: upstream.sent == expected, f"发送内容 {expected}")


async def consumer(
    app_id: str,
    port: int,
    rules,
    decide,
    observed: list,
    clients: dict,
    send_results: list,
    chat_changes: list,
    states: list,
    welcomes: dict,
) -> None:
    holder: dict = {}

    def on_offer(offer: Offer) -> dict:
        observed.append(
            {
                "app": app_id,
                "content": offer.content,
                "pinned": offer.pinned,
                "dispatch_id": offer.dispatch_id,
            }
        )
        return decide(app_id, offer.content)

    def on_connected(_pin) -> None:
        client = holder.get("client")
        if client is not None:
            welcomes[app_id] = client.context

    client = GatewayClient(
        app_id=app_id,
        token="t",
        url=f"ws://127.0.0.1:{port}/api/v1/gateway/ws/app",
        rules=rules,
        on_offer=on_offer,
        on_connected=on_connected,
        on_send_result=send_results.append,
        on_chat_changed=chat_changes.append,
        on_upstream_state=states.append,
        reconnect_seconds=0.5,
    )
    holder["client"] = client
    clients[app_id] = client
    await client.run()


async def main() -> None:
    upstream_port = free_port()
    gateway_port = free_port()

    upstream = FakeUpstream(upstream_port)
    upstream_server = uvicorn.Server(
        uvicorn.Config(
            upstream.app, host="127.0.0.1", port=upstream_port, log_level="critical"
        )
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
                health_poll_seconds=0.2,
                send_result_poll_seconds=0.05,
                send_result_timeout_seconds=5,
                log_dir=tmp,
                apps=[
                    GatewayAppConfig(id="checkin", priority=10, token="t"),
                    GatewayAppConfig(
                        id="game", priority=100, token="t", hold_seconds=60
                    ),
                    GatewayAppConfig(id="bot", priority=999, token="t"),
                ],
            )
            gateway_app = create_app(config=config)
            gateway_server = uvicorn.Server(
                uvicorn.Config(
                    gateway_app,
                    host="127.0.0.1",
                    port=gateway_port,
                    log_level="critical",
                )
            )
            gateway_task = asyncio.create_task(gateway_server.serve())
            await wait_started(gateway_server, "gateway")
            await wait_upstream_connected(gateway_port)
            await wait_health_ready(gateway_port)

            observed: list[dict] = []
            send_results: list = []
            chat_changes: list = []
            states: list = []
            welcomes: dict = {}

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
            rules = {
                "checkin": [{"keywords": ["42"]}],
                "game": [{"keywords": ["开始数字游戏"]}],
                "bot": [],
            }
            consumers = [
                asyncio.create_task(
                    consumer(
                        "checkin",
                        gateway_port,
                        rules["checkin"],
                        decide,
                        observed,
                        clients,
                        send_results,
                        chat_changes,
                        states,
                        welcomes,
                    )
                ),
                asyncio.create_task(
                    consumer(
                        "game",
                        gateway_port,
                        rules["game"],
                        decide,
                        observed,
                        clients,
                        send_results,
                        chat_changes,
                        states,
                        welcomes,
                    )
                ),
                asyncio.create_task(
                    consumer(
                        "bot",
                        gateway_port,
                        rules["bot"],
                        decide,
                        observed,
                        clients,
                        send_results,
                        chat_changes,
                        states,
                        welcomes,
                    )
                ),
            ]
            await wait_until(
                lambda: len(clients) == 3 and len(welcomes) == 3,
                "应用全部握手",
            )
            stage("consumers connected")

            await upstream.queue.put(
                {"type": "messages", "items": [item(1, "开始数字游戏")]}
            )
            await wait_sent(upstream, ["游戏开始"])
            stage("message 1 consumed by game")

            await upstream.queue.put(
                {"type": "messages", "items": [item(2, "42")]}
            )
            await wait_sent(upstream, ["游戏开始", "签到成功"])
            stage("message 2: pinned game skipped, checkin consumed")

            await upstream.queue.put(
                {"type": "messages", "items": [item(3, "你好")]}
            )
            await wait_sent(upstream, ["游戏开始", "签到成功", "bot"])
            stage("message 3: fallback bot consumed")

            await clients["bot"].send("主动消息", request_id="r1")
            await wait_sent(
                upstream, ["游戏开始", "签到成功", "bot", "主动消息"]
            )
            await wait_until(
                lambda: any(
                    result.request_id == "r1" and result.final
                    for result in send_results
                ),
                "主动发送的最终回执",
            )
            proactive = [
                result
                for result in send_results
                if result.request_id == "r1" and result.final
            ][0]
            assert proactive.status == "success" and proactive.ok, proactive
            stage("proactive send result works")

            def saw(app_id: str, content: str, pinned: bool) -> bool:
                return any(
                    entry["app"] == app_id
                    and entry["content"] == content
                    and entry["pinned"] is pinned
                    for entry in observed
                )

            assert not saw("bot", "开始数字游戏", False)
            assert not saw("bot", "42", False)
            assert saw("bot", "你好", False)
            assert saw("game", "42", True)

            assert welcomes["game"].chat_title == "群聊A", welcomes["game"]
            assert welcomes["game"].listener_running is True, welcomes["game"]

            first_dispatch = [
                entry["dispatch_id"]
                for entry in observed
                if entry["app"] == "game" and entry["content"] == "开始数字游戏"
            ][0]
            queued = [
                result
                for result in send_results
                if result.dispatch_id == first_dispatch and not result.final
            ]
            assert queued and queued[0].status == "queued", queued
            await wait_until(
                lambda: any(
                    result.dispatch_id == first_dispatch and result.final
                    for result in send_results
                ),
                "决策回复的最终回执",
            )
            final = [
                result
                for result in send_results
                if result.dispatch_id == first_dispatch and result.final
            ][0]
            assert final.status == "success" and final.ok, final
            stage("decision reply result works")

            upstream.health["chat_title"] = "群聊B"
            upstream.health["listener_running"] = False
            await wait_until(
                lambda: any(change.chat_title == "群聊B" for change in chat_changes),
                "聊天切换推送",
            )
            await wait_until(
                lambda: any(state.listener_running is False for state in states),
                "监听状态推送",
            )
            health = await asyncio.to_thread(
                _get_json,
                f"http://127.0.0.1:{gateway_port}/api/v1/gateway/health",
            )
            assert health["status"] == "ok", health
            assert health["chat_title"] == "群聊B", health
            stage("chat/state push and health endpoint work")

            status = await asyncio.to_thread(
                _get_json,
                f"http://127.0.0.1:{gateway_port}/api/v1/gateway/status",
            )
            assert status["pin"]["app_id"] == "game", status["pin"]
            assert status["pin"]["chat_title"] == "群聊A", status["pin"]
            assert status["apps"][0]["rules"]["mode"] == "rules", status["apps"]
            assert status["apps"][0]["rules"]["source"] == "hello", status["apps"]
            assert status["stats"]["dispatched"] == 3, status["stats"]
            assert status["stats"]["consumed"] == 3, status["stats"]
            assert status["context"]["chat_title"] == "群聊B", status["context"]
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

    print(
        "PASS 端到端：顺序询问、置顶、代发回执、聊天与状态推送、健康接口",
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(), timeout=60))
