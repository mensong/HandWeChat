# -*- coding: utf-8 -*-
"""OCR runtime facade shared by the GUI."""

from __future__ import annotations

from typing import Callable, Optional

from handwechat.ocr_channel import OcrChannel


StateCallback = Callable[[bool], None]


class WeChatRuntime:
    """Own one current-chat OCR channel and expose listener state."""

    def __init__(
        self,
        channel: OcrChannel,
        *,
        on_state_changed: Optional[StateCallback] = None,
    ) -> None:
        self.channel = channel
        self._on_state_changed = on_state_changed

    @property
    def listener_running(self) -> bool:
        return self.channel.running

    @property
    def send_queue_size(self) -> int:
        return self.channel.send_queue.size

    @property
    def active_chat(self) -> str:
        return self.channel.chat_title

    @property
    def active_chat_type(self) -> Optional[str]:
        return self.channel.chat_type

    def start_listener(self) -> None:
        self.channel.start()
        self._emit_state(True)

    def stop_listener(self) -> None:
        self.channel.stop()
        self._emit_state(False)

    def shutdown(self) -> None:
        if self.channel.running:
            self.channel.stop()

    def _emit_state(self, running: bool) -> None:
        if self._on_state_changed:
            try:
                self._on_state_changed(running)
            except Exception:
                pass
