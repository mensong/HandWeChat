# -*- coding: utf-8 -*-
"""FastAPI application for the current WeChat chat."""

from __future__ import annotations

from contextlib import asynccontextmanager
import json
import logging
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
    MessageListResponse,
    MessageResponse,
    SendRequest,
    SendTaskQueuedResponse,
    SendTaskResponse,
)
from handwechat.api.service import (
    ApiService,
    OcrChannelNotRunningError,
    SendTaskNotFoundError,
)
from handwechat.errors import AttachError, HWError, WeChatOfflineError
from handwechat.paths import app_data_path, app_path, resource_path


def _error_code(exc: Exception) -> str:
    if isinstance(exc, WeChatOfflineError):
        return "WECHAT_OFFLINE"
    if isinstance(exc, AttachError):
        return "ATTACH_FAILED"
    if isinstance(exc, SendTaskNotFoundError):
        return "SEND_TASK_NOT_FOUND"
    if isinstance(exc, OcrChannelNotRunningError):
        return "OCR_CHANNEL_NOT_RUNNING"
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
        "status": status_code,
        "duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
        "client": request.client.host if request.client else None,
    }
    access_logger.info(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _message_response(item) -> MessageResponse:
    return MessageResponse(
        id=item.id,
        cursor=item.cursor,
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
    config = config or ApiConfig.load(app_data_path("api_config.json"))
    service = service or ApiService(config)
    config.log_dir = str(app_path(config.log_dir))
    configure_api_logging(config)
    app = FastAPI(title="HandWeChat API", version="0.2.0", lifespan=None)

    @app.get("/", include_in_schema=False)
    def console() -> FileResponse:
        return FileResponse(
            resource_path("handwechat/api/static/index.html"),
            media_type="text/html",
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            service.attach()
        except HWError:
            # WeChat may not be logged in yet. Health and attach still work.
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
            if isinstance(exc, SendTaskNotFoundError)
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
        task = service.send(request.text)
        return SendTaskQueuedResponse(task_id=task.task_id, status="queued")

    @app.get("/api/v1/send/{task_id}", response_model=SendTaskResponse)
    def send_task(task_id: str) -> SendTaskResponse:
        return _send_task_response(service.get_send_task(task_id))

    return app


app = create_app()
