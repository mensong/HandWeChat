# -*- coding: utf-8 -*-
"""网关路由冒烟测试：组过滤、规则命中、加工链重算与置顶隔离。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from handwechat.gateway.config import GatewayAppConfig, GatewayConfig
from handwechat.gateway.dispatcher import Dispatcher, GatewayStats
from handwechat.gateway.models import IncomingMessage
from handwechat.gateway.pin import PinManager
from handwechat.gateway.protocol import Decision
from handwechat.gateway.registry import AppRegistry
from handwechat.gateway.rules import RuleError, parse_hello_rules


def app(app_id: str, priority: int, **kwargs) -> GatewayAppConfig:
    return GatewayAppConfig(id=app_id, priority=priority, **kwargs)


def message(content: str, cursor: int = 1) -> IncomingMessage:
    return IncomingMessage.from_upstream(
        {
            "id": str(cursor),
            "cursor": cursor,
            "sender": "张三",
            "content": content,
            "received_at": "2026-11-19T18:00:00+08:00",
            "is_latest": True,
            "confidence": 1.0,
        }
    )


class FakeOffer:
    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls: list[tuple[str, bool]] = []

    async def __call__(
        self, app_runtime, msg, *, pinned, pin, timeout_s, response
    ):
        self.calls.append((app_runtime.id, pinned))
        return self.handler(app_runtime.id, msg, pinned), f"offer-{len(self.calls)}"


def build(config: GatewayConfig, handler, context: dict):
    registry = AppRegistry(config)
    for runtime in registry.all():
        runtime.connection = object()
    pins = PinManager()
    offer = FakeOffer(handler)

    async def send_reply(parts, *, app_id, dispatch_id="", offer_id=""):
        return len(parts), 0

    async def notify(pin):
        return None

    dispatcher = Dispatcher(
        registry,
        pins,
        config=config,
        offer=offer,
        send_reply=send_reply,
        notify_pin_expired=notify,
        stats=GatewayStats(),
        context_provider=lambda: context,
    )
    return dispatcher, offer, pins, registry


def set_rules(registry: AppRegistry, app_id: str, rules) -> None:
    registry.get(app_id).hello_rules = parse_hello_rules({"rules": rules})


async def scenario_group_filter() -> None:
    config = GatewayConfig(
        apps=[
            app("a-group", 10, groups=["群A"]),
            app("b-group", 20, groups=["群B"]),
        ]
    )
    dispatcher, offer, _pins, registry = build(
        config,
        lambda app_id, _msg, _pinned: Decision.consume(),
        {"chat_title": "群A", "chat_type": "group"},
    )
    for app_id in ("a-group", "b-group"):
        set_rules(registry, app_id, [{"keywords": ["签到"]}])
    outcome = await dispatcher.dispatch(message("签到"))
    dispatcher.close()
    assert outcome.consumed_by == "a-group", outcome
    assert [call[0] for call in offer.calls] == ["a-group"], offer.calls
    print("PASS 组外应用不会收到 offer")


async def scenario_rules_and_catch_all() -> None:
    config = GatewayConfig(
        apps=[
            app("checkin", 10),
            app("bot-high", 900),
            app("bot-mid", 950),
            app("bot-low", 1000),
        ]
    )

    def handler(app_id: str, msg: IncomingMessage, _pinned: bool) -> Decision:
        if msg.content == "签到":
            return (
                Decision.consume()
                if app_id == "checkin"
                else Decision.rewrite()
            )
        if msg.content == "全部放弃":
            return Decision.rewrite()
        if app_id == "bot-high":
            return Decision.rewrite()
        return Decision.consume()

    dispatcher, offer, _pins, registry = build(
        config,
        handler,
        {"chat_title": "群A", "chat_type": "group"},
    )
    set_rules(registry, "checkin", [{"keywords": ["签到"]}])
    set_rules(registry, "bot-high", [])
    set_rules(registry, "bot-mid", [])
    set_rules(registry, "bot-low", [])

    outcome = await dispatcher.dispatch(message("签到", 1))
    assert outcome.consumed_by == "checkin", outcome
    assert [call[0] for call in offer.calls] == ["checkin"], offer.calls

    outcome = await dispatcher.dispatch(message("随便聊聊", 2))
    assert outcome.consumed_by == "bot-mid", outcome
    assert [call[0] for call in offer.calls] == [
        "checkin",
        "bot-high",
        "bot-mid",
    ], offer.calls

    outcome = await dispatcher.dispatch(message("全部放弃", 3))
    assert outcome.consumed_by is None, outcome
    assert [call[0] for call in offer.calls] == [
        "checkin",
        "bot-high",
        "bot-mid",
        "bot-high",
        "bot-mid",
        "bot-low",
    ], offer.calls
    dispatcher.close()
    print("PASS 规则命中优先；通用机器人 rewrite 后继续顺延，全部放弃才丢弃")


async def scenario_catch_all_failover() -> None:
    config = GatewayConfig(apps=[app("bot-high", 900), app("bot-low", 1000)])
    dispatcher, offer, _pins, registry = build(
        config,
        lambda app_id, _msg, _pinned: Decision.consume(),
        {"chat_title": "群A", "chat_type": "group"},
    )
    set_rules(registry, "bot-high", [])
    set_rules(registry, "bot-low", [])
    registry.get("bot-high").connection = None

    outcome = await dispatcher.dispatch(message("随便聊聊"))
    dispatcher.close()
    assert outcome.consumed_by == "bot-low", outcome
    assert [call[0] for call in offer.calls] == ["bot-low"], offer.calls
    print("PASS 通用机器人离线时自动顺延到下一名")


async def scenario_rewrite_recomputes_rules() -> None:
    config = GatewayConfig(
        apps=[
            app(
                "fixer",
                10,
                can_rewrite=True,
                rules=[{"keywords": ["签"]}],
            ),
            app("legacy", 20, rules=[{"keywords": ["签 到"]}]),
            app("checkin", 30, rules=[{"regex": "^签到$"}]),
        ]
    )

    def handler(app_id, msg, _pinned):
        if app_id == "fixer":
            assert msg.content == "签 到", msg.content
            return Decision.rewrite(content="签到")
        assert msg.content == "签到", msg.content
        return Decision.consume()

    dispatcher, offer, _pins, _registry = build(
        config,
        handler,
        {"chat_title": "群A", "chat_type": "group"},
    )
    outcome = await dispatcher.dispatch(message("签 到"))
    dispatcher.close()
    assert outcome.consumed_by == "checkin", outcome
    assert outcome.rewritten_by == ["fixer"], outcome
    assert [call[0] for call in offer.calls] == ["fixer", "checkin"], offer.calls
    print("PASS 改写后按新内容重算：新命中的应用进入，旧命中的应用退出")


async def scenario_pin_bypass_and_chat_scope() -> None:
    config = GatewayConfig(apps=[app("checkin", 10), app("game", 100)])
    context = {"chat_title": "群A", "chat_type": "group"}

    def handler(app_id, msg, _pinned):
        if msg.content == "开始游戏":
            return (
                Decision.consume(pin=True, state="playing")
                if app_id == "game"
                else Decision.rewrite()
            )
        return Decision.rewrite()

    dispatcher, offer, pins, registry = build(config, handler, context)
    set_rules(registry, "checkin", [{"keywords": ["签到"]}])
    set_rules(registry, "game", [{"keywords": ["开始游戏"]}])

    await dispatcher.dispatch(message("开始游戏", 1))
    assert [call[0] for call in offer.calls] == ["game"], offer.calls
    assert pins.pin is not None and pins.pin.chat_title == "群A"

    context["chat_title"] = "群B"
    await dispatcher.dispatch(message("签到", 2))
    assert offer.calls[-1] == ("checkin", False), offer.calls
    dispatcher.close()
    print("PASS 置顶绕过规则，但只对同一个群生效")


async def scenario_unmatched_dropped() -> None:
    config = GatewayConfig(apps=[app("checkin", 10)])
    dispatcher, offer, _pins, registry = build(
        config,
        lambda app_id, _msg, _pinned: Decision.consume(),
        {"chat_title": "群A", "chat_type": "group"},
    )
    set_rules(registry, "checkin", [{"keywords": ["签到"]}])
    outcome = await dispatcher.dispatch(message("无关消息"))
    dispatcher.close()
    assert outcome.consumed_by is None, outcome
    assert offer.calls == [], offer.calls
    print("PASS 没有任何应用命中时直接丢弃")


async def scenario_config_rules_priority() -> None:
    config = GatewayConfig(
        apps=[app("a", 10, rules=[{"keywords": ["签到"]}])]
    )
    dispatcher, offer, _pins, registry = build(
        config,
        lambda app_id, _msg, _pinned: Decision.consume(),
        {"chat_title": "群A", "chat_type": "group"},
    )
    set_rules(registry, "a", [{"keywords": ["报名"]}])
    assert registry.get("a").rules_source == "config"

    outcome = await dispatcher.dispatch(message("签到", 1))
    assert outcome.consumed_by == "a", outcome

    outcome = await dispatcher.dispatch(message("报名", 2))
    assert outcome.consumed_by is None, outcome
    assert offer.calls == [("a", False)], offer.calls

    # 配置热加载立即覆盖已连接应用，不需要重连。
    registry.reload(
        GatewayConfig(apps=[app("a", 10, rules=[{"keywords": ["报名"]}])])
    )
    offer.calls.clear()
    outcome = await dispatcher.dispatch(message("报名", 3))
    assert outcome.consumed_by == "a", outcome

    # 配置显式空数组覆盖 hello，按全匹配处理。
    registry.reload(GatewayConfig(apps=[app("a", 10, rules=[])]))
    assert registry.get("a").rules_source == "config"
    offer.calls.clear()
    outcome = await dispatcher.dispatch(message("任意内容", 4))
    assert outcome.consumed_by == "a", outcome

    # 移除配置规则后回落到 hello 上报的规则。
    registry.reload(GatewayConfig(apps=[app("a", 10)]))
    assert registry.get("a").rules_source == "hello"
    offer.calls.clear()
    outcome = await dispatcher.dispatch(message("报名", 5))
    assert outcome.consumed_by == "a", outcome
    dispatcher.close()
    print("PASS 配置规则优先、热加载即时生效，移除后回落到 hello 规则")


def scenario_rule_validation() -> None:
    for raw in (
        {"rules": "bad"},
        {"rules": [{}]},
        {"rules": [{"regex": "("}]},
        {"rules": [{"chat_types": ["unknown"]}]},
    ):
        try:
            parse_hello_rules(raw)
        except RuleError:
            continue
        raise AssertionError(f"非法规则未被拒绝：{raw}")
    print("PASS 非法规则会被拒绝")


async def main() -> None:
    await scenario_group_filter()
    await scenario_rules_and_catch_all()
    await scenario_catch_all_failover()
    await scenario_rewrite_recomputes_rules()
    await scenario_pin_bypass_and_chat_scope()
    await scenario_unmatched_dropped()
    await scenario_config_rules_priority()
    scenario_rule_validation()
    print("全部通过")


if __name__ == "__main__":
    asyncio.run(main())
