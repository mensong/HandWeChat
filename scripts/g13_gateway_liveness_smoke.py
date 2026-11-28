# -*- coding: utf-8 -*-
"""应用连接活性冒烟：pong 超时、心跳发送失败、连续未响应与推送失败都要摘除连接。"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from handwechat.gateway.config import GatewayAppConfig, GatewayConfig
from handwechat.gateway.hub import AppConnection, AppHub
from handwechat.gateway.models import (
    IncomingMessage,
    OfferTimeout,
    OfferUnavailable,
)
from handwechat.gateway.pin import PinManager
from handwechat.gateway.registry import AppRegistry


class FakeWebSocket:
    def __init__(self, *, fail_send: bool = False) -> None:
        self.sent: list[dict] = []
        self.fail_send = fail_send
        self.closed: tuple[int, str] | None = None

    async def send_json(self, payload: dict) -> None:
        if self.fail_send:
            raise ConnectionError("模拟发送失败")
        self.sent.append(payload)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)


def message(content: str = "签到") -> IncomingMessage:
    return IncomingMessage.from_upstream(
        {
            "id": "1",
            "cursor": 1,
            "sender": "张三",
            "content": content,
            "received_at": "2026-11-17T18:00:00+08:00",
            "is_latest": True,
            "confidence": 1.0,
        }
    )


def build(app_id: str = "demo", *, heartbeat_seconds: float = 1.0):
    config = GatewayConfig(
        heartbeat_seconds=heartbeat_seconds,
        apps=[GatewayAppConfig(id=app_id, timeout_ms=50)],
    )
    registry = AppRegistry(config)
    hub = AppHub(registry, PinManager(), config=config)
    return config, registry, hub


def attach(hub, registry, app_id: str, *, fail_send: bool = False):
    websocket = FakeWebSocket(fail_send=fail_send)
    connection = AppConnection(app_id, websocket)
    hub._connections[app_id] = connection
    registry.get(app_id).connection = connection
    return connection, websocket


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0.01)


async def scenario_pong_timeout() -> None:
    _config, registry, hub = build()
    connection, websocket = attach(hub, registry, "demo")
    connection.last_pong = time.monotonic() - 100.0
    await asyncio.wait_for(hub._heartbeat(connection), timeout=3)
    await settle()
    assert hub.connected("demo") is False, "pong 超时的连接应被摘除"
    assert connection.closed is True
    assert websocket.closed is not None and websocket.closed[0] == 4008
    assert registry.get("demo").connection is None
    print("PASS pong 超时的连接被摘除并关闭")


async def scenario_heartbeat_send_failure() -> None:
    _config, registry, hub = build()
    connection, _websocket = attach(hub, registry, "demo", fail_send=True)
    await asyncio.wait_for(hub._heartbeat(connection), timeout=3)
    await settle()
    assert hub.connected("demo") is False, "心跳发送失败的连接应被摘除"
    print("PASS 心跳发送失败后连接被摘除")


async def scenario_healthy_heartbeat() -> None:
    _config, registry, hub = build()
    connection, websocket = attach(hub, registry, "demo")
    connection.last_pong = time.monotonic()
    try:
        await asyncio.wait_for(hub._heartbeat(connection), timeout=1.5)
    except asyncio.TimeoutError:
        pass
    connection.cancel_heartbeat()
    await settle()
    assert hub.connected("demo") is True, "正常连接不应被摘除"
    assert websocket.sent and websocket.sent[-1]["type"] == "ping"
    print("PASS 正常连接只发 ping，不会被误摘")


async def scenario_unanswered_offers() -> None:
    _config, registry, hub = build()
    _connection, _websocket = attach(hub, registry, "demo")
    runtime = registry.get("demo")

    async def offer_once(timeout_s: float = 0.05) -> None:
        try:
            await hub.offer(
                runtime,
                message(),
                pinned=False,
                pin=None,
                timeout_s=timeout_s,
                response=[],
            )
        except OfferTimeout:
            return
        raise AssertionError("应用没有回 decision，应当超时")

    await offer_once()
    assert hub.connected("demo") is True, "单次超时不应断开"
    await offer_once()
    assert hub.connected("demo") is True, "两次超时仍不应断开"
    await offer_once()
    await settle()
    assert hub.connected("demo") is False, "连续未响应应摘除连接"
    print("PASS 连续 3 次未响应后连接被摘除")


async def scenario_truncated_timeouts() -> None:
    _config, registry, hub = build()
    _connection, _websocket = attach(hub, registry, "demo")
    runtime = registry.get("demo")
    for _ in range(5):
        try:
            # 被单轮 deadline 截断的超时不应计入未响应
            await hub.offer(
                runtime,
                message(),
                pinned=False,
                pin=None,
                timeout_s=0.01,
                response=[],
            )
        except OfferTimeout:
            pass
    await settle()
    assert hub.connected("demo") is True, "被截断的超时不应断开连接"
    print("PASS 被 deadline 截断的超时不计入未响应")


async def scenario_offer_send_failure() -> None:
    _config, registry, hub = build()
    attach(hub, registry, "demo", fail_send=True)
    runtime = registry.get("demo")
    try:
        await hub.offer(
            runtime,
            message(),
            pinned=False,
            pin=None,
            timeout_s=0.05,
            response=[],
        )
    except OfferUnavailable:
        pass
    else:
        raise AssertionError("推送失败应抛 OfferUnavailable")
    await settle()
    assert hub.connected("demo") is False, "推送失败的连接应被摘除"
    print("PASS offer 推送失败后连接被摘除")


async def main() -> None:
    await scenario_pong_timeout()
    await scenario_heartbeat_send_failure()
    await scenario_healthy_heartbeat()
    await scenario_unanswered_offers()
    await scenario_truncated_timeouts()
    await scenario_offer_send_failure()
    print("ALL PASS g13")


if __name__ == "__main__":
    asyncio.run(main())
