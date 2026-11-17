# -*- coding: utf-8 -*-
"""网关 FastAPI 服务：应用 WS 接入、状态查询与配置热加载。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
import json
import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket

from handwechat.gateway.config import GatewayConfig
from handwechat.gateway.dispatcher import Dispatcher, GatewayStats
from handwechat.gateway.hub import AppHub
from handwechat.gateway.logging_setup import configure_gateway_logging
from handwechat.gateway.pin import PinManager
from handwechat.gateway.registry import AppRegistry
from handwechat.gateway.upstream import UpstreamClient
from handwechat.paths import app_data_path, app_path

logger = logging.getLogger("handwechat.gateway")


def create_app(
    *,
    config: GatewayConfig | None = None,
    config_path: str | Path | None = None,
) -> FastAPI:
    if config is None:
        watch_path: Path | None = (
            Path(config_path)
            if config_path is not None
            else app_data_path("gateway_config.json")
        )
        config = GatewayConfig.load(watch_path)
    else:
        watch_path = Path(config_path) if config_path is not None else None

    config.log_dir = str(app_path(config.log_dir))
    configure_gateway_logging(config)

    registry = AppRegistry(config)
    pins = PinManager()
    queue: asyncio.Queue = asyncio.Queue(maxsize=max(1, config.queue_size))
    upstream = UpstreamClient(config, queue)
    hub = AppHub(registry, pins, config=config, send_text=upstream.send_text)
    stats = GatewayStats()
    dispatcher = Dispatcher(
        registry,
        pins,
        config=config,
        offer=hub.offer,
        send_reply=upstream.send_text,
        notify_pin_expired=hub.send_pin_expired,
        stats=stats,
        queue=queue,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        tasks = [
            asyncio.create_task(upstream.run(), name="gateway-upstream"),
            asyncio.create_task(dispatcher.run(), name="gateway-dispatcher"),
        ]
        if watch_path is not None:
            tasks.append(
                asyncio.create_task(
                    _watch_config(watch_path, registry, hub, upstream, dispatcher, config),
                    name="gateway-config",
                )
            )
        logger.info(
            json.dumps(
                {
                    "event": "gateway_started",
                    "listen": f"{config.listen_host}:{config.listen_port}",
                    "apps": [app.id for app in config.apps],
                },
                ensure_ascii=False,
            )
        )
        try:
            yield
        finally:
            dispatcher.close()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="HandWeChat Gateway", version="0.1.0")
    app.router.lifespan_context = lifespan

    @app.get("/api/v1/gateway/health")
    def health() -> dict:
        return {
            "status": "ok",
            "upstream_connected": upstream.connected,
            "last_error": upstream.last_error,
        }

    @app.get("/api/v1/gateway/status")
    def status() -> dict:
        pin = pins.pin
        apps = []
        for runtime in registry.all():
            apps.append(
                {
                    "id": runtime.id,
                    "name": runtime.name,
                    "priority": runtime.priority,
                    "enabled": runtime.config.enabled,
                    "connected": runtime.connected,
                    "circuit_open": runtime.circuit_open(),
                    "timeout_ms": runtime.config.timeout_ms,
                    "hold_seconds": runtime.config.hold_seconds,
                    "offers": runtime.offers,
                    "consumes": runtime.consumes,
                    "skips": runtime.skips,
                    "timeouts": runtime.timeouts,
                    "last_error": runtime.last_error,
                }
            )
        return {
            "upstream": {
                "url": upstream.ws_url,
                "connected": upstream.connected,
                "last_error": upstream.last_error,
                "received": upstream.received,
            },
            "queue": {
                "size": dispatcher.queue.qsize(),
                "capacity": dispatcher.queue.maxsize,
            },
            "pin": pin.to_payload() if pin is not None else None,
            "apps": apps,
            "stats": asdict(stats),
        }

    @app.websocket("/api/v1/gateway/ws/app")
    async def app_ws(websocket: WebSocket) -> None:
        await hub.handle(websocket)

    return app


async def _watch_config(
    path: Path,
    registry: AppRegistry,
    hub: AppHub,
    upstream: UpstreamClient,
    dispatcher: Dispatcher,
    config: GatewayConfig,
) -> None:
    interval = max(0.5, config.config_reload_seconds)
    last_mtime = _mtime(path)
    while True:
        await asyncio.sleep(interval)
        mtime = _mtime(path)
        if mtime == last_mtime:
            continue
        last_mtime = mtime
        try:
            new_config = GatewayConfig.load(path)
        except Exception as exc:
            # 配置写坏时保留旧配置，避免半生效。
            logger.error(
                json.dumps(
                    {"event": "config_reload_failed", "error": f"{type(exc).__name__}: {exc}"},
                    ensure_ascii=False,
                )
            )
            continue

        new_config.log_dir = str(app_path(new_config.log_dir))
        removed = registry.reload(new_config)
        hub.update_config(new_config)
        upstream.update_config(new_config)
        dispatcher.on_config_changed()
        for app_id in removed:
            await hub.close(app_id)
        logger.info(
            json.dumps(
                {
                    "event": "config_reloaded",
                    "apps": [app.id for app in new_config.apps],
                    "removed": removed,
                },
                ensure_ascii=False,
            )
        )


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
