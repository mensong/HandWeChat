# -*- coding: utf-8 -*-
"""HandWeChat current-chat GUI."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from handwechat.gui.calibration_dialog import (
    OcrCalibrationDialog,
    capture_wechat_screenshot,
)
from handwechat.gui.config import GuiConfig
from handwechat.gui.workers import FunctionWorker, WeChatController
from handwechat.models import BaseMessage
from handwechat.ocr_calibration import (
    OcrCalibration,
    load_ocr_calibration,
    save_ocr_calibration,
)
from handwechat.paths import app_data_path


def _now_text() -> str:
    return datetime.now().strftime("%H:%M:%S")


class MainWindow(QMainWindow):
    CONFIG_PATH = app_data_path("gui_config.json")
    OCR_CALIBRATION_PATH = app_data_path("ocr_calibration.json")

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("HandWeChat")
        self.resize(1080, 720)

        self.config = GuiConfig.load(self.CONFIG_PATH)
        self.controller: Optional[WeChatController] = None
        self.messages: list[BaseMessage] = []
        self._workers: list[FunctionWorker] = []
        self._active_chat: Optional[str] = None
        self._listener_running = False
        self._ocr_calibrating = False

        self._build_ui()
        self._log("界面已启动，正在确认微信窗口")
        self.attach()

    # -- UI 构建 -----------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget(self)
        root = QVBoxLayout(central)

        top_bar = QHBoxLayout()
        self.status_label = QLabel("微信：确认中")
        self.active_label = QLabel("当前聊天：待识别")
        self.start_button = QPushButton("开始监听")
        self.stop_button = QPushButton("暂停监听")
        self.attach_button = QPushButton("重新附加")
        self.start_button.clicked.connect(self._start_listener)
        self.stop_button.clicked.connect(self._stop_listener)
        self.attach_button.clicked.connect(self.attach)
        for widget in (
            self.status_label,
            self.active_label,
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
        self.tabs.addTab(self._build_listener_page(), "监听")
        self.tabs.addTab(self._build_log_page(), "运行日志")
        self.setCentralWidget(central)

    def _build_chat_page(self) -> QWidget:
        page = QWidget(self.tabs)
        layout = QVBoxLayout(page)

        self.message_view = QTextBrowser(page)
        self.message_view.setOpenExternalLinks(False)
        layout.addWidget(self.message_view, 1)

        bottom = QHBoxLayout()
        self.input_edit = QLineEdit(page)
        self.input_edit.setPlaceholderText("输入要发送到当前聊天的纯文本消息")
        self.input_edit.returnPressed.connect(self._send_current)
        self.send_button = QPushButton("发送")
        self.send_button.clicked.connect(self._send_current)
        self.queue_label = QLabel("发送队列：0")
        for widget in (
            self.input_edit,
            self.send_button,
            self.queue_label,
        ):
            bottom.addWidget(widget)
        bottom.setStretch(1, 1)
        layout.addLayout(bottom)
        return page

    def _build_listener_page(self) -> QWidget:
        page = QWidget(self.tabs)
        layout = QVBoxLayout(page)

        self.listener_state_label = QLabel("监听状态：未启动")
        self.listener_chat_label = QLabel("当前聊天：待识别")
        self.listener_type_label = QLabel("聊天类型：未知")
        self.listener_queue_label = QLabel("队列：发送 0，解析 0，截屏 0")
        self.listener_error_label = QLabel("最近错误：无")
        self.listener_error_label.setWordWrap(True)
        for label in (
            self.listener_state_label,
            self.listener_chat_label,
            self.listener_type_label,
            self.listener_queue_label,
            self.listener_error_label,
        ):
            layout.addWidget(label)

        calibration_row = QHBoxLayout()
        self.calibration_status_label = QLabel()
        self.calibrate_button = QPushButton("OCR 区域标定")
        self.calibrate_button.clicked.connect(self._start_ocr_calibration)
        calibration_row.addWidget(self.calibration_status_label, 1)
        calibration_row.addWidget(self.calibrate_button)

        history_row = QHBoxLayout()
        history_row.addWidget(QLabel("历史 OCR 消息数"))
        self.history_spin = QSpinBox(page)
        self.history_spin.setRange(1, 1000)
        self.history_spin.setValue(int(self.config.history_size))
        self.history_spin.valueChanged.connect(self._on_history_size_changed)
        history_row.addWidget(self.history_spin)
        self.history_count_label = QLabel("历史消息：0 / 10")
        history_row.addWidget(self.history_count_label)
        history_row.addStretch(1)

        layout.addLayout(history_row)
        layout.addLayout(calibration_row)
        layout.addStretch(1)
        self._refresh_ocr_calibration_status()
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
        self.status_label.setText("微信：确认中")
        self._run_async(
            self._attach_and_prepare,
            self._on_attached,
            self._on_attach_failed,
        )

    def _attach_and_prepare(self) -> WeChatController:
        from handwechat.ocr_channel import _find_wechat_window

        if _find_wechat_window() is None:
            raise RuntimeError("微信主窗口不可用，请确认微信已启动并登录")
        return WeChatController(
            poll_interval=self.config.poll_interval,
            max_send_per_minute=self.config.max_send_per_minute,
            history_size=self.config.history_size,
        )

    def _on_attached(self, controller: WeChatController) -> None:
        if self.controller:
            self.controller.shutdown()
        controller.bridge.message_received.connect(self._on_message_received)
        controller.bridge.chat_changed.connect(self._on_chat_changed)
        controller.bridge.listener_error.connect(self._on_listener_error)
        controller.bridge.state_changed.connect(self._on_listener_state_changed)
        controller.bridge.send_started.connect(self._on_send_started)
        controller.bridge.send_completed.connect(self._on_send_completed)
        self.controller = controller
        self.status_label.setText("微信：在线")
        self._log("微信窗口确认成功，OCR 通道已就绪")
        self._active_chat = None
        self.active_label.setText("当前聊天：待识别")
        self.messages.clear()
        self._render_messages()
        self._refresh_listener_status()

    def _on_attach_failed(self, detail: str) -> None:
        self.status_label.setText("微信：离线")
        self._log(f"附加微信失败：{detail}")

    def _start_listener(self) -> None:
        if not self.controller:
            self._log("微信未附加，无法开始监听")
            return
        try:
            self.controller.start_listener()
        except Exception as exc:
            self._log(f"启动监听失败：{type(exc).__name__}: {exc}")

    def _stop_listener(self) -> None:
        if not self.controller:
            return
        try:
            self.controller.stop_listener()
        except Exception as exc:
            self._log(f"暂停监听失败：{type(exc).__name__}: {exc}")

    def _on_listener_state_changed(self, running: bool) -> None:
        self._listener_running = running
        self._refresh_listener_status()
        self._log(f"监听状态：{'运行中' if running else '已暂停'}")

    def _on_listener_error(self, context: str, exc: Exception) -> None:
        self._log(f"监听错误 {context}：{type(exc).__name__}: {exc}")
        self._refresh_listener_status()

    def _refresh_listener_status(self) -> None:
        if not self.controller:
            return
        status = self.controller.status()
        chat = status.get("chat_title") or "待识别"
        chat_type = status.get("chat_type") or "未知"
        state = "运行中" if status.get("running") else "未启动"

        self._active_chat = status.get("chat_title") or None
        self.active_label.setText(f"当前聊天：{chat}")
        self.listener_state_label.setText(f"监听状态：{state}")
        self.listener_chat_label.setText(f"当前聊天：{chat}")
        self.listener_type_label.setText(f"聊天类型：{chat_type}")
        self.listener_queue_label.setText(
            "队列：发送 {send}，解析 {parse}，截屏 {capture}".format(
                send=status.get("send_queue_size", 0),
                parse=status.get("parse_queue_size", 0),
                capture=status.get("capture_queue_size", 0),
            )
        )
        last_error = status.get("last_error") or "无"
        self.listener_error_label.setText(f"最近错误：{last_error}")
        self.history_count_label.setText(
            "历史消息：{} / {}".format(
                status.get("history_size", 0),
                status.get("history_capacity", 0),
            )
        )

    def _on_history_size_changed(self, value: int) -> None:
        self.config.history_size = max(1, int(value))
        try:
            self.config.save(self.CONFIG_PATH)
        except OSError as exc:
            self._log(f"历史消息配置保存失败：{exc}")
        if self.controller:
            self.controller.channel.history_size = self.config.history_size
        self._log(f"历史 OCR 消息数已设为 {self.config.history_size}")

    # -- 消息 ---------------------------------------------------------------
    def _on_chat_changed(self, chat_title: str) -> None:
        self._active_chat = chat_title
        self.messages.clear()
        self._log(f"已跟随当前微信聊天：{chat_title}")
        self._render_messages()

    def _on_message_received(self, message: BaseMessage) -> None:
        if self.messages:
            self.messages[-1].is_latest = False
        self.messages.append(message)
        self._refresh_listener_status()
        self._render_messages()

    def _render_messages(self, *_args) -> None:
        if not self.controller:
            self.message_view.setHtml(
                "<div style='color:#666'>等待附加微信</div>"
            )
            return

        messages = self.messages

        chunks = []
        for item in messages:
            color = "#777777"
            prefix = ""
            if item.attr == "self":
                color = "#1f6fb2"
                prefix = "我"
            elif item.attr == "friend":
                color = "#257a46"
                prefix = item.sender or "对方"
            elif item.attr == "system":
                prefix = "系统"
            else:
                prefix = "时间"
            content = (
                item.content.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            if item.type == "other":
                content = "[暂不支持的消息类型]"
            if getattr(item, "is_latest", False):
                badge = (
                    "<span style='color:#176b2e;background:#e4f3e8;"
                    "padding:1px 5px;border-radius:4px;font-size:11px;'>最新</span>"
                )
            else:
                confidence = float(getattr(item, "confidence", 1.0))
                badge = (
                    "<span style='color:#657180;background:#eef1f4;"
                    "padding:1px 5px;border-radius:4px;font-size:11px;'>"
                    f"置信度 {confidence:.1f}</span>"
                )
            chunks.append(
                f"<div style='margin:4px 0;'>"
                f"<span style='color:{color};font-weight:bold;'>{prefix}</span>　"
                f"<span style='color:#999;font-size:11px;'>"
                f"{item.extra.get('listen_time', '')}</span> {badge}<br>"
                f"<span style='white-space:pre-wrap;'>{content}</span>"
                f"</div>"
            )
        if not chunks:
            chunks.append("<div style='color:#777'>暂无消息</div>")
        self.message_view.setHtml("".join(chunks))

    def _send_current(self) -> None:
        if not self.controller:
            return
        if not self.controller.channel.running:
            self._log("OCR 监听未启动，不能发送")
            return
        if not self.controller.active_chat:
            self._log("尚未识别到当前微信聊天，不能发送")
            return
        text = self.input_edit.text().strip()
        if not text:
            return
        task_id = self.controller.send_text(text)
        self.input_edit.clear()
        self._log(f"已入发送队列：{task_id} {text!r}")

    def _on_send_started(self, task_id: str, text: str) -> None:
        self._log(f"开始发送：{task_id} {text!r}")
        self._refresh_listener_status()

    def _on_send_completed(
        self, task_id: str, text: str, ok: bool, detail: str
    ) -> None:
        if ok:
            self._log(f"发送成功：{text!r}")
        else:
            self._log(f"发送失败：{text!r} {detail}")
        self._refresh_listener_status()

    def _on_queue_changed(self, count: int) -> None:
        self.queue_label.setText(f"发送队列：{count}")
        self._refresh_listener_status()

    # -- OCR 标定 -----------------------------------------------------------
    def _refresh_ocr_calibration_status(self) -> None:
        try:
            calibration = load_ocr_calibration(self.OCR_CALIBRATION_PATH)
        except (OSError, ValueError) as exc:
            self.calibration_status_label.setText(
                f"OCR 标定：读取失败，运行时将使用默认区域（{exc}）"
            )
            return
        if self.OCR_CALIBRATION_PATH.exists():
            updated_at = calibration.updated_at or "未知时间"
            self.calibration_status_label.setText(
                f"OCR 标定：已保存（{updated_at}）"
            )
        else:
            self.calibration_status_label.setText("OCR 标定：未标定，使用内置默认区域")

    def _start_ocr_calibration(self) -> None:
        if self._ocr_calibrating:
            return
        self._ocr_calibrating = True
        self.calibrate_button.setEnabled(False)
        self._log("正在截取微信主窗口用于 OCR 区域标定")
        self._run_async(
            capture_wechat_screenshot,
            self._on_ocr_calibration_captured,
            self._on_ocr_calibration_failed,
        )

    def _on_ocr_calibration_captured(self, image) -> None:
        try:
            try:
                calibration = load_ocr_calibration(self.OCR_CALIBRATION_PATH)
            except (OSError, ValueError):
                calibration = OcrCalibration()
            dialog = OcrCalibrationDialog(image, calibration, parent=self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                save_ocr_calibration(
                    dialog.calibration(),
                    self.OCR_CALIBRATION_PATH,
                )
                self._log(f"OCR 标定已保存：{self.OCR_CALIBRATION_PATH}")
        except Exception as exc:
            self._log(f"OCR 标定保存失败：{type(exc).__name__}: {exc}")
        finally:
            self._finish_ocr_calibration()

    def _on_ocr_calibration_failed(self, detail: str) -> None:
        self._finish_ocr_calibration()
        self._log(f"OCR 标定失败：{detail}")

    def _finish_ocr_calibration(self) -> None:
        self._ocr_calibrating = False
        self.calibrate_button.setEnabled(True)
        self._refresh_ocr_calibration_status()

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
