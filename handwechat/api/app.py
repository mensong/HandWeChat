# -*- coding: utf-8 -*-
"""FastAPI application for the current WeChat chat."""

from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Annotated

from fastapi import (
    FastAPI,
    File,
    Form,
    Query,
    Request,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AfterValidator
from starlette.exceptions import HTTPException as StarletteHTTPException

from handwechat.api.config import ApiConfig
from handwechat.api.logging_setup import configure_api_logging
from handwechat.api.models import (
    ApiConfigResponse,
    ApiConfigSaveRequest,
    ApiConfigSavedResponse,
    AttachResponse,
    HealthResponse,
    HistoryClearResponse,
    HistoryResponse,
    ListenerControlResponse,
    LogFileListResponse,
    LogTailResponse,
    MessageListResponse,
    MessageResponse,
    OcrCalibrationResponse,
    OcrCalibrationSaveRequest,
    OcrCalibrationSavedResponse,
    OcrCaptureResponse,
    OcrColorSampleRequest,
    OcrColorSampleResponse,
    SendRequest,
    SendTaskQueuedResponse,
    SendTaskResponse,
)
from handwechat.api.service import (
    ApiService,
    LogNotFoundError,
    OcrChannelNotRunningError,
    SendTaskNotFoundError,
)
from handwechat.errors import AttachError, HWError, OcrBindError, WeChatOfflineError
from handwechat.paths import app_data_path, app_path, resource_path
from handwechat.sending import MAX_IMAGE_BYTES
from handwechat.version import __version__


def _error_code(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code
    if isinstance(exc, WeChatOfflineError):
        return "WECHAT_OFFLINE"
    if isinstance(exc, AttachError):
        return "ATTACH_FAILED"
    if isinstance(exc, SendTaskNotFoundError):
        return "SEND_TASK_NOT_FOUND"
    if isinstance(exc, OcrChannelNotRunningError):
        return "OCR_CHANNEL_NOT_RUNNING"
    if isinstance(exc, OcrBindError):
        return "OCR_CHAT_UNBOUND"
    if isinstance(exc, LogNotFoundError):
        return "LOG_NOT_FOUND"
    return "INTERNAL_ERROR"


def _http_error_code(status_code: int) -> str:
    return {
        status.HTTP_404_NOT_FOUND: "NOT_FOUND",
        status.HTTP_405_METHOD_NOT_ALLOWED: "METHOD_NOT_ALLOWED",
    }.get(status_code, "HTTP_ERROR")


def _validate_message_cursor(value: int) -> int:
    # cursor 仅接受 -1（尾部）或非负整数，其余走 422 INVALID_REQUEST
    if value != -1 and value < 0:
        raise ValueError("cursor 仅支持 -1 或非负整数")
    return value


MessageCursorParam = Annotated[int, Query(), AfterValidator(_validate_message_cursor)]


access_logger = logging.getLogger("handwechat.api.access")

_HEALTH_PATH = "/api/v1/health"
_MESSAGES_PATH = "/api/v1/messages"
_LOGS_PATH = "/api/v1/logs"
_HISTORY_PATH = "/api/v1/ocr/history"
_MAX_LOG_CONTENT = 200


def _is_diagnostic_path(path: str) -> bool:
    """Paths the console polls to inspect state; logging them grows the log."""

    return (
        path == _LOGS_PATH
        or path.startswith(_LOGS_PATH + "/")
        or path == _HISTORY_PATH
    )


def _log_items(items) -> list[dict]:
    records = []
    total = len(items)
    for index, item in enumerate(items):
        content = str(item.content)
        record = _message_response(
            item, is_latest=index == total - 1
        ).model_dump()
        record["content"] = content[:_MAX_LOG_CONTENT]
        if len(content) > _MAX_LOG_CONTENT:
            record["truncated"] = True
        records.append(record)
    return records



def _write_access_log(
    request: Request,
    status_code: int,
    started_at: float,
    extra: dict | None = None,
) -> None:
    payload = {
        "method": request.method,
        "path": request.url.path,
        "status": status_code,
        "duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
        "client": request.client.host if request.client else None,
    }
    if extra:
        payload.update(extra)
    access_logger.info(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _health_snapshot(service, status_code: int) -> tuple[bool, dict]:
    """Return whether health looks normal plus the fields worth logging.

    The middleware runs around a streaming wrapper that has no readable body,
    and HTTP 200 alone is not a health signal anyway: /api/v1/health answers
    200 while WeChat is offline too.
    """

    try:
        payload = service.health()
    except Exception:
        return False, {"healthy": False}

    healthy = (
        status_code < 400
        and payload.get("status") == "ok"
        and not payload.get("last_error")
    )
    extra = {"healthy": healthy}
    if payload.get("status"):
        extra["health_status"] = payload["status"]
    if payload.get("last_error"):
        extra["last_error"] = payload["last_error"]
    return healthy, extra


def _message_response(item, *, is_latest: bool = False) -> MessageResponse:
    return MessageResponse(
        id=item.id,
        cursor=item.cursor,
        sender=item.sender,
        content=item.content,
        received_at=item.received_at,
        type=str(getattr(item, "type", "text")),
        is_latest=is_latest,
        confidence=(
            1.0 if is_latest else float(getattr(item, "confidence", 1.0))
        ),
    )


def _send_task_response(item) -> SendTaskResponse:
    return SendTaskResponse.model_validate(item, from_attributes=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _ws_access_log(
    websocket: WebSocket, started_at: float, messages_sent: int
) -> None:
    payload = {
        "method": "WS",
        "path": websocket.url.path,
        "status": status.HTTP_101_SWITCHING_PROTOCOLS,
        "duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
        "client": websocket.client.host if websocket.client else None,
        "messages_sent": messages_sent,
    }
    access_logger.info(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _write_ws_delivery(websocket: WebSocket, items) -> None:
    payload = {
        "method": "WS",
        "path": websocket.url.path,
        "client": websocket.client.host if websocket.client else None,
        "items": _log_items(items),
    }
    access_logger.info(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def create_app(
    *,
    config: ApiConfig | None = None,
    service: ApiService | None = None,
) -> FastAPI:
    config = config or ApiConfig.load(app_data_path("api_config.json"))
    service = service or ApiService(config)
    config.log_dir = str(app_path(config.log_dir))
    configure_api_logging(config)
    app = FastAPI(title="HandWeChat API", version=__version__, lifespan=None)

    @app.get("/", include_in_schema=False)
    def console() -> FileResponse:
        return FileResponse(
            resource_path("handwechat/api/static/index.html"),
            media_type="text/html",
        )

    app.mount(
        "/static",
        StaticFiles(directory=resource_path("handwechat/api/static")),
        name="static",
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

    # The access log only records what changed: a healthy poll and a
    # successful /api/v1/messages poll are noise, while every other request
    # keeps its line.
    health_state = {"healthy": True}

    @app.middleware("http")
    async def access_log_middleware(request: Request, call_next):
        started_at = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            _write_access_log(request, 500, started_at)
            raise

        path = request.url.path
        extra = None
        if path == _HEALTH_PATH:
            healthy, extra = _health_snapshot(service, response.status_code)
            if healthy == health_state["healthy"]:
                return response
            health_state["healthy"] = healthy
        elif path == _MESSAGES_PATH:
            extra = getattr(request.state, "access_extra", None)
            if response.status_code < 400 and not extra:
                return response
        elif _is_diagnostic_path(path) and response.status_code < 400:
            # Reading state must not grow the log, or auto-refresh would
            # keep appending a line every few seconds.
            return response

        _write_access_log(request, response.status_code, started_at, extra)
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
        error_status = getattr(exc, "status", None) or (
            status.HTTP_404_NOT_FOUND
            if isinstance(exc, (SendTaskNotFoundError, LogNotFoundError))
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
        request: Request,
        cursor: MessageCursorParam = 0,
        limit: int = Query(default=100, ge=1, le=100),
        timeout: int = Query(default=0, ge=0, le=30),
    ) -> MessageListResponse:
        items, next_cursor, has_more = service.get_messages(
            cursor,
            limit=limit,
            timeout=timeout,
        )
        if items:
            request.state.access_extra = {"items": _log_items(items)}
        return MessageListResponse(
            items=[
                _message_response(item, is_latest=index == len(items) - 1)
                for index, item in enumerate(items)
            ],
            next_cursor=next_cursor,
            has_more=has_more,
            latest_cursor=service.message_cursor,
        )

    @app.get("/api/v1/logs", response_model=LogFileListResponse)
    def log_files() -> LogFileListResponse:
        return LogFileListResponse.model_validate(service.list_logs())

    @app.get("/api/v1/logs/{name}", response_model=LogTailResponse)
    def log_tail(
        name: str,
        lines: int = Query(default=500, ge=1, le=5000),
        keyword: str = Query(default="", max_length=200),
    ) -> LogTailResponse:
        return LogTailResponse.model_validate(
            service.read_log(name, lines=lines, keyword=keyword)
        )

    @app.get("/api/v1/ocr/history", response_model=HistoryResponse)
    def ocr_history() -> HistoryResponse:
        return HistoryResponse.model_validate(service.get_history())

    @app.post("/api/v1/ocr/history/clear", response_model=HistoryClearResponse)
    def clear_ocr_history() -> HistoryClearResponse:
        return HistoryClearResponse.model_validate(service.clear_history())

    @app.get("/api/v1/config", response_model=ApiConfigResponse)
    def get_api_config() -> ApiConfigResponse:
        return ApiConfigResponse.model_validate(service.get_config())

    @app.put("/api/v1/config", response_model=ApiConfigSavedResponse)
    def save_api_config(request: ApiConfigSaveRequest) -> ApiConfigSavedResponse:
        return ApiConfigSavedResponse.model_validate(
            service.save_config(request.values)
        )

    @app.get(
        "/api/v1/config/ocr_calibration",
        response_model=OcrCalibrationResponse,
    )
    def get_ocr_calibration() -> OcrCalibrationResponse:
        return OcrCalibrationResponse.model_validate(service.get_ocr_calibration())

    @app.post(
        "/api/v1/config/ocr_calibration/capture",
        response_model=OcrCaptureResponse,
    )
    async def capture_ocr_screenshot() -> OcrCaptureResponse:
        return OcrCaptureResponse.model_validate(
            await asyncio.to_thread(service.capture_ocr_screenshot)
        )

    @app.get("/api/v1/config/ocr_calibration/capture/{capture_id}")
    def ocr_calibration_capture(capture_id: str) -> Response:
        return Response(
            content=service.capture_image(capture_id),
            media_type="image/png",
            headers={"Cache-Control": "no-store"},
        )

    @app.post(
        "/api/v1/config/ocr_calibration/sample_color",
        response_model=OcrColorSampleResponse,
    )
    def sample_ocr_color(request: OcrColorSampleRequest) -> OcrColorSampleResponse:
        return OcrColorSampleResponse.model_validate(
            service.sample_ocr_color(
                request.capture_id,
                request.x_ratio,
                request.y_ratio,
            )
        )

    @app.put(
        "/api/v1/config/ocr_calibration",
        response_model=OcrCalibrationSavedResponse,
    )
    def save_ocr_calibration(
        request: OcrCalibrationSaveRequest,
    ) -> OcrCalibrationSavedResponse:
        return OcrCalibrationSavedResponse.model_validate(
            service.save_ocr_calibration(
                request.capture_id,
                title_box=request.title_box,
                chat_box=request.chat_box,
                input_x_ratio=request.input_x_ratio,
                input_y_ratio=request.input_y_ratio,
                palette=request.palette,
            )
        )

    @app.post(
        "/api/v1/send",
        response_model=SendTaskQueuedResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def send_message(request: SendRequest) -> SendTaskQueuedResponse:
        task = service.send(request.text)
        return SendTaskQueuedResponse(task_id=task.task_id, status="queued")

    @app.post(
        "/api/v1/send/image",
        response_model=SendTaskQueuedResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def send_image(
        file: Annotated[UploadFile, File()],
        text: Annotated[str, Form()] = "",
        image_name: Annotated[str, Form()] = "",
    ) -> SendTaskQueuedResponse:
        data = await file.read(MAX_IMAGE_BYTES + 1)
        task = await asyncio.to_thread(
            service.send_image,
            data,
            filename=file.filename or "",
            text=text,
            image_name=image_name,
        )
        return SendTaskQueuedResponse(task_id=task.task_id, status="queued")

    @app.get("/api/v1/send/{task_id}", response_model=SendTaskResponse)
    def send_task(task_id: str) -> SendTaskResponse:
        return _send_task_response(service.get_send_task(task_id))

    @app.websocket("/api/v1/ws/messages")
    async def ws_messages(websocket: WebSocket) -> None:
        await websocket.accept()
        recv_logger = logging.getLogger("handwechat.api.ws")
        started_at = time.perf_counter()
        loop = asyncio.get_running_loop()
        wake = asyncio.Event()
        # 只推送连接建立之后的消息：先快照游标，再注册订阅，随后立刻 drain，
        # 这样快照与订阅之间到达的消息不会丢也不会重发。
        cursor = service.message_cursor
        sent_messages = 0

        def _notify() -> None:
            try:
                loop.call_soon_threadsafe(wake.set)
            except RuntimeError:
                pass  # 客户端已断开，事件循环不再接收回调

        async def _watch_client() -> None:
            try:
                while True:
                    await websocket.receive_text()
            except Exception:
                pass

        service.subscribe_messages(_notify)
        watcher = asyncio.create_task(_watch_client())
        try:
            await websocket.send_json(
                {
                    "type": "ready",
                    "buffer_size": service.message_buffer.size,
                }
            )
            while True:
                wake.clear()
                items, cursor, has_more = service.get_messages(
                    cursor, limit=config.ws_batch_limit
                )
                if items:
                    await websocket.send_json(
                        {
                            "type": "messages",
                            "items": [
                                _message_response(
                                    item, is_latest=index == len(items) - 1
                                ).model_dump()
                                for index, item in enumerate(items)
                            ],
                            "has_more": has_more,
                        }
                    )
                    sent_messages += len(items)
                    _write_ws_delivery(websocket, items)
                    continue

                waiter = asyncio.create_task(wake.wait())
                done, _pending = await asyncio.wait(
                    {waiter, watcher},
                    timeout=config.ws_ping_interval,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                waiter.cancel()
                if watcher in done:
                    break
                if waiter in done:
                    continue
                await websocket.send_json({"type": "ping", "ts": _now_iso()})
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            recv_logger.warning("WebSocket 消息推送中断：%s", exc)
            try:
                await websocket.send_json(
                    {"type": "error", "code": _error_code(exc), "message": str(exc)}
                )
            except Exception:
                pass
        finally:
            service.unsubscribe_messages(_notify)
            watcher.cancel()
            _ws_access_log(websocket, started_at, sent_messages)

    return app


app = create_app()
