# -*- coding: utf-8 -*-
"""网关核心调度冒烟测试：不启动服务，用假应用验证顺序、加工链与置顶。"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from handwechat.gateway.config import GatewayAppConfig, GatewayConfig
from handwechat.gateway.dispatcher import Dispatcher, GatewayStats
from handwechat.gateway.models import IncomingMessage, OfferTimeout, PinState
from handwechat.gateway.pin import PinManager
from handwechat.gateway.protocol import Decision, ResponseItem
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
        self.responses: list[tuple[str, list]] = []

    async def __call__(
        self, app_runtime, msg, *, pinned, pin, timeout_s, response
    ):
        self.calls.append((app_runtime.id, pinned))
        self.responses.append((app_runtime.id, list(response)))
        result = self.handler(app_runtime.id, msg, pinned, response)
        if isinstance(result, Exception):
            raise result
        return result, f"offer-{len(self.calls)}"


def build(config: GatewayConfig, handler):
    registry = AppRegistry(config)
    for runtime in registry.all():
        runtime.connection = object()
    pins = PinManager()
    sent: list[tuple[str, list[str]]] = []
    expired: list[PinState] = []

    async def send_reply(parts, *, app_id, dispatch_id="", offer_id=""):
        texts = [part["text"] for part in parts if part.get("text")]
        sent.append((app_id, texts))
        return len(parts), 0

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
        lambda app_id, _msg, _pinned, _response: (
            Decision.consume(response=[ResponseItem.new(text="好的")])
            if app_id == "a"
            else Decision.rewrite()
        ),
    )
    outcome = await dispatcher.dispatch(message("42"))
    dispatcher.close()
    assert outcome.consumed_by == "a", outcome
    assert [call[0] for call in offer.calls] == ["a"], offer.calls
    assert sent == [("a", ["好的"])], sent
    print("PASS 优先级最高且消费后不再询问后续应用")


async def scenario_fallthrough() -> None:
    config = GatewayConfig(apps=[app("a", 10), app("b", 20)])
    dispatcher, offer, _pins, _sent, _expired = build(
        config,
        lambda app_id, _msg, _pinned, _response: (
            Decision.rewrite()
            if app_id == "a"
            else Decision.consume()
        ),
    )
    outcome = await dispatcher.dispatch(message("42"))
    dispatcher.close()
    assert outcome.consumed_by == "b", outcome
    assert [call[0] for call in offer.calls] == ["a", "b"], offer.calls
    print("PASS 无改动 rewrite 后继续询问下一个应用")


async def scenario_timeout() -> None:
    config = GatewayConfig(apps=[app("a", 10), app("b", 20)])
    dispatcher, offer, _pins, _sent, _expired = build(
        config,
        lambda app_id, _msg, _pinned, _response: (
            OfferTimeout("模拟超时")
            if app_id == "a"
            else Decision.consume()
        ),
    )
    outcome = await dispatcher.dispatch(message("42"))
    dispatcher.close()
    assert outcome.consumed_by == "b", outcome
    assert outcome.timed_out == ["a"], outcome
    assert [call[0] for call in offer.calls] == ["a", "b"], offer.calls
    print("PASS 应用超时后继续询问下一个应用")


async def scenario_chain() -> None:
    config = GatewayConfig(
        apps=[
            app(
                "fixer",
                10,
                can_rewrite=True,
                rules=[{"keywords": ["签"]}],
            ),
            app("checkin", 20, rules=[{"regex": "^签到$"}]),
            app("bot", 100, rules=[]),
        ]
    )

    def handler(app_id, msg, _pinned, response):
        if app_id == "fixer":
            assert msg.content == "签 到", msg.content
            assert not response, response
            return Decision.rewrite(
                content="签到",
                extra={"ocr": {"fixed": True}},
                response=[ResponseItem.new(text="已识别：签到")],
            )
        if app_id == "checkin":
            assert msg.content == "签到", msg.content
            assert msg.extra == {"ocr": {"fixed": True}}, msg.extra
            assert [item["id"] for item in response] == ["r-1"], response
            return Decision.consume(
                response=[
                    ResponseItem.keep("r-1"),
                    ResponseItem.new(text="签到成功"),
                ]
            )
        raise AssertionError(f"不该询问 {app_id}")

    dispatcher, offer, _pins, sent, _expired = build(config, handler)
    outcome = await dispatcher.dispatch(message("签 到"))
    dispatcher.close()
    assert outcome.consumed_by == "checkin", outcome
    assert outcome.rewritten_by == ["fixer"], outcome
    assert [call[0] for call in offer.calls] == ["fixer", "checkin"], offer.calls
    assert sent == [("checkin", ["已识别：签到", "签到成功"])], sent
    print("PASS 改写后按新内容重算候选，response 累积并在终止时统一发送")


async def scenario_chain_without_terminal() -> None:
    config = GatewayConfig(
        apps=[
            app("a", 10, can_rewrite=True, rules=[]),
            app("b", 20, can_rewrite=True, rules=[]),
        ]
    )
    dispatcher, offer, _pins, sent, _expired = build(
        config,
        lambda app_id, _msg, _pinned, _response: Decision.rewrite(
            response=[ResponseItem.new(text=f"{app_id}-reply")]
        ),
    )
    outcome = await dispatcher.dispatch(message("hello"))
    dispatcher.close()
    assert outcome.consumed_by is None, outcome
    assert outcome.rewritten_by == ["a", "b"], outcome
    assert [call[0] for call in offer.calls] == ["a", "b"], offer.calls
    assert sent == [], sent
    print("PASS 没有终止者时丢弃消息和累积的 response")


async def scenario_can_rewrite_false() -> None:
    config = GatewayConfig(apps=[app("plain", 10), app("next", 20)])

    def handler(app_id, msg, _pinned, _response):
        if app_id == "plain":
            return Decision.rewrite(content="被篡改", sender="李四")
        assert msg.content == "原始消息", msg.content
        assert msg.sender == "张三", msg.sender
        return Decision.consume()

    dispatcher, _offer, _pins, _sent, _expired = build(config, handler)
    outcome = await dispatcher.dispatch(message("原始消息"))
    dispatcher.close()
    assert outcome.consumed_by == "next", outcome
    print("PASS can_rewrite=false 的应用不能修改消息")


async def scenario_response_delete() -> None:
    config = GatewayConfig(
        apps=[
            app("a", 10, can_rewrite=True, rules=[]),
            app("b", 20, can_rewrite=True, rules=[]),
        ]
    )

    def handler(app_id, _msg, _pinned, response):
        if app_id == "a":
            return Decision.rewrite(
                response=[
                    ResponseItem.new(text="第一条"),
                    ResponseItem.new(text="第二条"),
                ]
            )
        assert [item["id"] for item in response] == ["r-1", "r-2"], response
        return Decision.consume(response=[ResponseItem.keep("r-2")])

    dispatcher, _offer, _pins, sent, _expired = build(config, handler)
    outcome = await dispatcher.dispatch(message("hello"))
    dispatcher.close()
    assert outcome.consumed_by == "b", outcome
    assert sent == [("b", ["第二条"])], sent
    print("PASS 下游可以删除已有 response 项")


async def scenario_pin() -> None:
    config = GatewayConfig(apps=[app("a", 10), app("b", 100, hold_seconds=0.3)])
    calls: list[tuple[str, str]] = []

    def handler(app_id: str, msg: IncomingMessage, pinned: bool, _response):
        content = msg.content
        calls.append((app_id, content))
        if content == "开始数字游戏":
            return (
                Decision.consume(pin=True, state="playing")
                if app_id == "b"
                else Decision.rewrite()
            )
        if content == "42":
            return (
                Decision.rewrite() if app_id == "b" else Decision.consume()
            )
        return Decision.rewrite()

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
    print("PASS 置顶应用优先，rewrite 后回落，超时后恢复原顺序")


async def scenario_pin_expired_event() -> None:
    config = GatewayConfig(apps=[app("a", 10), app("b", 100)])
    dispatcher, _offer, _pins, _sent, expired = build(
        config,
        lambda app_id, _msg, _pinned, _response: (
            Decision.consume(pin=True, hold_seconds=0.05, state="playing")
            if app_id == "b"
            else Decision.rewrite()
        ),
    )
    await dispatcher.dispatch(message("开始", 1))
    await asyncio.sleep(0.12)
    assert expired and expired[0].app_id == "b", expired
    dispatcher.close()
    print("PASS 置顶超时会通知应用")


async def scenario_app_ends_pin() -> None:
    config = GatewayConfig(apps=[app("a", 10), app("b", 100)])

    def handler(app_id: str, msg: IncomingMessage, pinned: bool, _response):
        if msg.content == "开始":
            return (
                Decision.consume(pin=True, state="playing")
                if app_id == "b"
                else Decision.rewrite()
            )
        return (
            Decision.consume(pin=False)
            if app_id == "b"
            else Decision.rewrite()
        )

    dispatcher, offer, pins, _sent, _expired = build(config, handler)
    await dispatcher.dispatch(message("开始", 1))
    assert pins.pin is not None
    await dispatcher.dispatch(message("退出数字游戏", 2))
    assert pins.pin is None, pins.pin
    dispatcher.close()
    print("PASS 应用可主动结束置顶")


def scenario_config_validation() -> None:
    config = GatewayConfig(
        apps=[app("a", 10), app("b", 10), app("c", 5), app("d", 10)]
    )
    config.validate()
    registry = AppRegistry(config)
    assert [item.id for item in registry.enabled()] == ["c", "a", "b", "d"]
    registry.reload(
        GatewayConfig(
            apps=[app("d", 10), app("c", 5), app("a", 10), app("b", 10)]
        )
    )
    assert [item.id for item in registry.enabled()] == ["c", "d", "a", "b"]

    system_config = GatewayConfig(
        system_apps=[app("sys-b", 5), app("sys-a", 5)],
        apps=[app("early", -10), app("late", 100)],
    )
    system_config.validate()
    system_registry = AppRegistry(system_config)
    assert [item.id for item in system_registry.enabled()] == [
        "sys-b",
        "sys-a",
        "early",
        "late",
    ]

    base = GatewayConfig(apps=[app("mover", 10)])
    mover_registry = AppRegistry(base)
    mover = mover_registry.get("mover")
    mover.consumes = 7
    mover_registry.reload(GatewayConfig(system_apps=[app("mover", 10)]))
    assert mover_registry.get("mover") is mover
    assert mover.system is True and mover.consumes == 7

    try:
        GatewayConfig(
            system_apps=[app("dup", 0)], apps=[app("dup", 10)]
        ).validate()
    except ValueError:
        pass
    else:
        raise AssertionError("system_apps 与 apps 的重复 id 未被拒绝")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "gateway_config.json"
        path.write_text(
            json.dumps(
                {
                    "listen_port": 8100,
                    "max_response_images": 3,
                    "system_apps": [
                        {
                            "id": "sys",
                            "priority": 0,
                            "can_rewrite": True,
                            "rules": [],
                        }
                    ],
                    "apps": [
                        {
                            "id": "a",
                            "priority": 10,
                            "hold_seconds": 30,
                            "can_rewrite": True,
                            "groups": ["公司群", "公司群", "测试群"],
                            "rules": [{"keywords": ["签到"]}],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        loaded = GatewayConfig.load(path)
        assert loaded.system_apps[0].id == "sys"
        assert loaded.all_apps()[0].id == "sys"
        assert loaded.apps[0].id == "a"
        assert loaded.apps[0].hold_seconds == 30
        assert loaded.apps[0].can_rewrite is True
        assert loaded.apps[0].groups == ["公司群", "测试群"], loaded.apps[0].groups
        assert loaded.apps[0].rules == [{"keywords": ["签到"]}], loaded.apps[0].rules
        assert loaded.max_response_images == 3, loaded.max_response_images

        bad_path = Path(tmp) / "bad_gateway_config.json"
        bad_path.write_text(
            json.dumps({"apps": [{"id": "a", "rules": [{}]}]}),
            encoding="utf-8",
        )
        try:
            GatewayConfig.load(bad_path)
        except ValueError:
            pass
        else:
            raise AssertionError("非法配置规则未被拒绝")
    print("PASS 配置校验、同优先级按数组顺序、系统应用区与加载")


async def main() -> None:
    await scenario_priority()
    await scenario_fallthrough()
    await scenario_timeout()
    await scenario_chain()
    await scenario_chain_without_terminal()
    await scenario_can_rewrite_false()
    await scenario_response_delete()
    await scenario_pin()
    await scenario_pin_expired_event()
    await scenario_app_ends_pin()
    scenario_config_validation()
    print("全部通过")


if __name__ == "__main__":
    asyncio.run(main())
