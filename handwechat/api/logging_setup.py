# -*- coding: utf-8 -*-
"""HTTP API 独立文件日志配置。"""

from __future__ import annotations

import logging
from pathlib import Path

from handwechat.api.config import ApiConfig


def configure_api_logging(config: ApiConfig) -> None:
    """为访问日志与发送任务日志挂接 UTF-8 文件 handler。

    create_app 可能在测试中反复创建应用；按路径去重可避免同一日志重复写入。
    """

    log_dir = Path(config.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    for logger_name, filename in (
        ("handwechat.api.access", config.access_log_file),
        ("handwechat.api.send_tasks", config.send_task_log_file),
        ("handwechat.ocr", config.ocr_log_file),
    ):
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        target_path = (log_dir / filename).resolve()
        if _has_file_handler(logger, target_path):
            continue
        handler = logging.FileHandler(target_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)


def _has_file_handler(logger: logging.Logger, path: Path) -> bool:
    return any(
        isinstance(handler, logging.FileHandler)
        and getattr(handler, "baseFilename", None) == str(path)
        for handler in logger.handlers
    )
