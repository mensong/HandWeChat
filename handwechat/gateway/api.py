# -*- coding: utf-8 -*-
"""网关 FastAPI 服务：应用 WS 接入、状态查询与配置热加载。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
import json
import logging
import time
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from handwechat.gateway.admin import (
    ConfigStore,
    GatewayAdmin,
    app_entry,
    register_routes,
)
from handwechat.gateway.config import GatewayConfig
from handwechat.gateway.dispatcher import Dispatcher, GatewayStats
from handwechat.gateway.hub import AppHub
from handwechat.gateway.logging_setup import configure_gateway_logging
from handwechat.gateway.pin import PinManager
from handwechat.gateway.registry import AppRegistry
from handwechat.gateway.sends import SendCoordinator
from handwechat.gateway.trace import MessageLog
from handwechat.gateway.upstream import UpstreamClient
from handwechat.paths import app_data_path, app_path, resource_path
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
    message_log = MessageLog()
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
        message_log=message_log,
    )

    # 没带 --config 启动时，管理接口仍写默认配置路径。
    write_path = watch_path or app_data_path("gateway_config.json")
    watch_state = (
        _WatchState(watch_path) if watch_path is not None else None
    )
    config_lock = asyncio.Lock()

    async def apply_config_data(data: dict) -> None:
        new_config = GatewayConfig.from_dict(data)
        await _apply_config(
            new_config,
            registry=registry,
            hub=hub,
            upstream=upstream,
            dispatcher=dispatcher,
        )
        if watch_state is not None:
            watch_state.mark()

    admin = GatewayAdmin(
        store=ConfigStore(
            write_path,
            seed=None if watch_path is not None else _seed_apps(config),
        ),
        registry=registry,
        apply=apply_config_data,
        lock=config_lock,
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
            # chat_title/chat_type 只在监听启动时绑定，重启换会话后会变；
            # 一并纳入状态判定，应用从 upstream_state 拿新绑定（不再单独推 chat_changed）。
            if any(
                previous.get(key) != context.get(key)
                for key in (
                    "online",
                    "listener_running",
                    "attached",
                    "health_ok",
                    "chat_title",
                    "chat_type",
                )
            ):
                await hub.broadcast({"type": "upstream_state", **context})

        tasks = [
            asyncio.create_task(upstream.run(), name="gateway-upstream"),
            asyncio.create_task(dispatcher.run(), name="gateway-dispatcher"),
            asyncio.create_task(
                upstream.poll_health(config.health_poll_seconds, _on_health),
                name="gateway-health",
            ),
            asyncio.create_task(
                _watch_loop_lag(), name="gateway-loop-lag"
            ),
        ]
        if watch_state is not None:
            tasks.append(
                asyncio.create_task(
                    _watch_config(
                        watch_state,
                        interval=max(0.5, config.config_reload_seconds),
                        registry=registry,
                        hub=hub,
                        upstream=upstream,
                        dispatcher=dispatcher,
                        lock=config_lock,
                    ),
                    name="gateway-config",
                )
            )
        logger.info(
            json.dumps(
                {
                    "event": "gateway_started",
                    "listen": f"{config.listen_host}:{config.listen_port}",
                    "apps": [app.id for app in config.all_apps()],
                    "system_apps": [
                        app.id for app in config.system_apps
                    ],
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
    register_routes(app, admin, message_log)

    @app.get("/", include_in_schema=False)
    def console() -> FileResponse:
        return FileResponse(
            resource_path("handwechat/gateway/static/index.html"),
            media_type="text/html",
        )

    app.mount(
        "/static",
        StaticFiles(directory=resource_path("handwechat/gateway/static")),
        name="static",
    )

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
        apps = [runtime.status_payload() for runtime in registry.all()]
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
    """重复优先级不阻断配置，记录告警，同值按各自数组顺序询问。"""

    for (block, priority), ids in config.duplicate_priorities().items():
        logger.warning(
            json.dumps(
                {
                    "event": "duplicate_priority",
                    "block": block,
                    "priority": priority,
                    "apps": ids,
                },
                ensure_ascii=False,
            )
        )


def _describe_order(config: GatewayConfig) -> str:
    """按系统区、普通区分别列出询问顺序，供热加载日志使用。"""

    def block(apps) -> str:
        return " → ".join(
            f"{app.id}({app.priority})"
            for _, app in sorted(
                enumerate(apps),
                key=lambda item: (item[1].priority, item[0]),
            )
        )

    parts = []
    system = block(config.system_apps)
    if system:
        parts.append(f"系统：{system}")
    normal = block(config.apps)
    if normal:
        parts.append(f"普通：{normal}")
    return "；".join(parts)


def _console_notice(message: str) -> None:
    """热加载等运维事件直接打印到网关控制台窗口。"""

    try:
        print(f"[gateway {_clock()}] {message}", flush=True)
    except Exception:
        pass


def _clock() -> str:
    return datetime.now().strftime("%H:%M:%S")


class _WatchState:
    """配置文件 mtime 记录；管理接口写完后同步，避免重复热加载。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.last_mtime = _mtime(path)

    def changed(self) -> bool:
        mtime = _mtime(self.path)
        if mtime == self.last_mtime:
            return False
        self.last_mtime = mtime
        return True

    def mark(self) -> None:
        self.last_mtime = _mtime(self.path)


async def _watch_config(
    watch: _WatchState,
    *,
    interval: float,
    registry: AppRegistry,
    hub: AppHub,
    upstream: UpstreamClient,
    dispatcher: Dispatcher,
    lock: asyncio.Lock,
) -> None:
    while True:
        await asyncio.sleep(interval)
        # 与管理接口共用一把锁：写文件和生效之间不会互相插队。
        async with lock:
            if not watch.changed():
                continue
            try:
                new_config = GatewayConfig.load(watch.path)
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
            await _apply_config(
                new_config,
                registry=registry,
                hub=hub,
                upstream=upstream,
                dispatcher=dispatcher,
            )


async def _apply_config(
    new_config: GatewayConfig,
    *,
    registry: AppRegistry,
    hub: AppHub,
    upstream: UpstreamClient,
    dispatcher: Dispatcher,
) -> tuple[list[str], list[str]]:
    """应用新配置并通知各组件，返回（新增, 移除）的应用 id。"""

    new_config.log_dir = str(app_path(new_config.log_dir))
    _warn_duplicate_priorities(new_config)
    previous_ids = {app.id for app in registry.all()}
    removed = registry.reload(new_config)
    hub.update_config(new_config)
    upstream.update_config(new_config)
    dispatcher.on_config_changed()
    for app_id in removed:
        await hub.close(app_id)

    added = sorted(
        {app.id for app in new_config.all_apps()} - previous_ids
    )
    order = _describe_order(new_config) or "（无应用）"
    logger.info(
        json.dumps(
            {
                "event": "config_reloaded",
                "apps": [app.id for app in new_config.all_apps()],
                "system_apps": [
                    app.id for app in new_config.system_apps
                ],
                "added": added,
                "removed": removed,
                "order": order,
            },
            ensure_ascii=False,
        )
    )
    _console_notice(
        f"配置已热加载：{len(new_config.all_apps())} 个应用"
        f"（新增 {len(added)}，移除 {len(removed)}）；顺序：{order}"
    )
    if removed:
        _console_notice(f"已断开移除的应用：{', '.join(removed)}")
    return added, removed


def _seed_apps(config: GatewayConfig) -> dict:
    """没有配置文件可用时，管理接口以内存配置里的应用数组为起点。"""

    seed = {
        "system_apps": [app_entry(app) for app in config.system_apps],
        "apps": [app_entry(app) for app in config.apps],
    }
    if config.admin_token:
        seed["admin_token"] = config.admin_token
    return seed


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


async def _watch_loop_lag(
    interval: float = 5.0, threshold: float = 5.0
) -> None:
    """事件循环卡顿看门狗：定时器迟到就记录，区分进程卡顿与宿主停顿。"""

    while True:
        started = time.monotonic()
        await asyncio.sleep(interval)
        lag = time.monotonic() - started - interval
        if lag > threshold:
            logger.warning(
                json.dumps(
                    {"event": "loop_lag", "lag_ms": round(lag * 1000.0)},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
