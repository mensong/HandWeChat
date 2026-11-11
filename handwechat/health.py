# -*- coding: utf-8 -*-
"""微信健康监测与版本基线。"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import uiautomation as uia

from handwechat.selfcheck import SelfCheckResult, run_self_check


HealthEventCallback = Callable[[str, str], None]
HealthStateCallback = Callable[[bool, str], None]


def _now_text() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class HealthBaseline:
    version: str = ""
    pid: int = 0
    hwnd: str = ""
    updated_at: str = ""

    @classmethod
    def load(cls, path: str | Path) -> "HealthBaseline":
        path = Path(path)
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            version=str(data.get("version") or ""),
            pid=int(data.get("pid") or 0),
            hwnd=str(data.get("hwnd") or ""),
            updated_at=str(data.get("updated_at") or ""),
        )


class HealthMonitor:
    """周期性检查 UIA 树与版本基线，必要时尝试恢复控件树。"""

    def __init__(
        self,
        wx,
        *,
        interval: float = 15.0,
        baseline_path: str | Path = "data/version_baseline.json",
        on_event: Optional[HealthEventCallback] = None,
        on_health_changed: Optional[HealthStateCallback] = None,
    ) -> None:
        self._wx = wx
        self._interval = max(5.0, float(interval))
        self._baseline_path = Path(baseline_path)
        self._on_event = on_event
        self._on_health_changed = on_health_changed
        self._baseline = HealthBaseline.load(self._baseline_path)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def baseline(self) -> HealthBaseline:
        return self._baseline

    def Start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="handwechat-health",
            daemon=True,
        )
        self._thread.start()

    def Stop(self, timeout: float = 3.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout)

    def CheckOnce(self) -> tuple[SelfCheckResult, bool]:
        """返回自检结果和版本是否变化。"""

        if not self._wx.IsOnline():
            self._emit("恢复", "微信控件树离线，尝试恢复")
            recovered = self._wx.Recover()
            if not recovered:
                result = SelfCheckResult()
                result.add("主窗口", False, "控件树恢复失败")
                self._emit("自检失败", "主窗口不可用")
                self._set_health(False, "主窗口不可用")
                return result, False
            self._emit("恢复", "微信控件树已恢复")

        result = run_self_check(self._wx)
        version_changed = bool(
            self._baseline.version and result.wechat_version != self._baseline.version
        )
        if result.ok:
            if version_changed:
                self._emit(
                    "版本变化",
                    f"{self._baseline.version or '未知'} -> {result.wechat_version}",
                )
            self._save_baseline(result)
            self._emit(
                "自检通过",
                f"微信 {result.wechat_version or '未知版本'}，共 {len(result.items)} 项",
            )
        else:
            self._emit("自检失败", "；".join(result.failed_texts()))
        self._set_health(result.ok, "；".join(result.failed_texts()))
        return result, version_changed

    def _run(self) -> None:
        with uia.UIAutomationInitializerInThread():
            while not self._stop_event.is_set():
                try:
                    self.CheckOnce()
                except Exception as exc:
                    self._emit("健康监测", f"{type(exc).__name__}: {exc}")
                    self._set_health(False, str(exc))
                self._stop_event.wait(self._interval)

    def _save_baseline(self, result: SelfCheckResult) -> None:
        payload = {
            "version": result.wechat_version,
            "pid": self._wx.pid,
            "hwnd": f"{self._wx.hwnd:#x}",
            "updated_at": _now_text(),
        }
        self._baseline_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._baseline_path.with_suffix(self._baseline_path.suffix + ".tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(self._baseline_path)
        self._baseline = HealthBaseline.load(self._baseline_path)

    def _emit(self, kind: str, message: str) -> None:
        if self._on_event:
            self._on_event(kind, message)

    def _set_health(self, ok: bool, detail: str) -> None:
        if self._on_health_changed:
            self._on_health_changed(ok, detail)
