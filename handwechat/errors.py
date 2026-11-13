# -*- coding: utf-8 -*-
"""HandWeChat 异常体系。"""


class HWError(Exception):
    """适配层异常基类。"""


class AttachError(HWError):
    """附加微信主窗口失败（窗口不存在或未登录）。"""


class WeChatOfflineError(HWError):
    """微信不在线或已退出登录。"""
