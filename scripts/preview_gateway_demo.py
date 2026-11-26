# -*- coding: utf-8 -*-
"""本地演示：假上游 + 网关 + 假应用持续产生消息，供管理页查看。

用法：python scripts/preview_gateway_demo.py
然后打开 http://127.0.0.1:8199/ ，管理令牌见 data/gateway_preview.json。
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
from fastapi import FastAPI, WebSocket

from handwechat.gateway.api import create_app
from handwechat.gateway.client import GatewayClient, Offer
from handwechat.gateway.config import GatewayConfig

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "data" / "gateway_preview.json"
MESSAGES = [
    "签 到",
    "今天天气怎么样",
    "通知一下明天十点开会",
    "帮我慢一点统计一下人数",
    "签到",
    "随便聊聊",
]
SENDER = "张三"


class FakeUpstream:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[dict] = asyncio.Queue()
        self.sent: list[str] = []
        self.health = {
            "status": "ok",
            "online": True,
            "listener_running": True,
            "attached": True,
            "chat_title": "测试群",
            "chat_type": "group",
        }
        self.app = self._build()

    def _build(self) -> FastAPI:
        app = FastAPI()

        @app.websocket("/api/v1/ws/messages")
        async def ws(websocket: WebSocket) -> None:
            await websocket.accept()
            await websocket.send_json({"type": "ready", "buffer_size": 0})
            try:
                while True:
                    await websocket.send_json(await self.queue.get())
            except Exception:
                return

        @app.get("/api/v1/health")
        async def health() -> dict:
            return self.health

        @app.post("/api/v1/send")
        async def send(body: dict) -> dict:
            self.sent.append(str(body.get("text") or ""))
            return {"task_id": "demo", "status": "queued"}

        @app.get("/api/v1/send/{task_id}")
        async def task(task_id: str) -> dict:
            return {"task_id": task_id, "status": "success"}

        return app


def keep_existing(offer: Offer) -> list[dict]:
    return [{"id": item.id} for item in offer.response]


async def decide_dispatch(offer: Offer):
    if "签 到" in offer.content:
        return {
            "action": "rewrite",
            "content": offer.content.replace("签 到", "签到"),
            "response": keep_existing(offer) + [{"text": "已归一"}],
        }
    return {"action": "rewrite"}


async def decide_fixer(offer: Offer):
    if "签到" not in offer.content and "签" in offer.content:
        return {"action": "rewrite", "content": "签到"}
    return {"action": "rewrite"}


async def decide_checkin(offer: Offer):
    if offer.content.strip() == "签到":
        return {
            "action": "consume",
            "response": keep_existing(offer) + [{"text": "签到成功，今天第 1 位"}],
        }
    return {"action": "rewrite"}


async def decide_slow(offer: Offer):
    if "慢" in offer.content:
        # 故意拖过 timeout_ms，演示网关超时后继续询问下一个应用。
        await asyncio.sleep(1.2)
    return {"action": "rewrite"}


async def decide_bot(offer: Offer):
    return {
        "action": "consume",
        "response": keep_existing(offer) + [{"text": "收到：" + offer.content}],
    }


DECIDERS = {
    "HandWeChat-Dispatch": decide_dispatch,
    "fixer": decide_fixer,
    "checkin": decide_checkin,
    "slow": decide_slow,
    "chatbot": decide_bot,
}


async def consumer(app_id: str, token: str, port: int) -> None:
    handler = DECIDERS[app_id]

    def on_offer(offer: Offer):
        return handler(offer)

    client = GatewayClient(
        app_id=app_id,
        token=token,
        url=f"ws://127.0.0.1:{port}/api/v1/gateway/ws/app",
        rules=None,
        on_offer=on_offer,
        reconnect_seconds=1.0,
    )
    await client.run()


def free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def wait_started(server: uvicorn.Server) -> None:
    for _ in range(200):
        if server.started:
            return
        await asyncio.sleep(0.05)
    raise RuntimeError("服务未能启动")


async def push_messages(upstream: FakeUpstream) -> None:
    cursor = 0
    index = 0
    while True:
        cursor += 1
        content = MESSAGES[index % len(MESSAGES)]
        index += 1
        await upstream.queue.put(
            {
                "type": "messages",
                "items": [
                    {
                        "id": uuid.uuid4().hex[:12],
                        "cursor": cursor,
                        "sender": SENDER,
                        "content": content,
                        "received_at": "",
                        "is_latest": True,
                        "confidence": 1.0,
                    }
                ],
            }
        )
        await asyncio.sleep(5)


async def main() -> None:
    config = GatewayConfig.load(CONFIG_PATH)
    upstream_port = free_port()
    upstream = FakeUpstream()
    upstream_server = uvicorn.Server(
        uvicorn.Config(
            upstream.app, host="127.0.0.1", port=upstream_port, log_level="critical"
        )
    )
    asyncio.create_task(upstream_server.serve(), name="demo-upstream")
    await wait_started(upstream_server)

    config.upstream_ws_url = (
        f"ws://127.0.0.1:{upstream_port}/api/v1/ws/messages"
    )
    config.upstream_http_url = f"http://127.0.0.1:{upstream_port}"
    config.health_poll_seconds = 0.5
    config.reconnect_seconds = 1.0
    config.send_result_poll_seconds = 0.2

    gateway = create_app(config=config, config_path=CONFIG_PATH)
    gateway_server = uvicorn.Server(
        uvicorn.Config(
            gateway,
            host=config.listen_host,
            port=config.listen_port,
            log_level="critical",
        )
    )
    asyncio.create_task(gateway_server.serve(), name="demo-gateway")
    await wait_started(gateway_server)

    for app in config.all_apps():
        asyncio.create_task(
            consumer(app.id, app.token, config.listen_port),
            name=f"demo-app-{app.id}",
        )

    print(
        f"演示已启动：http://127.0.0.1:{config.listen_port}/"
        f"（管理令牌 {config.admin_token}）",
        flush=True,
    )
    # 等假应用握手完成再推消息，避免第一条落在「没有应用在线」上。
    await asyncio.sleep(2)
    await push_messages(upstream)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
