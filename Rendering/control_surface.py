"""
KronosControlSurface — custom QWidget that draws the hardware control panel.

The Kronos design space is 800×600 (matching the XAML Viewbox, uniform
letterbox scale — Stretch="Uniform"). All Kronos coordinates below are in
that space; they are scaled to the actual widget size in paintEvent.

Also draws the Nautilus front-panel skin (Views/NautilusRightPanel.xaml's
port) — same widget, same class, switched at runtime via set_device_family()
rather than a separate widget the way the C# app swaps two UserControls,
since this is a single custom-painted surface rather than a tree of real
child widgets. Nautilus's design space is 1024×771 (NautilusFront1.jpg's
native size) and uses a WIDTH-ONLY scale (`width / 1024`, matching C#'s
NautilusRightPanel.xaml.cs's own LayoutTransform — see set_device_family's
docstring for why that's deliberately not the same uniform-letterbox
strategy Kronos uses).

Both skins' buttons stay registered in self._btns/self._btn_map at all
times (only the active skin's subset is painted/hit-tested) — this lets
set_mode()/set_active() keep working unmodified when called for a Kronos
button name while the Nautilus skin is showing (Nautilus's MODE/PAGE
buttons reuse Kronos's Combi/Program mode indices for their command/
pending-mode bookkeeping — see Views/main_window.py's _CTRL_BTN_CMD
NAUT_MODE/NAUT_PAGE entries — so that machinery must keep working even
though nothing draws the invisible Kronos button it nominally touches).

Button images are shared with the C# project under
  ../KronosScreenRemote/Resources/Images/
"""
from __future__ import annotations
import pathlib
from typing import Optional

import time

from PySide6.QtCore import Qt, QPoint, QRect, QTimer, Signal
from PySide6.QtGui import QMouseEvent, QPainter, QPixmap, QWheelEvent
from PySide6.QtWidgets import QMenu, QWidget

# Design space
_DS_W = 800
_DS_H = 600

# Nautilus design space — NautilusFront1.jpg's native size (NOT Kronos's 800×600).
_NAUT_DS_W = 1024
_NAUT_DS_H = 771

# Pixels of vertical drag per wheel step (design-space pixels) — same physical
# gesture sensitivity for both skins; the two design spaces are similar enough
# scale (both ~200px wheel diameter) that no per-skin value is needed.
_WHEEL_PX_PER_STEP = 12

# Wheel animation — matches C# WheelAngles / WheelAnimIntervalMs / WheelAnimIdleMs
_WHEEL_ANGLES      = [0.0, 10.0, -10.0]
_WHEEL_ANIM_MS     = 100
_WHEEL_IDLE_MS     = 400


def _res(name: str) -> pathlib.Path:
    # Repo-root Resources/Images/, not Rendering/Resources/Images/ (which doesn't
    # exist) — this file lives one directory below the repo root, so it takes
    # TWO .parent hops to reach it. This was wrong (one hop) until 2026-09-25,
    # silently loading zero images the whole time — every .exists() guard below
    # made the miss invisible instead of erroring. See Views/main_window.py's
    # ValueSliderControl._load_images and Models/storage.py's cal_data.json
    # fallback for the same bug, fixed alongside this one.
    return pathlib.Path(__file__).parent.parent / "Resources" / "Images" / name


# ── Button descriptor ──────────────────────────────────────────────────────────

class _Btn:
    """One clickable region in design space."""
    def __init__(self, name: str, x: int, y: int, w: int, h: int,
                 img_unlit: str, img_lit: Optional[str],
                 toggle: bool, radio_group: Optional[str],
                 skin: str = "kronos", hold_lit: bool = False):
        self.name       = name
        self.rect       = QRect(x, y, w, h)
        self.img_unlit  = img_unlit
        self.img_lit    = img_lit
        self.toggle     = toggle
        self.radio_group = radio_group
        self.skin       = skin      # "kronos" or "nautilus" — which skin draws/hit-tests it
        # HoldLit (C#'s ButtonMode="HoldLit", Nautilus A-F): lit only while the
        # mouse is physically held down on it, not a persistent/toggled state —
        # mousePressEvent/mouseReleaseEvent set `active` directly for these
        # rather than the toggle/radio_group/set_active machinery below.
        self.hold_lit   = hold_lit
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

# Nautilus button layout, converted from Views/NautilusRightPanel.xaml's
# Margin="left,top,right,bottom" in its 1024×771 canvas:
#   (x, y, w, h) = (left, top, 1024-left-right, 771-top-bottom)
# Wire tokens/command wiring live in Views/main_window.py's _CTRL_BTN_CMD
# (NAUT_* entries) and are documented there, not here.
_NAUT_BUTTON_DEFS: list[tuple] = [
    # name         x    y    w    h   img_unlit              img_lit               hold_lit
    ("NAUT_MODE",  462, 279, 106, 44, "ButtonModeUnlit.png", "ButtonModeLit.png",  False),
    ("NAUT_PAGE",  782, 278, 105, 44, "ButtonPageUnlit.png", "ButtonPageLit.png",  False),
    ("NAUT_A",     463, 412, 107, 46, "ButtonAUnlit.png",    "ButtonALit.png",     True),
    ("NAUT_B",     625, 412, 107, 46, "ButtonBUnlit.png",    "ButtonBLit.png",     True),
    ("NAUT_C",     787, 410, 107, 46, "ButtonCUnlit.png",    "ButtonCLit.png",     True),
    ("NAUT_D",     464, 550, 107, 46, "ButtonDUnlit.png",    "ButtonDLit.png",     True),
    ("NAUT_E",     628, 549, 107, 46, "ButtonEUnlit.png",    "ButtonELit.png",     True),
    ("NAUT_F",     793, 548, 107, 46, "ButtonFUnlit.png",    "ButtonFLit.png",     True),
    ("NAUT_EXIT",   34, 551, 115, 53, "ButtonExit.png",      None,                 False),
    ("NAUT_ENTER", 143, 551, 115, 53, "ButtonEnter.png",     None,                 False),
    ("NAUT_INC",   146, 465, 115, 53, "ButtonInc.png",       None,                 False),
    ("NAUT_DEC",    37, 465, 109, 53, "ButtonDec.png",       None,                 False),
]

# Nautilus data wheel position (Data_Wheel Margin="52,190,771,383" in 1024×771).
_NAUT_WHEEL_X, _NAUT_WHEEL_Y, _NAUT_WHEEL_W, _NAUT_WHEEL_H = 52, 190, 201, 198


class KronosControlSurface(QWidget):
    """
    Emits button_pressed(name) on mouse-down, button_released(name) on mouse-up
    (only if released over the same button). Wheel_step(delta) is +n CW / -n CCW.
    """
    button_pressed  = Signal(str)
    button_released = Signal(str)
    wheel_step      = Signal(int)   # positive=CW, negative=CCW
    wheel_settings_requested = Signal()   # right-click wheel -> "Settings..."

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(200, 150)
        self._reverse_scroll = False   # set by MainWindow from settings.reverse_scrolling
        self._active_skin = "kronos"   # "kronos" or "nautilus" — see set_device_family()
        self._btns: list[_Btn] = [
            _Btn(name, x, y, w, h, ul, lit, toggle, group, skin="kronos")
            for name, x, y, w, h, ul, lit, toggle, group in _BUTTON_DEFS
        ] + [
            _Btn(name, x, y, w, h, ul, lit, False, None, skin="nautilus", hold_lit=hold_lit)
            for name, x, y, w, h, ul, lit, hold_lit in _NAUT_BUTTON_DEFS
        ]
        self._btn_map: dict[str, _Btn] = {b.name: b for b in self._btns}
        self._pixmap_cache: dict[str, QPixmap] = {}
        self._bg_pixmap:        Optional[QPixmap] = None   # Kronos background
        self._naut_bg_pixmap:   Optional[QPixmap] = None   # Nautilus background
        self._wheel_pixmap:      Optional[QPixmap] = None   # Kronos wheel (transparent)
        self._naut_wheel_pixmap: Optional[QPixmap] = None   # Nautilus wheel
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

    def set_device_family(self, is_nautilus: bool) -> None:
        """Switches which skin paints/hit-tests — port of C#'s two separate
        UserControls (KronosRightPanel/NautilusRightPanel) swapped by
        visibility, adapted to this single custom-painted widget instead.
        A no-op if unchanged. Does not touch button `active` state — Kronos's
        radio-group mode lighting and Nautilus's MODE_LIT/PAGE_LIT-driven
        lamps (Views/main_window.py's _apply_nautilus_lamps) are independent
        of which skin is currently drawn, exactly as C#'s ApplyNautilusLamps
        keeps updating _nautilusRightPanel even while it's hidden."""
        skin = "nautilus" if is_nautilus else "kronos"
        if skin == self._active_skin:
            return
        self._active_skin = skin
        self.update()

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

    def reset_wheel_animation(self):
        """Data-wheel context menu's "Reset Animation" — snaps back to the
        neutral angle and stops any in-flight rock animation."""
        self._wheel_timer.stop()
        self._wheel_anim_state = 0
        self._wheel_angle = 0.0
        self.update()

    # ── Skin geometry helpers ────────────────────────────────────────────────────

    def _skin_dims(self) -> tuple[int, int]:
        return (_NAUT_DS_W, _NAUT_DS_H) if self._active_skin == "nautilus" else (_DS_W, _DS_H)

    def _skin_wheel_rect(self) -> tuple[int, int, int, int]:
        if self._active_skin == "nautilus":
            return _NAUT_WHEEL_X, _NAUT_WHEEL_Y, _NAUT_WHEEL_W, _NAUT_WHEEL_H
        return _WHEEL_X, _WHEEL_Y, _WHEEL_W, _WHEEL_H

    def _scale_and_offset(self) -> tuple[float, float, float]:
        ds_w, ds_h = self._skin_dims()
        if self._active_skin == "nautilus":
            # Width-only scale — port of NautilusRightPanel.xaml.cs's OnSizeChanged
            # (scale = e.NewSize.Width / DesignWidth). Deliberately NOT the same
            # uniform-letterbox strategy Kronos uses below: the column always
            # matches the live screen's width exactly and only ever crops/gaps
            # top-bottom (that panel's own comment explains why — the tight
            # left/right margins around EXIT/PAGE would otherwise get clipped
            # instead of the generous ~150-unit margin above/below the buttons).
            scale = self.width() / ds_w if ds_w else 1.0
        else:
            # Uniform scale (Viewbox Stretch="Uniform") — letterbox if aspect differs.
            scale = min(self.width() / ds_w, self.height() / ds_h) if ds_w and ds_h else 1.0
        ox = (self.width()  - ds_w * scale) / 2
        oy = (self.height() - ds_h * scale) / 2
        return scale, ox, oy

    # ── Painting ───────────────────────────────────────────────────────────────

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.fillRect(self.rect(), Qt.black)

        ds_w, ds_h = self._skin_dims()
        scale, ox, oy = self._scale_and_offset()
        p.translate(ox, oy)
        p.scale(scale, scale)

        # Background
        bg = self._naut_bg_pixmap if self._active_skin == "nautilus" else self._bg_pixmap
        if bg:
            p.drawPixmap(0, 0, ds_w, ds_h, bg)
        else:
            p.fillRect(0, 0, ds_w, ds_h, Qt.black)

        # Buttons — only the active skin's subset (both skins' buttons stay
        # registered in self._btns at all times, see this module's docstring).
        for btn in self._btns:
            if btn.skin != self._active_skin:
                continue
            key = (btn.img_lit if btn.active and btn.img_lit else btn.img_unlit)
            px = self._pixmap_cache.get(key)
            if px:
                r = btn.rect
                y_off = 2 if btn is self._pressed_btn else 0
                p.drawPixmap(r.x(), r.y() + y_off, r.width(), r.height(), px)

        # Data wheel — draw at natural aspect ratio (circle stays circular)
        wheel_px = (self._naut_wheel_pixmap if self._active_skin == "nautilus"
                    else self._wheel_pixmap)
        wx, wy, ww, wh = self._skin_wheel_rect()
        if wheel_px:
            iw = wheel_px.width()
            ih = wheel_px.height()
            if iw > 0 and ih > 0:
                s  = min(ww / iw, wh / ih)
                dw = iw * s
                dh = ih * s
                # Centre within the design-space rect
                cx = wx + (ww - dw) / 2 + dw / 2
                cy = wy + (wh - dh) / 2 + dh / 2
                p.save()
                p.translate(cx, cy)
                p.rotate(self._wheel_angle)
                p.translate(-dw / 2, -dh / 2)
                p.drawPixmap(QRect(0, 0, round(dw), round(dh)), wheel_px)
                p.restore()

        p.end()

    # ── Mouse input ────────────────────────────────────────────────────────────

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() != Qt.LeftButton:
            return
        ds = self._to_design(event.position())

        # Wheel drag
        wx, wy, ww, wh = self._skin_wheel_rect()
        if wx <= ds.x() <= wx + ww and wy <= ds.y() <= wy + wh:
            self._wheel_dragging = True
            self._wheel_drag_y   = ds.y()
            self._wheel_drag_steps = 0
            self.grabMouse()
            return

        # Button click — find deepest match (active skin only)
        for btn in reversed(self._btns):
            if btn.skin != self._active_skin:
                continue
            if btn.rect.contains(int(ds.x()), int(ds.y())):
                self._pressed_btn = btn
                if btn.hold_lit:
                    btn.active = True
                self.update()
                self._handle_click(btn)
                return

    def contextMenuEvent(self, event):
        ds = self._to_design(event.pos())
        wx, wy, ww, wh = self._skin_wheel_rect()
        if not (wx <= ds.x() <= wx + ww and wy <= ds.y() <= wy + wh):
            return
        menu = QMenu(self)
        menu.addAction("Settings…", self.wheel_settings_requested.emit)
        menu.addAction("Reset Animation", self.reset_wheel_animation)
        menu.exec(event.globalPos())

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
                btn = self._pressed_btn
                self._pressed_btn = None
                if btn.hold_lit:
                    # Held-lit clears unconditionally on release — it tracks
                    # "physically depressed right now", not click completion,
                    # so a drag-off release still unlights it (unlike
                    # button_released below, which requires landing back on
                    # the button to fire at all).
                    btn.active = False
                self.update()
                # Fire release only if the cursor is still over the pressed
                # button (so a drag-off cancels it).
                ds = self._to_design(event.position())
                if btn.rect.contains(int(ds.x()), int(ds.y())):
                    self.button_released.emit(btn.name)

    def wheelEvent(self, event: QWheelEvent):
        delta = event.angleDelta().y()
        if self._reverse_scroll:
            delta = -delta
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
        """Convert widget pixel position to the active skin's design space
        (accounts for its letterbox/crop strategy — see _scale_and_offset)."""
        scale, ox, oy = self._scale_and_offset()
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
        naut_bg_path = _res("NautilusFront1.jpg")
        if naut_bg_path.exists():
            self._naut_bg_pixmap = QPixmap(str(naut_bg_path))
        naut_wheel_path = _res("DataWheel.png")
        if naut_wheel_path.exists():
            self._naut_wheel_pixmap = QPixmap(str(naut_wheel_path))
        for btn in self._btns:
            for img_name in (btn.img_unlit, btn.img_lit):
                if img_name and img_name not in self._pixmap_cache:
                    p = _res(img_name)
                    if p.exists():
                        self._pixmap_cache[img_name] = QPixmap(str(p))
