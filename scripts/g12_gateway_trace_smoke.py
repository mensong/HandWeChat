# -*- coding: utf-8 -*-
"""消息轨迹冒烟：记录每一步的入/出、消费方与未处理原因。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from handwechat.gateway.config import GatewayAppConfig, GatewayConfig
from handwechat.gateway.dispatcher import Dispatcher, GatewayStats
from handwechat.gateway.models import IncomingMessage, OfferTimeout
from handwechat.gateway.pin import PinManager
from handwechat.gateway.protocol import Decision, ResponseItem
from handwechat.gateway.registry import AppRegistry
from handwechat.gateway.trace import MessageLog


def app(app_id: str, priority: int, **kwargs) -> GatewayAppConfig:
    return GatewayAppConfig(id=app_id, priority=priority, **kwargs)


def message(content: str, cursor: int = 1) -> IncomingMessage:
    return IncomingMessage.from_upstream(
        {
            "id": str(cursor),
            "cursor": cursor,
            "sender": "张三",
            "content": content,
            "received_at": "2026-11-26T08:00:00+08:00",
            "is_latest": True,
            "confidence": 1.0,
        }
    )


class FakeOffer:
    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls: list[str] = []

    async def __call__(
        self, app_runtime, msg, *, pinned, pin, timeout_s, response
    ):
        self.calls.append(app_runtime.id)
        result = self.handler(app_runtime.id, msg)
        if isinstance(result, Exception):
            raise result
        return result, f"offer-{len(self.calls)}"


def build(config: GatewayConfig, handler, log: MessageLog):
    registry = AppRegistry(config)
    for runtime in registry.all():
        runtime.connection = object()
    pins = PinManager()

    async def send_reply(parts, *, app_id, dispatch_id="", offer_id=""):
        return len(parts), 0

    async def notify(_pin):
        return None

    offer = FakeOffer(handler)
    dispatcher = Dispatcher(
        registry,
        pins,
        config=config,
        offer=offer,
        send_reply=send_reply,
        notify_pin_expired=notify,
        stats=GatewayStats(),
        message_log=log,
    )
    return dispatcher, offer


async def scenario_chain_trace() -> None:
    log = MessageLog()
    config = GatewayConfig(
        apps=[
            app("fixer", 5, can_rewrite=True, rules=[{"keywords": ["签"]}]),
            app("checkin", 10, rules=[{"regex": "^签到$"}]),
            app("bot", 100, rules=[]),
        ]
    )

    def handler(app_id: str, msg: IncomingMessage):
        if app_id == "fixer":
            return Decision.rewrite(
                content="签到", response=[ResponseItem.new(text="已归一")]
            )
        if app_id == "checkin":
            assert msg.content == "签到", msg.content
            return Decision.consume(
                response=[
                    ResponseItem.keep("r-1"),
                    ResponseItem.new(text="签到成功"),
                ]
            )
        return Decision.rewrite()

    dispatcher, _offer = build(config, handler, log)
    outcome = await dispatcher.dispatch(message("签 到"))
    dispatcher.close()
    assert outcome.consumed_by == "checkin", outcome

    summary = log.recent(10)[0]
    assert summary["consumed_by"] == "checkin", summary
    assert summary["steps"] == 2, summary
    assert summary["tried"] == ["fixer", "checkin"], summary

    record = log.get(summary["id"])
    assert record is not None, summary
    assert record["content"] == "签 到", record
    assert record["final_content"] == "签到", record
    assert record["candidates"] == ["fixer", "bot"], record
    fixer_step, checkin_step = record["steps"]
    assert fixer_step["app_id"] == "fixer", fixer_step
    assert fixer_step["result"] == "rewrite", fixer_step
    assert fixer_step["input"]["content"] == "签 到", fixer_step
    assert fixer_step["output"]["content"] == "签到", fixer_step
    assert fixer_step["response_items"] == 1, fixer_step
    assert checkin_step["result"] == "consume", checkin_step
    assert checkin_step["response_items"] == 2, checkin_step
    assert record["reply"] == {"parts": 2, "text": "已归一 / 签到成功"}, record
    assert record["reason"] == "", record
    print("PASS 轨迹记录每步入/出、最终处理方与回复内容")


async def scenario_no_app_and_timeout() -> None:
    log = MessageLog()
    config = GatewayConfig(
        apps=[app("checkin", 10, rules=[{"regex": "^签到$"}])]
    )
    dispatcher, _offer = build(config, lambda app_id, msg: Decision.consume(), log)
    outcome = await dispatcher.dispatch(message("无关消息"))
    dispatcher.close()
    assert outcome.consumed_by is None, outcome
    record = log.get(log.recent(1)[0]["id"])
    assert record["reason"] == "no_app", record
    assert record["steps"] == [], record

    log = MessageLog()
    config = GatewayConfig(
        apps=[app("slow", 10, rules=[]), app("bot", 20, rules=[])]
    )

    def handler(app_id: str, _msg):
        if app_id == "slow":
            return OfferTimeout("模拟超时")
        return Decision.consume(response=[ResponseItem.new(text="兜底")])

    dispatcher, _offer = build(config, handler, log)
    outcome = await dispatcher.dispatch(message("任意内容"))
    dispatcher.close()
    assert outcome.consumed_by == "bot", outcome
    record = log.get(log.recent(1)[0]["id"])
    slow_step, bot_step = record["steps"]
    assert slow_step["result"] == "timeout", slow_step
    assert bot_step["result"] == "consume", bot_step
    assert record["timed_out"] == ["slow"], record
    assert record["candidates"] == ["slow", "bot"], record
    print("PASS 无匹配与超时都能在轨迹里定位")


async def scenario_rewrite_ignored() -> None:
    log = MessageLog()
    config = GatewayConfig(
        apps=[app("plain", 10, rules=[]), app("next", 20, rules=[])]
    )

    def handler(app_id: str, msg):
        if app_id == "plain":
            return Decision.rewrite(content="被篡改")
        assert msg.content == "原始消息", msg.content
        return Decision.consume()

    dispatcher, _offer = build(config, handler, log)
    await dispatcher.dispatch(message("原始消息"))
    dispatcher.close()
    record = log.get(log.recent(1)[0]["id"])
    plain_step, next_step = record["steps"]
    assert plain_step["result"] == "rewrite_ignored", plain_step
    assert plain_step["output"]["content"] == "原始消息", plain_step
    assert next_step["result"] == "consume", next_step
    print("PASS can_rewrite=false 的改写被标记为忽略且内容不变")


def scenario_ring_buffer() -> None:
    log = MessageLog(max_items=2)
    for index in range(3):
        log.add({"id": f"m{index}", "content": f"c{index}", "steps": []})
    summary_ids = [item["id"] for item in log.recent(10)]
    assert summary_ids == ["m2", "m1"], summary_ids
    assert log.recent(1)[0]["id"] == "m2"
    assert log.get("m0") is None
    assert log.get("m2") is not None
    print("PASS 环形缓冲按上限淘汰且最新的排在前面")


async def main() -> None:
    await scenario_chain_trace()
    await scenario_no_app_and_timeout()
    await scenario_rewrite_ignored()
    scenario_ring_buffer()
    print("全部通过")


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(), timeout=60))
