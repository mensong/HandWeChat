# -*- coding: utf-8 -*-
"""HandWeChat 异常体系。"""


class HWError(Exception):
    """适配层异常基类。"""


class AttachError(HWError):
    """附加微信主窗口失败（窗口不存在 / 未登录 / 控件树未物化）。"""


class ControlNotFoundError(HWError):
    """关键控件定位失败（通常意味着微信升级改变了控件结构）。"""


class WeChatOfflineError(HWError):
    """微信不在线或已退出登录。"""
