# -*- coding: utf-8 -*-
"""启动 HandWeChat HTTP API 服务。"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn

from handwechat.api.config import ApiConfig
from handwechat.api.app import create_app
from handwechat.paths import app_data_path, app_path


def _set_console_title(port: int) -> None:
    """在命令行窗口标题上提示微信发送快捷键要求。"""

    if sys.platform != "win32":
        return
    try:
        ctypes.windll.kernel32.SetConsoleTitleW(
            f"HandWeChatApi:{port} - 微信发送消息快捷键需设为 Ctrl+Enter"
        )
    except Exception:
        pass


def main() -> None:
    config = ApiConfig.load(app_data_path("api_config.json"))
    config.log_dir = str(app_path(config.log_dir))
    app = create_app(config=config)
    _set_console_title(config.port)
    uvicorn.run(app, host=config.host, port=config.port)


if __name__ == "__main__":
    main()
