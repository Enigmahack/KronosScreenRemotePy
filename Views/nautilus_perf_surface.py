"""Nautilus-only performance controls using the main application's connection."""
from __future__ import annotations
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLabel, QPushButton, QFrame, QSizePolicy,
)
from PySide6.QtCore import Qt, QRectF, QPointF, QSize, Signal, QEvent, QTimer
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QFont, QWheelEvent, QFontMetrics
import math
from PySide6.QtWidgets import QStyleOptionSlider, QStyle

# Shared panel-button style: black fill, white bold text, cyan border on
# hover/checked. Applied to every push button; the 3-way indicator is a custom
# widget and is unaffected.
BUTTON_STYLE = (
    "QPushButton {"
    "  background: #000000;"
    "  border: 1px solid #555;"
    "  border-radius: 4px;"
    "  color: #ffffff;"
    "  font-family: Arial;"
    "  font-size: 11px;"
    "  font-weight: bold;"
    "}"
    "QPushButton:hover, QPushButton:checked {"
    "  background: #1a1a1a;"
    "  border: 1px solid #00aacc;"
    "}"
    "QPushButton:pressed {"
    "  background: #000000;"
    "  color: #888;"
    "}"
)
BUTTON_FONT = QFont("Arial", 11, QFont.Bold)
BUTTON_FONT.setPixelSize(11)


# ── Knob widget ───────────────────────────────────────────────────────────────
class PerformanceKnob(QFrame):
    """Rotating knob: 5 o'clock = 0 (off), 7 o'clock = 127 (full on).
    Arc spans 120°: start at -150° (5 o'clock), end at -30° (7 o'clock).
    Value 0..127 maps linearly across that arc.
    """
    VALUE_CHANGED = 127  # max value
    changed = Signal(int)

    def __init__(self, label: str = "", parent=None):
        super().__init__(parent)
        self._value = 0
        self._label = label
        self._dragging = False
        self._knob_number = None  # set by window for knobs 1-6
        self._min_angle = -150  # 5/7 o'clock (0/off)
        self._max_angle = 150   # 11 o'clock (127/full) — 300° from start
        self._range = self._max_angle - self._min_angle  # 300°
        self.setFixedSize(60, 96)
        self.setStyleSheet(
            "PerformanceKnob { background: transparent; border: none; }"
        )

    def value(self) -> int:
        return self._value

    def set_value(self, v: int):
        v = max(0, min(self.VALUE_CHANGED, v))
        if v != self._value:
            self._value = v
            self.update()
            self.changed.emit(v)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        cx = self.width() / 2
        cy = self.height() / 2
        radius = min(self.width(), self.height()) / 2 - 4

        # ── Knob number (above arc) ───────────────────────────────────
        if self._knob_number is not None:
            text = str(self._knob_number)
        else:
            text = "|"  # Dynamics column marker, aligned with the other knob numbers
        if text:
            p.setPen(QPen(QColor(187, 187, 187), 1))
            p.setFont(QFont("Arial", 10, QFont.Bold))
            num_rect = QRectF(cx - 10, cy - radius - 14, 16, 12)
            p.drawText(num_rect, int(Qt.AlignCenter), text)

        # ── Knob body ───────────────────────────────────────────────────
        knob_r = radius * 0.65
        knob_pen = QPen(QColor(180, 180, 180), 2, Qt.SolidLine)
        p.setPen(knob_pen)
        p.setBrush(QBrush(QColor(40, 40, 40)))
        p.drawEllipse(QPointF(cx, cy), knob_r, knob_r)

        # ── Indicator line (rotates with knob, light grey, center→edge) ──
        indicator_angle = self._min_angle + (self._value / self.VALUE_CHANGED) * self._range
        ind_pen = QPen(QColor(190, 190, 190), 2, Qt.SolidLine)
        p.setPen(ind_pen)
        rad = indicator_angle * 3.14159 / 180
        p.drawLine(
            QPointF(cx, cy),
            QPointF(cx + knob_r * 0.85 * math.cos(rad - math.pi/2),
                    cy + knob_r * 0.85 * math.sin(rad - math.pi/2))
        )

        # ── Value text ──────────────────────────────────────────────────
        p.setPen(QPen(QColor(200, 200, 200), 1))
        p.setFont(QFont("Arial", 9, QFont.Bold))
        rect = QRectF(cx - 20, cy + radius + 4, 40, 14)
        p.drawText(rect, int(Qt.AlignCenter), str(self._value))

        p.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._dragging = True
            self._drag_start_y = event.position().y()
            self._drag_start_value = self._value
            event.accept()
        elif event.button() == Qt.RightButton:
            from PySide6.QtWidgets import QInputDialog
            result = QInputDialog.getInt(self, "Set Value", "Value (0-127):", self._value, 0, 127, 1)
            dialog_val = result[0]  # (value, ok) in this PySide6
            dialog_ok = result[1]
            if dialog_ok:
                self.set_value(int(dialog_val))
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._dragging = False
            self.update()
            event.accept()

    def mouseMoveEvent(self, event):
        if not self._dragging:
            return
        dy = self._drag_start_y - event.position().y()  # up = increase
        sensitivity = 1.0  # units per pixel
        new_val = self._drag_start_value + dy * sensitivity
        self.set_value(int(round(new_val)))
        event.accept()

    def wheelEvent(self, event: QWheelEvent):
        delta = event.angleDelta().y()
        if delta > 0:
            self.set_value(self._value + 1)
        elif delta < 0:
            self.set_value(self._value - 1)


# ── 3-way indicator ────────────────────────────────────────────────────────
class ThreeWayIndicator(QWidget):
    """3-position indicator: up / center / down. Pale white circle, no border."""
    STATE_UP, STATE_MID, STATE_DOWN = 0, 1, 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = self.STATE_MID
        self._ys = None  # set by window from grid row geometry
        self.setFixedSize(30, 68)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Click to cycle the indicator locally without sending a command.")

    def set_centers(self, ys, h):
        self._ys = list(ys)
        self.setFixedSize(30, h)

    def set_state(self, s):
        self._state = s
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.translate(self.width() / 2, self.height() / 2)

        w = self.width()
        h = self.height()
        r = 6  # circle radius

        # Circle y = vertical center of each row this widget spans.
        # Top/mid/bottom align with Cutoff / Gate / "1" text boxes.
        if self._ys is not None:
            y_positions = list(self._ys)
        else:
            third = h / 3
            y_positions = [-h/2 + third / 2, 0, h/2 - third / 2]
        y = y_positions[self._state]

        # Pale white indicator circle
        p.setPen(QPen(QColor(220, 220, 220), 1))
        p.setBrush(QColor(220, 220, 220, 200))
        p.drawEllipse(QPointF(0, y), r, r)

        p.end()

    def cycle(self):
        self._state = (self._state + 1) % 3
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.cycle()
            event.accept()
        else:
            super().mousePressEvent(event)


# ── Shift / label button ─────────────────────────────────────────────────────
class ShiftButton(QPushButton):
    def __init__(self, label: str, parent=None):
        super().__init__(label, parent)
        self.setFixedSize(max(32, QFontMetrics(BUTTON_FONT).horizontalAdvance(label) + 12), 30)
        self.setStyleSheet(BUTTON_STYLE)
        self.setCheckable(True)


# ── Cycle button (solid, no text) ────────────────────────────────────────────
class CycleButton(QPushButton):
    """Solid no-text button: press to advance the 3-way indicator.

    Same size/style as Shift; centered under the 3-way at the left.
    """

    def __init__(self, parent=None):
        super().__init__(parent)  # no text
        self.setFixedSize(92, 30)
        self.setStyleSheet(BUTTON_STYLE)


# ── Main window ──────────────────────────────────────────────────────────────
class NautilusPerfSurface(QMainWindow):
    def _adjust_three_way(self):
        """Align the 3-way circles with the Cutoff / Gate / number label rows.

        Reads each label's real vertical center (not hardcoded row heights) so
        the circles follow the labels if text, size, or rows change. Called on
        show, once the grid has valid geometries.
        """
        labels = getattr(self, "_three_way_labels", None)
        tw = getattr(self, "three_way", None)
        if not labels or tw is None or tw.height() <= 0:
            return
        QApplication.processEvents()
        tw_cy = tw.geometry().center().y()
        ys = [l.geometry().center().y() - tw_cy for l in labels]
        tw.set_centers(ys, tw.height())

    def showEvent(self, event):
        super().showEvent(event)
        self._adjust_three_way()
        self._position_added_buttons()

    def __init__(self, main_window=None):
        super().__init__(main_window if isinstance(main_window, QWidget) else None, Qt.Window)
        self._mw = main_window
        self._shift_session = None
        self.setWindowTitle("Nautilus Performance Surface")
        self.resize(900, 500)

        # Apply global stylesheet matching the rest of the app
        self.setStyleSheet(
            """
            QMainWindow {
                background: #1a1a1a;
            }
            QLabel {
                color: #aaa;
                font-family: Arial;
                font-size: 11px;
            }
            """
        )

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(16)

        # Title
        title = QLabel("Nautilus Performance Surface")
        title.setStyleSheet(f"color: #00b4e0; font-size: 18px; font-weight: bold;")
        title.setAlignment(Qt.AlignCenter)
        main_layout.addWidget(title)

        # ── Grid: 9 cols × 4 rows ─────────────────────────────────────
        grid = QGridLayout()
        grid.setSpacing(4)
        grid.setRowMinimumHeight(1, 100)
        grid.setRowMinimumHeight(2, 16)   # Cutoff row
        grid.setRowMinimumHeight(3, 16)   # Gate row
        grid.setRowMinimumHeight(4, 16)   # knob-number row
        grid.setColumnMinimumWidth(0, 30)   # 3-way indicator
        grid.setColumnMinimumWidth(1, 80)   # knob 1
        grid.setColumnMinimumWidth(2, 80)   # knob 2
        grid.setColumnMinimumWidth(3, 80)   # knob 3
        grid.setColumnMinimumWidth(4, 80)   # knob 4
        grid.setColumnMinimumWidth(5, 80)   # knob 5
        grid.setColumnMinimumWidth(6, 80)   # knob 6
        grid.setColumnMinimumWidth(7, 40)   # gap
        grid.setColumnMinimumWidth(8, 80)   # Dynamics

        # ── Row 2-4: 3-way indicator (col 0, spans rows) ──────────────
        # Top/mid/bottom circles align with Cutoff / Gate / "1" rows.
        self.three_way = ThreeWayIndicator()
        grid.addWidget(self.three_way, 2, 0, 3, 1, alignment=Qt.AlignCenter)

        # Row 0: 6 knobs (cols 1-6) + Dynamics (col 8)
        knob_labels = ["Filter", "Env 1", "Env 2", "LFO 1", "LFO 2", "Mix"]
        self.knobs = []
        for i, label in enumerate(knob_labels):
            knob = PerformanceKnob(label)
            knob._knob_number = i + 1
            grid.addWidget(knob, 1, i + 1, alignment=Qt.AlignCenter)
            knob.changed.connect(lambda value, n=i + 1: self._send(f"KNOB {n} {value}"))
            self.knobs.append(knob)

        # Dynamics knob at col 8
        knob7 = PerformanceKnob("Dynamics")
        grid.addWidget(knob7, 1, 8, alignment=Qt.AlignCenter)
        self.knobs.append(knob7)
        knob7.changed.connect(lambda value: self._send(f"KNOB 7 {value}"))

        # "DYNAMICS" button directly below the 7th knob (col 8, row 2)
        # Sized to its text and centered in the column so it sits under the knob.
        # 11px bold matches the rest of the label text; tight padding keeps the
        # border close to the lettering so it does not widen column 8.
        dynamics_btn = QPushButton("DYNAMICS")
        fb = BUTTON_FONT
        bw = QFontMetrics(fb).horizontalAdvance("DYNAMICS") + 12
        dynamics_btn.setFixedSize(int(bw), 30)
        dynamics_btn.setStyleSheet(BUTTON_STYLE)
        grid.addWidget(dynamics_btn, 3, 8, alignment=Qt.AlignCenter)
        self.dynamics_btn = dynamics_btn
        dynamics_btn.clicked.connect(lambda: self._send("BTN 13"))

        # Row 2: primary labels (Cutoff..Reverb)
        primary_labels = ["Cutoff", "Resonance", "EG Int", "EG Release", "Effect", "Reverb"]
        self._three_way_labels = []
        for i, text in enumerate(primary_labels):
            lbl = QLabel(text)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet("color: #bbb; font-size: 11px; font-weight: bold;")
            grid.addWidget(lbl, 2, i + 1)
            if i == 0:
                self._three_way_labels.append(lbl)

        # Row 3: secondary labels (Gate..Tempo)
        secondary_labels = ["Gate", "Velocity", "Length", "Swing", "Drum SD", "Tempo"]
        for i, text in enumerate(secondary_labels):
            lbl = QLabel(text)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet("color: #bbb; font-size: 11px; font-weight: bold;")
            grid.addWidget(lbl, 3, i + 1)
            if i == 0:
                self._three_way_labels.append(lbl)

        # Row 4: knob numbers 1-6
        for i in range(6):
            num = QLabel(str(i + 1))
            num.setAlignment(Qt.AlignCenter)
            num.setStyleSheet("color: #bbb; font-size: 11px; font-weight: bold;")
            grid.addWidget(num, 4, i + 1)
            if i == 0:
                self._three_way_labels.append(num)

        shift_btn = ShiftButton("SHIFT")
        shift_btn.setCheckable(False)
        shift_btn.pressed.connect(self._shift_down)
        shift_btn.released.connect(self._shift_up)
        self.shift_btn = shift_btn
        grid.addWidget(shift_btn, 6, 8)

        # Solid no-text button directly below the 3-way (rows 2-4) and in line
        # with Shift (col 8). The button cycles both the instrument and indicator.
        cycle_btn = CycleButton()
        grid.addWidget(cycle_btn, 6, 0, alignment=Qt.AlignCenter)
        cycle_btn.clicked.connect(self.three_way.cycle)
        cycle_btn.clicked.connect(lambda: self._send("BTN 14"))
        self.cycle_btn = cycle_btn

        # SHIFT (row 6) -> + (row 9): rows 7-8 fill like the old spacer rows.
        # Row 5 is an empty spacer (both buttons sit in row 6, so they share one
        # vertical band); minH 20 keeps the 45px DYNAMICS->buttons gap.
        grid.setRowMinimumHeight(5, 20)
        grid.setRowMinimumHeight(7, 20)
        grid.setRowMinimumHeight(8, 20)

        plus_btn = QPushButton("+")
        plus_btn.setFixedSize(32, 30)
        plus_btn.setStyleSheet(BUTTON_STYLE)
        grid.addWidget(plus_btn, 9, 8)
        plus_btn.clicked.connect(lambda: self._send("BTN 24"))
        self.plus_btn = plus_btn

        main_layout.addLayout(grid)

        self.buttons = {}
        for label, code in (("LATCH", 15), ("ARP", 16), ("DRUM", 17),
                            ("AUDIO IN", 18), ("TAP", 20), ("MFX", 21),
                            ("TFX", 22), ("-", 23)):
            button = QPushButton(label, central)
            button.setFixedSize(max(32, QFontMetrics(fb).horizontalAdvance(label) + 12), 30)
            button.setStyleSheet(BUTTON_STYLE)
            button.clicked.connect(lambda checked=False, c=code: self._send(f"BTN {c}"))
            self.buttons[label] = button
        for button in (shift_btn, cycle_btn, plus_btn, *self.buttons.values()):
            button.setFixedSize(dynamics_btn.size())

        # ── Bottom bar ──────────────────────────────────────────────────
        bottom = QLabel("Connect in the main window · Turn knobs or press buttons · SHIFT is hold-to-use")
        bottom.setStyleSheet("color: #555; font-size: 10px; font-style: italic;")
        bottom.setAlignment(Qt.AlignCenter)
        main_layout.addWidget(bottom)
        self.status_label = bottom

    def _position_added_buttons(self):
        if not hasattr(self, "buttons"):
            return
        parent = self.centralWidget()
        centers = [k.mapTo(parent, k.rect().center()).x() for k in self.knobs]
        top_y = self.shift_btn.mapTo(parent, self.shift_btn.rect().topLeft()).y()
        bottom_y = self.plus_btn.mapTo(parent, self.plus_btn.rect().topLeft()).y()
        start_x = (centers[1] + centers[2]) // 2
        audio_x = centers[5] + 10
        for label, y in (("AUDIO IN", top_y), ("-", bottom_y)):
            button = self.buttons[label]
            button.move(audio_x - button.rect().center().x(), y)
        pitch = round((self.shift_btn.mapTo(parent, self.shift_btn.rect().center()).x()
                       - self.buttons["AUDIO IN"].mapTo(parent, self.buttons["AUDIO IN"].rect().center()).x()) * 0.9)
        for labels, y in ((("LATCH", "ARP", "DRUM"), top_y),
                          (("TAP", "MFX", "TFX"), bottom_y)):
            for i, label in enumerate(labels):
                button = self.buttons[label]
                x = start_x + i * pitch
                button.move(x - button.rect().center().x(), y)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self._position_added_buttons)

    def _send(self, command):
        if self._mw is None or not self._mw._host or self._mw._receiver is None:
            self.status_label.setText("Not connected. Connect in the main window first.")
            return False
        if not self._mw._is_nautilus:
            self.status_label.setText("Performance Surface is available only for Nautilus.")
            return False
        if self._mw._daemon_booting:
            self.status_label.setText("Instrument is booting; command not sent.")
            return False
        self._mw._ctrl_send(command)
        self.status_label.setText(f"Queued {command}; confirm the effect on the instrument.")
        return True

    def _shift_down(self):
        if self._shift_session is None and self._send("BTN_DOWN 19"):
            self._shift_session = (self._mw._ctrl, self._mw._host,
                                   self._mw._ctrl_port, self._mw._receiver)

    def _shift_up(self):
        session, self._shift_session = getattr(self, "_shift_session", None), None
        if session is None:
            return
        client, host, port, receiver = session
        if self._mw._receiver is receiver and self._mw._host == host:
            client.send(host, port, "BTN_UP 19")
            self.status_label.setText("Queued BTN_UP 19.")
        self.shift_btn.setDown(False)

    def event(self, event):
        if event.type() in (QEvent.WindowDeactivate, QEvent.Hide, QEvent.Close):
            self._shift_up()
        return super().event(event)
