# -*- coding: utf-8 -*-
"""临时置顶状态：同一时刻只允许一个应用置顶。"""

from __future__ import annotations

import time

from handwechat.gateway.models import PinState


class PinManager:
    def __init__(self) -> None:
        self._pin: PinState | None = None

    @property
    def pin(self) -> PinState | None:
        return self._pin

    def set(
        self,
        app_id: str,
        state: str | None,
        hold_seconds: float,
        *,
        max_hold_seconds: float,
        now: float | None = None,
    ) -> PinState | None:
        """置顶或续期；hold_seconds 为 0 时取消该应用自己的置顶。"""

        hold = max(0.0, min(float(hold_seconds), float(max_hold_seconds)))
        if hold <= 0:
            return self.clear(app_id)
        now = time.monotonic() if now is None else now
        self._pin = PinState(
            app_id=app_id,
            state=state,
            hold_seconds=hold,
            expires_mono=now + hold,
        )
        return self._pin

    def clear(self, app_id: str | None = None) -> PinState | None:
        """清除置顶；指定 app_id 时只清除它自己的置顶。"""

        pin = self._pin
        if pin is None:
            return None
        if app_id is not None and pin.app_id != app_id:
            return None
        self._pin = None
        return pin

    def expire_if_due(self, now: float | None = None) -> PinState | None:
        """到点则清除并返回被清除的置顶，供调用方发送 pin_expired。"""

        pin = self._pin
        if pin is None:
            return None
        now = time.monotonic() if now is None else now
        if now < pin.expires_mono:
            return None
        self._pin = None
        return pin
