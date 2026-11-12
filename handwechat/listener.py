# -*- coding: utf-8 -*-
"""显式会话监听器：轮询、回调、去重与断点恢复。"""

from __future__ import annotations

import json
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import uiautomation as uia

from handwechat.errors import HWError, WeChatOfflineError
from handwechat.models import BaseMessage, Session


MessageCallback = Callable[[str, BaseMessage], None]
ErrorCallback = Callable[[str, Exception], None]


@dataclass
class ListenState:
    """单个监听会话的内存/落盘状态。"""

    session_type: Optional[str] = None
    enabled: bool = True
    last_seq: int = 0
    recent_keys: List[str] = field(default_factory=list)
    updated_at: str = ""
    anchor_misses: int = 0

    def to_dict(self) -> dict:
        return {
            "session_type": self.session_type,
            "enabled": self.enabled,
            "last_seq": self.last_seq,
            "recent_keys": self.recent_keys,
            "updated_at": self.updated_at,
            "anchor_misses": self.anchor_misses,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ListenState":
        return cls(
            session_type=data.get("session_type"),
            enabled=bool(data.get("enabled", True)),
            last_seq=int(data.get("last_seq") or 0),
            recent_keys=list(data.get("recent_keys") or []),
            updated_at=str(data.get("updated_at") or ""),
            anchor_misses=int(data.get("anchor_misses") or 0),
        )


def _now_text() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _message_key(msg: BaseMessage) -> str:
    """生成不依赖 UIA index 的顺序指纹。

    BaseMessage.hash 包含 index，列表滚动后同一气泡的 hash 会变化；
    群聊 sender 来自 OCR/昵称行解析，可能不稳定，因此不参与锚点。
    极端情况下“任何人连续发送完全相同文本”也仍能通过尾部连续
    片段匹配区分。
    """

    return "\x1f".join((msg.type, msg.attr, msg.content))


def _has_key_overlap(old_keys: List[str], new_keys: List[str]) -> bool:
    """判断旧锚点与新窗口是否存在任何重叠。"""

    return bool(set(old_keys) & set(new_keys))


def _find_suffix(keys: List[str], suffix: List[str]) -> Tuple[int, int]:
    """返回 suffix 在 keys 中最早出现的起点和匹配长度；找不到返回 (-1, 0)。"""

    max_length = min(len(keys), len(suffix))
    for length in range(max_length, 0, -1):
        pattern = suffix[-length:]
        for start in range(0, len(keys) - length + 1):
            if keys[start : start + length] == pattern:
                return start, length
    return -1, 0


class MessageListener:
    """监听显式添加的会话，并在后台线程回调新消息。

    A4 不做全会话扫描，也不解析媒体文件。最大监听数与方案保持一致，
    断点状态保存为 JSON；M2/M3 可以替换成 SQLite 存储而不改外部行为。
    """

    def __init__(
        self,
        wx,
        *,
        poll_interval: float = 2.0,
        my_names: Optional[Sequence[str]] = None,
        state_path: str | Path = "data/listener_state.json",
        recent_key_limit: int = 128,
        anchor_recovery_rounds: int = 12,
        anchor_recovery_wait: float = 0.18,
        anchor_recovery_timeout: float = 2.5,
        on_message: Optional[MessageCallback] = None,
        on_error: Optional[ErrorCallback] = None,
    ) -> None:
        self._wx = wx
        self._poll_interval = max(0.2, float(poll_interval))
        self._my_names: Tuple[str, ...] = tuple(my_names or ())
        self._state_path = Path(state_path)
        self._recent_key_limit = max(8, int(recent_key_limit))
        self._anchor_recovery_rounds = max(0, min(40, int(anchor_recovery_rounds)))
        self._anchor_recovery_wait = max(0.05, float(anchor_recovery_wait))
        self._anchor_recovery_timeout = max(0.1, float(anchor_recovery_timeout))
        self._max_sessions = 100

        self._state_lock = threading.RLock()
        self._callback_lock = threading.Lock()
        self._on_message_callbacks: List[MessageCallback] = []
        self._on_error_callbacks: List[ErrorCallback] = []
        if on_message:
            self._on_message_callbacks.append(on_message)
        if on_error:
            self._on_error_callbacks.append(on_error)

        self._states: Dict[str, ListenState] = {}
        self._load_state()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- 配置 --------------------------------------------------------------
    def AddListenChat(
        self,
        who: str,
        savepic: bool = False,
        savefile: bool = False,
        savevoice: bool = False,
    ) -> Session:
        """添加监听会话。首加时打开一次会话并建立当前消息窗口基线。"""

        if not who:
            raise ValueError("监听会话名不能为空")
        if savepic or savefile or savevoice:
            raise NotImplementedError("A4/MVP 只支持纯文本消息，不支持自动保存媒体")

        if not self._wx.IsOnline():
            raise WeChatOfflineError("微信不在线，无法添加监听")

        with self._state_lock:
            active_other_count = sum(
                1
                for name, item in self._states.items()
                if item.enabled and name != who
            )
            if active_other_count >= self._max_sessions:
                raise ValueError("监听会话数已达上限 100")

        session = self._wx.ChatWith(who)
        if session is None:
            raise ValueError(f"找不到要监听的会话：{who!r}")

        messages = self._wx.GetMessagesFrom(
            who,
            my_names=list(self._my_names),
        )
        keys = [_message_key(msg) for msg in messages]

        with self._state_lock:
            state = self._states.get(who)
            if state is None:
                state = ListenState()
                self._states[who] = state
                state.recent_keys = keys[-self._recent_key_limit :]
            elif keys and state.recent_keys and not _has_key_overlap(
                state.recent_keys, keys
            ):
                # 旧锚点已经滚出当前可读窗口（典型是重启后），重建基线。
                # 这里不能把当前窗口当新消息回调，否则会把旧消息误报为新增。
                state.recent_keys = keys[-self._recent_key_limit :]
                state.anchor_misses = 0
            state.enabled = True
            state.session_type = session.type
            state.updated_at = _now_text()
        self._save_state()
        return session

    def RemoveListenChat(self, who: str) -> bool:
        """停用监听；保留断点，便于重新添加后继续按旧锚点恢复。"""

        with self._state_lock:
            state = self._states.get(who)
            existed = state is not None and state.enabled
            if state:
                state.enabled = False
                state.updated_at = _now_text()
        if existed:
            self._save_state()
        return existed

    def GetAllListenChat(self) -> List[str]:
        with self._state_lock:
            return [name for name, state in self._states.items() if state.enabled]

    def GetListenState(self, who: str) -> Optional[ListenState]:
        with self._state_lock:
            state = self._states.get(who)
            return ListenState(
                session_type=state.session_type,
                last_seq=state.last_seq,
                recent_keys=list(state.recent_keys),
                updated_at=state.updated_at,
                anchor_misses=state.anchor_misses,
            ) if state else None

    def SetMyNames(self, names: Sequence[str]) -> None:
        """更新群消息 @我 匹配使用的昵称。"""

        self._my_names = tuple(name for name in names if name)

    def AddMessageCallback(self, callback: MessageCallback) -> None:
        with self._callback_lock:
            self._on_message_callbacks.append(callback)

    def AddErrorCallback(self, callback: ErrorCallback) -> None:
        with self._callback_lock:
            self._on_error_callbacks.append(callback)

    # -- 运行 --------------------------------------------------------------
    def Start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="handwechat-listener",
            daemon=True,
        )
        self._thread.start()

    def Stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout)
        self._save_state()

    def PollOnce(self, who: Optional[str] = None) -> Dict[str, List[BaseMessage]]:
        """立即轮询一次；返回每会话真正新增的消息。"""

        if who is not None:
            names = [who]
        else:
            with self._state_lock:
                names = list(self._states.keys())

        result: Dict[str, List[BaseMessage]] = {}
        for name in names:
            new_messages = self._poll_chat(name)
            result[name] = new_messages
            for msg in new_messages:
                self._emit_message(name, msg)
        return result

    # -- 内部 --------------------------------------------------------------
    def _run(self) -> None:
        with uia.UIAutomationInitializerInThread():
            while not self._stop_event.is_set():
                try:
                    self.PollOnce()
                except Exception as exc:  # 单次轮询失败不能终止监听线程
                    self._report_error("后台轮询", exc)
                self._stop_event.wait(self._poll_interval)

    def _poll_chat(self, who: str) -> List[BaseMessage]:
        try:
            with self._state_lock:
                state = self._states.get(who)
                if state is None or not state.enabled:
                    return []
                messages = self._wx.GetMessagesFrom(
                    who,
                    my_names=list(self._my_names),
                )
                keys = [_message_key(msg) for msg in messages]
                new_messages = self._diff_messages(state, keys, messages)
                if new_messages is None:
                    if not _has_key_overlap(state.recent_keys, keys):
                        def fallback_current_page(reason: str) -> List[BaseMessage]:
                            fallback_messages = self._wx.GetMessagesFrom(
                                who,
                                my_names=list(self._my_names),
                            )
                            fallback_keys = [
                                _message_key(msg) for msg in fallback_messages
                            ]
                            state.recent_keys = fallback_keys[
                                -self._recent_key_limit :
                            ]
                            state.anchor_misses = 0
                            state.updated_at = _now_text()
                            self._assign_seq(state, fallback_messages)
                            self._report_error(
                                f"监听 {who!r}",
                                RuntimeError(reason),
                            )
                            return fallback_messages

                        recovered_messages: Optional[List[BaseMessage]] = None
                        if self._anchor_recovery_rounds:
                            recovered_messages = self._wx.RecoverMessagesFrom(
                                who,
                                anchor_keys=state.recent_keys,
                                my_names=list(self._my_names),
                                max_scroll_rounds=self._anchor_recovery_rounds,
                                scroll_wait=self._anchor_recovery_wait,
                                recovery_timeout=self._anchor_recovery_timeout,
                            )

                        if recovered_messages is None:
                            # UIA 消息窗口很短；活跃群里的旧锚点可能整段滚出。
                            # 回溯失败时不静默重建；当前页消息也要计入并回调。
                            return fallback_current_page(
                                "旧锚点已滚出当前消息窗口，回溯恢复失败；"
                                "已回放当前页并重建基线，本轮可能存在消息缺口"
                            )

                        recovered_keys = [
                            _message_key(msg) for msg in recovered_messages
                        ]
                        new_messages = self._diff_messages(
                            state, recovered_keys, recovered_messages
                        )
                        if new_messages is None:
                            return fallback_current_page(
                                "回溯消息仍无法匹配旧锚点；"
                                "已回放当前页并重建基线，本轮可能存在消息缺口"
                            )
                        keys = recovered_keys
                    else:
                        state.anchor_misses += 1
                        if state.anchor_misses >= 3:
                            # 部分重叠但顺序对不上时仍短暂等待，避免把同文
                            # 消息的极端情况误判成全新基线。
                            state.recent_keys = keys[-self._recent_key_limit :]
                            state.anchor_misses = 0
                            state.updated_at = _now_text()
                            self._report_error(
                                f"监听 {who!r}",
                                RuntimeError("消息锚点连续失配，已重建基线"),
                            )
                        return []
                self._assign_seq(state, new_messages)
                state.anchor_misses = 0
                state.recent_keys = keys[-self._recent_key_limit :]
                state.updated_at = _now_text()
        except HWError as exc:
            self._report_error(f"轮询 {who!r}", exc)
            return []
        except Exception as exc:
            self._report_error(f"轮询 {who!r}", exc)
            return []
        self._save_state()
        return new_messages

    @staticmethod
    def _diff_messages(
        state: ListenState,
        keys: List[str],
        messages: List[BaseMessage],
    ) -> Optional[List[BaseMessage]]:
        if not state.recent_keys:
            return list(messages)
        if not keys:
            return []

        start, length = _find_suffix(keys, state.recent_keys)
        if start < 0:
            return None
        return list(messages[start + length :])

    @staticmethod
    def _assign_seq(state: ListenState, new_messages: List[BaseMessage]) -> None:
        for msg in new_messages:
            state.last_seq += 1
            msg.extra["listen_seq"] = state.last_seq
            msg.extra["listen_time"] = _now_text()

    def _emit_message(self, who: str, msg: BaseMessage) -> None:
        with self._callback_lock:
            callbacks = tuple(self._on_message_callbacks)
        for callback in callbacks:
            try:
                callback(who, msg)
            except Exception as exc:
                self._report_error(f"消息回调 {who!r}", exc)

    def _report_error(self, context: str, exc: Exception) -> None:
        with self._callback_lock:
            callbacks = tuple(self._on_error_callbacks)
        if callbacks:
            for callback in callbacks:
                try:
                    callback(context, exc)
                except Exception:
                    traceback.print_exc()
        else:
            print(f"[listener] {context}: {exc!r}")

    def _load_state(self) -> None:
        if not self._state_path.exists():
            return
        data = json.loads(self._state_path.read_text(encoding="utf-8"))
        if data.get("version") != 1:
            raise ValueError(f"不支持的监听断点版本：{data.get('version')!r}")
        for name, item in (data.get("chats") or {}).items():
            self._states[name] = ListenState.from_dict(item)

    def _save_state(self) -> None:
        with self._state_lock:
            payload = {
                "version": 1,
                "updated_at": _now_text(),
                "chats": {
                    name: state.to_dict() for name, state in self._states.items()
                },
            }
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
            temp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temp_path.replace(self._state_path)
