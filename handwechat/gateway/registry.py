# -*- coding: utf-8 -*-
"""应用注册表：配置顺序、连接状态和熔断计数。"""

from __future__ import annotations

import time
from dataclasses import dataclass

from handwechat.gateway.config import GatewayAppConfig, GatewayConfig
from handwechat.gateway.rules import Rule, parse_rules


@dataclass
class AppRuntime:
    config: GatewayAppConfig
    order: int = 0
    system: bool = False
    system_order: int = 0
    connection: object | None = None
    hello_rules: tuple[Rule, ...] | None = None
    config_rules: tuple[Rule, ...] | None = None
    failures: int = 0
    open_until: float = 0.0
    last_error: str | None = None
    offers: int = 0
    consumes: int = 0
    rewrites: int = 0
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
    def sort_key(self) -> tuple[int, int, int]:
        """系统应用永远在普通应用之前，区内按 priority、数组顺序。"""

        if self.system:
            return (0, self.priority, self.system_order)
        return (1, self.priority, self.order)

    @property
    def connected(self) -> bool:
        return self.connection is not None

    def effective_rules(self) -> tuple[Rule, ...] | None:
        """配置规则优先；未配置时使用应用 hello 上报的规则。"""

        if self.config_rules is not None:
            return self.config_rules
        return self.hello_rules

    @property
    def rules_source(self) -> str:
        if self.config_rules is not None:
            return "config"
        if self.hello_rules is not None:
            return "hello"
        return "legacy"

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
        self._apps: dict[str, AppRuntime] = {}
        for system, index, app in _iter_config_apps(config):
            self._apps[app.id] = AppRuntime(
                config=app,
                order=index,
                system=system,
                system_order=index if system else 0,
                config_rules=_compile_config_rules(app),
            )

    def get(self, app_id: str) -> AppRuntime | None:
        return self._apps.get(app_id)

    def all(self) -> list[AppRuntime]:
        return sorted(self._apps.values(), key=lambda app: app.sort_key)

    def enabled(self) -> list[AppRuntime]:
        return [app for app in self.all() if app.config.enabled]

    def reload(self, config: GatewayConfig) -> list[str]:
        """换用新配置，保留同 id 应用的连接和统计，返回被移除的应用 id。"""

        config.validate()
        previous = self._apps
        apps: dict[str, AppRuntime] = {}
        for system, index, app_config in _iter_config_apps(config):
            runtime = previous.get(app_config.id)
            if runtime is None:
                runtime = AppRuntime(
                    config=app_config,
                    order=index,
                    system=system,
                    system_order=index if system else 0,
                    config_rules=_compile_config_rules(app_config),
                )
            else:
                runtime.config = app_config
                runtime.order = index
                runtime.system = system
                runtime.system_order = index if system else 0
                runtime.config_rules = _compile_config_rules(app_config)
            apps[app_config.id] = runtime

        removed = [app_id for app_id in previous if app_id not in apps]
        self._apps = apps
        self.config = config
        return removed


def _iter_config_apps(
    config: GatewayConfig,
) -> list[tuple[bool, int, GatewayAppConfig]]:
    """系统应用在前；返回 (是否系统应用, 所在数组序号, 应用配置)。"""

    items = [
        (True, index, app)
        for index, app in enumerate(config.system_apps)
    ]
    items.extend(
        (False, index, app) for index, app in enumerate(config.apps)
    )
    return items


def _compile_config_rules(
    config: GatewayAppConfig,
) -> tuple[Rule, ...] | None:
    if config.rules is None:
        return None
    return parse_rules(config.rules)
