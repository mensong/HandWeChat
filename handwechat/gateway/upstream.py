# -*- coding: utf-8 -*-
"""上游 HandWeChat 客户端：订阅 WS、背压入队、健康快照、代发与任务查询。"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from typing import Awaitable, Callable

import websockets

from handwechat.gateway.config import GatewayConfig
from handwechat.gateway.models import IncomingMessage

logger = logging.getLogger("handwechat.gateway")

_REQUEST_TIMEOUT_S = 10.0

HealthCallback = Callable[[dict | None], Awaitable[None]]


class UpstreamClient:
    """唯一的上游业务订阅者；读到的消息只入队，不参与调度。"""

    def __init__(self, config: GatewayConfig, queue: asyncio.Queue) -> None:
        self._config = config
        self._queue = queue
        self.connected = False
        self.last_error: str | None = None
        self.received = 0
        self.health: dict | None = None
        self.health_error: str | None = None

    def update_config(self, config: GatewayConfig) -> None:
        self._config = config

    @property
    def ws_url(self) -> str:
        return self._config.upstream_ws_url

    def context(self) -> dict:
        """最近一次健康检查得到的当前上下文，供 welcome 和状态接口使用。"""

        health = self.health or {}
        return {
            "online": bool(health.get("online", False)),
            "listener_running": bool(health.get("listener_running", False)),
            "attached": bool(health.get("attached", False)),
            "chat_title": str(health.get("chat_title") or ""),
            "chat_type": health.get("chat_type"),
            "health_ok": self.health is not None and self.health_error is None,
            "health_error": self.health_error,
            "upstream_connected": self.connected,
        }

    async def run(self) -> None:
        while True:
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("上游连接中断：%s", self.last_error)
            finally:
                self.connected = False
            await asyncio.sleep(max(0.5, self._config.reconnect_seconds))

    async def poll_health(self, interval: float, on_change: HealthCallback) -> None:
        """定期读取 /api/v1/health，把变化交给调用方广播。"""

        interval = max(0.2, float(interval))
        while True:
            try:
                self.health = await asyncio.to_thread(
                    self._get_json, "/api/v1/health"
                )
                self.health_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.health_error = f"{type(exc).__name__}: {exc}"
            try:
                await on_change(self.health)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("上游健康状态回调失败")
            await asyncio.sleep(interval)

    async def send_text(self, text: str) -> dict:
        return await asyncio.to_thread(
            self._post_json, "/api/v1/send", {"text": text}
        )

    async def fetch_send_task(self, task_id: str) -> dict:
        return await asyncio.to_thread(
            self._get_json, f"/api/v1/send/{task_id}"
        )

    async def _connect_once(self) -> None:
        async with websockets.connect(
            self._config.upstream_ws_url,
            ping_interval=20,
            ping_timeout=20,
            max_size=None,
            open_timeout=10,
        ) as socket:
            self.connected = True
            self.last_error = None
            logger.info(
                json.dumps(
                    {"event": "upstream_connected", "url": self._config.upstream_ws_url},
                    ensure_ascii=False,
                )
            )
            async for raw in socket:
                await self._handle_raw(raw)

    async def _handle_raw(self, raw) -> None:
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("上游推送了无法解析的帧")
            return
        if not isinstance(payload, dict) or payload.get("type") != "messages":
            return

        for item in payload.get("items") or []:
            if not isinstance(item, dict):
                continue
            try:
                message = IncomingMessage.from_upstream(item)
            except (TypeError, ValueError) as exc:
                logger.warning("上游消息字段异常，已跳过：%s", exc)
                continue
            self.received += 1
            # 队列满时阻塞读协程，把背压传回上游，而不是丢弃消息。
            await self._queue.put(message)

    def _get_json(self, path: str) -> dict:
        request = urllib.request.Request(
            self._base_url() + path,
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_S) as response:
            return _loads(response.read())

    def _post_json(self, path: str, payload: dict) -> dict:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._base_url() + path,
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_S) as response:
            return _loads(response.read())

    def _base_url(self) -> str:
        return self._config.upstream_http_url.rstrip("/")


def _loads(raw: bytes) -> dict:
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8", errors="replace"))
