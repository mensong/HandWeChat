# -*- coding: utf-8 -*-
"""HandWeChat 三栏主界面。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from handwechat.gui.config import GuiConfig, SessionConfig
from handwechat.gui.workers import FunctionWorker, WeChatController
from handwechat.models import BaseMessage, Session


def _now_text() -> str:
    return datetime.now().strftime("%H:%M:%S")


class MainWindow(QMainWindow):
    CONFIG_PATH = "data/gui_config.json"

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("HandWeChat")
        self.resize(1080, 720)

        self.config = GuiConfig.load(self.CONFIG_PATH)
        self.controller: Optional[WeChatController] = None
        self.messages: dict[str, list[BaseMessage]] = {}
        self.unread: dict[str, int] = {}
        self.has_at_me: dict[str, bool] = {}
        self.session_types: dict[str, Optional[str]] = {}
        self._workers: list[FunctionWorker] = []
        self._selected_session: Optional[str] = None
        self._health_ok = False
        self._listener_running = False
        self._was_running_before_health_failure = False

        self._build_ui()
        self._log("界面已启动，正在附加微信")
        self.attach()

    # -- UI 构建 -----------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget(self)
        root = QVBoxLayout(central)

        top_bar = QHBoxLayout()
        self.status_label = QLabel("微信：附加中")
        self.version_label = QLabel("版本：未知")
        self.health_label = QLabel("自检：未运行")
        self.selfcheck_button = QPushButton("自检")
        self.start_button = QPushButton("开始监听")
        self.stop_button = QPushButton("暂停监听")
        self.attach_button = QPushButton("重新附加")
        self.selfcheck_button.clicked.connect(self._run_selfcheck)
        self.start_button.clicked.connect(self._start_listener)
        self.stop_button.clicked.connect(self._stop_listener)
        self.attach_button.clicked.connect(self.attach)
        for widget in (
            self.status_label,
            self.version_label,
            self.health_label,
            self.selfcheck_button,
            self.start_button,
            self.stop_button,
            self.attach_button,
        ):
            top_bar.addWidget(widget)
        top_bar.addStretch(1)
        root.addLayout(top_bar)

        self.tabs = QTabWidget(central)
        root.addWidget(self.tabs, 1)

        self.tabs.addTab(self._build_chat_page(), "聊天")
        self.tabs.addTab(self._build_config_page(), "监听配置")
        self.tabs.addTab(self._build_log_page(), "运行日志")
        self.setCentralWidget(central)

    def _build_chat_page(self) -> QWidget:
        page = QWidget(self.tabs)
        layout = QVBoxLayout(page)

        splitter = QSplitter(Qt.Horizontal, page)
        self.session_list = QListWidget(splitter)
        self.session_list.currentItemChanged.connect(self._on_session_selected)
        self.message_view = QTextBrowser(splitter)
        self.message_view.setOpenExternalLinks(False)
        splitter.addWidget(self.session_list)
        splitter.addWidget(self.message_view)
        splitter.setSizes([260, 760])
        layout.addWidget(splitter, 1)

        bottom = QHBoxLayout()
        self.at_me_filter = QCheckBox("只看@我")
        self.at_me_filter.stateChanged.connect(self._render_messages)
        self.input_edit = QLineEdit(page)
        self.input_edit.setPlaceholderText("输入要发送的纯文本消息")
        self.input_edit.returnPressed.connect(self._send_current)
        self.send_button = QPushButton("发送")
        self.send_button.clicked.connect(self._send_current)
        self.queue_label = QLabel("发送队列：0")
        for widget in (self.at_me_filter, self.input_edit, self.send_button, self.queue_label):
            bottom.addWidget(widget)
        bottom.setStretch(1, 1)
        layout.addLayout(bottom)
        return page

    def _build_config_page(self) -> QWidget:
        page = QWidget(self.tabs)
        layout = QVBoxLayout(page)

        add_row = QHBoxLayout()
        self.session_combo = QComboBox(page)
        self.session_combo.setMinimumWidth(280)
        self.refresh_sessions_button = QPushButton("刷新会话")
        self.add_session_button = QPushButton("添加监听")
        self.refresh_sessions_button.clicked.connect(self.refresh_sessions)
        self.add_session_button.clicked.connect(self._add_session_from_combo)
        add_row.addWidget(self.session_combo, 1)
        add_row.addWidget(self.refresh_sessions_button)
        add_row.addWidget(self.add_session_button)
        layout.addLayout(add_row)

        self.session_table = QTableWidget(0, 3, page)
        self.session_table.setHorizontalHeaderLabels(["会话名", "别名", "操作"])
        self.session_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.session_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.session_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.session_table.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.SelectedClicked
        )
        layout.addWidget(self.session_table, 1)

        names_row = QHBoxLayout()
        self.my_names_edit = QLineEdit(page)
        self.my_names_edit.setText(",".join(self.config.my_names))
        self.my_names_edit.setPlaceholderText("群消息 @我 使用的昵称，用英文逗号分隔")
        self.rate_limit_spin = QSpinBox(page)
        self.rate_limit_spin.setRange(1, 60)
        self.rate_limit_spin.setValue(self.config.max_send_per_minute)
        self.rate_limit_spin.setSuffix(" 条/分钟")
        self.save_config_button = QPushButton("保存配置")
        self.save_config_button.clicked.connect(self._save_config)
        names_row.addWidget(self.my_names_edit, 1)
        names_row.addWidget(self.rate_limit_spin)
        names_row.addWidget(self.save_config_button)
        layout.addLayout(names_row)
        return page

    def _build_log_page(self) -> QWidget:
        page = QWidget(self.tabs)
        layout = QVBoxLayout(page)
        self.log_view = QPlainTextEdit(page)
        self.log_view.setReadOnly(True)
        layout.addWidget(self.log_view)
        return page

    # -- 附加与监听 ---------------------------------------------------------
    def attach(self) -> None:
        self.status_label.setText("微信：附加中")
        self._run_async(self._attach_and_prepare, self._on_attached, self._on_attach_failed)

    def _attach_and_prepare(self):
        from handwechat import WeChat

        wx = WeChat()
        if not wx.IsOnline():
            raise RuntimeError("微信主窗口不可用，请确认微信已登录")
        return wx

    def _on_attached(self, wx) -> None:
        if self.controller:
            self.controller.shutdown()
        controller = WeChatController(
            wx,
            poll_interval=self.config.poll_interval,
            my_names=self.config.my_names,
        )
        controller.bridge.message_received.connect(self._on_message_received)
        controller.bridge.listener_error.connect(self._on_listener_error)
        controller.bridge.state_changed.connect(self._on_listener_state_changed)
        controller.bridge.health_event.connect(self._on_health_event)
        controller.bridge.health_changed.connect(self._on_health_changed)
        controller.send_worker.queue_changed.connect(self._on_queue_changed)
        controller.send_worker.send_completed.connect(self._on_send_completed)
        controller.start_send_worker()
        self.controller = controller
        self.status_label.setText(
            f"微信：在线  PID={wx.pid}  HWND={wx.hwnd:#x}"
        )
        self._log(f"微信附加成功 PID={wx.pid} HWND={wx.hwnd:#x}")
        self.refresh_sessions()
        self._load_configured_sessions(start_listener=False)
        self._run_selfcheck()

    def _on_attach_failed(self, detail: str) -> None:
        self.status_label.setText("微信：离线")
        self._log(f"附加微信失败：{detail}")

    def _load_configured_sessions(self, *, start_listener: bool) -> None:
        if not self.controller:
            return

        def add_all():
            added: list[str] = []
            failed: list[str] = []
            for name, item in self.config.sessions.items():
                try:
                    session = self.controller.listener.AddListenChat(name)
                    self.session_types[name] = session.type
                    added.append(name)
                except Exception as exc:
                    failed.append(f"{name} ({type(exc).__name__}: {exc})")
            return added, failed

        def done(result):
            added, failed = result
            if failed:
                self._log("以下监听会话添加失败：" + "、".join(failed))
            if start_listener:
                self._start_listener()
            self._refresh_session_list()

        self._run_async(add_all, done, lambda detail: self._log(f"恢复监听配置失败：{detail}"))

    def _start_listener(self) -> None:
        if not self.controller:
            self._log("微信未附加，无法开始监听")
            return
        self.controller.start_listener()

    def _run_selfcheck(self) -> None:
        if not self.controller:
            self._log("微信未附加，无法自检")
            return
        self.health_label.setText("自检：运行中")
        self._run_async(
            self.controller.health_monitor.CheckOnce,
            self._on_selfcheck_finished,
            lambda detail: self._on_health_event("自检异常", detail),
        )

    def _on_selfcheck_finished(self, result: tuple) -> None:
        selfcheck_result, _version_changed = result
        self.version_label.setText(
            f"版本：{selfcheck_result.wechat_version or '未知'}"
        )
        if selfcheck_result.ok and not self._listener_running:
            self._start_listener()

    def _stop_listener(self) -> None:
        if not self.controller:
            return
        self._listener_running = False
        self._was_running_before_health_failure = False
        self.controller.stop_listener()

    def _on_listener_state_changed(self, running: bool) -> None:
        state = "运行中" if running else "已暂停"
        self._listener_running = running
        self.status_label.setToolTip(state)
        self._log(f"监听状态：{state}")

    def _on_health_event(self, kind: str, message: str) -> None:
        self._log(f"[{kind}] {message}")

    def _on_health_changed(self, ok: bool, detail: str) -> None:
        self._health_ok = ok
        if ok:
            self.health_label.setText("自检：通过")
            if self._was_running_before_health_failure:
                self._was_running_before_health_failure = False
                self._start_listener()
        else:
            self.health_label.setText("自检：失败")
            if self._listener_running:
                self._was_running_before_health_failure = True
                self.controller.stop_listener()

    def _on_listener_error(self, context: str, exc: Exception) -> None:
        self._log(f"监听错误 {context}：{type(exc).__name__}: {exc}")

    # -- 会话 ---------------------------------------------------------------
    def refresh_sessions(self) -> None:
        if not self.controller:
            return
        self._run_async(
            lambda: self.controller.wx.GetSession(100),
            self._on_sessions_loaded,
            lambda detail: self._log(f"刷新会话失败：{detail}"),
        )

    def _on_sessions_loaded(self, sessions: list[Session]) -> None:
        self.session_combo.clear()
        self.session_types.clear()
        for session in sessions:
            self.session_combo.addItem(session.name)
            self.session_types[session.name] = session.type
        self._refresh_session_list()

    def _add_session_from_combo(self) -> None:
        if not self.controller:
            self._log("微信未附加，无法添加监听")
            return
        name = self.session_combo.currentText().strip()
        if not name:
            return

        def add():
            session = self.controller.listener.AddListenChat(name)
            self.config.sessions[name] = SessionConfig(name=name)
            self.config.save(self.CONFIG_PATH)
            return session

        def done(session: Session) -> None:
            self.session_types[name] = session.type
            self._refresh_session_list()
            self._log(f"已添加监听会话：{name}")

        self._run_async(add, done, lambda detail: self._log(f"添加监听失败：{detail}"))

    def _remove_session(self, name: str) -> None:
        if not self.controller:
            return

        def remove():
            removed = self.controller.listener.RemoveListenChat(name)
            self.config.sessions.pop(name, None)
            self.config.save(self.CONFIG_PATH)
            return removed

        def done(_: bool) -> None:
            self._refresh_session_list()
            self._log(f"已移除监听会话：{name}")

        self._run_async(remove, done, lambda detail: self._log(f"移除监听失败：{detail}"))

    def _save_config(self) -> None:
        names = [item.strip() for item in self.my_names_edit.text().split(",")]
        self.config.my_names = [item for item in names if item]
        self.config.max_send_per_minute = self.rate_limit_spin.value()
        if self.controller:
            self.controller.listener.SetMyNames(self.config.my_names)
            self.controller.send_worker.set_rate_limit(self.config.max_send_per_minute)
        for row in range(self.session_table.rowCount()):
            name = self.session_table.item(row, 0).text()
            if name in self.config.sessions:
                self.config.sessions[name].alias = self.session_table.item(row, 1).text().strip()
        self.config.save(self.CONFIG_PATH)
        self._refresh_session_list()
        self._log("监听配置已保存")

    def _refresh_session_list(self) -> None:
        if getattr(self, "_refreshing_sessions", False):
            return
        self._refreshing_sessions = True
        previous = self._selected_session
        self.session_table.setRowCount(0)
        for name, item in self.config.sessions.items():
            row = self.session_table.rowCount()
            self.session_table.insertRow(row)
            name_item = QTableWidgetItem(name)
            name_item.setData(Qt.UserRole, name)
            name_item.setFlags(name_item.flags() & ~Qt.ItemIsEditable)
            alias_item = QTableWidgetItem(item.alias)
            remove_button = QPushButton("移除")
            remove_button.clicked.connect(lambda _=False, n=name: self._remove_session(n))
            self.session_table.setItem(row, 0, name_item)
            self.session_table.setItem(row, 1, alias_item)
            self.session_table.setCellWidget(row, 2, remove_button)

        self.session_list.clear()
        for name in sorted(self.config.sessions):
            alias = self.config.sessions[name].alias or name
            unread = self.unread.get(name, 0)
            suffix = f"（未读 {unread}）" if unread else ""
            if self.has_at_me.get(name):
                suffix += "　[有@我]"
            list_item = QListWidgetItem(alias + suffix)
            list_item.setData(Qt.UserRole, name)
            if self.has_at_me.get(name):
                list_item.setBackground(Qt.red)
                list_item.setForeground(Qt.white)
            self.session_list.addItem(list_item)
            if name == previous:
                self.session_list.setCurrentItem(list_item)
        if previous is None and self.session_list.count():
            self.session_list.setCurrentRow(0)
        self._refreshing_sessions = False

    def _on_session_selected(self, current: Optional[QListWidgetItem], _previous=None) -> None:
        if current is None:
            self._selected_session = None
            return
        name = current.data(Qt.UserRole)
        self._selected_session = name
        self.unread[name] = 0
        self.has_at_me[name] = False
        self._render_messages()
        self._refresh_session_list()

    # -- 消息 ---------------------------------------------------------------
    def _on_message_received(self, session: str, message: BaseMessage) -> None:
        self.messages.setdefault(session, []).append(message)
        if message.is_at_me:
            self.has_at_me[session] = True
        if session != self._selected_session:
            self.unread[session] = self.unread.get(session, 0) + 1
        self._render_messages()
        self._refresh_session_list()

    def _render_messages(self, *_args) -> None:
        if not self._selected_session:
            self.message_view.setHtml("<div style='color:#666'>请选择左侧监听会话</div>")
            return
        messages = self.messages.get(self._selected_session, [])
        if self.at_me_filter.isChecked():
            messages = [item for item in messages if item.is_at_me]
        chunks = []
        for item in messages:
            alias = self.config.sessions.get(self._selected_session)
            title = alias.alias if alias and alias.alias else self._selected_session
            color = "#777777"
            prefix = ""
            if item.attr == "self":
                color = "#1f6fb2"
                prefix = "我"
            elif item.attr == "friend":
                color = "#257a46"
                prefix = item.sender or "好友"
            elif item.attr == "system":
                prefix = "系统"
            else:
                prefix = "时间"
            highlight = ""
            if item.is_at_me:
                highlight = "background:#fff2cc;"
            content = item.content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            if item.type == "other":
                content = "[暂不支持的消息类型]"
            chunks.append(
                f"<div style='margin:4px 0;{highlight}'>"
                f"<span style='color:{color};font-weight:bold;'>{prefix}</span>　"
                f"<span style='color:#999;font-size:11px;'>{item.extra.get('listen_time', '')}</span><br>"
                f"<span style='white-space:pre-wrap;'>{content}</span>"
                f"</div>"
            )
        if not chunks:
            chunks.append("<div style='color:#777'>暂无消息</div>")
        self.message_view.setHtml("".join(chunks))

    def _send_current(self) -> None:
        if not self.controller or not self._selected_session:
            return
        if not self._health_ok:
            self._log("自检未通过，已暂停发送")
            return
        text = self.input_edit.text().strip()
        if not text:
            return
        self.controller.send_worker.submit(self._selected_session, text)
        self.input_edit.clear()
        self._log(f"已入发送队列：{self._selected_session} {text!r}")

    def _on_send_completed(self, session: str, text: str, ok: bool, detail: str) -> None:
        if ok:
            self._log(f"发送成功：{session} {text!r}")
        else:
            self._log(f"发送失败：{session} {text!r} {detail}")

    def _on_queue_changed(self, count: int) -> None:
        self.queue_label.setText(f"发送队列：{count}")

    # -- 通用 ---------------------------------------------------------------
    def _run_async(self, func, done, failed) -> None:
        worker = FunctionWorker(func, self)
        worker.succeeded.connect(done)
        worker.failed.connect(failed)
        worker.finished.connect(lambda: self._cleanup_worker(worker))
        self._workers.append(worker)
        worker.start()

    def _cleanup_worker(self, worker: FunctionWorker) -> None:
        if worker in self._workers:
            self._workers.remove(worker)
        worker.deleteLater()

    def _log(self, text: str) -> None:
        self.log_view.appendPlainText(f"[{_now_text()}] {text}")

    def closeEvent(self, event) -> None:
        if self.controller:
            self.controller.shutdown()
        for worker in list(self._workers):
            worker.wait(2000)
        super().closeEvent(event)
