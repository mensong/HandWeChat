# -*- coding: utf-8 -*-
"""M3 冒烟：健康监测、版本基线和发送限速。"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from handwechat import WeChat
from handwechat.health import HealthMonitor
from handwechat.listener import MessageListener
from handwechat.models import Session, SessionType
from handwechat.rate_limit import TokenBucketRateLimiter


def main() -> int:
    wx = WeChat()
    if not wx.IsOnline():
        raise AssertionError("微信未附加")

    monitor = HealthMonitor(
        wx,
        interval=15,
        baseline_path="data/version_baseline.json",
        on_event=lambda kind, message: print(f"[{kind}] {message}"),
        on_health_changed=lambda ok, detail: print(f"[健康] {ok} {detail}"),
    )
    result, version_changed = monitor.CheckOnce()
    assert result.ok, "自检失败：" + "；".join(result.failed_texts())
    print("selfcheck:", [(item.name, item.ok) for item in result.items])
    print("version:", result.wechat_version, "changed:", version_changed)

    limiter = TokenBucketRateLimiter(max_per_minute=20)
    started = time.monotonic()
    for _ in range(3):
        limiter.wait_one()
    elapsed = time.monotonic() - started
    assert elapsed >= 1.0, f"限速抖动未生效，实际耗时 {elapsed:.2f}s"
    print(f"rate-limit smoke elapsed: {elapsed:.2f}s")

    with tempfile.TemporaryDirectory() as temp_dir:
        state_path = Path(temp_dir) / "listener_state.json"
        fake_wx = FakeWeChat()
        listener = MessageListener(fake_wx, state_path=state_path)
        for index in range(100):
            listener.AddListenChat(f"stress-{index:03d}")
        assert len(listener.GetAllListenChat()) == 100, "100 会话恢复失败"
        result_map = listener.PollOnce()
        assert len(result_map) == 100, f"100 会话轮询失败，实际 {len(result_map)}"
        print("100-session logic stress: OK")
    return 0


class FakeWeChat:
    """100 会话纯逻辑压测用的最小 wx 适配对象。"""

    def IsOnline(self) -> bool:
        return True

    def ChatWith(self, name: str, timeout: float = 4.0):
        return Session(
            name=name,
            automation_id=f"session_item_{name}",
            type=SessionType.GROUP if index_even(name) else SessionType.FRIEND,
        )

    def GetMessagesFrom(self, name: str, my_names=None, timeout: float = 4.0):
        return []


def index_even(name: str) -> bool:
    return int(name.rsplit("-", 1)[-1]) % 2 == 0


if __name__ == "__main__":
    raise SystemExit(main())
