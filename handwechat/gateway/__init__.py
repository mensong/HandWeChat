# -*- coding: utf-8 -*-
"""消息网关：在多个消费应用之前做唯一的有序调度。"""

from __future__ import annotations

__all__ = [
    "create_app",
    "GatewayClient",
    "Offer",
    "PinInfo",
    "GatewayContext",
    "SendResult",
    "ChatInfo",
    "Decision",
]


def create_app(*args, **kwargs):
    """延迟导入 FastAPI 应用，避免只引用配置时拉起 Web 依赖。"""

    from handwechat.gateway.api import create_app as _create_app

    return _create_app(*args, **kwargs)


def __getattr__(name: str):
    if name in (
        "GatewayClient",
        "Offer",
        "PinInfo",
        "GatewayContext",
        "SendResult",
        "ChatInfo",
    ):
        from handwechat.gateway import client

        return getattr(client, name)
    if name == "Decision":
        from handwechat.gateway.models import Decision

        return Decision
    raise AttributeError(name)
