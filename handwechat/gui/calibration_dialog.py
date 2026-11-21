# -*- coding: utf-8 -*-
"""Interactive screenshot calibration for the OCR channel."""

from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass
from typing import Callable, Optional

from PIL import Image, ImageGrab
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from handwechat.ocr_calibration import OcrCalibration, OcrPalette


@dataclass(frozen=True)
class _CalibrationStep:
    key: str
    title: str
    mode: str
    hint: str
    color: str


_STEPS = (
    _CalibrationStep(
        "title_box",
        "标题区",
        "rect",
        "框选微信右侧聊天顶部的当前聊天标题。",
        "#1f6fb2",
    ),
    _CalibrationStep(
        "chat_box",
        "聊天区",
        "rect",
        "框选右侧完整聊天消息区域：保留群昵称列，避开头像，不要包含输入框。",
        "#257a46",
    ),
    _CalibrationStep(
        "page_color",
        "页面底色",
        "color",
        "点击聊天背景的纯色处（消息之间的页面底色），取该点颜色。",
        "#6b6b6b",
    ),
    _CalibrationStep(
        "friend_color",
        "对方气泡底色",
        "color",
        "点击对方气泡的纯色处，取该点颜色；避开文字、头像与圆角。",
        "#a36500",
    ),
    _CalibrationStep(
        "self_color",
        "自己气泡底色",
        "color",
        "点击自己气泡的纯色处，取该点颜色；没有自己的消息时可使用「默认色」。",
        "#0f7a7a",
    ),
    _CalibrationStep(
        "input_point",
        "发送输入区",
        "point",
        "点击聊天底部的文本输入框。",
        "#a4262c",
    ),
)


_DEFAULT_COLORS: dict[str, tuple[int, int, int]] = {
    "page_color": (250, 250, 250),
    "friend_color": (238, 238, 240),
    "self_color": (157, 242, 159),
}


@dataclass(frozen=True)
class _ColorPick:
    point: QPointF
    rgb: tuple[int, int, int]
    defaulted: bool = False


def _point_color(
    image: Image.Image, x: float, y: float
) -> Optional[tuple[int, int, int]]:
    """Exact pixel color at the clicked point."""

    px = min(max(0, int(round(x))), image.width - 1)
    py = min(max(0, int(round(y))), image.height - 1)
    color = image.getpixel((px, py))
    if not isinstance(color, tuple) or len(color) < 3:
        return None
    return (int(color[0]), int(color[1]), int(color[2]))


class _CalibrationCanvas(QLabel):
    def __init__(self, image: Image.Image, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._image = image
        self.image_width = image.width
        self.image_height = image.height
        self._pixmap = QPixmap.fromImage(
            _pil_image_to_qimage(image)
        )
        self._active_key = _STEPS[0].key
        self._active_mode = _STEPS[0].mode
        self._active_index = 0
        self._selections: dict[str, QRectF | QPointF | _ColorPick] = {}
        self._start: Optional[QPointF] = None
        self._current: Optional[QPointF] = None
        self.on_color_picked: Optional[
            Callable[[str, tuple[int, int, int]], None]
        ] = None
        self.on_color_rejected: Optional[Callable[[str], None]] = None
        self.setMouseTracking(True)
        self.setCursor(Qt.CrossCursor)
        self.setPixmap(self._pixmap)
        self.setScaledContents(True)

    def set_display_size(self, width: int, height: int) -> None:
        self.setFixedSize(width, height)

    def set_selection(self, key: str, value: QRectF | QPointF | _ColorPick | None) -> None:
        if value is None:
            self._selections.pop(key, None)
        else:
            self._selections[key] = value
        self.update()

    def selection_box(self, key: str) -> tuple[float, float, float, float] | None:
        value = self._selections.get(key)
        if not isinstance(value, QRectF):
            return None
        return (
            value.left() / self.image_width,
            value.top() / self.image_height,
            value.right() / self.image_width,
            value.bottom() / self.image_height,
        )

    def selection_point(self, key: str) -> tuple[float, float] | None:
        value = self._selections.get(key)
        if not isinstance(value, QPointF):
            return None
        return value.x() / self.image_width, value.y() / self.image_height

    def selection_color(self, key: str) -> tuple[int, int, int] | None:
        value = self._selections.get(key)
        if not isinstance(value, _ColorPick):
            return None
        return value.rgb

    def set_step(self, step: _CalibrationStep) -> None:
        self._start = None
        self._current = None
        self._active_key = step.key
        self._active_mode = step.mode
        self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            point = self._to_image_point(event.position())
            if self._active_mode == "color":
                self._pick_color(point)
            elif self._active_mode == "point":
                self._selections[self._active_key] = point
                self.update()
            else:
                self._start = point
                self._current = point
            self.update()
        super().mousePressEvent(event)

    def _pick_color(self, point: QPointF) -> None:
        chat = self._selections.get("chat_box")
        if not isinstance(chat, QRectF) or not chat.contains(point):
            if self.on_color_rejected:
                self.on_color_rejected("请先框选聊天区，并在聊天区内部取色")
            return
        rgb = _point_color(self._image, point.x(), point.y())
        if rgb is None:
            if self.on_color_rejected:
                self.on_color_rejected("无法读取该点颜色，请重新点击")
            return
        self._selections[self._active_key] = _ColorPick(point, rgb)
        if self.on_color_picked:
            self.on_color_picked(self._active_key, rgb)
        self.update()

    def mouseMoveEvent(self, event) -> None:
        if self._start is not None and self._active_mode == "rect":
            self._current = self._to_image_point(event.position())
            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if (
            event.button() == Qt.LeftButton
            and self._start is not None
            and self._active_mode == "rect"
        ):
            point = self._to_image_point(event.position())
            box = QRectF(self._start, point).normalized()
            if box.width() >= 1 and box.height() >= 1:
                self._selections[self._active_key] = box
            self._start = None
            self._current = None
            self.update()
        super().mouseReleaseEvent(event)

    def _to_image_point(self, position) -> QPointF:
        return QPointF(
            position.x() * self.image_width / max(1, self.width()),
            position.y() * self.image_height / max(1, self.height()),
        )

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        scale_x = self.width() / self.image_width
        scale_y = self.height() / self.image_height

        for step in _STEPS:
            value = self._selections.get(step.key)
            if isinstance(value, QRectF):
                self._draw_box(
                    painter,
                    QRectF(
                        value.left() * scale_x,
                        value.top() * scale_y,
                        value.width() * scale_x,
                        value.height() * scale_y,
                    ),
                    QColor(step.color),
                    2 if step.key == self._active_key else 1,
                )
            elif isinstance(value, QPointF):
                self._draw_point(
                    painter,
                    QPointF(value.x() * scale_x, value.y() * scale_y),
                    QColor(step.color),
                    4 if step.key == self._active_key else 3,
                )
            elif isinstance(value, _ColorPick):
                self._draw_color_pick(
                    painter,
                    QPointF(value.point.x() * scale_x, value.point.y() * scale_y),
                    QColor(*value.rgb),
                    7 if step.key == self._active_key else 5,
                )

        if self._start is not None and self._current is not None:
            box = QRectF(self._start, self._current).normalized()
            self._draw_box(
                painter,
                QRectF(
                    box.left() * scale_x,
                    box.top() * scale_y,
                    box.width() * scale_x,
                    box.height() * scale_y,
                ),
                QColor(_STEPS[self._active_index].color),
                2,
            )
        painter.end()

    def _draw_box(self, painter: QPainter, rect: QRectF, color: QColor, width: int) -> None:
        painter.setPen(QPen(color, width, Qt.SolidLine, Qt.SquareCap, Qt.MiterJoin))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(rect)

    def _draw_point(self, painter: QPainter, point: QPointF, color: QColor, radius: int) -> None:
        painter.setPen(QPen(color, 2))
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(point, radius, radius)
        painter.drawLine(QPointF(point.x() - radius - 3, point.y()), QPointF(point.x() + radius + 3, point.y()))
        painter.drawLine(QPointF(point.x(), point.y() - radius - 3), QPointF(point.x(), point.y() + radius + 3))

    def _draw_color_pick(
        self, painter: QPainter, point: QPointF, color: QColor, radius: int
    ) -> None:
        rect = QRectF(
            point.x() - radius,
            point.y() - radius,
            radius * 2,
            radius * 2,
        )
        painter.fillRect(rect, color)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor(30, 30, 30), 1))
        painter.drawRect(rect)
        painter.setPen(QPen(QColor(255, 255, 255), 1))
        painter.drawLine(
            QPointF(point.x() - radius - 3, point.y()),
            QPointF(point.x() + radius + 3, point.y()),
        )
        painter.drawLine(
            QPointF(point.x(), point.y() - radius - 3),
            QPointF(point.x(), point.y() + radius + 3),
        )


def _pil_image_to_qimage(image: Image.Image):
    from PySide6.QtGui import QImage

    rgb = image.convert("RGBA")
    return QImage(
        rgb.tobytes("raw", "RGBA"),
        rgb.width,
        rgb.height,
        rgb.width * 4,
        QImage.Format_RGBA8888,
    )


def _legend_item(title: str, color: str, parent: Optional[QWidget] = None) -> QWidget:
    container = QWidget(parent)
    row = QHBoxLayout(container)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(6)

    swatch = QWidget(container)
    swatch.setFixedSize(14, 14)
    swatch.setStyleSheet(f"background-color: {color}; border: 1px solid #5a5a5a;")
    label = QLabel(title, container)

    row.addWidget(swatch)
    row.addWidget(label)
    row.addStretch(1)
    return container


@dataclass(frozen=True)
class WeChatCapture:
    """One calibration screenshot plus the window state it was taken with."""

    image: Image.Image
    window_width: int
    window_height: int
    maximized: bool


class OcrCalibrationDialog(QDialog):
    def __init__(
        self,
        capture: WeChatCapture,
        calibration: OcrCalibration,
        *,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("OCR 区域标定")
        self.setModal(True)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self._capture = capture
        self._image = capture.image
        self._previous_palette = calibration.palette
        self._canvas = _CalibrationCanvas(capture.image, self)
        self._canvas.on_color_picked = self._on_color_picked
        self._canvas.on_color_rejected = self._on_color_rejected
        self._active_index = 0
        self._step_label = QLabel(self)
        self._hint_label = QLabel(self)
        self._hint_label.setWordWrap(True)
        self._color_label = QLabel(self)
        self._color_label.setWordWrap(True)

        self._set_initial_selections(calibration)

        self._previous_button = QPushButton("上一步")
        self._next_button = QPushButton("下一步")
        self._clear_button = QPushButton("清空当前")
        self._default_button = QPushButton("使用默认色")
        self._save_button = QPushButton("保存标定")
        self._cancel_button = QPushButton("取消")
        self._previous_button.clicked.connect(self._previous_step)
        self._next_button.clicked.connect(self._next_step)
        self._clear_button.clicked.connect(self._clear_current)
        self._default_button.clicked.connect(self._use_default_color)
        self._save_button.clicked.connect(self._save)
        self._cancel_button.clicked.connect(self.reject)

        nav_row = QHBoxLayout()
        nav_row.addWidget(self._previous_button)
        nav_row.addWidget(self._next_button)
        nav_row.addWidget(self._clear_button)
        nav_row.addWidget(self._default_button)
        nav_row.addStretch(1)
        nav_row.addWidget(self._save_button)
        nav_row.addWidget(self._cancel_button)

        root = QVBoxLayout(self)
        root.addWidget(self._step_label)
        root.addWidget(self._hint_label)
        root.addWidget(self._color_label)
        legend_row = QHBoxLayout()
        for step in _STEPS:
            legend_row.addWidget(_legend_item(step.title, step.color, self))
        root.addLayout(legend_row)
        root.addWidget(self._canvas, 1)
        root.addLayout(nav_row)

        display_width, display_height = self._display_size()
        self._canvas.set_display_size(display_width, display_height)
        self._update_color_label()
        self._apply_step()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.raise_()
        self.activateWindow()

    def calibration(self) -> OcrCalibration:
        title = self._canvas.selection_box("title_box")
        chat = self._canvas.selection_box("chat_box")
        input_point = self._canvas.selection_point("input_point")
        if not title or not chat or not input_point:
            raise ValueError("请完成全部三个标定项")
        page = self._canvas.selection_color("page_color")
        friend = self._canvas.selection_color("friend_color")
        own = self._canvas.selection_color("self_color")
        if not page or not friend or not own:
            raise ValueError("请完成三种底色的取色（页面 / 对方气泡 / 自己气泡）")
        palette = OcrPalette(page=page, friend=friend, own=own)
        problem = palette.distance_problem()
        if problem:
            raise ValueError(problem)
        return OcrCalibration(
            input_x_ratio=input_point[0],
            input_y_ratio=input_point[1],
            title_box=title,
            chat_box=chat,
            window_size=(self._capture.window_width, self._capture.window_height),
            window_maximized=self._capture.maximized,
            palette=palette,
        )

    def _set_initial_selections(self, calibration: OcrCalibration) -> None:
        self._canvas._selections = {
            "title_box": QRectF(
                calibration.title_box[0] * self._image.width,
                calibration.title_box[1] * self._image.height,
                (calibration.title_box[2] - calibration.title_box[0]) * self._image.width,
                (calibration.title_box[3] - calibration.title_box[1]) * self._image.height,
            ),
            "chat_box": QRectF(
                calibration.chat_box[0] * self._image.width,
                calibration.chat_box[1] * self._image.height,
                (calibration.chat_box[2] - calibration.chat_box[0]) * self._image.width,
                (calibration.chat_box[3] - calibration.chat_box[1]) * self._image.height,
            ),
            "input_point": QPointF(
                calibration.input_x_ratio * self._image.width,
                calibration.input_y_ratio * self._image.height,
            ),
        }
        palette = calibration.palette
        if palette is not None:
            center = self._canvas._selections["chat_box"].center()
            for key, rgb in (
                ("page_color", palette.page),
                ("friend_color", palette.friend),
                ("self_color", palette.own),
            ):
                self._canvas._selections[key] = _ColorPick(center, rgb)

    def _display_size(self) -> tuple[int, int]:
        screen = QApplication.primaryScreen()
        available = screen.availableGeometry() if screen else QRectF(0, 0, 1200, 760)
        max_width = max(420, min(1280, int(available.width() * 0.92)))
        max_height = max(360, min(820, int(available.height() * 0.82)))
        scale = min(
            1.0,
            max_width / self._image.width,
            max_height / self._image.height,
        )
        return (
            max(360, int(self._image.width * scale)),
            max(300, int(self._image.height * scale)),
        )

    def _apply_step(self) -> None:
        step = _STEPS[self._active_index]
        self._step_label.setText(
            f"步骤 {self._active_index + 1} / {len(_STEPS)}：{step.title}"
        )
        self._canvas._active_index = self._active_index
        self._canvas.set_step(step)
        self._default_button.setEnabled(step.mode == "color")
        self._update_hint()
        self._previous_button.setEnabled(self._active_index > 0)
        self._next_button.setEnabled(self._active_index < len(_STEPS) - 1)

    def _previous_step(self) -> None:
        if self._active_index > 0:
            self._active_index -= 1
            self._apply_step()

    def _next_step(self) -> None:
        if self._active_index < len(_STEPS) - 1:
            self._active_index += 1
            self._apply_step()

    def _clear_current(self) -> None:
        step = _STEPS[self._active_index]
        self._canvas.set_selection(step.key, None)
        if step.mode == "color":
            self._update_color_label()
            self._update_hint()

    def _update_hint(self) -> None:
        step = _STEPS[self._active_index]
        text = step.hint
        if step.mode == "color":
            value = self._canvas._selections.get(step.key)
            if isinstance(value, _ColorPick):
                suffix = "（默认色）" if value.defaulted else ""
                text += f"\n当前取色点：RGB{value.rgb}{suffix}"
            else:
                text += "\n尚未取色。"
        self._hint_label.setText(text)

    def _update_color_label(self) -> None:
        parts: list[str] = []
        for step in _STEPS:
            if step.mode != "color":
                continue
            value = self._canvas._selections.get(step.key)
            if isinstance(value, _ColorPick):
                suffix = "（默认色）" if value.defaulted else ""
                parts.append(f"{step.title} {value.rgb}{suffix}")
            else:
                parts.append(f"{step.title} 未选")
        self._color_label.setText("　".join(parts))

    def _on_color_picked(
        self, key: str, rgb: tuple[int, int, int]
    ) -> None:
        self._update_color_label()
        self._update_hint()

    def _on_color_rejected(self, message: str) -> None:
        QMessageBox.warning(self, "OCR 区域标定", message)

    def _use_default_color(self) -> None:
        step = _STEPS[self._active_index]
        if step.mode != "color":
            return
        previous = self._previous_palette
        fallback = _DEFAULT_COLORS[step.key]
        if previous is not None:
            fallback = {
                "page_color": previous.page,
                "friend_color": previous.friend,
                "self_color": previous.own,
            }[step.key]
        chat = self._canvas._selections.get("chat_box")
        if isinstance(chat, QRectF):
            point = chat.center()
        else:
            point = QPointF(self._image.width / 2, self._image.height / 2)
        self._canvas._selections[step.key] = _ColorPick(
            point, fallback, defaulted=True
        )
        self._canvas.update()
        self._update_color_label()
        self._update_hint()

    def _save(self) -> None:
        try:
            self.calibration()
        except ValueError as exc:
            QMessageBox.warning(self, "OCR 区域标定", str(exc))
            return
        self.accept()


def capture_wechat_screenshot() -> WeChatCapture:
    """Bring WeChat forward and capture it together with its window state."""

    from handwechat.ocr_channel import _find_wechat_window

    def set_window_topmost(hwnd: int, topmost: bool) -> None:
        # HWND_TOPMOST/HWND_NOTOPMOST with SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW.
        user32.SetWindowPos(
            hwnd,
            -1 if topmost else -2,
            0,
            0,
            0,
            0,
            0x0043,
        )

    located = _find_wechat_window()
    if located is None:
        raise RuntimeError("未找到微信主窗口，请确认微信已启动并登录")
    hwnd, _rect = located
    user32 = ctypes.windll.user32
    GWL_EXSTYLE = -20
    WS_EX_TOPMOST = 0x00000008
    was_topmost = bool(user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST)
    if user32.IsIconic(hwnd):
        # Only a minimized window needs restoring: SW_RESTORE would also
        # un-maximize a maximized window and change the calibrated layout.
        user32.ShowWindow(hwnd, 9)
        time.sleep(0.15)
    set_window_topmost(hwnd, True)
    user32.SetForegroundWindow(hwnd)

    try:
        time.sleep(0.2)

        located = _find_wechat_window()
        if located is None:
            raise RuntimeError("截图前未找到微信主窗口")
        _hwnd, rect = located
        image = ImageGrab.grab(bbox=rect, all_screens=True)
        if image.width <= 1 or image.height <= 1:
            raise RuntimeError("微信窗口截图尺寸无效")
        return WeChatCapture(
            image=image,
            window_width=int(rect[2] - rect[0]),
            window_height=int(rect[3] - rect[1]),
            maximized=bool(user32.IsZoomed(hwnd)),
        )
    finally:
        if not was_topmost:
            set_window_topmost(hwnd, False)
