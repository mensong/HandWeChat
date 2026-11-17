# -*- coding: utf-8 -*-
"""应用注册表：配置顺序、连接状态和熔断计数。"""

from __future__ import annotations

import time
from dataclasses import dataclass

from handwechat.gateway.config import GatewayAppConfig, GatewayConfig


@dataclass
class AppRuntime:
    config: GatewayAppConfig
    connection: object | None = None
    failures: int = 0
    open_until: float = 0.0
    last_error: str | None = None
    offers: int = 0
    consumes: int = 0
    skips: int = 0
    timeouts: int = 0

    @property
    def id(self) -> str:
        return self.config.id

    @property
    def name(self) -> str:
        return self.config.name or self.config.id

    @property
    def priority(self) -> int:
        return self.config.priority

    @property
    def connected(self) -> bool:
        return self.connection is not None

    def circuit_open(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return now < self.open_until

    def record_success(self) -> None:
        self.failures = 0
        self.open_until = 0.0

    def record_failure(
        self,
        *,
        threshold: int,
        cooldown: float,
        now: float | None = None,
        error: str | None = None,
    ) -> None:
        now = time.monotonic() if now is None else now
        self.failures += 1
        if error:
            self.last_error = error
        if self.failures >= threshold:
            self.open_until = now + cooldown


class AppRegistry:
    def __init__(self, config: GatewayConfig) -> None:
        config.validate()
        self.config = config
        self._apps: dict[str, AppRuntime] = {
            app.id: AppRuntime(config=app) for app in config.apps
        }

    def get(self, app_id: str) -> AppRuntime | None:
        return self._apps.get(app_id)

    def all(self) -> list[AppRuntime]:
        return sorted(self._apps.values(), key=lambda app: (app.priority, app.id))

    def enabled(self) -> list[AppRuntime]:
        return [app for app in self.all() if app.config.enabled]

    def reload(self, config: GatewayConfig) -> list[str]:
        """换用新配置，保留同 id 应用的连接和统计，返回被移除的应用 id。"""

        config.validate()
        previous = self._apps
        apps: dict[str, AppRuntime] = {}
        for app_config in config.apps:
            runtime = previous.get(app_config.id)
            if runtime is None:
                runtime = AppRuntime(config=app_config)
            else:
                runtime.config = app_config
            apps[app_config.id] = runtime

        removed = [app_id for app_id in previous if app_id not in apps]
        self._apps = apps
        self.config = config
        return removed
