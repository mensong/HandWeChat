# -*- coding: utf-8 -*-
"""网关核心调度冒烟测试：不启动服务，用假应用验证顺序与置顶。"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from handwechat.gateway.config import GatewayAppConfig, GatewayConfig
from handwechat.gateway.dispatcher import Dispatcher, GatewayStats
from handwechat.gateway.models import (
    Decision,
    IncomingMessage,
    OfferTimeout,
    PinState,
)
from handwechat.gateway.pin import PinManager
from handwechat.gateway.registry import AppRegistry


def app(app_id: str, priority: int, **kwargs) -> GatewayAppConfig:
    return GatewayAppConfig(id=app_id, priority=priority, **kwargs)


def message(content: str, cursor: int = 1) -> IncomingMessage:
    return IncomingMessage.from_upstream(
        {
            "id": str(cursor),
            "cursor": cursor,
            "sender": "张三",
            "content": content,
            "received_at": "2026-11-17T18:00:00+08:00",
            "is_latest": True,
            "confidence": 1.0,
        }
    )


class FakeOffer:
    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls: list[tuple[str, bool]] = []

    async def __call__(self, app_runtime, msg, *, pinned, pin, timeout_s):
        self.calls.append((app_runtime.id, pinned))
        result = self.handler(app_runtime.id, msg, pinned)
        if isinstance(result, Exception):
            raise result
        return result, f"offer-{len(self.calls)}"


def build(config: GatewayConfig, handler):
    registry = AppRegistry(config)
    for runtime in registry.all():
        runtime.connection = object()
    pins = PinManager()
    sent: list[tuple[str, str]] = []
    expired: list[PinState] = []

    async def send_reply(text, *, app_id, dispatch_id="", offer_id=""):
        sent.append((app_id, text))

    async def notify(pin):
        expired.append(pin)

    offer = FakeOffer(handler)
    dispatcher = Dispatcher(
        registry,
        pins,
        config=config,
        offer=offer,
        send_reply=send_reply,
        notify_pin_expired=notify,
        stats=GatewayStats(),
    )
    return dispatcher, offer, pins, sent, expired


async def scenario_priority() -> None:
    config = GatewayConfig(apps=[app("a", 10), app("b", 20)])
    dispatcher, offer, _pins, sent, _expired = build(
        config,
        lambda app_id, _msg, _pinned: (
            Decision("consume", reply="好的") if app_id == "a" else Decision("skip")
        ),
    )
    outcome = await dispatcher.dispatch(message("42"))
    dispatcher.close()
    assert outcome.consumed_by == "a", outcome
    assert [call[0] for call in offer.calls] == ["a"], offer.calls
    assert sent == [("a", "好的")], sent
    print("PASS 优先级最高且消费后不再询问后续应用")


async def scenario_fallthrough() -> None:
    config = GatewayConfig(apps=[app("a", 10), app("b", 20)])
    dispatcher, offer, _pins, _sent, _expired = build(
        config,
        lambda app_id, _msg, _pinned: (
            Decision("skip") if app_id == "a" else Decision("consume")
        ),
    )
    outcome = await dispatcher.dispatch(message("42"))
    dispatcher.close()
    assert outcome.consumed_by == "b", outcome
    assert [call[0] for call in offer.calls] == ["a", "b"], offer.calls
    print("PASS skip 后继续询问下一个应用")


async def scenario_timeout() -> None:
    config = GatewayConfig(apps=[app("a", 10), app("b", 20)])
    dispatcher, offer, _pins, _sent, _expired = build(
        config,
        lambda app_id, _msg, _pinned: (
            OfferTimeout("模拟超时") if app_id == "a" else Decision("consume")
        ),
    )
    outcome = await dispatcher.dispatch(message("42"))
    dispatcher.close()
    assert outcome.consumed_by == "b", outcome
    assert outcome.timed_out == ["a"], outcome
    assert [call[0] for call in offer.calls] == ["a", "b"], offer.calls
    print("PASS 应用超时后继续询问下一个应用")


async def scenario_pin() -> None:
    config = GatewayConfig(apps=[app("a", 10), app("b", 100, hold_seconds=0.3)])
    calls: list[tuple[str, str]] = []

    def handler(app_id: str, msg: IncomingMessage, pinned: bool):
        content = msg.content
        calls.append((app_id, content))
        if content == "开始数字游戏":
            return (
                Decision("consume", pin=True, state="playing")
                if app_id == "b"
                else Decision("skip")
            )
        if content == "42":
            return Decision("skip") if app_id == "b" else Decision("consume")
        return Decision("skip")

    dispatcher, offer, pins, _sent, _expired = build(config, handler)
    await dispatcher.dispatch(message("开始数字游戏", 1))
    assert [call[0] for call in offer.calls] == ["a", "b"], offer.calls
    assert pins.pin is not None and pins.pin.app_id == "b"

    await dispatcher.dispatch(message("42", 2))
    assert [call[0] for call in offer.calls] == ["a", "b", "b", "a"], offer.calls
    assert offer.calls[2] == ("b", True), offer.calls
    await asyncio.sleep(0.35)
    assert pins.pin is None, pins.pin
    dispatcher.close()
    print("PASS 置顶应用优先，skip 后回落，超时后恢复原顺序")


async def scenario_pin_expired_event() -> None:
    config = GatewayConfig(apps=[app("a", 10), app("b", 100)])
    dispatcher, _offer, _pins, _sent, expired = build(
        config,
        lambda app_id, _msg, _pinned: (
            Decision("consume", pin=True, hold_seconds=0.05, state="playing")
            if app_id == "b"
            else Decision("skip")
        ),
    )
    await dispatcher.dispatch(message("开始", 1))
    await asyncio.sleep(0.12)
    assert expired and expired[0].app_id == "b", expired
    dispatcher.close()
    print("PASS 置顶超时会通知应用")


async def scenario_app_ends_pin() -> None:
    config = GatewayConfig(apps=[app("a", 10), app("b", 100)])

    def handler(app_id: str, msg: IncomingMessage, pinned: bool):
        if msg.content == "开始":
            return (
                Decision("consume", pin=True, state="playing")
                if app_id == "b"
                else Decision("skip")
            )
        return Decision("consume", pin=False) if app_id == "b" else Decision("skip")

    dispatcher, offer, pins, _sent, _expired = build(config, handler)
    await dispatcher.dispatch(message("开始", 1))
    assert pins.pin is not None
    await dispatcher.dispatch(message("退出数字游戏", 2))
    assert pins.pin is None, pins.pin
    dispatcher.close()
    print("PASS 应用可主动结束置顶")


def scenario_config_validation() -> None:
    try:
        GatewayConfig(apps=[app("a", 10), app("b", 10)]).validate()
    except ValueError:
        pass
    else:
        raise AssertionError("重复优先级应当报错")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "gateway_config.json"
        path.write_text(
            json.dumps(
                {
                    "listen_port": 8100,
                    "apps": [{"id": "a", "priority": 10, "hold_seconds": 30}],
                }
            ),
            encoding="utf-8",
        )
        loaded = GatewayConfig.load(path)
        assert loaded.apps[0].id == "a"
        assert loaded.apps[0].hold_seconds == 30
    print("PASS 配置校验与加载")


async def main() -> None:
    await scenario_priority()
    await scenario_fallthrough()
    await scenario_timeout()
    await scenario_pin()
    await scenario_pin_expired_event()
    await scenario_app_ends_pin()
    scenario_config_validation()
    print("全部通过")


if __name__ == "__main__":
    asyncio.run(main())
