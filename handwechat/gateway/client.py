# -*- coding: utf-8 -*-
"""消费应用侧的网关客户端封装。"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, replace
import inspect
import json
import logging
import time
from typing import Any, Callable, Optional

import websockets
from websockets.exceptions import ConnectionClosed

from .protocol import (
    PROTOCOL_VERSION,
    Decision,
    ResponseItem,
)

DEFAULT_URL = "ws://127.0.0.1:8100/api/v1/gateway/ws/app"

_TERMINAL_CLOSE_CODES = {
    4400: "握手帧非法或协议版本不匹配",
    4401: "token 不匹配",
    4404: "应用未注册或已禁用",
}

_DEFAULT_OFFER_QUEUE = 100


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
    response: tuple[ResponseItem, ...] = ()

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

    @property
    def extra(self) -> dict:
        value = self.message.get("extra")
        return value if isinstance(value, dict) else {}

    @classmethod
    def from_payload(cls, payload: dict) -> "Offer":
        message = payload.get("message")
        if not isinstance(message, dict):
            message = {}
        response: list[ResponseItem] = []
        raw_response = payload.get("response")
        if isinstance(raw_response, list):
            for item in raw_response:
                try:
                    response.append(ResponseItem.from_offer(item))
                except ValueError:
                    continue
        return cls(
            offer_id=str(payload.get("offer_id") or ""),
            dispatch_id=str(payload.get("dispatch_id") or ""),
            pinned=bool(payload.get("pinned")),
            deadline_ms=int(payload.get("deadline_ms") or 0),
            pin=_pin_from(payload.get("pin")),
            content=str(message.get("content") or ""),
            sender=str(message.get("sender") or ""),
            message=message,
            response=tuple(response),
        )


OfferHandler = Callable[[Offer], Any]
PinHandler = Callable[[PinInfo], Any]
ConnectHandler = Callable[[Optional[PinInfo]], Any]


@dataclass(frozen=True)
class GatewayContext:
    """微信与 OCR 监听的上游状态，来自 welcome 和后续状态推送。"""

    online: bool = False
    listener_running: bool = False
    attached: bool = False
    chat_title: str = ""
    chat_type: Optional[str] = None
    health_ok: bool = False
    upstream_connected: bool = False

    @classmethod
    def from_payload(cls, payload: dict) -> "GatewayContext":
        data = payload if isinstance(payload, dict) else {}
        return cls(
            online=bool(data.get("online", False)),
            listener_running=bool(data.get("listener_running", False)),
            attached=bool(data.get("attached", False)),
            chat_title=str(data.get("chat_title") or ""),
            chat_type=data.get("chat_type"),
            health_ok=bool(data.get("health_ok", False)),
            upstream_connected=bool(data.get("upstream_connected", False)),
        )


@dataclass(frozen=True)
class ChatInfo:
    chat_title: str = ""
    chat_type: Optional[str] = None
    occurred_at: Optional[str] = None

    @classmethod
    def from_payload(cls, payload: dict) -> "ChatInfo":
        data = payload if isinstance(payload, dict) else {}
        return cls(
            chat_title=str(data.get("chat_title") or ""),
            chat_type=data.get("chat_type"),
            occurred_at=data.get("occurred_at"),
        )


@dataclass(frozen=True)
class SendResult:
    """网关代发的回执：final=False 是入队结果，final=True 是最终结果。"""

    task_id: str = ""
    offer_id: str = ""
    request_id: str = ""
    dispatch_id: str = ""
    part_index: int = 0
    part_count: int = 1
    status: str = ""
    ok: bool = False
    final: bool = False
    error: Optional[str] = None
    error_code: Optional[str] = None

    @classmethod
    def from_payload(cls, payload: dict) -> "SendResult":
        data = payload if isinstance(payload, dict) else {}
        return cls(
            task_id=str(data.get("task_id") or ""),
            offer_id=str(data.get("offer_id") or ""),
            request_id=str(data.get("request_id") or ""),
            dispatch_id=str(data.get("dispatch_id") or ""),
            part_index=int(data.get("part_index") or 0),
            part_count=int(data.get("part_count") or 1),
            status=str(data.get("status") or ""),
            ok=bool(data.get("ok", False)),
            final=bool(data.get("final", False)),
            error=data.get("error"),
            error_code=data.get("error_code"),
        )


SendResultHandler = Callable[[SendResult], Any]
ChatHandler = Callable[[ChatInfo], Any]
StateHandler = Callable[[GatewayContext], Any]


class GatewayClient:
    """自动重连、自动回 decision 的消费应用客户端。

    on_offer 可以返回 Decision、dict 或 None；None 和异常都按无改动
    rewrite 处理，保证一个处理失误不会让整条链路卡住。

    on_offer 在独立的消费任务里执行，不会堵住收帧循环；已经超过网关
    deadline 的 offer 直接回 rewrite，不调用 on_offer。
    """

    def __init__(
        self,
        app_id: str,
        *,
        token: str = "",
        url: str = DEFAULT_URL,
        rules: Optional[list[dict]] = None,
        on_offer: Optional[OfferHandler] = None,
        on_pin_expired: Optional[PinHandler] = None,
        on_connected: Optional[ConnectHandler] = None,
        on_send_result: Optional[SendResultHandler] = None,
        on_chat_changed: Optional[ChatHandler] = None,
        on_upstream_state: Optional[StateHandler] = None,
        reconnect_seconds: float = 3.0,
        handshake_timeout: float = 10.0,
        ping_interval: Optional[float] = 20.0,
        ping_timeout: Optional[float] = 20.0,
        offer_queue_size: int = _DEFAULT_OFFER_QUEUE,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.app_id = app_id
        self.token = token
        self.url = url
        self.rules = rules
        self.on_offer = on_offer
        self.on_pin_expired = on_pin_expired
        self.on_connected = on_connected
        self.on_send_result = on_send_result
        self.on_chat_changed = on_chat_changed
        self.on_upstream_state = on_upstream_state
        self.reconnect_seconds = max(0.5, float(reconnect_seconds))
        self.handshake_timeout = max(1.0, float(handshake_timeout))
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.offer_queue_size = max(1, int(offer_queue_size))
        self.logger = logger or logging.getLogger("handwechat.gateway.client")

        self.connected = False
        self.pin: Optional[PinInfo] = None
        self.context: Optional[GatewayContext] = None
        self._socket = None
        self._offers: Optional[asyncio.Queue] = None
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
                self.logger.warning(
                    "网关连接关闭（%s），准备重连", _close_detail(exc)
                )
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
        self.discard_pending_offers()
        socket = self._socket
        if socket is not None:
            try:
                await socket.close()
            except Exception:
                pass

    async def send(
        self,
        text: str,
        request_id: str = "",
        dispatch_id: str = "",
        image: bytes | None = None,
        image_name: str = "image",
    ) -> None:
        """主动发一条微信消息（仍由网关统一发送）。"""

        payload = {"type": "send", "text": text}
        if image is not None:
            payload["image"] = {
                "name": image_name or "image",
                "data": base64.b64encode(image).decode("ascii"),
            }
        if request_id:
            payload["request_id"] = request_id
        if dispatch_id:
            payload["dispatch_id"] = dispatch_id
        await self._send(payload)

    async def _connect_once(self) -> None:
        async with websockets.connect(
            self.url,
            ping_interval=self.ping_interval,
            ping_timeout=self.ping_timeout,
            max_size=None,
            open_timeout=self.handshake_timeout,
        ) as socket:
            self._socket = socket
            hello = {
                "type": "hello",
                "app_id": self.app_id,
                "protocol": PROTOCOL_VERSION,
                "token": self.token,
            }
            if self.rules is not None:
                hello["rules"] = self.rules
            await socket.send(json.dumps(hello, ensure_ascii=False))
            raw = await asyncio.wait_for(
                socket.recv(), timeout=self.handshake_timeout
            )
            try:
                first = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                first = {}
            if first.get("type") != "welcome":
                raise GatewayClientError(f"握手失败：{first!r}")
            if str(first.get("protocol")) != str(PROTOCOL_VERSION):
                raise GatewayAuthError(
                    "网关协议版本不匹配，请升级网关到 protocol 2"
                )

            self.connected = True
            self.pin = _pin_from(first.get("pin"))
            self.context = GatewayContext.from_payload(first.get("context") or {})
            await _maybe_await(self.on_connected, self.pin)

            self._offers = asyncio.Queue(maxsize=self.offer_queue_size)
            tasks = (
                asyncio.create_task(
                    self._receive_loop(socket), name="gateway-receive"
                ),
                asyncio.create_task(
                    self._offer_worker(), name="gateway-offers"
                ),
            )
            try:
                done, pending = await asyncio.wait(
                    set(tasks), return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    if task.cancelled():
                        continue
                    error = task.exception()
                    if error is not None:
                        raise error
                self.logger.warning("网关连接已结束，准备重连")
            except asyncio.CancelledError:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
            finally:
                self.discard_pending_offers()

    async def _receive_loop(self, socket) -> None:
        async for raw in socket:
            await self._handle_raw(raw)

    def discard_pending_offers(self) -> None:
        """丢弃积压的 offer，供 stop() 或断线时清理。"""

        queue = self._offers
        self._offers = None
        if queue is None:
            return
        dropped = 0
        while True:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            dropped += 1
        if dropped:
            self.logger.warning("断开前丢弃 %d 条未处理 offer", dropped)

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
            self._enqueue_offer(payload)
        elif kind == "send_result":
            await _maybe_await(
                self.on_send_result, SendResult.from_payload(payload)
            )
        elif kind == "upstream_state":
            previous = self.context
            context = GatewayContext.from_payload(payload)
            self.context = context
            if previous is not None and (
                previous.chat_title != context.chat_title
                or previous.chat_type != context.chat_type
            ):
                await _maybe_await(
                    self.on_chat_changed,
                    ChatInfo(
                        chat_title=context.chat_title,
                        chat_type=context.chat_type,
                    ),
                )
            await _maybe_await(self.on_upstream_state, context)
        elif kind == "chat_changed":
            info = ChatInfo.from_payload(payload)
            if self.context is not None:
                self.context = replace(
                    self.context,
                    chat_title=info.chat_title,
                    chat_type=info.chat_type,
                )
            await _maybe_await(self.on_chat_changed, info)
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

    def _enqueue_offer(self, payload: dict) -> None:
        offer = Offer.from_payload(payload)
        queue = self._offers
        if queue is None:
            return
        try:
            queue.put_nowait((offer, time.monotonic()))
        except asyncio.QueueFull:
            self.logger.warning(
                "offer 积压已满（%d 条），丢弃 offer_id=%s 并回 rewrite",
                queue.maxsize,
                offer.offer_id,
            )
            asyncio.create_task(self._drop_offer(offer))

    async def _drop_offer(self, offer: Offer) -> None:
        try:
            await self._reply_decision(offer, Decision.rewrite())
        except Exception as exc:
            self.logger.debug("丢弃 offer 后回 rewrite 失败：%s", exc)

    async def _offer_worker(self) -> None:
        queue = self._offers
        if queue is None:
            return
        while True:
            offer, received_at = await queue.get()
            if offer.deadline_ms > 0 and (
                time.monotonic() - received_at
            ) * 1000.0 > offer.deadline_ms:
                # 网关早已判超时，继续算只会让应用越落越远。
                await self._reply_decision(offer, Decision.rewrite())
                continue
            await self._handle_offer(offer)

    async def _handle_offer(self, offer: Offer) -> None:
        decision = Decision.rewrite()
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
            self.logger.exception("处理 offer 失败，按无改动 rewrite 返回")
            decision = Decision.rewrite()

        await self._reply_decision(offer, decision)

    async def _reply_decision(self, offer: Offer, decision: Decision) -> None:
        await self._send(
            {
                "type": "decision",
                "offer_id": offer.offer_id,
                **decision.to_payload(),
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


def _close_code(exc: ConnectionClosed) -> Optional[int]:
    received = getattr(exc, "rcvd", None)
    return getattr(received, "code", None)


def _close_detail(exc: ConnectionClosed) -> str:
    """把收到和发出的关闭帧都写出来，便于定位断线原因。"""

    parts = []
    frames = (
        ("rcvd", getattr(exc, "rcvd", None)),
        ("sent", getattr(exc, "sent", None)),
    )
    for label, frame in frames:
        if frame is None:
            continue
        reason = str(getattr(frame, "reason", "") or "").strip()
        parts.append(f"{label}={getattr(frame, 'code', None)} {reason}".strip())
    return "; ".join(parts) or "无关闭帧"
