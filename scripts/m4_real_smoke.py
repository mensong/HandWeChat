# -*- coding: utf-8 -*-
"""M4 真机冒烟：发送任务接口链路。"""

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
    service = ApiService(ApiConfig.load(Path("data/api_config.json")))

    attached = service.attach()
    print(f"attached: {attached}")
    health = service.health()
    print(f"health: {health}")
    assert health["version_baseline_matched"], (
        f"微信版本与基线不匹配：{health['wechat_version']!r}"
    )

    service.stop_listener()
    for name in list(service.runtime.list_listener_sessions()):
        service.remove_listener_session(name)
    service.add_listener_session(TARGET)
    service.start_listener()
    time.sleep(0.5)

    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    text = f"HandWeChat M4 real smoke {stamp}"
    task = service.send(TARGET, text)
    print(f"queued: task_id={task.task_id} text={text!r}")

    deadline = time.time() + 20
    while time.time() < deadline:
        current = service.get_send_task(task.task_id)
        if current.status in ("success", "failed"):
            break
        time.sleep(0.2)
    else:
        raise AssertionError("20 秒内发送任务未完成")

    print(
        "task: "
        f"status={current.status} completed_at={current.completed_at!r} "
        f"error_code={current.error_code!r} error_message={current.error_message!r}"
    )
    assert current.status == "success", current.error_message

    deadline = time.time() + 15
    buffered = []
    while time.time() < deadline:
        items, _, _ = service.get_messages(0, limit=100, timeout=0)
        buffered = [item for item in items if item.content == text]
        if buffered:
            break
        time.sleep(0.5)

    assert buffered, "15 秒内监听缓冲区未收到发送回读消息"
    message = buffered[-1]
    print(
        "buffered message: "
        f"cursor={message.cursor} session={message.session!r} "
        f"session_type={message.session_type!r} content={message.content!r}"
    )
    assert message.session == TARGET
    assert message.session_type == "friend"

    service.remove_listener_session(TARGET)
    service.shutdown()
    print("M4 real smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
