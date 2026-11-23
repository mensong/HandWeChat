# -*- coding: utf-8 -*-
"""网关配置：上游地址、调度参数和注册应用。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from handwechat.gateway.rules import parse_rules


@dataclass
class GatewayAppConfig:
    """一个消费应用的注册信息；应用通过 WS 连上网关后才会被询问。"""

    id: str
    name: str = ""
    priority: int = 0
    token: str = ""
    timeout_ms: int = 800
    hold_seconds: float = 120.0
    enabled: bool = True
    can_rewrite: bool = False
    groups: list[str] = field(default_factory=list)
    rules: list[dict] | None = None


@dataclass
class GatewayConfig:
    upstream_ws_url: str = "ws://127.0.0.1:8000/api/v1/ws/messages"
    upstream_http_url: str = "http://127.0.0.1:8000"
    listen_host: str = "127.0.0.1"
    listen_port: int = 8100
    queue_size: int = 1000
    round_deadline_ms: int = 3000
    default_offer_timeout_ms: int = 800
    default_hold_seconds: float = 120.0
    max_hold_seconds: float = 3600.0
    heartbeat_seconds: float = 20.0
    reconnect_seconds: float = 3.0
    breaker_threshold: int = 2
    breaker_cooldown_seconds: float = 30.0
    config_reload_seconds: float = 2.0
    health_poll_seconds: float = 2.0
    send_result_poll_seconds: float = 0.5
    send_result_timeout_seconds: float = 30.0
    log_dir: str = "data/logs"
    gateway_log_file: str = "gateway.log"
    max_response_items: int = 20
    max_response_text: int = 2000
    max_response_images: int = 5
    max_response_image_bytes: int = 1024 * 1024
    max_response_total_bytes: int = 5 * 1024 * 1024
    system_apps: list[GatewayAppConfig] = field(default_factory=list)
    apps: list[GatewayAppConfig] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> "GatewayConfig":
        path = Path(path)
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("网关配置必须是 JSON 对象")

        system_apps = _parse_apps(data.get("system_apps"), "system_apps")
        apps = _parse_apps(data.get("apps"), "apps")
        config = cls(
            upstream_ws_url=str(
                data.get("upstream_ws_url") or "ws://127.0.0.1:8000/api/v1/ws/messages"
            ),
            upstream_http_url=str(
                data.get("upstream_http_url") or "http://127.0.0.1:8000"
            ),
            listen_host=str(data.get("listen_host") or "127.0.0.1"),
            listen_port=_as_int(data.get("listen_port"), 8100),
            queue_size=max(1, _as_int(data.get("queue_size"), 1000)),
            round_deadline_ms=max(100, _as_int(data.get("round_deadline_ms"), 3000)),
            default_offer_timeout_ms=max(
                50, _as_int(data.get("default_offer_timeout_ms"), 800)
            ),
            default_hold_seconds=max(
                0.0, _as_float(data.get("default_hold_seconds"), 120.0)
            ),
            max_hold_seconds=max(
                0.0, _as_float(data.get("max_hold_seconds"), 3600.0)
            ),
            heartbeat_seconds=max(
                0.0, _as_float(data.get("heartbeat_seconds"), 20.0)
            ),
            reconnect_seconds=max(
                0.5, _as_float(data.get("reconnect_seconds"), 3.0)
            ),
            breaker_threshold=max(1, _as_int(data.get("breaker_threshold"), 2)),
            breaker_cooldown_seconds=max(
                1.0, _as_float(data.get("breaker_cooldown_seconds"), 30.0)
            ),
            config_reload_seconds=max(
                0.5, _as_float(data.get("config_reload_seconds"), 2.0)
            ),
            health_poll_seconds=max(
                0.2, _as_float(data.get("health_poll_seconds"), 2.0)
            ),
            send_result_poll_seconds=max(
                0.05, _as_float(data.get("send_result_poll_seconds"), 0.5)
            ),
            send_result_timeout_seconds=max(
                1.0, _as_float(data.get("send_result_timeout_seconds"), 30.0)
            ),
            log_dir=str(data.get("log_dir") or "data/logs"),
            gateway_log_file=str(data.get("gateway_log_file") or "gateway.log"),
            max_response_items=max(
                1, _as_int(data.get("max_response_items"), 20)
            ),
            max_response_text=max(
                1, _as_int(data.get("max_response_text"), 2000)
            ),
            max_response_images=max(
                0, _as_int(data.get("max_response_images"), 5)
            ),
            max_response_image_bytes=max(
                1,
                _as_int(
                    data.get("max_response_image_bytes"), 1024 * 1024
                ),
            ),
            max_response_total_bytes=max(
                0,
                _as_int(
                    data.get("max_response_total_bytes"), 5 * 1024 * 1024
                ),
            ),
            system_apps=system_apps,
            apps=apps,
        )
        config.validate()
        return config

    def all_apps(self) -> list[GatewayAppConfig]:
        """系统应用在前、普通应用在后，保留各自的数组顺序。"""

        return [*self.system_apps, *self.apps]

    def validate(self) -> None:
        """应用 id 在 system_apps 与 apps 间必须唯一；priority 允许重复。"""

        seen_ids: set[str] = set()
        for label, apps in (
            ("system_apps", self.system_apps),
            ("apps", self.apps),
        ):
            for app in apps:
                if not app.id:
                    raise ValueError(f"{label} 中的应用 id 不能为空")
                if app.id in seen_ids:
                    raise ValueError(f"应用 id 重复：{app.id}")
                seen_ids.add(app.id)
                if app.rules is not None:
                    parse_rules(app.rules)

    def duplicate_priorities(self) -> dict[tuple[str, int], list[str]]:
        """按区和配置顺序返回重复出现的 priority 及其应用 id。"""

        groups: dict[tuple[str, int], list[str]] = {}
        for label, apps in (
            ("system_apps", self.system_apps),
            ("apps", self.apps),
        ):
            for app in apps:
                groups.setdefault((label, app.priority), []).append(app.id)
        return {
            key: ids for key, ids in groups.items() if len(ids) > 1
        }

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_apps(value, label: str) -> list[GatewayAppConfig]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{label} 必须是数组")
    apps: list[GatewayAppConfig] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(f"{label} 中的每一项必须是对象")
        apps.append(_parse_app(item))
    return apps


def _parse_app(data: dict) -> GatewayAppConfig:
    app_id = str(data.get("id") or "").strip()
    return GatewayAppConfig(
        id=app_id,
        name=str(data.get("name") or app_id),
        priority=_as_int(data.get("priority"), 0),
        token=str(data.get("token") or ""),
        timeout_ms=max(50, _as_int(data.get("timeout_ms"), 800)),
        hold_seconds=max(0.0, _as_float(data.get("hold_seconds"), 120.0)),
        enabled=bool(data.get("enabled", True)),
        can_rewrite=bool(data.get("can_rewrite", False)),
        groups=_parse_groups(data.get("groups")),
        rules=_parse_config_rules(data),
    )


def _parse_config_rules(data: dict) -> list[dict] | None:
    """配置里显式写了 rules 就校验并保留原始结构；缺省返回 None。"""

    if "rules" not in data:
        return None
    raw = data.get("rules")
    parse_rules(raw)
    return raw


def _parse_groups(value) -> list[str]:
    """组的白名单；空数组表示在所有组生效。"""

    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("groups 必须是数组")
    groups: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("groups 中的每一项必须是字符串")
        name = item.strip()
        if not name:
            raise ValueError("groups 不能包含空字符串")
        if name not in groups:
            groups.append(name)
    return groups


def _as_int(value, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError("配置项不能是布尔值")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"配置项不是整数：{value!r}") from exc


def _as_float(value, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError("配置项不能是布尔值")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"配置项不是数字：{value!r}") from exc
