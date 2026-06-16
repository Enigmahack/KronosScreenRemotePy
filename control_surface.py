"""
KronosControlSurface — custom QWidget that draws the hardware control panel.

The design space is 800×600 (matching the XAML Viewbox).  All coordinates
below are in that space; they are scaled to the actual widget size in paintEvent.

Button images are shared with the C# project under
  ../KronosScreenRemote/Resources/Images/
"""
from __future__ import annotations
import pathlib
from typing import Callable, Dict, List, Optional, Tuple

import time

from PySide6.QtCore import Qt, QPoint, QRect, QSize, QTimer, Signal
from PySide6.QtGui import QImage, QMouseEvent, QPainter, QPixmap, QWheelEvent
from PySide6.QtWidgets import QWidget

# Design space
_DS_W = 800
_DS_H = 600

# Pixels of vertical drag per wheel step (design-space pixels)
_WHEEL_PX_PER_STEP = 12

# Wheel animation — matches C# WheelAngles / WheelAnimIntervalMs / WheelAnimIdleMs
_WHEEL_ANGLES      = [0.0, 10.0, -10.0]
_WHEEL_ANIM_MS     = 100
_WHEEL_IDLE_MS     = 400


def _res(name: str) -> pathlib.Path:
    return pathlib.Path(__file__).parent / "Resources" / "Images" / name


# ── Button descriptor ──────────────────────────────────────────────────────────

class _Btn:
    """One clickable region in design space."""
    def __init__(self, name: str, x: int, y: int, w: int, h: int,
                 img_unlit: str, img_lit: Optional[str],
                 toggle: bool, radio_group: Optional[str]):
        self.name       = name
        self.rect       = QRect(x, y, w, h)
        self.img_unlit  = img_unlit
        self.img_lit    = img_lit
        self.toggle     = toggle
        self.radio_group = radio_group
        self.active     = False     # lit state


# Button layout derived from XAML Margin="left,top,right,bottom" in 800×600 grid.
# (x, y, w, h) = (left, top, 800-left-right, 600-top-bottom)
_BUTTON_DEFS: list[tuple] = [
    # name           x    y    w    h   img_unlit        img_lit               toggle  group
    ("Setlist",      87,  90,  110, 27, "UnlitWideButton.png", "LitWideButton.png",  False, "Mode"),
    ("Combi",       289,  91,  110, 27, "UnlitWideButton.png", "LitWideButton.png",  False, "Mode"),
    ("Program",     407,  91,  110, 27, "UnlitWideButton.png", "LitWideButton.png",  False, "Mode"),
    ("Sequence",    522,  91,  110, 27, "UnlitWideButton.png", "LitWideButton.png",  False, "Mode"),
    ("Sampling",    289, 169,  110, 27, "UnlitWideButton.png", "LitWideButton.png",  False, "Mode"),
    ("Global",      407, 169,  110, 27, "UnlitWideButton.png", "LitWideButton.png",  False, "Mode"),
    ("Disk",        522, 169,  110, 27, "UnlitWideButton.png", "LitWideButton.png",  False, "Mode"),
    ("Help",        671,  91,   50, 27, "UnlitThinButton.png", "LitThinButton.png",  True,  None),
    ("Compare",     671, 169,   50, 27, "UnlitThinButton.png", "LitThinButton.png",  True,  None),
    ("NUM7",        289, 250,  110, 25, "UnlitWideButton.png", None,                 False, None),
    ("NUM8",        407, 250,  110, 25, "UnlitWideButton.png", None,                 False, None),
    ("NUM9",        522, 250,  110, 25, "UnlitWideButton.png", None,                 False, None),
    ("NUM4",        289, 328,  110, 27, "UnlitWideButton.png", None,                 False, None),
    ("NUM5",        407, 328,  110, 27, "UnlitWideButton.png", None,                 False, None),
    ("NUM6",        524, 328,  110, 27, "UnlitWideButton.png", None,                 False, None),
    ("NUM1",        289, 409,  110, 27, "UnlitWideButton.png", None,                 False, None),
    ("NUM2",        407, 409,  110, 27, "UnlitWideButton.png", None,                 False, None),
    ("NUM3",        524, 409,  110, 27, "UnlitWideButton.png", None,                 False, None),
    ("NUM_DASH",    290, 489,  110, 27, "UnlitWideButton.png", None,                 False, None),
    ("NUM0",        407, 489,  110, 27, "UnlitWideButton.png", None,                 False, None),
    ("NUM_DOT",     524, 489,  110, 27, "UnlitWideButton.png", None,                 False, None),
    ("EXIT",         80, 493,  117, 27, "ExitButton.png",      None,                 False, None),
    ("ENTER",       639, 489,  117, 27, "ExitButton.png",      None,                 False, None),
]

# Data wheel position in design space
_WHEEL_X, _WHEEL_Y, _WHEEL_W, _WHEEL_H = 41, 222, 202, 218


class KronosControlSurface(QWidget):
    """
    Emits button_pressed(name) when a button is clicked.
    Emits wheel_step(delta) with +n for CW, -n for CCW.
    """
    button_pressed = Signal(str)
    wheel_step     = Signal(int)   # positive=CW, negative=CCW

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(200, 150)
        self._btns: list[_Btn] = [
            _Btn(name, x, y, w, h, ul, lit, toggle, group)
            for name, x, y, w, h, ul, lit, toggle, group in _BUTTON_DEFS
        ]
        self._btn_map: dict[str, _Btn] = {b.name: b for b in self._btns}
        self._pixmap_cache: dict[str, QPixmap] = {}
        self._bg_pixmap:    Optional[QPixmap] = None
        self._wheel_pixmap: Optional[QPixmap] = None
        self._wheel_angle      = 0.0
        self._wheel_anim_state = 0
        self._wheel_anim_dir   = 1
        self._wheel_last_act   = 0.0   # monotonic seconds
        self._wheel_dragging   = False
        self._wheel_drag_y     = 0.0
        self._wheel_drag_steps = 0

        self._pressed_btn: Optional[_Btn] = None

        self._wheel_timer = QTimer(self)
        self._wheel_timer.setInterval(_WHEEL_ANIM_MS)
        self._wheel_timer.timeout.connect(self._advance_wheel_anim)
        self.setMouseTracking(True)
        self.setCursor(Qt.ArrowCursor)
        self._load_images()

    # ── State helpers ──────────────────────────────────────────────────────────

    def set_active(self, name: str, active: bool):
        """Light up or unlight a button."""
        b = self._btn_map.get(name)
        if b:
            b.active = active
            self.update()

    def set_mode(self, mode: int):
        """Light the mode button for mode 1–7 (0 = none)."""
        names = ["Setlist", "Combi", "Program", "Sequence", "Sampling", "Global", "Disk"]
        for i, name in enumerate(names, 1):
            self.set_active(name, i == mode)

    def press_button(self, name: str):
        """Animate a named button as pressed (e.g. from a keyboard shortcut)."""
        btn = self._btn_map.get(name)
        if btn and self._pressed_btn is not btn:
            self._pressed_btn = btn
            self.update()

    def release_button(self, name: str):
        """Release the press animation for a named button."""
        btn = self._btn_map.get(name)
        if btn and self._pressed_btn is btn:
            self._pressed_btn = None
            self.update()

    def rotate_wheel(self, angle: float):
        self._wheel_angle = angle
        self.update()

    # ── Painting ───────────────────────────────────────────────────────────────

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.fillRect(self.rect(), Qt.black)

        # Uniform scale (Viewbox Stretch="Uniform") — letterbox if aspect differs
        scale = min(self.width() / _DS_W, self.height() / _DS_H)
        ox = (self.width()  - _DS_W * scale) / 2
        oy = (self.height() - _DS_H * scale) / 2
        p.translate(ox, oy)
        p.scale(scale, scale)

        # Background
        if self._bg_pixmap:
            p.drawPixmap(0, 0, _DS_W, _DS_H, self._bg_pixmap)
        else:
            p.fillRect(0, 0, _DS_W, _DS_H, Qt.black)

        # Buttons
        for btn in self._btns:
            key = (btn.img_lit if btn.active and btn.img_lit else btn.img_unlit)
            px = self._pixmap_cache.get(key)
            if px:
                r = btn.rect
                y_off = 2 if btn is self._pressed_btn else 0
                p.drawPixmap(r.x(), r.y() + y_off, r.width(), r.height(), px)

        # Data wheel — draw at natural aspect ratio (circle stays circular)
        if self._wheel_pixmap:
            iw = self._wheel_pixmap.width()
            ih = self._wheel_pixmap.height()
            if iw > 0 and ih > 0:
                s  = min(_WHEEL_W / iw, _WHEEL_H / ih)
                dw = iw * s
                dh = ih * s
                # Centre within the design-space rect
                cx = _WHEEL_X + (_WHEEL_W - dw) / 2 + dw / 2
                cy = _WHEEL_Y + (_WHEEL_H - dh) / 2 + dh / 2
                p.save()
                p.translate(cx, cy)
                p.rotate(self._wheel_angle)
                p.translate(-dw / 2, -dh / 2)
                p.drawPixmap(QRect(0, 0, round(dw), round(dh)), self._wheel_pixmap)
                p.restore()

        p.end()

    # ── Mouse input ────────────────────────────────────────────────────────────

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() != Qt.LeftButton:
            return
        ds = self._to_design(event.position())

        # Wheel drag
        if _WHEEL_X <= ds.x() <= _WHEEL_X + _WHEEL_W and \
                _WHEEL_Y <= ds.y() <= _WHEEL_Y + _WHEEL_H:
            self._wheel_dragging = True
            self._wheel_drag_y   = ds.y()
            self._wheel_drag_steps = 0
            self.grabMouse()
            return

        # Button click — find deepest match
        for btn in reversed(self._btns):
            if btn.rect.contains(int(ds.x()), int(ds.y())):
                self._pressed_btn = btn
                self.update()
                self._handle_click(btn)
                return

    def mouseMoveEvent(self, event: QMouseEvent):
        if not self._wheel_dragging:
            return
        ds = self._to_design(event.position())
        dy = self._wheel_drag_y - ds.y()   # positive = dragged up = CW
        steps = int(dy / _WHEEL_PX_PER_STEP)
        diff  = steps - self._wheel_drag_steps
        if diff != 0:
            self._wheel_drag_steps = steps
            self.wheel_step.emit(diff)
            self._trigger_wheel_anim(1 if diff > 0 else -1)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            if self._wheel_dragging:
                self._wheel_dragging = False
                self.releaseMouse()
            if self._pressed_btn is not None:
                self._pressed_btn = None
                self.update()

    def wheelEvent(self, event: QWheelEvent):
        delta = event.angleDelta().y()
        if delta > 0:
            self.wheel_step.emit(1)
        elif delta < 0:
            self.wheel_step.emit(-1)
        event.accept()

    # ── Wheel animation ────────────────────────────────────────────────────────

    def trigger_wheel_anim(self, direction: int):
        """Public entry point — called by MainWindow on mouse-wheel scroll."""
        self._trigger_wheel_anim(direction)

    def _trigger_wheel_anim(self, direction: int):
        self._wheel_anim_dir = 1 if direction >= 0 else -1
        self._wheel_last_act = time.monotonic()
        if not self._wheel_timer.isActive():
            self._wheel_timer.start()
        self._advance_wheel_anim()   # jump to next state immediately on first trigger

    def _advance_wheel_anim(self):
        if (time.monotonic() - self._wheel_last_act) * 1000 > _WHEEL_IDLE_MS:
            self._wheel_timer.stop()
            return   # hold current angle — no snap-back (matches C# behaviour)
        self._wheel_anim_state = (self._wheel_anim_state + self._wheel_anim_dir) % 3
        self._wheel_angle = _WHEEL_ANGLES[self._wheel_anim_state]
        self.update()

    # ── Internals ──────────────────────────────────────────────────────────────

    def _to_design(self, pos) -> QPoint:
        """Convert widget pixel position to 800×600 design space (accounts for letterbox)."""
        scale = min(self.width() / _DS_W, self.height() / _DS_H)
        ox = (self.width()  - _DS_W * scale) / 2
        oy = (self.height() - _DS_H * scale) / 2
        dx = (pos.x() - ox) / scale
        dy = (pos.y() - oy) / scale
        return QPoint(int(dx), int(dy))

    def _handle_click(self, btn: _Btn):
        if btn.radio_group:
            # Radio: unlight all others in group
            for b in self._btns:
                if b.radio_group == btn.radio_group and b is not btn:
                    b.active = False
        if btn.toggle:
            btn.active = not btn.active
            self.update()
        self.button_pressed.emit(btn.name)

    def _load_images(self):
        """Load shared images from the C# Resources/Images/ folder."""
        bg_path = _res("DataEntrySurfaceEmpty.png")
        if bg_path.exists():
            self._bg_pixmap = QPixmap(str(bg_path))
        wheel_path = _res("DataWheelTransparent.png")
        if wheel_path.exists():
            self._wheel_pixmap = QPixmap(str(wheel_path))
        for btn in self._btns:
            for img_name in (btn.img_unlit, btn.img_lit):
                if img_name and img_name not in self._pixmap_cache:
                    p = _res(img_name)
                    if p.exists():
                        self._pixmap_cache[img_name] = QPixmap(str(p))
