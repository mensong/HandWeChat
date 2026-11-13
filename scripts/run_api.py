# -*- coding: utf-8 -*-
"""启动 HandWeChat HTTP API 服务。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn

from handwechat.api.config import ApiConfig
from handwechat.api.app import create_app
from handwechat.paths import app_data_path, app_path


def main() -> None:
    config = ApiConfig.load(app_data_path("api_config.json"))
    config.log_dir = str(app_path(config.log_dir))
    app = create_app(config=config)
    uvicorn.run(app, host=config.host, port=config.port)


if __name__ == "__main__":
    main()
