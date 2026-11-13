# -*- coding: utf-8 -*-
"""HandWeChat launcher: start WeChat when needed."""

from __future__ import annotations

import os
import subprocess
import sys
import winreg
from pathlib import Path


WEIXIN_PROCESS_NAME = "Weixin.exe"
REGISTRY_WEIXIN_KEYS = (
    (winreg.HKEY_CURRENT_USER, r"Software\Tencent\Weixin", "InstallPath"),
    (winreg.HKEY_LOCAL_MACHINE, r"Software\Tencent\Weixin", "InstallPath"),
    (
        winreg.HKEY_LOCAL_MACHINE,
        r"Software\WOW6432Node\Tencent\Weixin",
        "InstallPath",
    ),
)


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
    weixin_path = find_wechat_path()
    if weixin_path is None:
        print("[start_wechat] 未找到微信：请确认微信已安装，或重新安装后重试")
        return 2

    if "--check" in sys.argv:
        print(f"[start_wechat] 微信路径：{weixin_path}")
        return 0

    subprocess.Popen([str(weixin_path)])
    print("[start_wechat] 微信已启动；如停在登录界面请完成登录")
    return 0


if __name__ == "__main__":
    sys.exit(main())
