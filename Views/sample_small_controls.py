"""Small Sample Editor controls: time ruler, pan, volume, VU meter — ports of Views/SampleWaveformRulerControl.cs,
SamplePanControl.cs, SampleVolumeControl.cs, SampleVuMeterControl.cs. All custom-painted for the same reason the
C# ones are: the knob-with-value-inside look is easier to draw than to retemplate a native slider into."""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

PANEL = QColor("#232323")
CONSOLE = QColor("#141414")
ACCENT = QColor("#88AADD")
BORDER = QColor("#555555")
TEXT = QColor("#E0E0E0")
MUTED = QColor("#888888")
GRID = QColor(255, 255, 255, 0x1A)
DANGER = QColor("#DD6666")
SUCCESS = QColor("#7DCB7D")

NICE_SECONDS = [0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800]


def nice_time_interval(rough_seconds: float) -> float:
    for s in NICE_SECONDS:
        if rough_seconds <= s:
            return s
    return NICE_SECONDS[-1]


def format_time(seconds: float, interval: float) -> str:
    if interval < 1:
        return f"{seconds:.3f}s"
    total = int(round(seconds))
    h, m, s = total // 3600, total // 60 % 60, total % 60
    return f"{h}:{m:02d}:{s:02d}" if h > 0 else f"{m}:{s:02d}"


def format_frame(seconds: float, sample_rate: int) -> str:
    return str(int(round(seconds * sample_rate)))


def _font(p: QPainter, px: int) -> None:
    f = QFont(p.font())
    f.setPixelSize(px)
    p.setFont(f)


class SampleWaveformRuler(QWidget):
    """Time ruler footer: tick + time label per nice interval, with the same tick's frame number just below so a
    second-marker can be read off as an exact frame without doing the seconds * rate math."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(30)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._view_start = 0
        self._view_end = 0
        self._sample_rate = 44100

    def set_view(self, view_start: int, view_end: int, sample_rate: int) -> None:
        if (view_start, view_end, sample_rate) != (self._view_start, self._view_end, self._sample_rate):
            self._view_start, self._view_end, self._sample_rate = view_start, view_end, sample_rate
            self.update()

    def paintEvent(self, _e) -> None:
        w, h = self.width(), self.height()
        p = QPainter(self)
        p.fillRect(0, 0, w, h, PANEL)
        rate, vs, ve = self._sample_rate, self._view_start, self._view_end
        if rate <= 0 or ve <= vs or w <= 0:
            p.end()
            return
        start_s = vs / rate
        len_s = (ve - vs) / rate
        interval = nice_time_interval(len_s * 80 / w)
        t = math.ceil(start_s / interval) * interval
        end_s = start_s + len_s
        while t <= end_s + 1e-12:
            x = (t - start_s) / len_s * w
            if 0 <= x <= w:
                p.setPen(QPen(GRID, 1))
                p.drawLine(QPointF(x, 0), QPointF(x, 5))
                p.setPen(MUTED)
                _font(p, 10)
                label = format_time(t, interval)
                lx = min(max(0.0, x + 2), max(0.0, w - p.fontMetrics().horizontalAdvance(label)))
                p.drawText(QPointF(lx, 17), label)
                _font(p, 9)
                frame = format_frame(t, rate)
                fx = min(max(0.0, x + 2), max(0.0, w - p.fontMetrics().horizontalAdvance(frame)))
                p.drawText(QPointF(fx, 28), frame)
            t += interval
        p.end()


class SamplePanControl(QWidget):
    """Horizontal pan, MIDI convention 0..127 (64 = centre). The fill grows from the CENTRE to the knob — pan has
    no 'silent' end the way volume's bottom is 0 %. Double-click re-centres."""
    pan_changed = Signal(int)
    KNOB_W = 30
    CENTER = 64

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pan = 64
        self.setMinimumHeight(20)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setToolTip("Pan (0=L, 64=Center, 127=R) - double-click to re-center")

    @property
    def pan(self) -> int:
        return self._pan

    @pan.setter
    def pan(self, v: int) -> None:
        v = max(0, min(127, int(v)))
        if v != self._pan:
            self._pan = v
            self.update()

    def _set(self, v: int) -> None:
        self.pan = v
        self.pan_changed.emit(self._pan)

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._from_mouse(e.position().x())

    def mouseDoubleClickEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._set(self.CENTER)

    def mouseMoveEvent(self, e) -> None:
        if e.buttons() & Qt.MouseButton.LeftButton:
            self._from_mouse(e.position().x())

    def _from_mouse(self, x: float) -> None:
        w = self.width()
        if w > 0:
            self._set(int(round(max(0.0, min(1.0, x / w)) * 127)))

    def paintEvent(self, _e) -> None:
        w, h = self.width(), self.height()
        p = QPainter(self)
        track_h = max(2.0, h * 0.3)
        ty = (h - track_h) / 2
        p.fillRect(QRectF(0, ty, w, track_h), CONSOLE)
        kx = max(self.KNOB_W / 2, min(self._pan / 127.0 * w, w - self.KNOB_W / 2))
        cx = w / 2
        p.fillRect(QRectF(min(cx, kx), ty, abs(kx - cx), track_h), ACCENT)
        p.setPen(QPen(BORDER, 1))
        p.drawLine(QPointF(cx, 0), QPointF(cx, h))
        knob = QRectF(kx - self.KNOB_W / 2, 0, self.KNOB_W, h - 1)
        p.setBrush(PANEL)
        p.drawRoundedRect(knob, 4, 4)
        label = {0: "L", self.CENTER: "C", 127: "R"}.get(self._pan, str(self._pan))
        p.setPen(TEXT)
        _font(p, 10)
        p.drawText(knob, Qt.AlignmentFlag.AlignCenter, label)
        p.end()


class SampleVolumeControl(QWidget):
    """Vertical volume fader, 0..1 — the percentage is printed inside the knob."""
    volume_changed = Signal(float)
    KNOB_H = 30

    def __init__(self, parent=None):
        super().__init__(parent)
        self._volume = 1.0
        self.setMinimumWidth(34)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setToolTip("Playback volume (this app only)")

    @property
    def volume(self) -> float:
        return self._volume

    @volume.setter
    def volume(self, v: float) -> None:
        v = max(0.0, min(1.0, float(v)))
        if v != self._volume:
            self._volume = v
            self.update()

    def _from_mouse(self, y: float) -> None:
        h = self.height()
        if h > 0:
            self.volume = 1.0 - max(0.0, min(1.0, y / h))
            self.volume_changed.emit(self._volume)

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._from_mouse(e.position().y())

    def mouseMoveEvent(self, e) -> None:
        if e.buttons() & Qt.MouseButton.LeftButton:
            self._from_mouse(e.position().y())

    def paintEvent(self, _e) -> None:
        w, h = self.width(), self.height()
        p = QPainter(self)
        track_w = max(2.0, w * 0.14)
        tx = (w - track_w) / 2
        p.fillRect(QRectF(tx, 0, track_w, h), CONSOLE)
        ky = max(self.KNOB_H / 2, min((1.0 - self._volume) * h, h - self.KNOB_H / 2))
        p.fillRect(QRectF(tx, ky, track_w, h - ky), ACCENT)
        knob = QRectF(0, ky - self.KNOB_H / 2, w - 1, self.KNOB_H)
        p.setPen(QPen(BORDER, 1))
        p.setBrush(PANEL)
        p.drawRoundedRect(knob, 4, 4)
        p.setPen(TEXT)
        _font(p, 11)
        p.drawText(knob, Qt.AlignmentFlag.AlignCenter, f"{int(round(self._volume * 100))}%")
        p.end()


class SampleVuMeter(QWidget):
    """Vertical VU meter, -90..0 dB (0 at the top). `level` is a linear peak 0..1."""
    TICKS = [0, -6, -12, -20, -40, -60, -90]
    MIN_DB = -90.0

    def __init__(self, parent=None, show_labels: bool = True):
        super().__init__(parent)
        self._level = 0.0
        self.show_labels = show_labels
        self.setMinimumWidth(8)

    @property
    def level(self) -> float:
        return self._level

    @level.setter
    def level(self, v: float) -> None:
        if v != self._level:
            self._level = v
            self.update()

    @classmethod
    def to_db(cls, linear: float) -> float:
        return cls.MIN_DB if linear <= 0.0000316 else max(cls.MIN_DB, min(0.0, 20.0 * math.log10(linear)))

    def paintEvent(self, _e) -> None:
        w, h = self.width(), self.height()
        p = QPainter(self)
        track_w = max(4, w - 20) if self.show_labels else max(4, w)
        p.fillRect(QRectF(0, 0, track_w, h), CONSOLE)
        db = self.to_db(self._level)
        fill_h = (db - self.MIN_DB) / -self.MIN_DB * h
        color = DANGER if db > -6 else ACCENT if db > -20 else SUCCESS
        if fill_h > 0:
            p.fillRect(QRectF(0, h - fill_h, track_w, fill_h), color)
        if self.show_labels:
            p.setPen(MUTED)
            _font(p, 8)
            fm = p.fontMetrics()
            for tick in self.TICKS:
                y = max(0.0, min(h - (tick - self.MIN_DB) / -self.MIN_DB * h, float(h)))
                p.drawLine(QPointF(track_w, y), QPointF(track_w + 3, y))
                p.drawText(QPointF(track_w + 5, max(fm.ascent() * 0.5 + 1, min(y + fm.ascent() / 2 - 1, h))), str(tick))
        p.end()
