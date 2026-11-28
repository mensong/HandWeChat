# -*- coding: utf-8 -*-
"""消费应用的 WebSocket 接入：hello、offer/decision、心跳与失效通知。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from handwechat.gateway.config import GatewayConfig
from handwechat.gateway.models import (
    Decision,
    IncomingMessage,
    OfferTimeout,
    OfferUnavailable,
    PinState,
    now_iso,
)
from handwechat.gateway.pin import PinManager
from handwechat.gateway.protocol import PROTOCOL_VERSION, parse_image
from handwechat.gateway.registry import AppRegistry, AppRuntime
from handwechat.gateway.rules import RuleError, parse_hello_rules

logger = logging.getLogger("handwechat.gateway")

_MAX_PENDING_EVENTS = 20
_MAX_UNANSWERED_OFFERS = 3
_MAX_SEND_SECONDS = 5.0


class AppConnection:
    def __init__(self, app_id: str, websocket: WebSocket) -> None:
        self.app_id = app_id
        self.websocket = websocket
        self.closed = False
        self.heartbeat_task: asyncio.Task | None = None
        self.opened_at = time.monotonic()
        # 只有客户端回过 pong 才按 pong 判活，兼容忽略 ping 的旧客户端。
        self.last_pong: float | None = None
        self.unanswered = 0
        self._send_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future] = {}

    async def send_json(
        self, payload: dict, *, timeout: float | None = None
    ) -> None:
        if self.closed:
            raise ConnectionError("应用连接已关闭")
        async with self._send_lock:
            sending = self.websocket.send_json(payload)
            if timeout is None:
                await sending
            else:
                await asyncio.wait_for(sending, timeout=max(0.5, float(timeout)))

    def expect(self, offer_id: str) -> asyncio.Future:
        future = asyncio.get_running_loop().create_future()
        self._pending[offer_id] = future
        return future

    def resolve(self, offer_id: str, decision: Decision | None) -> None:
        future = self._pending.pop(offer_id, None)
        if future is not None and not future.done():
            future.set_result(decision)

    def drop(self, offer_id: str) -> None:
        self._pending.pop(offer_id, None)

    def fail_pending(self) -> None:
        self.closed = True
        pending = list(self._pending.values())
        self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_result(None)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True
        try:
            await self.websocket.close(code=code, reason=reason)
        except Exception:
            pass

    def cancel_heartbeat(self) -> None:
        if self.heartbeat_task is not None:
            self.heartbeat_task.cancel()
            self.heartbeat_task = None


class AppHub:
    def __init__(
        self,
        registry: AppRegistry,
        pin_manager: PinManager,
        *,
        config: GatewayConfig,
        send_text=None,
        context_provider=None,
    ) -> None:
        self._registry = registry
        self._pins = pin_manager
        self._config = config
        self._send_text = send_text
        self._context_provider = context_provider
        self._connections: dict[str, AppConnection] = {}
        self._pending_events: dict[str, list[dict]] = {}

    def set_sender(self, send_text) -> None:
        self._send_text = send_text

    def set_context_provider(self, context_provider) -> None:
        self._context_provider = context_provider

    def update_config(self, config: GatewayConfig) -> None:
        self._config = config

    def connected(self, app_id: str) -> bool:
        return app_id in self._connections

    async def handle(self, websocket: WebSocket) -> None:
        await websocket.accept()
        connection: AppConnection | None = None
        close_code: int | None = None
        close_reason = ""
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
            try:
                hello = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.close(code=4400, reason="首帧不是合法 JSON")
                return

            app_id, code, reason = self._validate_hello(hello)
            if app_id is None:
                await websocket.close(code=code, reason=reason)
                return

            try:
                rules = parse_hello_rules(hello)
            except RuleError as exc:
                logger.warning("应用 %s 上报的 rules 非法：%s", app_id, exc)
                await websocket.close(code=4400, reason="rules 非法")
                return

            old = self._connections.pop(app_id, None)
            if old is not None:
                old.cancel_heartbeat()
                old.fail_pending()
                self._log_connection("app_replaced", old)
                await old.close(code=4009, reason="连接已被新连接替换")

            connection = AppConnection(app_id, websocket)
            self._connections[app_id] = connection
            runtime = self._registry.get(app_id)
            if runtime is not None:
                runtime.hello_rules = rules
                runtime.connection = connection
            self._log_connection("app_connected", connection)

            pin = self._pins.pin
            await connection.send_json(
                {
                    "type": "welcome",
                    "app_id": app_id,
                    "protocol": PROTOCOL_VERSION,
                    "heartbeat_seconds": self._config.heartbeat_seconds,
                    "pin": pin.to_payload() if pin and pin.app_id == app_id else None,
                    "context": self._context(),
                }
            )
            await self._flush_pending_events(app_id, connection)

            if self._config.heartbeat_seconds > 0:
                connection.heartbeat_task = asyncio.create_task(
                    self._heartbeat(connection)
                )

            await self._receive_loop(connection)
        except WebSocketDisconnect as exc:
            close_code = exc.code
            close_reason = exc.reason or ""
        except asyncio.CancelledError:
            close_reason = "服务停止"
        except Exception as exc:
            close_reason = f"{type(exc).__name__}: {exc}"
            logger.warning("应用 WS 连接异常：%s: %s", type(exc).__name__, exc)
        finally:
            if connection is not None:
                was_current = self._connections.get(connection.app_id) is connection
                connection.cancel_heartbeat()
                connection.fail_pending()
                if was_current:
                    self._connections.pop(connection.app_id, None)
                runtime = self._registry.get(connection.app_id)
                if runtime is not None and runtime.connection is connection:
                    runtime.connection = None
                self._log_connection(
                    "app_disconnected",
                    connection,
                    code=close_code,
                    reason=close_reason,
                    current=was_current,
                )

    async def offer(
        self,
        app: AppRuntime,
        message: IncomingMessage,
        *,
        pinned: bool,
        pin: PinState | None,
        timeout_s: float,
        response: list[dict],
    ) -> "tuple[Decision, str]":
        connection = self._connections.get(app.id)
        if connection is None:
            raise OfferUnavailable(f"{app.id} 未连接")

        offer_id = uuid.uuid4().hex
        future = connection.expect(offer_id)
        try:
            await connection.send_json(
                {
                    "type": "offer",
                    "offer_id": offer_id,
                    "dispatch_id": message.dispatch_id,
                    "pinned": pinned,
                    "deadline_ms": int(round(timeout_s * 1000)),
                    "pin": pin.to_payload() if pin is not None else None,
                    "message": message.to_payload(),
                    "response": response,
                },
                timeout=min(_MAX_SEND_SECONDS, max(1.0, timeout_s)),
            )
        except Exception as exc:
            connection.drop(offer_id)
            self._drop_connection(connection, code=4008, reason="推送失败")
            raise OfferUnavailable(f"{app.id} 推送失败：{exc}") from exc

        try:
            decision = await asyncio.wait_for(future, timeout=timeout_s)
        except asyncio.TimeoutError:
            # 只有用满该应用配置的超时才算「未响应」，被单轮 deadline
            # 截断的超时不计数，避免把慢但对的应用误判掉。
            full_timeout = (
                app.config.timeout_ms
                or self._registry.config.default_offer_timeout_ms
            ) / 1000.0
            if timeout_s >= full_timeout - 1e-6:
                connection.unanswered += 1
                if connection.unanswered >= _MAX_UNANSWERED_OFFERS:
                    # 连接看着在线却连续不回，断开让客户端重连自愈。
                    self._drop_connection(connection, code=4008, reason="连续未响应")
            raise OfferTimeout(f"{app.id} 超过 {int(timeout_s * 1000)}ms 未返回") from None
        finally:
            connection.drop(offer_id)

        if decision is None:
            raise OfferUnavailable(f"{app.id} 连接在等待中关闭")
        connection.unanswered = 0
        return decision, offer_id

    async def send_pin_expired(self, pin: PinState) -> None:
        payload = {"type": "pin_expired", **pin.to_payload()}
        connection = self._connections.get(pin.app_id)
        if connection is None:
            self._queue_event(pin.app_id, payload)
            return
        try:
            await connection.send_json(payload)
        except Exception:
            self._queue_event(pin.app_id, payload)

    async def send_to(self, app_id: str, payload: dict) -> bool:
        connection = self._connections.get(app_id)
        if connection is None:
            return False
        try:
            await connection.send_json(payload)
            return True
        except Exception:
            return False

    async def broadcast(self, payload: dict) -> None:
        for connection in list(self._connections.values()):
            try:
                await connection.send_json(payload)
            except Exception:
                continue

    async def close(self, app_id: str) -> None:
        connection = self._connections.pop(app_id, None)
        if connection is None:
            return
        connection.cancel_heartbeat()
        connection.fail_pending()
        self._log_connection("app_closed", connection, reason="应用已从配置移除")
        await connection.close(code=4001, reason="应用已从配置移除")
        runtime = self._registry.get(app_id)
        if runtime is not None and runtime.connection is connection:
            runtime.connection = None

    async def _receive_loop(self, connection: AppConnection) -> None:
        while True:
            raw = await connection.websocket.receive_text()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("应用 %s 发送了非法 JSON 帧", connection.app_id)
                continue
            if not isinstance(payload, dict):
                continue

            kind = payload.get("type")
            if kind == "decision":
                offer_id = str(payload.get("offer_id") or "")
                try:
                    decision = Decision.parse(payload)
                except ValueError as exc:
                    logger.warning(
                        "应用 %s 的 decision 非法，按无改动处理：%s",
                        connection.app_id,
                        exc,
                    )
                    decision = Decision.rewrite()
                connection.resolve(offer_id, decision)
            elif kind == "send":
                text = payload.get("text")
                if not isinstance(text, str):
                    text = ""
                try:
                    image = parse_image(payload.get("image"))
                except ValueError as exc:
                    logger.warning(
                        "应用 %s 的图片非法，已忽略：%s", connection.app_id, exc
                    )
                    image = None
                if (text or image) and self._send_text is not None:
                    asyncio.create_task(
                        self._send_from_app(
                            connection.app_id,
                            text,
                            str(payload.get("request_id") or ""),
                            str(payload.get("dispatch_id") or ""),
                            image,
                        )
                    )
            elif kind in ("ping", "pong"):
                if kind == "pong":
                    connection.last_pong = time.monotonic()
                continue
            else:
                logger.debug(
                    "应用 %s 发送了未知帧类型：%r", connection.app_id, kind
                )

    async def _heartbeat(self, connection: AppConnection) -> None:
        while not connection.closed:
            interval = max(1.0, self._config.heartbeat_seconds)
            await asyncio.sleep(interval)
            if connection.closed:
                return
            if (
                connection.last_pong is not None
                and time.monotonic() - connection.last_pong > interval * 2 + 5.0
            ):
                self._drop_connection(connection, code=4008, reason="心跳无响应")
                return
            try:
                await connection.send_json(
                    {"type": "ping", "ts": now_iso()},
                    timeout=max(_MAX_SEND_SECONDS, interval),
                )
            except Exception as exc:
                logger.warning("应用 %s 心跳发送失败：%s", connection.app_id, exc)
                self._drop_connection(connection, code=4008, reason="心跳发送失败")
                return

    def _drop_connection(
        self, connection: AppConnection, *, code: int, reason: str
    ) -> None:
        """立即摘除连接，关闭握手放到后台，避免拖住调度协程。"""

        if self._connections.get(connection.app_id) is connection:
            self._connections.pop(connection.app_id, None)
        runtime = self._registry.get(connection.app_id)
        if runtime is not None and runtime.connection is connection:
            runtime.connection = None
        if connection.heartbeat_task is asyncio.current_task():
            connection.heartbeat_task = None
        else:
            connection.cancel_heartbeat()
        connection.fail_pending()
        self._log_connection("app_dropped", connection, code=code, reason=reason)
        asyncio.create_task(self._close_quietly(connection, code, reason))

    @staticmethod
    async def _close_quietly(
        connection: AppConnection, code: int, reason: str
    ) -> None:
        try:
            await asyncio.wait_for(
                connection.close(code=code, reason=reason),
                timeout=_MAX_SEND_SECONDS,
            )
        except Exception:
            pass

    @staticmethod
    def _log_connection(
        event: str, connection: AppConnection, **extra
    ) -> None:
        payload = {
            "event": event,
            "app_id": connection.app_id,
            "seconds": round(time.monotonic() - connection.opened_at, 1),
            **extra,
        }
        logger.info(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )

    async def _send_from_app(
        self,
        app_id: str,
        text: str,
        request_id: str,
        dispatch_id: str,
        image: dict | None = None,
    ) -> None:
        try:
            await self._send_text(
                text,
                app_id=app_id,
                request_id=request_id,
                dispatch_id=dispatch_id,
                image=image,
            )
        except Exception as exc:
            logger.warning("应用 %s 主动发送失败：%s", app_id, exc)

    def _context(self) -> dict:
        if self._context_provider is None:
            return {}
        try:
            return self._context_provider()
        except Exception:
            logger.debug("读取上游上下文失败", exc_info=True)
            return {}

    def _queue_event(self, app_id: str, payload: dict) -> None:
        events = self._pending_events.setdefault(app_id, [])
        events.append(payload)
        if len(events) > _MAX_PENDING_EVENTS:
            del events[: len(events) - _MAX_PENDING_EVENTS]

    async def _flush_pending_events(
        self, app_id: str, connection: AppConnection
    ) -> None:
        events = self._pending_events.pop(app_id, [])
        for payload in events:
            try:
                await connection.send_json(payload)
            except Exception:
                self._queue_event(app_id, payload)
                return

    def _validate_hello(self, hello: dict) -> tuple[str | None, int, str]:
        if not isinstance(hello, dict) or hello.get("type") != "hello":
            return None, 4400, "首帧必须是 hello"
        if not _protocol_matches(hello.get("protocol")):
            return None, 4400, "protocol 版本不匹配"
        app_id = str(hello.get("app_id") or "").strip()
        if not app_id:
            return None, 4400, "hello 缺少 app_id"
        app = self._registry.get(app_id)
        if app is None or not app.config.enabled:
            return None, 4404, "应用未注册或已禁用"
        if app.config.token and str(hello.get("token") or "") != app.config.token:
            return None, 4401, "token 不匹配"
        return app_id, 0, ""


def _protocol_matches(value) -> bool:
    return value == PROTOCOL_VERSION or str(value) == str(PROTOCOL_VERSION)
