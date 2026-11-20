# -*- coding: utf-8 -*-
"""网关 FastAPI 服务：应用 WS 接入、状态查询与配置热加载。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
import json
import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket

from handwechat.gateway.config import GatewayConfig
from handwechat.gateway.dispatcher import Dispatcher, GatewayStats
from handwechat.gateway.hub import AppHub
from handwechat.gateway.logging_setup import configure_gateway_logging
from handwechat.gateway.models import now_iso
from handwechat.gateway.pin import PinManager
from handwechat.gateway.registry import AppRegistry
from handwechat.gateway.rules import summarize as summarize_rules
from handwechat.gateway.sends import SendCoordinator
from handwechat.gateway.upstream import UpstreamClient
from handwechat.paths import app_data_path, app_path
from handwechat.version import __version__

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
    _warn_duplicate_priorities(config)

    registry = AppRegistry(config)
    pins = PinManager()
    queue: asyncio.Queue = asyncio.Queue(maxsize=max(1, config.queue_size))
    upstream = UpstreamClient(config, queue)
    hub = AppHub(registry, pins, config=config)
    sender = SendCoordinator(
        upstream,
        hub.send_to,
        poll_interval=config.send_result_poll_seconds,
        timeout=config.send_result_timeout_seconds,
    )
    hub.set_sender(sender.send_from_app)
    hub.set_context_provider(upstream.context)
    stats = GatewayStats()
    dispatcher = Dispatcher(
        registry,
        pins,
        config=config,
        offer=hub.offer,
        send_reply=sender.send_for_decision,
        notify_pin_expired=hub.send_pin_expired,
        stats=stats,
        queue=queue,
        context_provider=upstream.context,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        health_state: dict[str, dict | None] = {"context": None}

        async def _on_health(_health: dict | None) -> None:
            context = upstream.context()
            previous = health_state["context"]
            health_state["context"] = context
            if previous is None:
                return
            if any(
                previous.get(key) != context.get(key)
                for key in ("online", "listener_running", "attached", "health_ok")
            ):
                await hub.broadcast({"type": "upstream_state", **context})
            if (
                previous.get("chat_title") != context.get("chat_title")
                or previous.get("chat_type") != context.get("chat_type")
            ):
                await hub.broadcast(
                    {
                        "type": "chat_changed",
                        "chat_title": context["chat_title"],
                        "chat_type": context["chat_type"],
                        "occurred_at": now_iso(),
                    }
                )

        tasks = [
            asyncio.create_task(upstream.run(), name="gateway-upstream"),
            asyncio.create_task(dispatcher.run(), name="gateway-dispatcher"),
            asyncio.create_task(
                upstream.poll_health(config.health_poll_seconds, _on_health),
                name="gateway-health",
            ),
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
            await sender.close()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="HandWeChat Gateway", version=__version__)
    app.router.lifespan_context = lifespan

    @app.get("/api/v1/gateway/health")
    def health() -> dict:
        context = upstream.context()
        healthy = bool(
            context["upstream_connected"]
            and context["online"]
            and context["health_ok"]
        )
        return {
            "status": "ok" if healthy else "degraded",
            **context,
            "upstream_url": upstream.ws_url,
            "last_error": upstream.last_error,
            "received": upstream.received,
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
                    "groups": list(runtime.config.groups),
                    "rules": {
                        **summarize_rules(runtime.effective_rules()),
                        "source": runtime.rules_source,
                    },
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
            "context": upstream.context(),
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


def _warn_duplicate_priorities(config: GatewayConfig) -> None:
    """重复优先级不阻断配置，记录告警，同值按 apps 数组顺序询问。"""

    for priority, ids in config.duplicate_priorities().items():
        logger.warning(
            json.dumps(
                {
                    "event": "duplicate_priority",
                    "priority": priority,
                    "apps": ids,
                },
                ensure_ascii=False,
            )
        )


def _console_notice(message: str) -> None:
    """热加载等运维事件直接打印到网关控制台窗口。"""

    try:
        print(f"[gateway {_clock()}] {message}", flush=True)
    except Exception:
        pass


def _clock() -> str:
    return datetime.now().strftime("%H:%M:%S")


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
        previous_ids = {app.id for app in registry.all()}
        try:
            new_config = GatewayConfig.load(path)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            # 配置写坏时保留旧配置，避免半生效。
            logger.error(
                json.dumps(
                    {"event": "config_reload_failed", "error": detail},
                    ensure_ascii=False,
                )
            )
            _console_notice(f"配置热加载失败，已保留旧配置：{detail}")
            continue

        new_config.log_dir = str(app_path(new_config.log_dir))
        _warn_duplicate_priorities(new_config)
        removed = registry.reload(new_config)
        hub.update_config(new_config)
        upstream.update_config(new_config)
        dispatcher.on_config_changed()
        for app_id in removed:
            await hub.close(app_id)

        added = sorted({app.id for app in new_config.apps} - previous_ids)
        order = " → ".join(
            f"{app.id}({app.priority})"
            for _, app in sorted(
                enumerate(new_config.apps),
                key=lambda item: (item[1].priority, item[0]),
            )
        ) or "（无应用）"
        logger.info(
            json.dumps(
                {
                    "event": "config_reloaded",
                    "apps": [app.id for app in new_config.apps],
                    "added": added,
                    "removed": removed,
                    "order": order,
                },
                ensure_ascii=False,
            )
        )
        _console_notice(
            f"配置已热加载：{len(new_config.apps)} 个应用"
            f"（新增 {len(added)}，移除 {len(removed)}）；顺序：{order}"
        )
        if removed:
            _console_notice(f"已断开移除的应用：{', '.join(removed)}")


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
