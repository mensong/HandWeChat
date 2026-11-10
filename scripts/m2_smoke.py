# -*- coding: utf-8 -*-
"""M2 GUI 冒烟：离屏界面 + 真机附加/监听/发送。"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from handwechat.gui.config import GuiConfig, SessionConfig
from handwechat.gui.main_window import MainWindow
from handwechat.models import TextMessage


def process_for(seconds: float) -> None:
    app = QApplication.instance()
    assert app is not None
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.05)


def main() -> int:
    config_path = Path("data/gui_config.json")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config = GuiConfig.load(config_path)
    config.sessions["文件传输助手"] = SessionConfig(name="文件传输助手", alias="传输助手")
    config.save(config_path)

    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow()
    process_for(5)
    assert window.controller is not None, "微信未附加成功"
    process_for(30)
    print("GUI log:", window.log_view.toPlainText())

    session = "文件传输助手"
    window._selected_session = session
    window.unread[session] = 0
    test_text = f"HandWeChat M2 GUI smoke {datetime.now().strftime('%Y%m%d%H%M%S')}"
    fake = TextMessage(
        session_name=session,
        sender="self",
        content=test_text,
        attr="self",
        type="text",
        index=0,
    )
    window._on_message_received(session, fake)
    window._render_messages()
    assert window.message_view.toPlainText().find(test_text) >= 0, "消息流未渲染测试文本"

    window.input_edit.setText(test_text)
    window._send_current()
    deadline = time.time() + 20
    while time.time() < deadline:
        process_for(0.5)
        if any(item.content == test_text for item in window.messages.get(session, [])):
            break
    else:
        raise AssertionError("20 秒内未收到发送回读消息")

    print("M2 GUI smoke OK:", test_text)
    print("listener sessions:", window.controller.listener.GetAllListenChat())
    window.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
