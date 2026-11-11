# -*- coding: utf-8 -*-
"""启动 HandWeChat HTTP API 服务。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn

from handwechat.api.config import ApiConfig
from handwechat.api.app import create_app


def main() -> None:
    config = ApiConfig.load("data/api_config.json")
    app = create_app(config=config)
    uvicorn.run(app, host=config.host, port=config.port)


if __name__ == "__main__":
    main()
