# -*- coding: utf-8 -*-
"""网关文件日志配置。"""

from __future__ import annotations

import logging
from pathlib import Path

from handwechat.gateway.config import GatewayConfig


def configure_gateway_logging(config: GatewayConfig) -> None:
    log_dir = Path(config.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("handwechat.gateway")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    target_path = (log_dir / config.gateway_log_file).resolve()
    if _has_file_handler(logger, target_path):
        return
    handler = logging.FileHandler(target_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)


def _has_file_handler(logger: logging.Logger, path: Path) -> bool:
    return any(
        isinstance(handler, logging.FileHandler)
        and getattr(handler, "baseFilename", None) == str(path)
        for handler in logger.handlers
    )
