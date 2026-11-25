# -*- coding: utf-8 -*-
"""HTTP API request and response models."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class AttachResponse(BaseModel):
    status: str
    wechat_pid: Optional[int] = None
    wechat_hwnd: str


class HealthResponse(BaseModel):
    status: Literal["ok", "offline"]
    wechat_pid: Optional[int] = None
    wechat_hwnd: Optional[str] = None
    wechat_version: str = ""
    online: bool
    listener_running: bool
    send_queue_size: int
    attached: bool
    chat_title: str = ""
    chat_type: Optional[Literal["friend", "group"]] = None
    message_buffer_size: int
    send_task_active: int
    poll_interval: float
    max_send_per_minute: int
    history_capacity: int = 0
    history_size: int = 0
    last_error: Optional[str] = None


class ListenerControlResponse(BaseModel):
    status: Literal["running", "stopped"]


class MessageResponse(BaseModel):
    id: str
    cursor: int
    sender: str
    content: str
    received_at: str
    type: str = "text"
    is_latest: bool = False
    confidence: float = 1.0


class MessageListResponse(BaseModel):
    items: list[MessageResponse] = Field(default_factory=list)
    next_cursor: int
    has_more: bool
    latest_cursor: int = 0


class LogFileResponse(BaseModel):
    name: str
    size: int
    updated_at: str


class LogFileListResponse(BaseModel):
    log_dir: str
    files: list[LogFileResponse] = Field(default_factory=list)


class LogTailResponse(BaseModel):
    name: str
    size: int
    scanned_lines: int
    matched_lines: int
    truncated: bool
    lines: list[str] = Field(default_factory=list)


class HistoryItemResponse(BaseModel):
    attr: str
    sender: str = ""
    text: str
    confidence: float = 1.0
    is_latest: bool = False


class HistoryResponse(BaseModel):
    running: bool
    capacity: int
    size: int
    items: list[HistoryItemResponse] = Field(default_factory=list)


class HistoryClearResponse(BaseModel):
    cleared: int


class SendRequest(BaseModel):
    text: str = Field(min_length=1)


class SendTaskQueuedResponse(BaseModel):
    task_id: str
    status: Literal["queued"]


class SendTaskResponse(BaseModel):
    task_id: str
    status: Literal["queued", "running", "success", "failed"]
    text: str
    submitted_at: str
    completed_at: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None


class ConfigFieldSpec(BaseModel):
    key: str
    label: str
    group: str
    kind: str
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    step: Optional[float] = None
    restart_required: bool = False
    hint: str = ""


class ApiConfigResponse(BaseModel):
    path: str
    exists: bool
    error: Optional[str] = None
    values: dict
    defaults: dict
    fields: list[ConfigFieldSpec] = Field(default_factory=list)


class ApiConfigSaveRequest(BaseModel):
    values: dict


class ApiConfigSavedResponse(BaseModel):
    saved: bool = True
    path: str
    values: dict
    restart_required: bool


class OcrCalibrationResponse(BaseModel):
    path: str
    exists: bool
    error: Optional[str] = None
    calibration: dict
    defaults: dict


class OcrCaptureResponse(BaseModel):
    capture_id: str
    width: int
    height: int
    window_width: int
    window_height: int
    maximized: bool
    image_url: str


class OcrColorSampleRequest(BaseModel):
    capture_id: str
    x_ratio: float = Field(ge=0.0, le=1.0)
    y_ratio: float = Field(ge=0.0, le=1.0)


class OcrColorSampleResponse(BaseModel):
    rgb: list[int]
    x: int
    y: int


class OcrCalibrationSaveRequest(BaseModel):
    capture_id: str
    title_box: list[float]
    chat_box: list[float]
    input_x_ratio: float
    input_y_ratio: float
    palette: dict


class OcrCalibrationSavedResponse(BaseModel):
    saved: bool = True
    path: str
    calibration: dict
