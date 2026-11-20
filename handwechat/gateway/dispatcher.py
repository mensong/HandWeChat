# -*- coding: utf-8 -*-
"""串行调度：按优先级逐个询问应用，第一个消费的消息不再下发。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import logging
import time
from typing import Awaitable, Callable

from handwechat.gateway.config import GatewayConfig
from handwechat.gateway.models import (
    Decision,
    DispatchOutcome,
    IncomingMessage,
    OfferTimeout,
    OfferUnavailable,
    PinState,
)
from handwechat.gateway.pin import PinManager
from handwechat.gateway.registry import AppRegistry, AppRuntime
from handwechat.gateway.rules import matches as rules_match

logger = logging.getLogger("handwechat.gateway")

_MAX_LOG_CONTENT = 200

OfferFunc = Callable[..., Awaitable["tuple[Decision, str]"]]
SendReplyFunc = Callable[..., Awaitable[object]]
NotifyExpiredFunc = Callable[[PinState], Awaitable[None]]


@dataclass
class GatewayStats:
    dispatched: int = 0
    consumed: int = 0
    unconsumed: int = 0
    timeouts: int = 0
    unavailable: int = 0
    deadline_exceeded: int = 0
    sends: int = 0
    send_failures: int = 0
    pin_expired: int = 0


class Dispatcher:
    """单协程消费队列，保证一条消息完整走完顺序后才处理下一条。"""

    def __init__(
        self,
        registry: AppRegistry,
        pin_manager: PinManager,
        *,
        config: GatewayConfig,
        offer: OfferFunc,
        send_reply: SendReplyFunc,
        notify_pin_expired: NotifyExpiredFunc,
        stats: GatewayStats | None = None,
        queue: asyncio.Queue | None = None,
        now: Callable[[], float] = time.monotonic,
        context_provider: Callable[[], dict] | None = None,
    ) -> None:
        self.registry = registry
        self.pin_manager = pin_manager
        self.queue: asyncio.Queue[IncomingMessage] = queue or asyncio.Queue(
            maxsize=max(1, config.queue_size)
        )
        self.stats = stats or GatewayStats()
        self._offer = offer
        self._send_reply = send_reply
        self._notify_pin_expired = notify_pin_expired
        self._now = now
        self._context_provider = context_provider
        self._pin_token = 0
        self._pin_task: asyncio.Task | None = None

    async def run(self) -> None:
        while True:
            message = await self.queue.get()
            try:
                await self.dispatch(message)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("消息调度异常")
            finally:
                self.queue.task_done()

    async def dispatch(self, message: IncomingMessage) -> DispatchOutcome:
        started = self._now()
        config = self.registry.config
        deadline = started + config.round_deadline_ms / 1000.0
        outcome = DispatchOutcome()
        self.stats.dispatched += 1

        expired = self.pin_manager.expire_if_due(started)
        if expired is not None:
            await self._notify_expired(expired)

        context = self._context()
        chat_title = str(context.get("chat_title") or "")
        chat_type = context.get("chat_type")
        chat_type = str(chat_type) if chat_type else None

        active_pin = self._active_pin(started, chat_title)
        candidates = self._candidates(
            started, message, chat_title, chat_type, active_pin
        )
        if not candidates:
            self.stats.unconsumed += 1
            outcome.elapsed_ms = (self._now() - started) * 1000.0
            self._log_dispatch(
                message,
                outcome,
                reason="no_app",
                candidates=[],
                chat=chat_title,
            )
            return outcome

        for app in candidates:
            pin = active_pin
            remaining = deadline - self._now()
            if remaining <= 0.001:
                self.stats.deadline_exceeded += 1
                break

            pinned = bool(pin is not None and pin.app_id == app.id)
            timeout_s = min(self._timeout_seconds(app), remaining)
            app.offers += 1
            outcome.tried.append(app.id)

            try:
                decision, offer_id = await self._offer(
                    app,
                    message,
                    pinned=pinned,
                    pin=pin,
                    timeout_s=timeout_s,
                )
            except OfferTimeout as exc:
                self.stats.timeouts += 1
                app.timeouts += 1
                outcome.timed_out.append(app.id)
                app.record_failure(
                    threshold=config.breaker_threshold,
                    cooldown=config.breaker_cooldown_seconds,
                    now=self._now(),
                    error=str(exc),
                )
                continue
            except OfferUnavailable as exc:
                self.stats.unavailable += 1
                app.record_failure(
                    threshold=config.breaker_threshold,
                    cooldown=config.breaker_cooldown_seconds,
                    now=self._now(),
                    error=str(exc),
                )
                continue

            app.record_success()
            if decision.action == "consume":
                app.consumes += 1
                self.stats.consumed += 1
                outcome.consumed_by = app.id
                pin = self._apply_pin(app, decision, chat_title)
                if decision.reply:
                    await self._send(app, decision.reply, message, offer_id)
                break

            app.skips += 1
        else:
            self.stats.unconsumed += 1

        outcome.elapsed_ms = (self._now() - started) * 1000.0
        self._log_dispatch(
            message,
            outcome,
            reason="",
            candidates=[app.id for app in candidates],
            chat=chat_title,
        )
        return outcome

    def on_config_changed(self) -> None:
        """配置热加载后，置顶应用被移除或禁用时立即撤销置顶。"""

        pin = self.pin_manager.pin
        if pin is None:
            return
        app = self.registry.get(pin.app_id)
        if app is None or not app.config.enabled:
            self.pin_manager.clear(pin.app_id)
            self._schedule_pin_timer()
            self._log("pin_cleared", app_id=pin.app_id, reason="config_changed")

    @property
    def pin(self) -> PinState | None:
        return self.pin_manager.pin

    def close(self) -> None:
        if self._pin_task is not None:
            self._pin_task.cancel()
            self._pin_task = None

    def _candidates(
        self,
        now: float,
        message: IncomingMessage,
        chat_title: str,
        chat_type: str | None,
        active_pin: PinState | None,
    ) -> list[AppRuntime]:
        enabled = sorted(
            (
                app
                for app in self.registry.enabled()
                if app.connected
                and not app.circuit_open(now)
                and self._group_eligible(app, chat_title)
                and rules_match(app.effective_rules(), message, chat_type)
            ),
            key=lambda app: (app.priority, app.order),
        )
        pinned = (
            self.registry.get(active_pin.app_id)
            if active_pin is not None
            else None
        )
        if pinned is not None:
            return [pinned] + [app for app in enabled if app.id != pinned.id]
        return enabled

    @staticmethod
    def _group_eligible(app: AppRuntime, chat_title: str) -> bool:
        groups = app.config.groups
        if not groups:
            return True
        return bool(chat_title) and chat_title in groups

    def _active_pin(
        self, now: float, chat_title: str
    ) -> PinState | None:
        """置顶只提升顺序，不改变在线、组和熔断资格，但绕过规则匹配。"""

        pin = self.pin_manager.pin
        if pin is None:
            return None
        app = self.registry.get(pin.app_id)
        if (
            app is None
            or not app.config.enabled
            or not app.connected
            or app.circuit_open(now)
            or not self._group_eligible(app, chat_title)
            or not self._pin_chat_eligible(pin, chat_title)
        ):
            return None
        return pin

    @staticmethod
    def _pin_chat_eligible(pin: PinState, chat_title: str) -> bool:
        if not pin.chat_title or not chat_title:
            return True
        return pin.chat_title == chat_title

    def _timeout_seconds(self, app: AppRuntime) -> float:
        timeout_ms = app.config.timeout_ms or self.registry.config.default_offer_timeout_ms
        return max(0.05, timeout_ms / 1000.0)

    def _apply_pin(
        self, app: AppRuntime, decision: Decision, chat_title: str
    ) -> PinState | None:
        config = self.registry.config
        if decision.pin is True:
            hold = (
                decision.hold_seconds
                if decision.hold_seconds is not None
                else app.config.hold_seconds
            )
            pin = self.pin_manager.set(
                app.id,
                decision.state,
                hold,
                max_hold_seconds=config.max_hold_seconds,
                chat_title=chat_title,
            )
            self._schedule_pin_timer()
            if pin is not None:
                self._log(
                    "pin_set",
                    app_id=app.id,
                    state=pin.state,
                    hold_seconds=pin.hold_seconds,
                )
            return pin

        if decision.pin is False:
            cleared = self.pin_manager.clear(app.id)
            if cleared is not None:
                self._schedule_pin_timer()
                self._log("pin_cleared", app_id=app.id, reason="app_request")
        return self.pin_manager.pin

    def _schedule_pin_timer(self) -> None:
        if self._pin_task is not None:
            self._pin_task.cancel()
            self._pin_task = None
        self._pin_token += 1
        pin = self.pin_manager.pin
        if pin is None:
            return
        token = self._pin_token
        self._pin_task = asyncio.create_task(
            self._pin_timer(token, pin.remaining(self._now()))
        )

    async def _pin_timer(self, token: int, delay: float) -> None:
        try:
            await asyncio.sleep(max(0.0, delay))
        except asyncio.CancelledError:
            return
        if token != self._pin_token:
            return
        expired = self.pin_manager.expire_if_due(self._now())
        if expired is not None:
            await self._notify_expired(expired)
            return
        # 定时器提前醒来时按剩余时间重排，避免置顶一直挂着。
        pin = self.pin_manager.pin
        if pin is not None and token == self._pin_token:
            self._pin_task = asyncio.create_task(
                self._pin_timer(token, pin.remaining(self._now()))
            )

    async def _notify_expired(self, pin: PinState) -> None:
        self.stats.pin_expired += 1
        self._log(
            "pin_expired",
            app_id=pin.app_id,
            state=pin.state,
            expires_in=round(pin.remaining(self._now()), 3),
        )
        try:
            await self._notify_pin_expired(pin)
        except Exception:
            logger.exception("pin_expired 通知失败")

    async def _send(
        self,
        app: AppRuntime,
        text: str,
        message: IncomingMessage,
        offer_id: str,
    ) -> None:
        try:
            await self._send_reply(
                text,
                app_id=app.id,
                dispatch_id=message.dispatch_id,
                offer_id=offer_id,
            )
            self.stats.sends += 1
            self._log(
                "reply",
                app_id=app.id,
                dispatch_id=message.dispatch_id,
                offer_id=offer_id,
                text=text[:_MAX_LOG_CONTENT],
            )
        except Exception as exc:
            self.stats.send_failures += 1
            app.last_error = f"回复发送失败：{type(exc).__name__}: {exc}"
            logger.warning("应用 %s 的回复发送失败：%s", app.id, exc)

    def _log_dispatch(
        self,
        message: IncomingMessage,
        outcome: DispatchOutcome,
        *,
        reason: str,
        candidates: list[str],
        chat: str,
    ) -> None:
        fields = {
            "event": "dispatch",
            "dispatch_id": message.dispatch_id,
            "message_id": message.id,
            "sender": message.sender,
            "content": message.content[:_MAX_LOG_CONTENT],
            "chat": chat,
            "candidates": candidates,
            "consumed_by": outcome.consumed_by,
            "tried": outcome.tried,
            "timed_out": outcome.timed_out,
            "elapsed_ms": round(outcome.elapsed_ms, 2),
        }
        if reason:
            fields["reason"] = reason
        self._log_fields(fields)

    def _context(self) -> dict:
        if self._context_provider is None:
            return {}
        try:
            return self._context_provider() or {}
        except Exception:
            logger.debug("读取上游上下文失败", exc_info=True)
            return {}

    def _log(self, event: str, **fields) -> None:
        self._log_fields({"event": event, **fields})

    @staticmethod
    def _log_fields(fields: dict) -> None:
        logger.info(json.dumps(fields, ensure_ascii=False, separators=(",", ":")))
