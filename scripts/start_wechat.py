# -*- coding: utf-8 -*-
"""HandWeChat 启动器：先开系统读屏标志，再拉起微信。

微信 4.1.15.56 的 mmui 控件树只在「冷启动时读屏标志已开启」才会物化
（方案.md 第十二节），所以必须先设标志后启动微信。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from handwechat.version_info import get_process_ids_by_image_name

SPI_GETSCREENREADER = 0x1046
SPI_SETSCREENREADER = 0x1047
SPIF_SENDCHANGE = 0x02

WEIXIN_PATH = r"D:\Program Files\Tencent\Weixin\Weixin.exe"


def get_screen_reader() -> bool:
    value = wintypes.BOOL()
    ctypes.windll.user32.SystemParametersInfoW(
        SPI_GETSCREENREADER, 0, ctypes.byref(value), 0)
    return bool(value.value)


def set_screen_reader(enable: bool) -> bool:
    ctypes.windll.user32.SystemParametersInfoW(
        SPI_SETSCREENREADER, 0, ctypes.c_void_p(1 if enable else 0), SPIF_SENDCHANGE)
    return get_screen_reader() == enable


def main() -> int:
    running_pids = get_process_ids_by_image_name(Path(WEIXIN_PATH).name)
    if running_pids:
        print(
            f"[start_wechat] 微信已在运行（PID: {', '.join(map(str, running_pids))}）；"
            "不再重复拉起。"
        )
        return 0

    if not os.path.exists(WEIXIN_PATH):
        print(f"[start_wechat] 未找到微信：{WEIXIN_PATH}")
        return 2

    if not get_screen_reader():
        if not set_screen_reader(True):
            print("[start_wechat] 读屏标志设置失败（可能被组策略锁定），微信控件树将不会物化")
            return 1
        print("[start_wechat] 已开启系统读屏标志")
    else:
        print("[start_wechat] 读屏标志已开启")

    subprocess.Popen([WEIXIN_PATH])
    print("[start_wechat] 微信已启动；如停在登录界面请完成登录")
    return 0


if __name__ == "__main__":
    sys.exit(main())
