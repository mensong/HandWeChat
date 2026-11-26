# -*- coding: utf-8 -*-
"""串行调度：按优先级逐个询问应用，支持消息加工链与统一回复。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
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
    now_iso,
)
from handwechat.gateway.pin import PinManager
from handwechat.gateway.registry import AppRegistry, AppRuntime
from handwechat.gateway.response import ResponseError, ResponseState
from handwechat.gateway.rules import matches as rules_match
from handwechat.gateway.trace import MessageLog, clip

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
    rewrites: int = 0
    sends: int = 0
    send_failures: int = 0
    pin_expired: int = 0


def _new_step(app: AppRuntime, message: IncomingMessage, pinned: bool) -> dict:
    return {
        "app_id": app.id,
        "name": app.name,
        "system": app.system,
        "priority": app.priority,
        "pinned": pinned,
        "input": _content_of(message),
        "output": None,
        "result": "",
        "extra_changed": False,
        "response_items": 0,
        "elapsed_ms": 0.0,
    }


def _content_of(message: IncomingMessage) -> dict:
    return {"content": clip(message.content), "sender": message.sender}


def _elapsed_ms(started: float, now: float) -> float:
    return round((now - started) * 1000.0, 2)


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
        message_log: MessageLog | None = None,
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
        self.message_log = message_log
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
        working = message
        original_content = message.content
        response = ResponseState(
            max_items=config.max_response_items,
            max_text=config.max_response_text,
            max_images=config.max_response_images,
            max_image_bytes=config.max_response_image_bytes,
            max_total_bytes=config.max_response_total_bytes,
        )
        asked: set[str] = set()
        steps: list[dict] = []
        initial_candidates: list[str] = []

        while True:
            now = self._now()
            candidates = self._candidates(
                now, working, chat_title, chat_type, active_pin, asked
            )
            if candidates and not initial_candidates:
                initial_candidates = [item.id for item in candidates]

            if not candidates:
                self.stats.unconsumed += 1
                outcome.elapsed_ms = (now - started) * 1000.0
                self._log_dispatch(
                    working,
                    outcome,
                    reason="no_app",
                    candidates=[],
                    chat=chat_title,
                    original_content=original_content,
                    response_items=response.count,
                )
                self._record_trace(
                    message,
                    outcome,
                    chat=chat_title,
                    chat_type=chat_type,
                    original_content=original_content,
                    final_content=working.content,
                    steps=steps,
                    candidates=initial_candidates,
                    reason="no_app",
                    reply=None,
                    response_items=response.count,
                )
                return outcome

            remaining = deadline - now
            if remaining <= 0.001:
                self.stats.deadline_exceeded += 1
                break

            app = candidates[0]
            pinned = bool(
                active_pin is not None and active_pin.app_id == app.id
            )
            timeout_s = min(self._timeout_seconds(app), remaining)
            app.offers += 1
            outcome.tried.append(app.id)
            asked.add(app.id)

            step_started = self._now()
            step = _new_step(app, working, pinned)

            try:
                decision, offer_id = await self._offer(
                    app,
                    working,
                    pinned=pinned,
                    pin=active_pin,
                    timeout_s=timeout_s,
                    response=response.snapshot(),
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
                step["result"] = "timeout"
                step["elapsed_ms"] = _elapsed_ms(step_started, self._now())
                steps.append(step)
                continue
            except OfferUnavailable as exc:
                self.stats.unavailable += 1
                app.record_failure(
                    threshold=config.breaker_threshold,
                    cooldown=config.breaker_cooldown_seconds,
                    now=self._now(),
                    error=str(exc),
                )
                step["result"] = "unavailable"
                step["elapsed_ms"] = _elapsed_ms(step_started, self._now())
                steps.append(step)
                continue

            app.record_success()
            step["response_items"] = len(decision.response or [])
            if decision.action == "consume":
                app.consumes += 1
                self.stats.consumed += 1
                outcome.consumed_by = app.id
                self._apply_pin(app, decision, chat_title)
                self._apply_response(
                    app, response, decision, working.dispatch_id
                )
                step["result"] = "consume"
                step["output"] = _content_of(working)
                step["elapsed_ms"] = _elapsed_ms(step_started, self._now())
                steps.append(step)
                parts = response.final_parts()
                reply: dict = {"parts": 0, "text": ""}
                if parts:
                    await self._send(app, parts, working, offer_id)
                    texts = [
                        str(part.get("text") or "")
                        for part in parts
                        if part.get("text")
                    ]
                    reply = {
                        "parts": len(parts),
                        "text": clip(" / ".join(texts)),
                    }
                else:
                    self._log(
                        "reply_summary",
                        app_id=app.id,
                        dispatch_id=working.dispatch_id,
                        offer_id=offer_id,
                        parts=0,
                        text="",
                    )
                outcome.elapsed_ms = (self._now() - started) * 1000.0
                self._log_dispatch(
                    working,
                    outcome,
                    reason="",
                    candidates=[item.id for item in candidates],
                    chat=chat_title,
                    original_content=original_content,
                    response_items=response.count,
                )
                self._record_trace(
                    message,
                    outcome,
                    chat=chat_title,
                    chat_type=chat_type,
                    original_content=original_content,
                    final_content=working.content,
                    steps=steps,
                    candidates=initial_candidates,
                    reason="",
                    reply=reply,
                    response_items=response.count,
                )
                return outcome

            app.rewrites += 1
            self.stats.rewrites += 1
            outcome.rewritten_by.append(app.id)
            updated = self._apply_message(app, decision, working)
            ignored = not app.config.can_rewrite and (
                decision.content is not None
                or decision.sender is not None
                or decision.extra is not None
            )
            if ignored:
                step["result"] = "rewrite_ignored"
            elif updated is not working:
                step["result"] = "rewrite"
            else:
                step["result"] = "rewrite_noop"
            step["output"] = _content_of(updated)
            step["extra_changed"] = updated.extra != working.extra
            step["elapsed_ms"] = _elapsed_ms(step_started, self._now())
            steps.append(step)
            working = updated
            self._apply_response(
                app, response, decision, working.dispatch_id
            )

        outcome.elapsed_ms = (self._now() - started) * 1000.0
        self._log_dispatch(
            working,
            outcome,
            reason="deadline",
            candidates=[],
            chat=chat_title,
            original_content=original_content,
            response_items=response.count,
        )
        self._record_trace(
            message,
            outcome,
            chat=chat_title,
            chat_type=chat_type,
            original_content=original_content,
            final_content=working.content,
            steps=steps,
            candidates=initial_candidates,
            reason="deadline",
            reply=None,
            response_items=response.count,
        )
        return outcome

    def _record_trace(
        self,
        message: IncomingMessage,
        outcome: DispatchOutcome,
        *,
        chat: str,
        chat_type: str | None,
        original_content: str,
        final_content: str,
        steps: list[dict],
        candidates: list[str],
        reason: str,
        reply: dict | None,
        response_items: int,
    ) -> None:
        if self.message_log is None:
            return
        self.message_log.add(
            {
                "id": message.dispatch_id or message.id,
                "message_id": message.id,
                "received_at": message.received_at,
                "recorded_at": now_iso(),
                "sender": message.sender,
                "chat": chat,
                "chat_type": chat_type,
                "content": clip(original_content),
                "final_content": clip(final_content),
                "consumed_by": outcome.consumed_by,
                "reason": reason,
                "tried": list(outcome.tried),
                "timed_out": list(outcome.timed_out),
                "rewritten_by": list(outcome.rewritten_by),
                "candidates": list(candidates),
                "steps": steps,
                "reply": reply,
                "response_items": response_items,
                "elapsed_ms": round(outcome.elapsed_ms, 2),
            }
        )

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
        asked: set[str],
    ) -> list[AppRuntime]:
        pinned = (
            self.registry.get(active_pin.app_id)
            if active_pin is not None
            else None
        )
        system_apps: list[AppRuntime] = []
        normal_apps: list[AppRuntime] = []
        for app in self.registry.enabled():
            if (
                not app.connected
                or app.id in asked
                or app.circuit_open(now)
                or not self._group_eligible(app, chat_title)
            ):
                continue
            is_pinned = pinned is not None and app.id == pinned.id
            if not is_pinned and not rules_match(
                app.effective_rules(), message, chat_type
            ):
                continue
            if app.system:
                system_apps.append(app)
            else:
                normal_apps.append(app)
        if pinned is not None and not pinned.system and pinned.id not in asked:
            normal_apps = [pinned] + [
                app for app in normal_apps if app.id != pinned.id
            ]
        return system_apps + normal_apps

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

    def _apply_message(
        self,
        app: AppRuntime,
        decision: Decision,
        working: IncomingMessage,
    ) -> IncomingMessage:
        if not app.config.can_rewrite:
            if (
                decision.content is not None
                or decision.sender is not None
                or decision.extra is not None
            ):
                self._log(
                    "rewrite_ignored",
                    app_id=app.id,
                    dispatch_id=working.dispatch_id,
                    reason="can_rewrite=false",
                )
            return working

        updated = working
        if decision.content is not None and decision.content != updated.content:
            updated = replace(updated, content=decision.content)
        if decision.sender is not None and decision.sender != updated.sender:
            updated = replace(updated, sender=decision.sender)
        if decision.extra is not None:
            merged = dict(updated.extra)
            merged.update(decision.extra)
            if merged != updated.extra:
                updated = replace(updated, extra=merged)
        if updated is not working:
            self._log(
                "rewrite",
                app_id=app.id,
                dispatch_id=working.dispatch_id,
                content=working.content[:_MAX_LOG_CONTENT],
                new_content=updated.content[:_MAX_LOG_CONTENT],
                sender=updated.sender,
                extra_keys=sorted(updated.extra),
            )
        return updated

    def _apply_response(
        self,
        app: AppRuntime,
        state: ResponseState,
        decision: Decision,
        dispatch_id: str,
    ) -> None:
        if decision.response is None:
            return
        try:
            state.apply(app.id, app.config.can_rewrite, decision.response)
        except ResponseError as exc:
            self._log(
                "response_ignored",
                app_id=app.id,
                dispatch_id=dispatch_id,
                reason=str(exc),
            )
            return
        self._log(
            "response",
            app_id=app.id,
            dispatch_id=dispatch_id,
            items=state.count,
        )

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
        parts: list[dict],
        message: IncomingMessage,
        offer_id: str,
    ) -> None:
        try:
            result = await self._send_reply(
                parts,
                app_id=app.id,
                dispatch_id=message.dispatch_id,
                offer_id=offer_id,
            )
        except Exception as exc:
            self.stats.send_failures += len(parts)
            app.last_error = f"回复发送失败：{type(exc).__name__}: {exc}"
            logger.warning("应用 %s 的回复发送失败：%s", app.id, exc)
            return

        if isinstance(result, tuple) and len(result) == 2:
            sent, failed = int(result[0]), int(result[1])
        else:
            sent, failed = len(parts), 0
        self.stats.sends += sent
        self.stats.send_failures += failed
        if failed:
            app.last_error = f"{failed} 条回复发送失败"
        summary = next(
            (part["text"] for part in parts if part.get("text")), ""
        )
        self._log(
            "reply_summary",
            app_id=app.id,
            dispatch_id=message.dispatch_id,
            offer_id=offer_id,
            parts=len(parts),
            sent=sent,
            failed=failed,
            text=summary[:_MAX_LOG_CONTENT],
        )

    def _log_dispatch(
        self,
        message: IncomingMessage,
        outcome: DispatchOutcome,
        *,
        reason: str,
        candidates: list[str],
        chat: str,
        original_content: str = "",
        response_items: int = 0,
    ) -> None:
        fields = {
            "event": "dispatch",
            "dispatch_id": message.dispatch_id,
            "message_id": message.id,
            "sender": message.sender,
            "content": message.content[:_MAX_LOG_CONTENT],
            "original_content": original_content[:_MAX_LOG_CONTENT],
            "chat": chat,
            "candidates": candidates,
            "consumed_by": outcome.consumed_by,
            "tried": outcome.tried,
            "rewritten_by": outcome.rewritten_by,
            "timed_out": outcome.timed_out,
            "response_items": response_items,
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
