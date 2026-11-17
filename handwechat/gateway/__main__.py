# -*- coding: utf-8 -*-
"""python -m handwechat.gateway 入口。"""

from __future__ import annotations

import argparse

import uvicorn

from handwechat.gateway.api import create_app
from handwechat.gateway.config import GatewayConfig
from handwechat.paths import app_data_path, app_path


def main() -> None:
    parser = argparse.ArgumentParser(description="HandWeChat 消息网关")
    parser.add_argument(
        "--config",
        default=str(app_data_path("gateway_config.json")),
        help="网关配置文件路径",
    )
    args = parser.parse_args()

    config = GatewayConfig.load(args.config)
    config.log_dir = str(app_path(config.log_dir))
    uvicorn.run(
        create_app(config=config, config_path=args.config),
        host=config.listen_host,
        port=config.listen_port,
    )


if __name__ == "__main__":
    main()
