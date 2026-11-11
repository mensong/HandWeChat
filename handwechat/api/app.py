# -*- coding: utf-8 -*-
"""FastAPI 应用入口。"""

from __future__ import annotations

from contextlib import asynccontextmanager
import json
import logging
from pathlib import Path
import time

from fastapi import FastAPI, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from handwechat.api.config import ApiConfig
from handwechat.api.logging_setup import configure_api_logging
from handwechat.api.models import (
    AttachResponse,
    HealthResponse,
    ListenerControlResponse,
    ListenerSessionAddRequest,
    ListenerSessionAddResponse,
    ListenerSessionListResponse,
    ListenerSessionRemoveResponse,
    ListenerSessionResponse,
    MessageListResponse,
    MessageResponse,
    SendRequest,
    SessionListResponse,
    SessionResponse,
    SendTaskQueuedResponse,
    SendTaskResponse,
)
from handwechat.api.service import ApiService
from handwechat.api.service import ListenerSessionLimitError
from handwechat.api.service import ListenerSessionNotFoundError
from handwechat.api.service import SendTaskNotFoundError
from handwechat.api.service import WeChatVersionError
from handwechat.errors import (
    AttachError,
    ControlNotFoundError,
    HWError,
    WeChatOfflineError,
)


def _error_code(exc: Exception) -> str:
    if isinstance(exc, WeChatOfflineError):
        return "WECHAT_OFFLINE"
    if isinstance(exc, AttachError):
        return "ATTACH_FAILED"
    if isinstance(exc, ControlNotFoundError):
        return "CONTROL_NOT_FOUND"
    if isinstance(exc, WeChatVersionError):
        return "WECHAT_VERSION_UNSUPPORTED"
    if isinstance(exc, ListenerSessionNotFoundError):
        return "LISTENER_SESSION_NOT_FOUND"
    if isinstance(exc, ListenerSessionLimitError):
        return "LISTENER_SESSION_LIMIT_REACHED"
    if isinstance(exc, SendTaskNotFoundError):
        return "SEND_TASK_NOT_FOUND"
    return "INTERNAL_ERROR"


def _http_error_code(status_code: int) -> str:
    return {
        status.HTTP_404_NOT_FOUND: "NOT_FOUND",
        status.HTTP_405_METHOD_NOT_ALLOWED: "METHOD_NOT_ALLOWED",
    }.get(status_code, "HTTP_ERROR")


access_logger = logging.getLogger("handwechat.api.access")


def _write_access_log(request: Request, status_code: int, started_at: float) -> None:
    payload = {
        "method": request.method,
        "path": request.url.path,
        "query": request.url.query,
        "status": status_code,
        "duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
        "client": request.client.host if request.client else None,
    }
    access_logger.info(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _session_response(item) -> SessionResponse:
    return SessionResponse(
        name=item.name,
        type=item.type,
        unread=bool(item.is_new or item.unread_count > 0),
        preview=item.preview,
        time=item.time_raw,
    )


def _message_response(item) -> MessageResponse:
    return MessageResponse(
        id=item.id,
        cursor=item.cursor,
        session=item.session,
        session_type=item.session_type,
        sender=item.sender,
        content=item.content,
        is_at_me=item.is_at_me,
        received_at=item.received_at,
    )


def _send_task_response(item) -> SendTaskResponse:
    return SendTaskResponse.model_validate(item, from_attributes=True)


def create_app(
    *,
    config: ApiConfig | None = None,
    service: ApiService | None = None,
) -> FastAPI:
    config = config or ApiConfig.load(Path("data/api_config.json"))
    service = service or ApiService(config)
    configure_api_logging(config)
    app = FastAPI(
        title="HandWeChat API",
        version="0.1.0",
        lifespan=None,
    )

    @app.get("/", include_in_schema=False)
    def console() -> FileResponse:
        return FileResponse(
            Path(__file__).resolve().parent / "static" / "index.html",
            media_type="text/html",
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            service.attach()
        except HWError:
            # 微信未登录时仍允许启动服务，调用方可通过 health/attach 观察。
            pass
        yield
        service.shutdown()

    app.router.lifespan_context = lifespan

    @app.middleware("http")
    async def access_log_middleware(request: Request, call_next):
        started_at = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            _write_access_log(request, 500, started_at)
            raise
        _write_access_log(request, response.status_code, started_at)
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request, _exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"code": "INVALID_REQUEST", "message": "请求参数校验失败"},
        )

    @app.exception_handler(HWError)
    async def hw_error_handler(_request: Request, exc: HWError) -> JSONResponse:
        error_status = (
            status.HTTP_404_NOT_FOUND
            if isinstance(exc, (ListenerSessionNotFoundError, SendTaskNotFoundError))
            else status.HTTP_409_CONFLICT
        )
        return JSONResponse(
            status_code=error_status,
            content={"code": _error_code(exc), "message": str(exc)},
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(
        _request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": _http_error_code(exc.status_code),
                "message": str(exc.detail),
            },
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"code": "INTERNAL_ERROR", "message": "服务内部错误"},
        )

    @app.get("/api/v1/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse.model_validate(service.health())

    @app.post("/api/v1/attach", response_model=AttachResponse)
    def attach() -> AttachResponse:
        return AttachResponse.model_validate(service.attach())

    @app.get("/api/v1/sessions", response_model=SessionListResponse)
    def sessions() -> SessionListResponse:
        items = service.get_sessions()
        return SessionListResponse(items=[_session_response(item) for item in items])

    @app.get(
        "/api/v1/listener/sessions",
        response_model=ListenerSessionListResponse,
    )
    def listener_sessions() -> ListenerSessionListResponse:
        items = service.get_listener_sessions()
        return ListenerSessionListResponse(
            items=[ListenerSessionResponse.model_validate(item) for item in items]
        )

    @app.post(
        "/api/v1/listener/sessions",
        response_model=ListenerSessionAddResponse,
    )
    def add_listener_session(
        request: ListenerSessionAddRequest,
    ) -> ListenerSessionAddResponse:
        session = service.add_listener_session(request.name)
        return ListenerSessionAddResponse(
            status="added",
            name=session.name if hasattr(session, "name") else request.name,
        )

    @app.delete(
        "/api/v1/listener/sessions/{name}",
        response_model=ListenerSessionRemoveResponse,
    )
    def remove_listener_session(name: str) -> ListenerSessionRemoveResponse:
        service.remove_listener_session(name)
        return ListenerSessionRemoveResponse(status="removed", name=name)

    @app.post("/api/v1/listener/start", response_model=ListenerControlResponse)
    def start_listener() -> ListenerControlResponse:
        service.start_listener()
        return ListenerControlResponse(status="running")

    @app.post("/api/v1/listener/stop", response_model=ListenerControlResponse)
    def stop_listener() -> ListenerControlResponse:
        service.stop_listener()
        return ListenerControlResponse(status="stopped")

    @app.get("/api/v1/messages", response_model=MessageListResponse)
    def messages(
        cursor: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=100),
        timeout: int = Query(default=0, ge=0, le=30),
    ) -> MessageListResponse:
        items, next_cursor, has_more = service.get_messages(
            cursor,
            limit=limit,
            timeout=timeout,
        )
        return MessageListResponse(
            items=[_message_response(item) for item in items],
            next_cursor=next_cursor,
            has_more=has_more,
        )

    @app.post(
        "/api/v1/send",
        response_model=SendTaskQueuedResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def send_message(request: SendRequest) -> SendTaskQueuedResponse:
        task = service.send(request.session, request.text)
        return SendTaskQueuedResponse(task_id=task.task_id, status="queued")

    @app.get("/api/v1/send/{task_id}", response_model=SendTaskResponse)
    def send_task(task_id: str) -> SendTaskResponse:
        return _send_task_response(service.get_send_task(task_id))

    return app


app = create_app()
