# -*- coding: utf-8 -*-
"""HTTP API 请求与响应模型。"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class AttachResponse(BaseModel):
    status: str
    wechat_pid: int
    wechat_hwnd: str


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded", "offline"]
    wechat_pid: Optional[int] = None
    wechat_hwnd: Optional[str] = None
    wechat_version: str = ""
    online: bool
    listener_running: bool
    send_queue_size: int
    version_baseline_matched: bool
    attached: bool
    listener_session_count: int
    message_buffer_size: int
    send_task_active: int
    poll_interval: float
    max_send_per_minute: int
    last_error: Optional[str] = None


class SessionResponse(BaseModel):
    name: str
    type: Optional[Literal["friend", "group"]] = None
    unread: bool
    preview: str = ""
    time: str = ""


class SessionListResponse(BaseModel):
    items: list[SessionResponse] = Field(default_factory=list)


class ListenerSessionAddRequest(BaseModel):
    name: str = Field(min_length=1)


class ListenerSessionResponse(BaseModel):
    name: str
    type: Optional[Literal["friend", "group"]] = None


class ListenerSessionListResponse(BaseModel):
    items: list[ListenerSessionResponse] = Field(default_factory=list)


class ListenerSessionAddResponse(BaseModel):
    status: str
    name: str


class ListenerSessionRemoveResponse(BaseModel):
    status: str
    name: str


class ListenerControlResponse(BaseModel):
    status: Literal["running", "stopped"]


class MessageResponse(BaseModel):
    id: str
    cursor: int
    session: str
    session_type: Optional[Literal["friend", "group"]] = None
    sender: str
    content: str
    is_at_me: bool
    received_at: str


class MessageListResponse(BaseModel):
    items: list[MessageResponse] = Field(default_factory=list)
    next_cursor: int
    has_more: bool


class SendRequest(BaseModel):
    session: str = Field(min_length=1)
    text: str = Field(min_length=1)


class SendTaskQueuedResponse(BaseModel):
    task_id: str
    status: Literal["queued"]


class SendTaskResponse(BaseModel):
    task_id: str
    status: Literal["queued", "running", "success", "failed"]
    session: str
    text: str
    submitted_at: str
    completed_at: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
