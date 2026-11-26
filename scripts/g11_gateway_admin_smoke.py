# -*- coding: utf-8 -*-
"""管理接口冒烟：注册、列表、更新、注销、令牌校验与配置文件保留。"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
import websockets
from websockets.exceptions import ConnectionClosed

from handwechat.gateway.api import create_app
from handwechat.gateway.config import GatewayConfig
from handwechat.gateway.protocol import PROTOCOL_VERSION

ADMIN_TOKEN = "admin-secret"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def request_json(
    method: str,
    url: str,
    body: dict | None = None,
    token: str | None = None,
) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    if token is not None:
        request.add_header("X-Admin-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(
                response.read().decode("utf-8") or "{}"
            )
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8")
        return exc.code, json.loads(payload) if payload else {}


async def call(
    method: str,
    port: int,
    path: str,
    body: dict | None = None,
    token: str | None = ADMIN_TOKEN,
) -> tuple[int, dict]:
    return await asyncio.to_thread(
        request_json,
        method,
        f"http://127.0.0.1:{port}{path}",
        body,
        token,
    )


async def wait_started(server: uvicorn.Server) -> None:
    for _ in range(200):
        if server.started:
            return
        await asyncio.sleep(0.05)
    raise RuntimeError("网关未能启动")


async def connect_app(port: int, app_id: str, token: str):
    socket_ = await websockets.connect(
        f"ws://127.0.0.1:{port}/api/v1/gateway/ws/app"
    )
    await socket_.send(
        json.dumps(
            {
                "type": "hello",
                "protocol": PROTOCOL_VERSION,
                "app_id": app_id,
                "token": token,
            }
        )
    )
    welcome = json.loads(await asyncio.wait_for(socket_.recv(), 5))
    assert welcome["type"] == "welcome", welcome
    return socket_


def close_gateway_logging() -> None:
    import logging

    logger = logging.getLogger("handwechat.gateway")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


async def start_gateway(
    config_path: Path, tmp: str
) -> tuple[uvicorn.Server, asyncio.Task, int]:
    port = free_port()
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
    await wait_started(server)
    return server, task, port


async def stop_gateway(server: uvicorn.Server, task: asyncio.Task) -> None:
    server.should_exit = True
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def read_config(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def fetch_text(url: str) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, response.read().decode("utf-8")


async def scenario_admin(tmp: str) -> None:
    config_path = Path(tmp) / "gateway_config.json"
    config_path.write_text(
        json.dumps(
            {
                "admin_token": ADMIN_TOKEN,
                "max_response_images": 3,
                "custom_note": {"keep": True},
                "apps": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    server, task, port = await start_gateway(config_path, tmp)
    try:
        code, _ = await call("POST", port, "/api/v1/gateway/apps", {"id": "x"}, token=None)
        assert code == 401, code
        code, _ = await call("POST", port, "/api/v1/gateway/apps", {"id": "x"}, token="bad")
        assert code == 401, code

        code, payload = await call(
            "POST",
            port,
            "/api/v1/gateway/apps",
            {"id": "checkin", "priority": 10, "rules": [{"regex": "^签到$"}]},
        )
        assert code == 201, (code, payload)
        app_token = payload["token"]
        assert app_token and payload["system"] is False, payload
        assert payload["app"]["id"] == "checkin", payload

        code, listing = await call("GET", port, "/api/v1/gateway/apps")
        assert code == 200, (code, listing)
        assert listing["count"] == 1, listing
        assert "token" not in listing["apps"][0], listing

        saved = read_config(config_path)
        assert saved["max_response_images"] == 3, saved
        assert saved["custom_note"] == {"keep": True}, saved
        assert saved["apps"][0]["token"] == app_token, saved

        code, detail = await call(
            "GET", port, "/api/v1/gateway/apps/checkin", token=None
        )
        assert code == 401, code
        code, detail = await call("GET", port, "/api/v1/gateway/apps/checkin")
        assert code == 200, (code, detail)
        assert "token" not in detail["app"], detail
        assert detail["app"]["rules"] == [{"regex": "^签到$"}], detail

        code, page = await asyncio.to_thread(
            fetch_text, f"http://127.0.0.1:{port}/"
        )
        assert code == 200 and "网关管理" in page, code
        code, script = await asyncio.to_thread(
            fetch_text, f"http://127.0.0.1:{port}/static/lucide.min.js"
        )
        assert code == 200 and script, code

        code, _ = await call(
            "GET", port, "/api/v1/gateway/messages", token=None
        )
        assert code == 401, code
        code, listing = await call("GET", port, "/api/v1/gateway/messages")
        assert code == 200, (code, listing)
        assert listing["count"] == 0 and listing["stored"] == 0, listing
        code, _ = await call(
            "GET", port, "/api/v1/gateway/messages/missing"
        )
        assert code == 404, code

        connection = await connect_app(port, "checkin", app_token)
        _, status = await call("GET", port, "/api/v1/gateway/status", token=None)
        assert status["apps"][0]["connected"] is True, status

        code, payload = await call(
            "PUT",
            port,
            "/api/v1/gateway/apps/checkin/enabled",
            {"enabled": False},
        )
        assert code == 200 and payload["enabled"] is False, (code, payload)
        saved = read_config(config_path)
        assert saved["apps"][0]["enabled"] is False, saved
        _, status = await call("GET", port, "/api/v1/gateway/status", token=None)
        assert status["apps"][0]["enabled"] is False, status

        try:
            await connect_app(port, "checkin", app_token)
        except ConnectionClosed as exc:
            assert getattr(getattr(exc, "rcvd", None), "code", None) == 4404, exc
        else:
            raise AssertionError("停用后的应用仍能握手")

        code, _ = await call(
            "PUT",
            port,
            "/api/v1/gateway/apps/checkin/enabled",
            {"enabled": "yes"},
        )
        assert code == 400, code
        code, _ = await call(
            "PUT", port, "/api/v1/gateway/apps/missing/enabled", {"enabled": True}
        )
        assert code == 404, code

        code, payload = await call(
            "PUT",
            port,
            "/api/v1/gateway/apps/checkin/enabled",
            {"enabled": True},
        )
        assert code == 200 and payload["enabled"] is True, (code, payload)
        _, status = await call("GET", port, "/api/v1/gateway/status", token=None)
        assert status["apps"][0]["enabled"] is True, status
        assert status["apps"][0]["connected"] is True, status

        code, _ = await call(
            "POST", port, "/api/v1/gateway/apps", {"id": "checkin"}
        )
        assert code == 409, code
        code, _ = await call(
            "POST", port, "/api/v1/gateway/apps", {"id": "bad", "nope": 1}
        )
        assert code == 400, code
        code, _ = await call(
            "POST",
            port,
            "/api/v1/gateway/apps",
            {"id": "bad", "rules": [{"regex": "("}]},
        )
        assert code == 400, code

        code, payload = await call(
            "PUT",
            port,
            "/api/v1/gateway/apps/checkin",
            {"priority": 5, "system": True},
        )
        assert code == 200, (code, payload)
        assert payload["system"] is True, payload
        saved = read_config(config_path)
        assert [item["id"] for item in saved["system_apps"]] == ["checkin"], saved
        assert saved["apps"] == [], saved
        _, status = await call("GET", port, "/api/v1/gateway/status", token=None)
        assert status["apps"][0]["system"] is True, status
        assert status["apps"][0]["connected"] is True, status

        code, _ = await call(
            "PUT", port, "/api/v1/gateway/apps/checkin", {"id": "other"}
        )
        assert code == 400, code
        code, _ = await call(
            "PUT", port, "/api/v1/gateway/apps/checkin", {"nope": 1}
        )
        assert code == 400, code
        code, _ = await call(
            "PUT", port, "/api/v1/gateway/apps/missing", {"priority": 1}
        )
        assert code == 404, code

        code, _ = await call("DELETE", port, "/api/v1/gateway/apps/checkin")
        assert code == 200, code
        try:
            await asyncio.wait_for(connection.recv(), 5)
        except ConnectionClosed as exc:
            assert getattr(getattr(exc, "rcvd", None), "code", None) == 4001, exc
        else:
            raise AssertionError("注销后应用连接未被关闭")
        saved = read_config(config_path)
        assert saved["system_apps"] == [] and saved["apps"] == [], saved
        _, status = await call("GET", port, "/api/v1/gateway/status", token=None)
        assert status["apps"] == [], status

        code, _ = await call("DELETE", port, "/api/v1/gateway/apps/checkin")
        assert code == 404, code
    finally:
        close_gateway_logging()
        await stop_gateway(server, task)
    print("PASS 管理令牌、注册、列表、更新迁移、注销与配置文件保留")


async def scenario_admin_disabled(tmp: str) -> None:
    config_path = Path(tmp) / "gateway_no_admin.json"
    config_path.write_text(
        json.dumps({"apps": []}, ensure_ascii=False), encoding="utf-8"
    )
    server, task, port = await start_gateway(config_path, tmp)
    try:
        code, payload = await call(
            "POST", port, "/api/v1/gateway/apps", {"id": "x"}, token="anything"
        )
        assert code == 403, (code, payload)
        assert "admin_token" in payload["detail"], payload
    finally:
        close_gateway_logging()
        await stop_gateway(server, task)
    print("PASS 未配置 admin_token 时管理接口一律拒绝")


async def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        await scenario_admin(tmp)
        await scenario_admin_disabled(tmp)
        print("全部通过")


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(), timeout=120))
