# -*- coding: utf-8 -*-
"""M3 真机冒烟：附加已登录微信并验证监听、发送回读和消息缓冲。"""

from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from handwechat.api.config import ApiConfig
from handwechat.api.service import ApiService


TARGET = "文件传输助手"


def main() -> int:
    config = ApiConfig.load(Path("data/api_config.json"))
    service = ApiService(config)

    send_result: dict | None = None

    health = service.health()
    print(
        f"pre-attach health: status={health['status']} "
        f"pid={health['wechat_pid']} version={health['wechat_version']!r}"
    )

    attached = service.attach()
    print(f"attached: {attached}")

    runtime = service.runtime
    assert runtime is not None, "微信附加失败"

    health = service.health()
    print(f"post-attach health: {health}")
    assert health["version_baseline_matched"], (
        f"微信版本与基线不匹配：{health['wechat_version']!r}"
    )

    service.stop_listener()
    for name in list(runtime.list_listener_sessions()):
        service.remove_listener_session(name)

    service.add_listener_session(TARGET)
    sessions = service.get_listener_sessions()
    print(f"listener sessions: {sessions}")
    assert sessions == [{"name": TARGET, "type": "friend"}], sessions

    def on_send_completed(session: str, text: str, ok: bool, detail: str) -> None:
        nonlocal send_result
        if session == TARGET and text == test_text:
            send_result = {"ok": ok, "detail": detail}

    runtime.send_queue._on_completed = on_send_completed

    service.start_listener()
    time.sleep(0.5)

    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    test_text = f"HandWeChat M3 real smoke {stamp}"
    runtime.send_queue.submit(TARGET, test_text)
    print(f"submitted: {test_text}")

    deadline = time.time() + 20
    while time.time() < deadline and send_result is None:
        time.sleep(0.2)

    assert send_result is not None, "20 秒内发送队列未回写完成状态"
    print(f"send result: {send_result}")
    assert send_result["ok"], f"发送失败：{send_result['detail']}"

    deadline = time.time() + 15
    messages = []
    while time.time() < deadline:
        items, _, _ = service.get_messages(0, limit=100, timeout=0)
        messages = [item for item in items if item.content == test_text]
        if messages:
            break
        time.sleep(0.5)

    assert messages, "15 秒内监听缓冲区未收到发送回读消息"
    item = messages[-1]
    print(
        "buffered message: "
        f"cursor={item.cursor} session={item.session!r} "
        f"session_type={item.session_type!r} sender={item.sender!r} "
        f"content={item.content!r}"
    )
    assert item.session == TARGET
    assert item.session_type == "friend"

    service.remove_listener_session(TARGET)
    service.shutdown()
    print("M3 real smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
