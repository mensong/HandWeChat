# -*- coding: utf-8 -*-
"""热加载冒烟：修改配置文件后状态生效，并在控制台打印提示。"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import socket
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn

from handwechat.gateway.api import create_app
from handwechat.gateway.config import GatewayConfig


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def write_apps(
    path: Path,
    apps: list[dict],
    system_apps: list[dict] | None = None,
) -> None:
    payload: dict = {"apps": apps}
    if system_apps is not None:
        payload["system_apps"] = system_apps
    path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


async def wait_started(server: uvicorn.Server) -> None:
    for _ in range(200):
        if server.started:
            return
        await asyncio.sleep(0.05)
    raise RuntimeError("网关未能启动")


async def wait_output(buffer: io.StringIO, needle: str) -> str:
    for _ in range(60):
        text = buffer.getvalue()
        if needle in text:
            return text
        await asyncio.sleep(0.1)
    raise AssertionError(f"控制台未出现提示：{needle}\n实际输出：{buffer.getvalue()}")


def close_gateway_logging() -> None:
    import logging

    logger = logging.getLogger("handwechat.gateway")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


async def main() -> None:
    port = free_port()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        config_path = Path(tmp) / "gateway_config.json"
        write_apps(config_path, [{"id": "a", "priority": 10}])

        config = GatewayConfig.load(config_path)
        config.listen_port = port
        config.config_reload_seconds = 0.5
        config.health_poll_seconds = 60
        config.reconnect_seconds = 60
        config.log_dir = tmp

        app = create_app(config=config, config_path=config_path)
        server = uvicorn.Server(
            uvicorn.Config(
                app, host="127.0.0.1", port=port, log_level="critical"
            )
        )
        task = asyncio.create_task(server.serve())
        try:
            await wait_started(server)

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                config_path.write_text("{ broken json", encoding="utf-8")
                await wait_output(buffer, "配置热加载失败，已保留旧配置")

                write_apps(
                    config_path,
                    [
                        {"id": "a", "priority": 10},
                        {"id": "b", "priority": 20},
                    ],
                )
                text = await wait_output(buffer, "配置已热加载")

                assert "b(20)" in text, text
                assert "新增 1，移除 0" in text, text

                write_apps(
                    config_path,
                    [{"id": "b", "priority": 20}],
                    [{"id": "a", "priority": 10}],
                )
                text = await wait_output(buffer, "系统：a(10)")

            status = await asyncio.to_thread(
                get_json,
                f"http://127.0.0.1:{port}/api/v1/gateway/status",
            )
            ids = [item["id"] for item in status["apps"]]
            assert ids == ["a", "b"], ids
            assert status["apps"][0]["system"] is True
            assert status["apps"][1]["system"] is False
            print(
                "PASS 热加载：失败保留旧配置、成功后生效并在控制台提示，"
                "应用迁入系统区后排序生效"
            )
        finally:
            close_gateway_logging()
            server.should_exit = True
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(), timeout=60))
