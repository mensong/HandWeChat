# -*- coding: utf-8 -*-
"""网关管理接口：应用的注册、列表、更新与注销。"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from pathlib import Path
from typing import Awaitable, Callable

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Request

from handwechat.gateway.config import GatewayAppConfig, parse_app
from handwechat.gateway.registry import AppRegistry
from handwechat.gateway.trace import MessageLog

logger = logging.getLogger("handwechat.gateway")

_ADMIN_HEADER = "x-admin-token"
_APP_FIELDS = (
    "id",
    "name",
    "priority",
    "token",
    "timeout_ms",
    "hold_seconds",
    "enabled",
    "can_rewrite",
    "groups",
    "rules",
)


class StoreError(Exception):
    """配置文件读写失败，携带对应的 HTTP 状态码。"""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class ConfigStore:
    """读写 gateway_config.json：只改应用数组，保留其他顶层键。"""

    def __init__(self, path: Path, seed: dict | None = None) -> None:
        self.path = path
        self._seed = seed or {}

    def read(self) -> dict:
        if not self.path.exists():
            return json.loads(json.dumps(self._seed))
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StoreError(500, f"网关配置文件读取失败：{exc}") from exc
        if not isinstance(data, dict):
            raise StoreError(500, "网关配置必须是 JSON 对象")
        for key in ("system_apps", "apps"):
            value = data.get(key)
            if value is not None and not isinstance(value, list):
                raise StoreError(500, f"{key} 必须是数组")
        return data

    def write(self, data: dict) -> str | None:
        """原子写回并返回旧内容，供校验失败时回滚。"""

        previous = (
            self.path.read_text(encoding="utf-8")
            if self.path.exists()
            else None
        )
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(text, encoding="utf-8")
        temp.replace(self.path)
        return previous

    def restore(self, previous: str | None) -> None:
        try:
            if previous is None:
                self.path.unlink(missing_ok=True)
                return
            temp = self.path.with_suffix(self.path.suffix + ".tmp")
            temp.write_text(previous, encoding="utf-8")
            temp.replace(self.path)
        except OSError:
            logger.error(
                "回滚网关配置文件失败：%s", self.path, exc_info=True
            )


class GatewayAdmin:
    def __init__(
        self,
        *,
        store: ConfigStore,
        registry: AppRegistry,
        apply: Callable[[dict], Awaitable[None]],
        lock: asyncio.Lock,
    ) -> None:
        self._store = store
        self._registry = registry
        self._apply = apply
        self._lock = lock

    @property
    def registry(self) -> AppRegistry:
        return self._registry

    @property
    def admin_token(self) -> str:
        return self._registry.config.admin_token

    async def create(self, body: dict, *, system: bool) -> dict:
        fields = _app_fields(body)
        app_id = str(fields.get("id") or "").strip()
        if not app_id:
            raise HTTPException(400, "缺少应用 id")
        fields["id"] = app_id
        if not fields.get("token"):
            fields["token"] = secrets.token_urlsafe(24)
        app = _parse(fields)
        async with self._lock:
            data = self._read()
            if _locate(data, app_id) is not None:
                raise HTTPException(409, f"应用 id 已存在：{app_id}")
            _block(data, system).append(app_entry(app))
            await self._commit(data, action="register", app_id=app_id)
        return {
            "ok": True,
            "id": app_id,
            "system": system,
            "token": app.token,
            "app": self._status(app_id),
        }

    async def update(self, app_id: str, body: dict) -> dict:
        fields = _app_fields(body)
        if "id" in fields and str(fields["id"]).strip() != app_id:
            raise HTTPException(400, "body.id 与路径中的 id 不一致")
        system = _optional_system(body)
        async with self._lock:
            data = self._read()
            located = _locate(data, app_id)
            if located is None:
                raise HTTPException(404, f"应用不存在：{app_id}")
            key, index = located
            current = data[key][index]
            if not isinstance(current, dict):
                raise HTTPException(500, f"应用 {app_id} 的配置项不是对象")
            app = _parse({**current, **fields, "id": app_id})
            target = (
                key if system is None else _block_key(system)
            )
            if target == key:
                data[key][index] = app_entry(app)
            else:
                del data[key][index]
                _block(data, system).append(app_entry(app))
            await self._commit(data, action="update", app_id=app_id)
        return {
            "ok": True,
            "id": app_id,
            "system": target == "system_apps",
            "app": self._status(app_id),
        }

    async def delete(self, app_id: str) -> dict:
        async with self._lock:
            data = self._read()
            located = _locate(data, app_id)
            if located is None:
                raise HTTPException(404, f"应用不存在：{app_id}")
            key, index = located
            del data[key][index]
            await self._commit(data, action="delete", app_id=app_id)
        return {"ok": True, "id": app_id, "removed": True}

    def get(self, app_id: str) -> dict:
        """返回可编辑的配置项（不含 token），供管理界面回填。"""

        runtime = self._registry.get(app_id)
        if runtime is None:
            raise HTTPException(404, f"应用不存在：{app_id}")
        entry = app_entry(runtime.config)
        entry.pop("token", None)
        return {"ok": True, "app": entry}

    def _read(self) -> dict:
        try:
            return self._store.read()
        except StoreError as exc:
            raise HTTPException(exc.status, exc.detail) from exc

    async def _commit(self, data: dict, *, action: str, app_id: str) -> None:
        # 调用方已持有 self._lock，写文件和生效之间不会被文件监听插队。
        previous = self._store.write(data)
        try:
            await self._apply(data)
        except Exception as exc:
            self._store.restore(previous)
            detail = f"{type(exc).__name__}: {exc}"
            logger.error(
                json.dumps(
                    {
                        "event": "admin_apply_failed",
                        "action": action,
                        "app": app_id,
                        "error": detail,
                    },
                    ensure_ascii=False,
                )
            )
            raise HTTPException(
                500, f"配置写入后校验失败，已回滚：{detail}"
            ) from exc
        logger.info(
            json.dumps(
                {
                    "event": "admin_apps_changed",
                    "action": action,
                    "app": app_id,
                    "apps": [
                        item.id for item in self._registry.config.all_apps()
                    ],
                },
                ensure_ascii=False,
            )
        )

    def _status(self, app_id: str) -> dict | None:
        runtime = self._registry.get(app_id)
        return runtime.status_payload() if runtime is not None else None


def register_routes(
    app: FastAPI, admin: GatewayAdmin, messages: MessageLog
) -> None:
    def require_admin(request: Request) -> None:
        token = admin.admin_token
        if not token:
            raise HTTPException(403, "未配置 admin_token，管理接口已禁用")
        supplied = request.headers.get(_ADMIN_HEADER, "")
        if not secrets.compare_digest(
            supplied.encode("utf-8"), token.encode("utf-8")
        ):
            raise HTTPException(401, "管理令牌无效")

    router = APIRouter(
        prefix="/api/v1/gateway/apps",
        dependencies=[Depends(require_admin)],
    )

    @router.get("")
    async def list_apps() -> dict:
        apps = [
            runtime.status_payload() for runtime in admin.registry.all()
        ]
        return {"apps": apps, "count": len(apps)}

    @router.post("", status_code=201)
    async def create_app(body: dict = Body(...)) -> dict:
        return await admin.create(body, system=_system_flag(body))

    @router.get("/{app_id}")
    async def get_app(app_id: str) -> dict:
        return admin.get(app_id)

    @router.put("/{app_id}")
    async def update_app(app_id: str, body: dict = Body(...)) -> dict:
        return await admin.update(app_id, body)

    @router.delete("/{app_id}")
    async def delete_app(app_id: str) -> dict:
        return await admin.delete(app_id)

    app.include_router(router)

    messages_router = APIRouter(
        prefix="/api/v1/gateway/messages",
        dependencies=[Depends(require_admin)],
    )

    @messages_router.get("")
    async def list_messages(limit: int = 50) -> dict:
        items = messages.recent(limit)
        return {"messages": items, "count": len(items), "stored": len(messages)}

    @messages_router.get("/{trace_id}")
    async def get_message(trace_id: str) -> dict:
        record = messages.get(trace_id)
        if record is None:
            raise HTTPException(404, "消息不存在或已滑出日志")
        return {"message": record}

    app.include_router(messages_router)


def app_entry(app: GatewayAppConfig) -> dict:
    """把校验后的应用配置写成配置文件条目；未配置 rules 时不写该键。"""

    entry: dict = {
        "id": app.id,
        "name": app.name,
        "priority": app.priority,
        "timeout_ms": app.timeout_ms,
        "hold_seconds": app.hold_seconds,
        "enabled": app.enabled,
        "can_rewrite": app.can_rewrite,
        "groups": list(app.groups),
    }
    if app.token:
        entry["token"] = app.token
    if app.rules is not None:
        entry["rules"] = app.rules
    return entry


def _app_fields(body: dict) -> dict:
    unknown = sorted(set(body) - set(_APP_FIELDS) - {"system"})
    if unknown:
        raise HTTPException(400, f"未知字段：{', '.join(unknown)}")
    return {key: body[key] for key in _APP_FIELDS if key in body}


def _system_flag(body: dict) -> bool:
    return _optional_system(body) or False


def _optional_system(body: dict) -> bool | None:
    if "system" not in body:
        return None
    value = body["system"]
    if not isinstance(value, bool):
        raise HTTPException(400, "system 必须是布尔值")
    return value


def _parse(data: dict) -> GatewayAppConfig:
    try:
        return parse_app(data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _block_key(system: bool) -> str:
    return "system_apps" if system else "apps"


def _block(data: dict, system: bool | None) -> list:
    key = _block_key(bool(system))
    block = data.get(key)
    if block is None:
        block = data[key] = []
    return block


def _locate(data: dict, app_id: str) -> tuple[str, int] | None:
    for key in ("system_apps", "apps"):
        for index, item in enumerate(data.get(key) or []):
            if isinstance(item, dict) and str(item.get("id") or "") == app_id:
                return key, index
    return None
