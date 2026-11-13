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
    last_error: Optional[str] = None


class ListenerControlResponse(BaseModel):
    status: Literal["running", "stopped"]


class MessageResponse(BaseModel):
    id: str
    cursor: int
    sender: str
    content: str
    is_at_me: bool
    received_at: str


class MessageListResponse(BaseModel):
    items: list[MessageResponse] = Field(default_factory=list)
    next_cursor: int
    has_more: bool


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
