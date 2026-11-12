# -*- coding: utf-8 -*-
"""WeChat 门面类 —— API 命名与 wxauto4 对齐，便于将来一行 import 切换。"""

from __future__ import annotations

from typing import List, Optional, Sequence

from handwechat.controls.main_wnd import MainWnd, screen_reader_enabled
from handwechat.controls.search import SearchBox
from handwechat.errors import WeChatOfflineError
from handwechat.models import BaseMessage, Session, SessionType, TextMessage
from handwechat.uia.wrapper import ui_lock


class WeChat:
    """微信 PC 4.x 自动化入口。

    用法：
        from handwechat import WeChat
        wx = WeChat()           # 附加已登录、控件树已物化的微信主窗口
        wx.IsOnline()
        wx.GetSession()

    前置条件：用 scripts/start_wechat.py 启动微信（先设读屏标志再拉起微信）。
    """

    def __init__(self, attach_timeout: float = 10.0) -> None:
        self._wnd = MainWnd(attach_timeout)

    # -- 状态 --------------------------------------------------------------
    def IsOnline(self) -> bool:
        """主窗口健在且控件树可用。"""
        return self._wnd.is_alive()

    @property
    def pid(self) -> int:
        return self._wnd.pid

    @property
    def hwnd(self) -> int:
        return self._wnd.hwnd

    @staticmethod
    def ScreenReaderEnabled() -> bool:
        return screen_reader_enabled()

    # -- 会话 --------------------------------------------------------------
    def GetSession(self, max_sessions: int = 100) -> List[Session]:
        """枚举会话列表（含滚动）。字段：name/preview/time/unread/is_at_me/is_pinned。

        注意：type（群/私聊）在 A1 恒为 None，A2 经 ChatWith 读标题栏后回填。
        """
        with ui_lock:
            if not self._wnd.is_alive():
                raise WeChatOfflineError("微信窗口已失效，请重新附加")
            lst = self._wnd.session_list()
            if lst is None:
                from handwechat.errors import ControlNotFoundError
                raise ControlNotFoundError("会话列表控件（session_list）未找到：微信可能已升级")
            return lst.enumerate_sessions(max_sessions)

    # -- A2：打开会话 + 读当前消息 -----------------------------------------
    def ChatWith(self, who: str, timeout: float = 4.0) -> Optional[Session]:
        """精确打开指定会话，成功后返回标题栏解析出的 Session。"""

        if not who:
            return None
        with ui_lock:
            if not self._wnd.is_alive():
                raise WeChatOfflineError("微信窗口已失效，请重新附加")
            self._wnd.show()

            search = SearchBox(self._wnd.control)
            if search.open(who, timeout=timeout) is None:
                session_list = self._wnd.session_list(timeout)
                cell = session_list.find_cell(who) if session_list else None
                if cell is None:
                    return None
                cell.Click(simulateMove=False, waitTime=0.2)

            chat = self._wnd.chat_box(timeout)
            if chat is None:
                return None
            chat_name = chat.current_chat_name()
            if chat_name != who:
                return None
            return Session(
                name=chat_name,
                automation_id="session_item_" + chat_name,
                type=chat.session_type(),
            )

    def CurrentChat(self) -> Optional[str]:
        """返回当前会话名；未打开聊天页时返回 None。"""

        with ui_lock:
            if not self._wnd.is_alive():
                raise WeChatOfflineError("微信窗口已失效，请重新附加")
            chat = self._wnd.chat_box(1.0)
            return chat.current_chat_name() if chat else None

    def Recover(self) -> bool:
        """尝试恢复失效/收起的 UIA 控件树。"""

        return self._wnd.recover_tree()

    def SendMsg(
        self,
        msg: str,
        who: Optional[str] = None,
        clear: bool = True,
        at: Optional[str | List[str]] = None,
        exact: bool = False,
        verify: bool = True,
        verify_timeout: float = 4.0,
    ) -> Optional[TextMessage]:
        """向指定会话发送纯文本，并回读校验。

        兼容 wxauto4 的基本签名。MVP 只支持纯文本；at 参数保留但要求
        调用方显式降级为纯文本，避免生成“看起来像 @，但不是真实提及”的消息。
        verify=False 时只执行输入/发送动作，返回 None。
        """

        if not msg:
            return None
        if at:
            raise NotImplementedError(
                "MVP SendMsg 暂不支持真实 @人；请先把消息内容改为纯文本"
            )

        with ui_lock:
            if not self._wnd.is_alive():
                raise WeChatOfflineError("微信窗口已失效，请重新附加")
            self._wnd.show()

            chat = self._wnd.chat_box(2.0)
            current = chat.current_chat_name() if chat else None
            if who and current != who:
                chat = None if self.ChatWith(who) is None else self._wnd.chat_box(2.0)
            elif who is None and chat is None:
                return None

            if chat is None:
                return None
            if who and chat.current_chat_name() != who:
                return None

            chat.send_text(msg, clear=clear)
            if not verify:
                return None
            return chat.wait_for_self_text(msg, timeout=verify_timeout)

    def GetAllMessage(
        self, my_names: Optional[List[str]] = None
    ) -> List[BaseMessage]:
        """读取当前会话已加载的消息；UIA 只能看到聊天页当前加载范围。"""

        with ui_lock:
            if not self._wnd.is_alive():
                raise WeChatOfflineError("微信窗口已失效，请重新附加")
            self._wnd.show()
            chat = self._wnd.chat_box(2.0)
            if chat is None:
                return []
            return chat.get_all_messages(my_names=my_names or [])

    def GetMessagesFrom(
        self,
        who: str,
        my_names: Optional[List[str]] = None,
        timeout: float = 4.0,
    ) -> List[BaseMessage]:
        """切换到指定会话并读取当前消息窗口。

        与“ChatWith + GetAllMessage”分开调用相比，这里把切换和读取包在
        同一个 UI 锁内，避免监听线程与发送线程交替切换会话时读错页面。
        """

        if not who:
            return []
        with ui_lock:
            if not self._wnd.is_alive():
                raise WeChatOfflineError("微信窗口已失效，请重新附加")
            self._wnd.show()

            current_chat = self._wnd.chat_box(1.0)
            current = current_chat.current_chat_name() if current_chat else None
            if current != who and self.ChatWith(who, timeout=timeout) is None:
                return []

            chat = self._wnd.chat_box(timeout)
            if chat is None or chat.current_chat_name() != who:
                return []
            return chat.get_all_messages(my_names=my_names or [])

    def RecoverMessagesFrom(
        self,
        who: str,
        anchor_keys: Sequence[str],
        *,
        my_names: Optional[List[str]] = None,
        timeout: float = 2.0,
        max_scroll_rounds: int = 12,
        scroll_wait: float = 0.18,
        recovery_timeout: float = 2.5,
    ) -> Optional[List[BaseMessage]]:
        """向上回看指定会话，尝试找回旧锚点后的完整消息序列。

        返回 None 表示回溯预算内仍无法拼接出连续消息序列；调用方应把这种情况
        当作缺口暴露出来，而不是把当前窗口静默当作新基线。
        """

        if not who or not anchor_keys:
            return None
        with ui_lock:
            if not self._wnd.is_alive():
                raise WeChatOfflineError("微信窗口已失效，请重新附加")
            self._wnd.show()

            chat = self._wnd.chat_box(1.0)
            if chat is None or chat.current_chat_name() != who:
                if self.ChatWith(who, timeout=timeout) is None:
                    return None
                chat = self._wnd.chat_box(timeout)

            if chat is None or chat.current_chat_name() != who:
                return None
            return chat.read_messages_until_anchor(
                anchor_keys,
                my_names=my_names or [],
                max_scroll_rounds=max_scroll_rounds,
                scroll_wait=scroll_wait,
                timeout_seconds=recovery_timeout,
            )
