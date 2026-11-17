# -*- coding: utf-8 -*-
"""上游 HandWeChat 客户端：订阅 WS 推送、背压入队、代发回复。"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request

import websockets

from handwechat.gateway.config import GatewayConfig
from handwechat.gateway.models import IncomingMessage

logger = logging.getLogger("handwechat.gateway")

_SEND_TIMEOUT_S = 10.0


class UpstreamClient:
    """唯一的上游业务订阅者；读到的消息只入队，不参与调度。"""

    def __init__(self, config: GatewayConfig, queue: asyncio.Queue) -> None:
        self._config = config
        self._queue = queue
        self.connected = False
        self.last_error: str | None = None
        self.received = 0

    def update_config(self, config: GatewayConfig) -> None:
        self._config = config

    @property
    def ws_url(self) -> str:
        return self._config.upstream_ws_url

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

    async def send_text(
        self, text: str, *, app_id: str, dispatch_id: str = ""
    ) -> dict:
        return await asyncio.to_thread(self._post_send, text)

    def _post_send(self, text: str) -> dict:
        data = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._config.upstream_http_url.rstrip("/") + "/api/v1/send",
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=_SEND_TIMEOUT_S) as response:
            body = response.read().decode("utf-8", errors="replace")
        return json.loads(body) if body else {}
