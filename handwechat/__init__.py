# -*- coding: utf-8 -*-
"""HandWeChat current-chat OCR adapter for WeChat PC 4.x."""

from handwechat.errors import AttachError, HWError, WeChatOfflineError
from handwechat.models import (
    BaseMessage,
    MessageAttr,
    MessageType,
    OtherMessage,
    SystemMessage,
    TextMessage,
    TimeMessage,
)
from handwechat.ocr_calibration import OcrCalibration
from handwechat.ocr_channel import OcrChannel
from handwechat.rate_limit import TokenBucketRateLimiter
from handwechat.runtime import WeChatRuntime
from handwechat.version import __version__

__all__ = [
    "AttachError",
    "BaseMessage",
    "HWError",
    "MessageAttr",
    "MessageType",
    "OcrCalibration",
    "OcrChannel",
    "OtherMessage",
    "SystemMessage",
    "TextMessage",
    "TimeMessage",
    "TokenBucketRateLimiter",
    "WeChatOfflineError",
    "WeChatRuntime",
    "__version__",
]
