# -*- coding: utf-8 -*-
"""消费应用侧的网关客户端封装。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
import json
import logging
from typing import Any, Callable, Optional

import websockets
from websockets.exceptions import ConnectionClosed

from handwechat.gateway.protocol import Decision

DEFAULT_URL = "ws://127.0.0.1:8100/api/v1/gateway/ws/app"

_TERMINAL_CLOSE_CODES = {
    4009: "连接被同一应用的新连接替换",
    4400: "握手帧非法",
    4401: "token 不匹配",
    4404: "应用未注册或已禁用",
}


class GatewayClientError(Exception):
    """客户端无法继续运行。"""


class GatewayAuthError(GatewayClientError):
    """注册或认证失败，重连也不会成功。"""


@dataclass(frozen=True)
class PinInfo:
    app_id: str
    state: Optional[str] = None
    expires_at: Optional[str] = None
    hold_seconds: Optional[float] = None


@dataclass(frozen=True)
class Offer:
    """网关发来的一次询问。"""

    offer_id: str
    dispatch_id: str
    pinned: bool
    deadline_ms: int
    pin: Optional[PinInfo]
    content: str
    sender: str
    message: dict

    @property
    def message_id(self) -> str:
        return str(self.message.get("id") or "")

    @property
    def cursor(self) -> int:
        return int(self.message.get("cursor") or 0)

    @property
    def confidence(self) -> float:
        value = self.message.get("confidence")
        return 1.0 if value is None else float(value)

    @classmethod
    def from_payload(cls, payload: dict) -> "Offer":
        message = payload.get("message")
        if not isinstance(message, dict):
            message = {}
        return cls(
            offer_id=str(payload.get("offer_id") or ""),
            dispatch_id=str(payload.get("dispatch_id") or ""),
            pinned=bool(payload.get("pinned")),
            deadline_ms=int(payload.get("deadline_ms") or 0),
            pin=_pin_from(payload.get("pin")),
            content=str(message.get("content") or ""),
            sender=str(message.get("sender") or ""),
            message=message,
        )


OfferHandler = Callable[[Offer], Any]
PinHandler = Callable[[PinInfo], Any]
ConnectHandler = Callable[[Optional[PinInfo]], Any]


class GatewayClient:
    """自动重连、自动回 decision 的消费应用客户端。

    on_offer 可以返回 Decision、dict 或 None；None 和异常都按 skip 处理，
    保证一个处理失误不会让整条链路卡住。
    """

    def __init__(
        self,
        app_id: str,
        *,
        token: str = "",
        url: str = DEFAULT_URL,
        on_offer: Optional[OfferHandler] = None,
        on_pin_expired: Optional[PinHandler] = None,
        on_connected: Optional[ConnectHandler] = None,
        reconnect_seconds: float = 3.0,
        handshake_timeout: float = 10.0,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.app_id = app_id
        self.token = token
        self.url = url
        self.on_offer = on_offer
        self.on_pin_expired = on_pin_expired
        self.on_connected = on_connected
        self.reconnect_seconds = max(0.5, float(reconnect_seconds))
        self.handshake_timeout = max(1.0, float(handshake_timeout))
        self.logger = logger or logging.getLogger("handwechat.gateway.client")

        self.connected = False
        self.pin: Optional[PinInfo] = None
        self._socket = None
        self._stopped = False
        self._send_lock = asyncio.Lock()

    async def run(self) -> None:
        """连接网关并持续处理询问，直到 stop() 或遇到不可恢复的关闭码。"""

        while not self._stopped:
            try:
                await self._connect_once()
            except GatewayAuthError:
                raise
            except asyncio.CancelledError:
                raise
            except ConnectionClosed as exc:
                code = _close_code(exc)
                reason = _TERMINAL_CLOSE_CODES.get(code)
                if reason is not None:
                    raise GatewayAuthError(f"{reason}（close={code}）") from exc
                self.logger.warning("网关连接关闭（close=%s），准备重连", code)
            except Exception as exc:
                self.logger.warning(
                    "网关连接异常：%s: %s", type(exc).__name__, exc
                )
            finally:
                self.connected = False
                self.pin = None
                self._socket = None
            if self._stopped:
                break
            await asyncio.sleep(self.reconnect_seconds)

    async def stop(self) -> None:
        self._stopped = True
        socket = self._socket
        if socket is not None:
            try:
                await socket.close()
            except Exception:
                pass

    async def send(self, text: str, dispatch_id: str = "") -> None:
        """主动发一条微信消息（仍由网关统一发送）。"""

        payload = {"type": "send", "text": text}
        if dispatch_id:
            payload["dispatch_id"] = dispatch_id
        await self._send(payload)

    async def _connect_once(self) -> None:
        async with websockets.connect(
            self.url,
            ping_interval=20,
            ping_timeout=20,
            max_size=None,
            open_timeout=self.handshake_timeout,
        ) as socket:
            self._socket = socket
            await socket.send(
                json.dumps(
                    {"type": "hello", "app_id": self.app_id, "token": self.token},
                    ensure_ascii=False,
                )
            )
            raw = await asyncio.wait_for(
                socket.recv(), timeout=self.handshake_timeout
            )
            try:
                first = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                first = {}
            if first.get("type") != "welcome":
                raise GatewayClientError(f"握手失败：{first!r}")

            self.connected = True
            self.pin = _pin_from(first.get("pin"))
            await _maybe_await(self.on_connected, self.pin)

            async for raw in socket:
                await self._handle_raw(raw)

    async def _handle_raw(self, raw) -> None:
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.logger.warning("网关推送了无法解析的帧")
            return
        if not isinstance(payload, dict):
            return

        kind = payload.get("type")
        if kind == "offer":
            await self._handle_offer(payload)
        elif kind == "pin_expired":
            pin = _pin_from(payload)
            self.pin = None
            if pin is not None:
                await _maybe_await(self.on_pin_expired, pin)
        elif kind == "ping":
            try:
                await self._send({"type": "pong"})
            except Exception:
                pass
        elif kind == "welcome":
            self.pin = _pin_from(payload.get("pin"))

    async def _handle_offer(self, payload: dict) -> None:
        offer = Offer.from_payload(payload)
        decision = Decision.skip()
        try:
            result = None
            if self.on_offer is not None:
                result = self.on_offer(offer)
                if inspect.isawaitable(result):
                    result = await result
            if isinstance(result, Decision):
                decision = result
            elif isinstance(result, dict):
                decision = Decision.parse(result)
            elif result is not None:
                raise TypeError(f"on_offer 返回了不支持的类型：{type(result).__name__}")
        except Exception:
            self.logger.exception("处理 offer 失败，按 skip 返回")
            decision = Decision.skip()

        await self._send(
            {
                "type": "decision",
                "offer_id": offer.offer_id,
                **_decision_payload(decision),
            }
        )

    async def _send(self, payload: dict) -> None:
        socket = self._socket
        if socket is None:
            raise GatewayClientError("尚未连接网关")
        async with self._send_lock:
            await socket.send(json.dumps(payload, ensure_ascii=False))


async def _maybe_await(callback, *args) -> None:
    if callback is None:
        return
    result = callback(*args)
    if inspect.isawaitable(result):
        await result


def _pin_from(payload) -> Optional[PinInfo]:
    if not isinstance(payload, dict):
        return None
    app_id = str(payload.get("app_id") or "")
    if not app_id:
        return None
    hold = payload.get("hold_seconds")
    return PinInfo(
        app_id=app_id,
        state=payload.get("state"),
        expires_at=payload.get("expires_at"),
        hold_seconds=float(hold) if hold is not None else None,
    )


def _decision_payload(decision: Decision) -> dict:
    payload = {"action": decision.action}
    if decision.reply is not None:
        payload["reply"] = decision.reply
    if decision.pin is not None:
        payload["pin"] = decision.pin
    if decision.hold_seconds is not None:
        payload["hold_seconds"] = decision.hold_seconds
    if decision.state is not None:
        payload["state"] = decision.state
    return payload


def _close_code(exc: ConnectionClosed) -> Optional[int]:
    received = getattr(exc, "rcvd", None)
    return getattr(received, "code", None)
