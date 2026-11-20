# -*- coding: utf-8 -*-
"""发送协调：统一代发，并向应用回执入队与最终发送结果。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable

from handwechat.gateway.upstream import UpstreamClient

logger = logging.getLogger("handwechat.gateway")

NotifyFunc = Callable[[str, dict], Awaitable[bool]]


class SendCoordinator:
    def __init__(
        self,
        upstream: UpstreamClient,
        notify: NotifyFunc,
        *,
        poll_interval: float = 0.5,
        timeout: float = 30.0,
    ) -> None:
        self._upstream = upstream
        self._notify = notify
        self._poll_interval = max(0.05, float(poll_interval))
        self._timeout = max(1.0, float(timeout))
        self._tasks: set[asyncio.Task] = set()

    async def send_for_decision(
        self,
        text: str,
        *,
        app_id: str,
        dispatch_id: str = "",
        offer_id: str = "",
        image: dict | None = None,
    ) -> None:
        await self._send(
            text,
            app_id=app_id,
            dispatch_id=dispatch_id,
            offer_id=offer_id,
            request_id="",
            image=image,
        )

    async def send_from_app(
        self,
        text: str,
        *,
        app_id: str,
        request_id: str = "",
        dispatch_id: str = "",
        image: dict | None = None,
    ) -> None:
        await self._send(
            text,
            app_id=app_id,
            dispatch_id=dispatch_id,
            offer_id="",
            request_id=request_id,
            image=image,
        )

    async def close(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def _send(
        self,
        text: str,
        *,
        app_id: str,
        dispatch_id: str,
        offer_id: str,
        request_id: str,
        image: dict | None = None,
    ) -> None:
        try:
            result = await self._upstream.send_text(text, image=image)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            await self._notify_safe(
                app_id,
                self._payload(
                    app_id=app_id,
                    dispatch_id=dispatch_id,
                    offer_id=offer_id,
                    request_id=request_id,
                    status="failed",
                    ok=False,
                    final=True,
                    error=error,
                ),
            )
            raise

        task_id = str(result.get("task_id") or "")
        status = str(result.get("status") or "queued")
        await self._notify_safe(
            app_id,
            self._payload(
                app_id=app_id,
                dispatch_id=dispatch_id,
                offer_id=offer_id,
                request_id=request_id,
                task_id=task_id,
                status=status,
                ok=True,
                final=False,
            ),
        )
        logger.info(
            json.dumps(
                {
                    "event": "reply",
                    "app_id": app_id,
                    "dispatch_id": dispatch_id,
                    "request_id": request_id,
                    "task_id": task_id,
                    "text": text[:200],
                    "image": bool(image),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        if task_id:
            task = asyncio.create_task(
                self._poll(
                    task_id,
                    app_id=app_id,
                    dispatch_id=dispatch_id,
                    offer_id=offer_id,
                    request_id=request_id,
                )
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _poll(
        self,
        task_id: str,
        *,
        app_id: str,
        dispatch_id: str,
        offer_id: str,
        request_id: str,
    ) -> None:
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(self._poll_interval)
            try:
                task = await self._upstream.fetch_send_task(task_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("发送任务查询失败：%s", exc)
                continue
            status = str(task.get("status") or "")
            if status in ("success", "failed"):
                await self._notify_safe(
                    app_id,
                    self._payload(
                        app_id=app_id,
                        dispatch_id=dispatch_id,
                        offer_id=offer_id,
                        request_id=request_id,
                        task_id=task_id,
                        status=status,
                        ok=status == "success",
                        final=True,
                        error=task.get("error_message"),
                        error_code=task.get("error_code"),
                    ),
                )
                return
        await self._notify_safe(
            app_id,
            self._payload(
                app_id=app_id,
                dispatch_id=dispatch_id,
                offer_id=offer_id,
                request_id=request_id,
                task_id=task_id,
                status="unknown",
                ok=False,
                final=True,
                error="发送结果查询超时",
            ),
        )

    async def _notify_safe(self, app_id: str, payload: dict) -> None:
        try:
            await self._notify(app_id, payload)
        except Exception:
            logger.debug("send_result 推送失败", exc_info=True)

    @staticmethod
    def _payload(
        *,
        app_id: str,
        dispatch_id: str,
        offer_id: str,
        request_id: str,
        task_id: str = "",
        status: str,
        ok: bool,
        final: bool,
        error: str | None = None,
        error_code: str | None = None,
    ) -> dict:
        return {
            "type": "send_result",
            "app_id": app_id,
            "dispatch_id": dispatch_id or "",
            "offer_id": offer_id or "",
            "request_id": request_id or "",
            "task_id": task_id,
            "status": status,
            "ok": ok,
            "final": final,
            "error": error,
            "error_code": error_code,
        }
