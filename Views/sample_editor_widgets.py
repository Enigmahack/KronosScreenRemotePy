"""Small shared widgets for the Sample Editor window: the transport / tool icons (drawn from the same path data
the C# XAML uses) and the field box that commits on Enter / wheel / Up-Down."""
from __future__ import annotations

import re
from typing import List

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QLineEdit

ICON_GREY = QColor("#B4B4B4")
ICON_GREEN = QColor("#7DCB7D")
ICON_RED = QColor("#DD6666")

_TOKEN = re.compile(r"([MLHVZ])|(-?\d*\.?\d+)")

# Path data copied from Views/SampleEditorWindow.xaml; only the absolute M/L/H/V/Z subset is used.
PATHS = {
    "locate_start": ("M0,0 H1.8 V10 H0 Z M10,0 L3,5 L10,10 Z", 10, 10),
    "rewind": ("M5,0 L0,5 L5,10 Z M10,0 L5,5 L10,10 Z", 10, 10),
    "play": ("M0.5,0 L10,5 L0.5,10 Z", 10, 10),
    "stop": ("M1,1 H9 V9 H1 Z", 10, 10),
    "pause": ("M0.5,0 H3 V10 H0.5 Z M6,0 H8.5 V10 H6 Z", 10, 10),
    "forward": ("M0,0 L5,5 L0,10 Z M5,0 L10,5 L5,10 Z", 10, 10),
    "locate_end": ("M0,0 L7,5 L0,10 Z M8.2,0 H10 V10 H8.2 Z", 10, 10),
    "select": ("M0,0 H10 V1.4 H6 V12.6 H10 V14 H0 V12.6 H4 V1.4 H0 Z", 10, 14),
    "move": ("M7,4 H9 V12 H7 Z M4,7 H12 V9 H4 Z M8,0 L11,4 H5 Z M8,16 L5,12 H11 Z M0,8 L4,5 V11 Z M16,8 L12,11 V5 Z", 16, 16),
}


def _parse(data: str) -> QPainterPath:
    path = QPainterPath()
    cmd, nums, cx, cy = None, [], 0.0, 0.0
    toks = [(m.group(1), m.group(2)) for m in _TOKEN.finditer(data)]

    def flush() -> None:
        nonlocal cx, cy
        if cmd == "M" and len(nums) >= 2:
            cx, cy = nums[0], nums[1]
            path.moveTo(cx, cy)
            for i in range(2, len(nums) - 1, 2):
                cx, cy = nums[i], nums[i + 1]
                path.lineTo(cx, cy)
        elif cmd == "L":
            for i in range(0, len(nums) - 1, 2):
                cx, cy = nums[i], nums[i + 1]
                path.lineTo(cx, cy)
        elif cmd == "H":
            for v in nums:
                cx = v
                path.lineTo(cx, cy)
        elif cmd == "V":
            for v in nums:
                cy = v
                path.lineTo(cx, cy)
        elif cmd == "Z":
            path.closeSubpath()

    for c, n in toks:
        if c:
            flush()
            cmd, nums = c, []
        else:
            nums.append(float(n))
    flush()
    return path


def path_icon(name: str, size: int = 18, color: QColor = ICON_GREY) -> QIcon:
    data, w, h = PATHS[name]
    pm = QPixmap(size * 2, size * 2)                   # 2x for crisp edges on scaled displays
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    scale = min(pm.width() / (w + 4), pm.height() / (h + 4))
    p.translate((pm.width() - w * scale) / 2, (pm.height() - h * scale) / 2)
    p.scale(scale, scale)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    p.drawPath(_parse(data))
    p.end()
    return QIcon(pm)


def play_stop_icon(playing: bool) -> QIcon:
    return path_icon("stop" if playing else "play", color=ICON_RED if playing else ICON_GREEN)


def zoom_icon(plus: bool, size: int = 20) -> QIcon:
    pm = QPixmap(size * 2, size * 2)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = pm.width() / 14.0
    p.scale(s, s)
    pen = QPen(ICON_GREY, 1.3)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.drawEllipse(QRectF(1.5, 1.5, 8, 8))
    p.drawLine(QPointF(8.4, 8.4), QPointF(13, 13))
    p.setPen(QPen(ICON_GREY, 1.2))
    p.drawLine(QPointF(3.5, 5.5), QPointF(7.5, 5.5))
    if plus:
        p.drawLine(QPointF(5.5, 3.5), QPointF(5.5, 7.5))
    p.end()
    return QIcon(pm)


def undo_icon(redo: bool, size: int = 20) -> QIcon:
    pm = QPixmap(size * 2, size * 2)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = pm.width() / 14.0
    p.scale(s, s)
    p.setPen(QPen(ICON_GREY, 1.3))
    p.setBrush(Qt.BrushStyle.NoBrush)
    rect = QRectF(2.5, 2.5, 9, 9)
    # 270-degree arc in 1/16-degree units, opening at the top-right (undo) or top-left (redo).
    if redo:
        p.drawArc(rect, 90 * 16, -270 * 16)
        head = QPolygonF([QPointF(4.2, 7.3), QPointF(1, 6.3), QPointF(2.4, 9.3)])
    else:
        p.drawArc(rect, 90 * 16, 270 * 16)
        head = QPolygonF([QPointF(9.8, 7.3), QPointF(13, 6.3), QPointF(11.6, 9.3)])
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(ICON_GREY)
    p.drawPolygon(head)
    p.end()
    return QIcon(pm)


class FieldBox(QLineEdit):
    """Line edit for the live-committing fields. Emits `committed` on Enter, `wheel_step(+1/-1)` on the wheel and
    `key_step(+1/-1)` on Up/Down when enabled. (Ctrl+Z is claimed by the window's key filter before this widget's
    own text undo can see it, so it always means the app's Undo.)"""
    committed = Signal()
    wheel_step = Signal(int)
    key_step = Signal(int)

    def __init__(self, text: str = "", width: int = 90, parent=None):
        super().__init__(text, parent)
        self.setFixedWidth(width)
        self.step_with_arrows = False
        self.wheel_enabled = False

    def keyPressEvent(self, e) -> None:
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.committed.emit()
            e.accept()
            return
        if self.step_with_arrows and e.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self.key_step.emit(1 if e.key() == Qt.Key.Key_Up else -1)
            e.accept()
            return
        super().keyPressEvent(e)

    def wheelEvent(self, e) -> None:
        if not self.wheel_enabled:
            e.ignore()
            return
        self.wheel_step.emit(1 if e.angleDelta().y() > 0 else -1)
        e.accept()
