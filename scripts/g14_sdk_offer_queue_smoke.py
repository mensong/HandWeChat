# -*- coding: utf-8 -*-
"""SDK 消费冒烟：慢处理不堵收帧循环，过期 offer 直接回 rewrite。"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import websockets

from handwechat.gateway.client import GatewayClient
from handwechat.gateway.protocol import PROTOCOL_VERSION, Decision

HANDLER_SECONDS = 0.35
STALE_AFTER = 0.15


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def offer_payload(offer_id: str, *, deadline_ms: int) -> dict:
    return {
        "type": "offer",
        "offer_id": offer_id,
        "dispatch_id": f"dispatch-{offer_id}",
        "pinned": False,
        "deadline_ms": deadline_ms,
        "pin": None,
        "message": {
            "id": "1",
            "cursor": 1,
            "sender": "张三",
            "content": "你好",
            "confidence": 1.0,
        },
        "response": [],
    }


async def main() -> None:
    port = free_port()
    pong_after: dict[str, float] = {}
    decisions: dict[str, dict] = {}
    stale_replied = asyncio.Event()

    async def handler(socket) -> None:
        hello = json.loads(await socket.recv())
        assert hello["type"] == "hello", hello
        await socket.send(
            json.dumps(
                {
                    "type": "welcome",
                    "app_id": "demo",
                    "protocol": PROTOCOL_VERSION,
                    "heartbeat_seconds": 20,
                    "pin": None,
                    "context": {"chat_title": "群聊A", "chat_type": "group"},
                }
            )
        )
        await socket.send(
            json.dumps(
                {
                    "type": "upstream_state",
                    "online": True,
                    "listener_running": True,
                    "attached": True,
                    "chat_title": "群聊B",
                    "chat_type": "group",
                    "health_ok": True,
                    "upstream_connected": True,
                }
            )
        )
        await socket.send(json.dumps(offer_payload("slow", deadline_ms=5000)))
        await asyncio.sleep(STALE_AFTER)
        ping_sent = time.monotonic()
        await socket.send(json.dumps({"type": "ping", "ts": "t"}))
        await socket.send(json.dumps(offer_payload("stale", deadline_ms=20)))
        async for raw in socket:
            frame = json.loads(raw)
            if frame.get("type") == "pong":
                pong_after["delay"] = time.monotonic() - ping_sent
            elif frame.get("type") == "decision":
                decisions[str(frame.get("offer_id"))] = frame
                if frame.get("offer_id") == "stale":
                    stale_replied.set()

    handled: list[str] = []
    chats: list[str] = []

    async def on_offer(offer) -> Decision:
        handled.append(offer.offer_id)
        await asyncio.sleep(HANDLER_SECONDS)
        return Decision.rewrite()

    def on_chat_changed(info) -> None:
        chats.append(info.chat_title)

    server = await websockets.serve(handler, "127.0.0.1", port)
    client = GatewayClient(
        app_id="demo",
        url=f"ws://127.0.0.1:{port}",
        on_offer=on_offer,
        on_chat_changed=on_chat_changed,
        reconnect_seconds=0.5,
    )
    task = asyncio.create_task(client.run())
    try:
        await asyncio.wait_for(stale_replied.wait(), timeout=6)
        await asyncio.sleep(0.05)
    finally:
        await client.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        server.close()
        await server.wait_closed()

    assert handled == ["slow"], f"过期 offer 不应进入 on_offer：{handled}"
    assert chats == ["群聊B"], f"聊天切换应由 upstream_state 派生：{chats}"
    assert "slow" in decisions and "stale" in decisions, decisions
    assert decisions["stale"]["action"] == "rewrite", decisions["stale"]
    delay = pong_after.get("delay")
    assert delay is not None, "慢处理期间没有回 pong"
    assert delay < HANDLER_SECONDS, f"pong 被业务阻塞：{delay:.2f}s"
    print(f"PASS 慢处理期间 pong 延迟 {delay:.3f}s，过期 offer 未执行")


if __name__ == "__main__":
    asyncio.run(main())
