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
from ctypes import wintypes
from pathlib import Path
import winreg

SPI_GETSCREENREADER = 0x1046
SPI_SETSCREENREADER = 0x1047
SPIF_SENDCHANGE = 0x02
TH32CS_SNAPPROCESS = 0x02

WEIXIN_PROCESS_NAME = "Weixin.exe"
REGISTRY_WEIXIN_KEYS = (
    (winreg.HKEY_CURRENT_USER, r"Software\Tencent\Weixin", "InstallPath"),
    (winreg.HKEY_LOCAL_MACHINE, r"Software\Tencent\Weixin", "InstallPath"),
    (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Tencent\Weixin", "InstallPath"),
)


def get_screen_reader() -> bool:
    value = wintypes.BOOL()
    ctypes.windll.user32.SystemParametersInfoW(
        SPI_GETSCREENREADER, 0, ctypes.byref(value), 0)
    return bool(value.value)


def set_screen_reader(enable: bool) -> bool:
    ctypes.windll.user32.SystemParametersInfoW(
        SPI_SETSCREENREADER, 0, ctypes.c_void_p(1 if enable else 0), SPIF_SENDCHANGE)
    return get_screen_reader() == enable


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * wintypes.MAX_PATH),
    ]


def get_process_ids_by_image_name(image_name: str) -> list[int]:
    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        return []

    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    result: list[int] = []
    try:
        if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            while True:
                if entry.szExeFile.lower() == image_name.lower():
                    result.append(entry.th32ProcessID)
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
    finally:
        kernel32.CloseHandle(snapshot)
    return result


def find_wechat_path() -> Path | None:
    candidates: list[Path] = []

    for root, subkey, value_name in REGISTRY_WEIXIN_KEYS:
        try:
            with winreg.OpenKey(root, subkey) as key:
                value, _ = winreg.QueryValueEx(key, value_name)
            candidates.append(Path(os.path.expandvars(value).strip('"')))
        except OSError:
            continue

    env_roots = (
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("LOCALAPPDATA"),
    )
    for env_root in env_roots:
        if env_root:
            candidates.append(Path(env_root) / "Tencent" / "Weixin")

    for candidate in candidates:
        exe_path = (
            candidate
            if candidate.name.lower() == WEIXIN_PROCESS_NAME.lower()
            else candidate / WEIXIN_PROCESS_NAME
        )
        if exe_path.is_file():
            return exe_path.resolve()

    return None


def main() -> int:
    if "--check" in sys.argv:
        path = find_wechat_path()
        if path is None:
            print("[start_wechat] 未找到微信可执行文件")
            return 2
        print(f"[start_wechat] 微信路径：{path}")
        print(f"[start_wechat] 读屏标志：{'开启' if get_screen_reader() else '关闭'}")
        return 0

    running_pids = get_process_ids_by_image_name(WEIXIN_PROCESS_NAME)
    if running_pids:
        print(
            f"[start_wechat] 微信已在运行（PID: {', '.join(map(str, running_pids))}）；"
            "不再重复拉起。"
        )
        if not get_screen_reader():
            print(
                "[start_wechat] 警告：读屏标志关闭。请从托盘彻底退出微信后，"
                "重新运行本启动器。"
            )
        return 0

    weixin_path = find_wechat_path()
    if weixin_path is None:
        print("[start_wechat] 未找到微信：请确认微信已安装，或重新安装后重试")
        return 2

    if not get_screen_reader():
        if not set_screen_reader(True):
            print("[start_wechat] 读屏标志设置失败（可能被组策略锁定），微信控件树将不会物化")
            return 1
        print("[start_wechat] 已开启系统读屏标志")
    else:
        print("[start_wechat] 读屏标志已开启")

    subprocess.Popen([str(weixin_path)])
    print("[start_wechat] 微信已启动；如停在登录界面请完成登录")
    return 0


if __name__ == "__main__":
    sys.exit(main())
