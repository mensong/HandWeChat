# -*- coding: utf-8 -*-
"""HTTP API 与 WeChatRuntime 之间的服务层。"""

from __future__ import annotations

import threading
import logging
import time
from typing import Callable, Optional

import uiautomation as uia

from handwechat.errors import HWError, WeChatOfflineError
from handwechat.runtime import WeChatRuntime
from handwechat.version_info import get_process_version
from handwechat.wechat import WeChat
from handwechat.api.config import ApiConfig
from handwechat.api.message_buffer import BufferedMessage, MessageBuffer
from handwechat.api.send_tasks import SendTask, SendTaskStore


REQUIRED_WECHAT_VERSION = "4.2.0.52"


class WeChatVersionError(HWError):
    """微信版本偏离当前控件树基线。"""


class ListenerSessionNotFoundError(HWError):
    """监听会话不存在或未启用。"""


class ListenerSessionLimitError(HWError):
    """监听会话数量已达上限。"""


class SendTaskNotFoundError(HWError):
    """发送任务不存在或已被清理。"""


class ApiService:
    """持有 API 进程内唯一的微信运行时。"""

    def __init__(
        self,
        config: Optional[ApiConfig] = None,
        *,
        wechat_factory: Callable[..., WeChat] = WeChat,
        runtime_factory: Callable[..., WeChatRuntime] = WeChatRuntime,
        message_buffer_factory: Callable[..., MessageBuffer] = MessageBuffer,
        send_task_store_factory: Callable[..., SendTaskStore] = SendTaskStore,
        baseline_version: str = REQUIRED_WECHAT_VERSION,
    ) -> None:
        self.config = config or ApiConfig()
        self._wechat_factory = wechat_factory
        self._runtime_factory = runtime_factory
        self._message_buffer_factory = message_buffer_factory
        self._send_task_store_factory = send_task_store_factory
        self._baseline_version = baseline_version
        self._wx: Optional[WeChat] = None
        self._runtime: Optional[WeChatRuntime] = None
        self._attach_lock = threading.RLock()
        self._last_error: Optional[str] = None
        self._last_recovery_at = 0.0
        self.message_buffer = message_buffer_factory(self.config.message_buffer_size)
        self.send_tasks = send_task_store_factory(
            self.config.send_task_retention_minutes
        )

    @property
    def wx(self) -> Optional[WeChat]:
        return self._wx

    @property
    def runtime(self) -> Optional[WeChatRuntime]:
        return self._runtime

    @property
    def attached(self) -> bool:
        return self._runtime is not None

    def health(self) -> dict:
        wx = self._wx
        runtime = self._runtime
        online = bool(wx and wx.IsOnline())
        if not online and wx is not None:
            online = self._try_recover()
            if not online:
                self._last_error = self._last_error or "微信不在线或控件树不可用"
        version = ""
        if online:
            try:
                version = get_process_version(wx.pid)
            except Exception:
                version = ""

        baseline_matched = bool(version and version == self._baseline_version)
        if not online:
            status = "offline"
        elif not baseline_matched:
            status = "degraded"
            self._last_error = (
                f"微信版本 {version or '未知'} 与基线 {self._baseline_version} 不匹配"
            )
        else:
            status = "ok"
            if self._last_error and "版本" in self._last_error:
                self._last_error = None

        listener_session_count = 0
        if runtime is not None:
            try:
                listener_session_count = len(runtime.list_listener_sessions())
            except Exception:
                listener_session_count = 0

        return {
            "status": status,
            "wechat_pid": wx.pid if online and wx else None,
            "wechat_hwnd": f"{wx.hwnd:#x}" if online and wx else None,
            "wechat_version": version,
            "online": online,
            "listener_running": bool(
                online and runtime and runtime.listener_running
            ),
            "send_queue_size": runtime.send_queue_size if runtime else 0,
            "version_baseline_matched": baseline_matched,
            "attached": runtime is not None,
            "listener_session_count": listener_session_count,
            "message_buffer_size": self.message_buffer.size,
            "send_task_active": self.send_tasks.active_count(),
            "poll_interval": self.config.poll_interval,
            "max_send_per_minute": self.config.max_send_per_minute,
            "last_error": self._last_error,
        }

    def attach(self) -> dict:
        with self._attach_lock:
            if self._runtime is not None:
                self._runtime.shutdown()
            self.send_tasks.fail_active(
                error_message="微信已重新附加，原发送任务不再继续执行"
            )
            self._runtime = None
            self._wx = None

            with uia.UIAutomationInitializerInThread():
                wx = self._wechat_factory()
                if not wx.IsOnline():
                    raise WeChatOfflineError("微信不在线或未登录，无法附加")

                runtime = self._runtime_factory(
                    wx,
                    poll_interval=self.config.poll_interval,
                    my_names=self.config.my_names,
                    max_send_per_minute=self.config.max_send_per_minute,
                    on_message=self._on_runtime_message,
                    on_send_started=self._on_send_started,
                    on_send_task_completed=self._on_send_task_completed,
                    on_error=self._on_runtime_error,
                    on_state_changed=self._on_listener_state_changed,
                    on_health_event=self._on_health_event,
                    on_health_changed=self._on_health_changed,
                )
                runtime.start_send_queue()
                self._wx = wx
                self._runtime = runtime

                version = get_process_version(wx.pid)
                if version == self._baseline_version:
                    self._restore_listener_sessions(runtime)
                    runtime.start_listener()

            return {
                "status": "attached",
                "wechat_pid": wx.pid,
                "wechat_hwnd": f"{wx.hwnd:#x}",
            }

    def add_listener_session(self, name: str):
        self._require_ready()
        runtime = self._runtime
        if runtime is None:
            raise WeChatOfflineError("微信未附加，请先调用 POST /api/v1/attach")
        with uia.UIAutomationInitializerInThread():
            try:
                return runtime.add_listener_session(name)
            except ValueError as exc:
                self._raise_listener_error(exc)

    def remove_listener_session(self, name: str) -> None:
        self._require_ready()
        runtime = self._runtime
        if runtime is None:
            raise WeChatOfflineError("微信未附加，请先调用 POST /api/v1/attach")
        with uia.UIAutomationInitializerInThread():
            if not runtime.remove_listener_session(name):
                raise ListenerSessionNotFoundError(f"监听会话不存在：{name!r}")

    def get_listener_sessions(self) -> list[dict]:
        runtime = self._runtime
        if runtime is None:
            return []
        return [
            {
                "name": name,
                "type": runtime.listener_session_type(name),
            }
            for name in runtime.list_listener_sessions()
        ]

    def start_listener(self) -> None:
        with uia.UIAutomationInitializerInThread():
            self._require_ready()
            if self._runtime is not None:
                self._runtime.start_listener()

    def stop_listener(self) -> None:
        if self._runtime is None:
            raise WeChatOfflineError("微信未附加，请先调用 POST /api/v1/attach")
        self._runtime.stop_listener()

    def get_messages(
        self,
        cursor: int,
        *,
        limit: int = 100,
        timeout: float = 0.0,
    ) -> tuple[list[BufferedMessage], int, bool]:
        return self.message_buffer.get_after(cursor, limit=limit, timeout=timeout)

    def send(self, session: str, text: str) -> SendTask:
        with uia.UIAutomationInitializerInThread():
            self._require_ready()
            runtime = self._runtime
            if runtime is None:
                raise WeChatOfflineError("微信未附加，请先调用 POST /api/v1/attach")
            task = self.send_tasks.create(session, text)
            runtime.send_queue.submit(session, text, task_id=task.task_id)
            return task

    def get_send_task(self, task_id: str) -> SendTask:
        task = self.send_tasks.get(task_id)
        if task is None:
            raise SendTaskNotFoundError(f"发送任务不存在：{task_id!r}")
        return task

    def get_sessions(self) -> list:
        with uia.UIAutomationInitializerInThread():
            wx = self._require_ready()
            return wx.GetSession(self.config.max_sessions)

    def shutdown(self) -> None:
        with self._attach_lock:
            if self._runtime is not None:
                self._runtime.shutdown()
            self._runtime = None
            self._wx = None

    def _require_ready(self) -> WeChat:
        wx = self._wx
        if wx is None:
            raise WeChatOfflineError("微信未附加，请先调用 POST /api/v1/attach")
        if not wx.IsOnline():
            raise WeChatOfflineError("微信已离线或已退出登录")
        version = get_process_version(wx.pid)
        if version != self._baseline_version:
            raise WeChatVersionError(
                f"微信版本 {version or '未知'} 与基线 {self._baseline_version} 不匹配，"
                "已拒绝 UIA 会话操作"
            )
        return wx

    def _try_recover(self) -> bool:
        """微信窗口仍在但 UIA 树失效时，尝试恢复控件树。"""

        wx = self._wx
        if wx is None:
            return False
        now = time.monotonic()
        if now - self._last_recovery_at < 5.0:
            return bool(wx.IsOnline())
        self._last_recovery_at = now
        try:
            with uia.UIAutomationInitializerInThread():
                recovered = bool(wx.Recover())
        except Exception as exc:
            self._last_error = f"微信控件树恢复失败：{type(exc).__name__}: {exc}"
            logging.getLogger(__name__).warning(self._last_error)
            return False
        online = bool(wx.IsOnline())
        if recovered and online:
            self._last_error = None
            logging.getLogger(__name__).info("微信控件树恢复成功")
        return online

    def _restore_listener_sessions(self, runtime: WeChatRuntime) -> None:
        for name in self.config.listener_sessions:
            try:
                runtime.add_listener_session(name)
            except Exception:
                # 配置中的会话可能暂未出现在微信里；服务仍保持可用。
                pass

    def _on_runtime_message(self, session: str, message) -> None:
        self.message_buffer.append(session, message, self._listener_session_type(session))

    def _on_runtime_error(self, context: str, exc: Exception) -> None:
        self._last_error = f"{context}: {type(exc).__name__}: {exc}"
        logging.getLogger(__name__).warning(self._last_error)

    def _on_listener_state_changed(self, running: bool) -> None:
        logging.getLogger(__name__).info(
            "微信监听状态：%s", "running" if running else "stopped"
        )

    def _on_health_event(self, kind: str, message: str) -> None:
        logging.getLogger(__name__).info("[%s] %s", kind, message)

    def _on_health_changed(self, ok: bool, detail: str) -> None:
        if ok:
            self._last_error = None
        else:
            self._last_error = detail or "微信健康自检未通过"

    def _on_send_started(self, task_id: str, _session: str, _text: str) -> None:
        self.send_tasks.mark_started(task_id)

    def _on_send_task_completed(
        self,
        task_id: str,
        _session: str,
        _text: str,
        ok: bool,
        detail: str,
    ) -> None:
        self.send_tasks.mark_completed(task_id, ok=ok, detail=detail)

    def _listener_session_type(self, session: str) -> Optional[str]:
        runtime = self._runtime
        if runtime is None:
            return None
        return runtime.listener_session_type(session)

    @staticmethod
    def _raise_listener_error(exc: ValueError) -> None:
        text = str(exc)
        if "上限" in text:
            raise ListenerSessionLimitError(text)
        raise ListenerSessionNotFoundError(text)
