"""Standalone floating Nautilus control surface: 6 knobs + Dynamics knob, Mode / Latch / Arp / Drum / Tap / MFX / TFX.

Run:  python -m Views.nautilus_surface_window <host> [--port 7374]

Wire (docs/api.md): knobs = `KNOB <n> <0-127>`; buttons = `BTN <code>` with the Nautilus codes captured in
kronosology/docs/hardware/nks4_button_codes.md (NOT the Kronos table in Core/button_codes.py: 13-26 differ).
"""
from __future__ import annotations

import math
import sys

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget

from Core.ctrl_client import CTRL_PORT, CtrlClient

# Nautilus raw button codes (hardware-captured 2026-11-24).
BTN_MODE, BTN_LATCH, BTN_ARP, BTN_DRUM, BTN_TAP, BTN_MFX, BTN_TFX = 14, 15, 16, 17, 20, 21, 22
# HW-VERIFY: KNOB 1-6 for the six knobs is the documented RT-knob range; the Dynamics knob has no documented
# analog device code (KNOB accepts 1-8 only), so 7 is a placeholder until measured on the unit.
DYNAMICS_KNOB_INDEX = 7

BLUE = QColor(40, 140, 255)
BODY, EDGE, TEXT = QColor(38, 38, 42), QColor(90, 90, 98), QColor(215, 215, 220)
SWEEP = 300.0          # 7 o'clock -> 5 o'clock, clockwise over the top (10 hours x 30 deg)
HOME_DYN = 64          # 12 o'clock


def _pen(color, width):
    return QPen(color, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)


class Knob(QWidget):
    changed = Signal(int)

    def __init__(self, label: str, home: int | None = None, parent=None):
        super().__init__(parent)
        self.label, self.home, self.value = label, home, (home if home is not None else 0)
        self._drag_y = self._drag_v = None
        self.setFixedSize(76, 92)
        self.setCursor(Qt.CursorShape.SizeVerCursor)

    def _set(self, v: int):
        v = max(0, min(127, v))
        if v != self.value:
            self.value = v
            self.update()
            self.changed.emit(v)

    def mousePressEvent(self, e):
        self._drag_y, self._drag_v = e.position().y(), self.value

    def mouseMoveEvent(self, e):
        if self._drag_y is not None:   # 1 px = 1 step; Shift = fine
            step = 0.25 if e.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1.0
            self._set(round(self._drag_v + (self._drag_y - e.position().y()) * step))

    def mouseReleaseEvent(self, e):
        self._drag_y = None

    def mouseDoubleClickEvent(self, e):
        self._set(self.home if self.home is not None else 0)

    def wheelEvent(self, e):
        self._set(self.value + (1 if e.angleDelta().y() > 0 else -1))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c, r = QPointF(self.width() / 2, 38), 28.0
        start = 240.0                                   # 7 o'clock; Qt angles: 0 = 3 o'clock, CCW positive
        p.setPen(_pen(EDGE, 3))
        arc = QRectF(c.x() - r - 5, c.y() - r - 5, 2 * r + 10, 2 * r + 10)
        p.drawArc(arc, int(start * 16), int(-SWEEP * 16))
        frac = self.value / 127.0
        p.setPen(_pen(BLUE, 3))
        p.drawArc(arc, int(start * 16), int(-SWEEP * frac * 16))
        p.setBrush(BODY)
        p.setPen(QPen(EDGE, 2))
        p.drawEllipse(c, r, r)
        a = math.radians(start - SWEEP * frac)
        p.setPen(_pen(TEXT, 3))
        p.drawLine(c + QPointF(math.cos(a), -math.sin(a)) * (r * 0.3),
                   c + QPointF(math.cos(a), -math.sin(a)) * (r * 0.9))
        if self.home is not None:                       # home tick at 12 o'clock
            p.setPen(QPen(TEXT, 2))
            p.drawLine(QPointF(c.x(), c.y() - r - 9), QPointF(c.x(), c.y() - r - 4))
        p.setPen(TEXT)
        p.setFont(QFont(self.font().family(), 8))
        p.drawText(QRectF(0, 74, self.width(), 16), Qt.AlignmentFlag.AlignCenter, f"{self.label}  {self.value}")


class PanelButton(QWidget):
    pressed_code = Signal(int)

    def __init__(self, label: str, code: int, lit: bool = False, parent=None):
        super().__init__(parent)
        self.label, self.code, self.lights, self.on = label, code, lit, False
        self._down = False
        self.setFixedSize(58, 34)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, e):
        self._down = True
        self.update()
        self.pressed_code.emit(self.code)

    def mouseReleaseEvent(self, e):
        self._down = False
        if self.lights:   # HW-VERIFY: LED is output-only on the unit; this mirrors a press toggle (no STATE field yet)
            self.on = not self.on
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        fill = BLUE if self.on else (QColor(60, 60, 66) if self._down else BODY)
        p.setBrush(fill)
        p.setPen(QPen(EDGE, 2))
        p.drawRoundedRect(self.rect().adjusted(2, 2, -2, -2), 6, 6)
        p.setPen(QColor(10, 10, 10) if self.on else TEXT)
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.label)


class NautilusSurface(QWidget):
    COL, X0 = 86, 20   # column pitch, left margin

    def __init__(self, host: str, port: int = CTRL_PORT):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle("Nautilus Controls")
        self._host, self._port, self._client = host, port, CtrlClient()
        self.setStyleSheet("background:#1b1b1e;")
        col = lambda i: self.X0 + i * self.COL          # noqa: E731  left edge of knob column i
        self.knobs = []
        for i in range(6):
            k = Knob(str(i + 1), parent=self)
            k.move(col(i), 14)
            k.changed.connect(lambda v, n=i + 1: self._knob(n, v))
            self.knobs.append(k)
        self.dyn = Knob("DYN", home=HOME_DYN, parent=self)
        self.dyn.move(col(6) + 10, 14)
        self.dyn.changed.connect(lambda v: self._knob(DYNAMICS_KNOB_INDEX, v))

        def place(label, code, centre_col, row, lit):    # centre_col in knob-column units
            b = PanelButton(label, code, lit, self)
            b.move(int(self.X0 + 38 + centre_col * self.COL - b.width() / 2), 118 + row * 48)
            b.pressed_code.connect(self._btn)
            return b
        place("MODE", BTN_MODE, 0, 0, False)             # under knob 1, never lit
        place("LATCH", BTN_LATCH, 1.5, 0, True)          # between knobs 2/3
        place("ARP", BTN_ARP, 2.5, 0, True)              # 3/4
        place("DRUM", BTN_DRUM, 3.5, 0, True)            # 4/5
        place("TAP", BTN_TAP, 1.5, 1, False)             # row below, same columns
        place("MFX", BTN_MFX, 2.5, 1, True)
        place("TFX", BTN_TFX, 3.5, 1, True)
        self.setFixedSize(col(6) + 10 + 76 + self.X0, 118 + 48 + 34 + 16)

    def _knob(self, n: int, v: int):
        self._client.send(self._host, self._port, f"KNOB {n} {v}")

    def _btn(self, code: int):
        self._client.send(self._host, self._port, f"BTN {code}")


def main(argv: list[str]) -> int:
    host = next((a for a in argv[1:] if not a.startswith("-")), "")
    port = int(argv[argv.index("--port") + 1]) if "--port" in argv else CTRL_PORT
    if not host:
        print(__doc__)
        return 2
    app = QApplication(argv)
    w = NautilusSurface(host, port)
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
