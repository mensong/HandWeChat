# -*- coding: utf-8 -*-
"""HandWeChat 微信 4.x 自研适配层（基于 uiautomation）。"""

from handwechat.errors import AttachError, ControlNotFoundError, HWError
from handwechat.health import HealthMonitor
from handwechat.listener import ListenState, MessageListener
from handwechat.models import (
    BaseMessage,
    MessageAttr,
    MessageType,
    OtherMessage,
    Session,
    SessionType,
    SystemMessage,
    TextMessage,
    TimeMessage,
)
from handwechat.wechat import WeChat
from handwechat.rate_limit import TokenBucketRateLimiter
from handwechat.selfcheck import SelfCheckItem, SelfCheckResult

__version__ = "0.1.0"

__all__ = [
    "AttachError",
    "BaseMessage",
    "ControlNotFoundError",
    "HWError",
    "HealthMonitor",
    "ListenState",
    "MessageListener",
    "MessageAttr",
    "MessageType",
    "OtherMessage",
    "SelfCheckItem",
    "SelfCheckResult",
    "Session",
    "SessionType",
    "SystemMessage",
    "TextMessage",
    "TimeMessage",
    "TokenBucketRateLimiter",
    "WeChat",
    "__version__",
]
