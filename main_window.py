"""
MainWindow — primary application window.

Layout (LayoutPreset.Full):
  [frame widget (cols 0+1)] [control surface (col 2)]

Frame widget handles:
  - 8bpp→RGB rendering via QImage.Format_Indexed8 + color table
  - All overlays (palette editor, zoom, cal, help, boot splash, touch marker)
  - Mouse drag → TOUCH_DOWN/MOVE/UP
  - Keyboard forwarding to Kronos via ctrl_client

Control surface widget handles:
  - Mode / numpad / nav buttons
  - Data wheel drag
"""
from __future__ import annotations
import dataclasses
import datetime
import logging
import math
import os
import pathlib
import subprocess
import sys
import threading
import time
from typing import Dict, List, Optional, Set, Tuple

from PySide6.QtCore import (
    QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal, Slot,
)
from PySide6.QtGui import (
    QAction, QActionGroup, QBrush, QClipboard, QColor, QFont, QIcon, QImage, QKeyEvent,
    QMouseEvent, QPainter, QPainterPath, QPen, QPixmap, QPolygonF, QResizeEvent, QWheelEvent,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFileDialog, QFormLayout, QFrame,
    QGraphicsOpacityEffect, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMainWindow, QMenu,
    QMenuBar, QMessageBox, QPushButton, QSizePolicy, QStatusBar, QSystemTrayIcon, QTextEdit,
    QVBoxLayout, QWidget,
)

import char_map
import ctrl_client as CtrlClient
import image_adjust
import key_map
import storage
import theme as T
from app_settings import AppSettings, get_rebindable
from boot_phase_detector import BootPhaseDetector, Phase as BootPhase
from control_surface import KronosControlSurface
from mode_detector import CombiProgramEditDetector, ModeDetector, is_frame_mostly_black
from models import CalBiasDot, CalHistEntry, CalHistKind, CalMesh, HistEntry, PaletteEntry
from overlay_renderer import OverlayRenderer
from stream_receiver import StreamReceiver
from sysex_service import SysExService


# ── ICMP ping (matches C# System.Net.NetworkInformation.Ping) ─────────────────

def _icmp_ping(host: str, timeout_ms: int = 2000) -> float:
    """Return round-trip ms via ICMP echo, or -1 on failure."""
    if sys.platform == "win32":
        return _icmp_ping_win32(host, timeout_ms)
    return _icmp_ping_subprocess(host, timeout_ms)


def _icmp_ping_win32(host: str, timeout_ms: int) -> float:
    import ctypes
    import ctypes.wintypes
    import socket
    import struct
    try:
        addr = socket.gethostbyname(host)
    except socket.gaierror:
        return -1.0
    iphlpapi = ctypes.windll.iphlpapi
    iphlpapi.IcmpCreateFile.restype = ctypes.wintypes.HANDLE
    iphlpapi.IcmpCloseHandle.argtypes = [ctypes.wintypes.HANDLE]
    iphlpapi.IcmpCloseHandle.restype = ctypes.wintypes.BOOL
    iphlpapi.IcmpSendEcho.argtypes = [
        ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD,
        ctypes.c_void_p, ctypes.wintypes.WORD,
        ctypes.c_void_p,
        ctypes.c_void_p, ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
    ]
    iphlpapi.IcmpSendEcho.restype = ctypes.wintypes.DWORD
    handle = iphlpapi.IcmpCreateFile()
    if handle is None or handle == ctypes.wintypes.HANDLE(-1).value:
        return -1.0
    try:
        addr_int = struct.unpack("<I", socket.inet_aton(addr))[0]
        send_data = b"\x00" * 8
        reply_size = 28 + len(send_data) + 8 + 256
        reply_buf = ctypes.create_string_buffer(reply_size)
        ret = iphlpapi.IcmpSendEcho(
            handle, addr_int,
            send_data, len(send_data),
            None,
            reply_buf, reply_size,
            timeout_ms,
        )
        if ret > 0:
            status = struct.unpack_from("<I", reply_buf, 4)[0]
            rtt = struct.unpack_from("<I", reply_buf, 8)[0]
            if status == 0:
                return float(rtt)
        return -1.0
    except Exception:
        return -1.0
    finally:
        iphlpapi.IcmpCloseHandle(handle)


def _icmp_ping_subprocess(host: str, timeout_ms: int) -> float:
    import re
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(max(1, timeout_ms // 1000)), host],
            capture_output=True, text=True, timeout=timeout_ms / 1000 + 2,
        )
        if result.returncode == 0:
            m = re.search(r"time[=<](\d+\.?\d*)", result.stdout)
            if m:
                return float(m.group(1))
    except Exception:
        pass
    return -1.0


def _mods_to_int(mods) -> int:
    result = 0
    if mods & Qt.ControlModifier:
        result |= Qt.ControlModifier.value
    if mods & Qt.AltModifier:
        result |= Qt.AltModifier.value
    if mods & Qt.ShiftModifier:
        result |= Qt.ShiftModifier.value
    if mods & Qt.MetaModifier:
        result |= Qt.MetaModifier.value
    return result


_APP_TITLE  = "Kronos ScreenRemote"

# Boot bar fill fractions (0..1, left edge to right edge of bar)
_BOOT_F_STATIC_END  = 724.0 / 1302   # px 864 in 1600-wide image
_BOOT_F_PRELOAD_END = 1190.0 / 1302  # px 1330
_BOOT_F_BANK_START  = 1190.0 / 1302  # px 1330
_BOOT_F_BANK_END    = 1.0            # px 1442 = right edge
_BOOT_ENTRY_DELAY   = 0.5            # seconds before entering boot phase

_NUMPAD_MAP: dict[int, str] = {
    Qt.Key_0: "NUM0", Qt.Key_1: "NUM1", Qt.Key_2: "NUM2",
    Qt.Key_3: "NUM3", Qt.Key_4: "NUM4", Qt.Key_5: "NUM5",
    Qt.Key_6: "NUM6", Qt.Key_7: "NUM7", Qt.Key_8: "NUM8",
    Qt.Key_9: "NUM9",
    Qt.Key_Minus:  "NUM_DASH",
    Qt.Key_Period: "NUM_DOT",
    Qt.Key_Enter:  "ENTER",
}

# Mode index 0 = none; 1–7 = Setlist…Disk
_MODE_NAMES = ("", "Setlist", "Combi", "Program", "Sequence", "Sampling", "Global", "Disk")
_MODE_CMDS  = ("", "SETLIST", "COMBI",  "PROGRAM", "SEQUENCE", "SAMPLING", "GLOBAL", "DISK")
# Mode-menu display labels carrying the C# accelerator mnemonics (index-aligned to
# _MODE_NAMES). Kept separate so _MODE_NAMES stays clean for keybind/command lookups.
_MODE_MENU_LABELS = ("", "&Setlist", "&Combi", "&Program", "S&equence", "S&ampling", "&Global", "&Disk")

# Ctrl+1..Ctrl+5 -> Window Size menu scale, matching the C# hardcoded shortcuts
# (MainWindow.Input.cs) and the View > Window Size menu's own scale values.
_WINDOW_SIZE_CTRL_KEYS = {
    Qt.Key_1: 0.75, Qt.Key_2: 1.0, Qt.Key_3: 1.25, Qt.Key_4: 1.50, Qt.Key_5: 2.00,
}


# "Bank <suffix>" rebindable-action names -> daemon command, matching the handlers
# wired in _build_bank_menu (Internal I-A..I-G, User U-A..U-G, double-press U-AA..U-GG).
def _bank_action_cmd(action: str) -> str | None:
    if not action.startswith("Bank "):
        return None
    suffix = action[len("Bank "):]
    if suffix.startswith("I-") and len(suffix) == 3:
        return f"BUTTON BANK_I{suffix[2]}"
    if suffix.startswith("U-"):
        letters = suffix[2:]
        if len(letters) == 1:
            return f"BUTTON BANK_U{letters}"
        if len(letters) == 2 and letters[0] == letters[1]:
            l = letters[0]
            return f"CHORD BANK_U{l} BANK_I{l}"
    return None


# Keys eligible for held-key auto-repeat when forwarded to the Kronos (mirrors the
# C# RepeatableKeys set): letters, digits, edit/nav keys, common punctuation.
# Deliberately excludes Escape, function keys, etc. so a held Escape can't spam EXIT.
_REPEATABLE_KEYS = frozenset(
    list(range(int(Qt.Key_A), int(Qt.Key_Z) + 1)) +
    list(range(int(Qt.Key_0), int(Qt.Key_9) + 1)) +
    [int(k) for k in (
        Qt.Key_Backspace, Qt.Key_Delete, Qt.Key_Space, Qt.Key_Tab,
        Qt.Key_Return, Qt.Key_Enter, Qt.Key_Up, Qt.Key_Down, Qt.Key_Left,
        Qt.Key_Right, Qt.Key_Home, Qt.Key_End, Qt.Key_PageUp, Qt.Key_PageDown,
        Qt.Key_Minus, Qt.Key_Plus, Qt.Key_Equal, Qt.Key_Comma, Qt.Key_Period,
        Qt.Key_Slash, Qt.Key_BracketLeft, Qt.Key_BracketRight, Qt.Key_Backslash,
        Qt.Key_Semicolon, Qt.Key_Apostrophe, Qt.Key_QuoteLeft)])

# Control-surface button name → (daemon command, mode index or 0)
_CTRL_BTN_CMD: dict[str, tuple[str, int]] = {
    "Setlist":  ("BUTTON SETLIST",  1),
    "Combi":    ("BUTTON COMBI",    2),
    "Program":  ("BUTTON PROGRAM",  3),
    "Sequence": ("BUTTON SEQUENCE", 4),
    "Sampling": ("BUTTON SAMPLING", 5),
    "Global":   ("BUTTON GLOBAL",   6),
    "Disk":     ("BUTTON DISK",     7),
    "Help":     ("BUTTON HELP",     0),
    "Compare":  ("BUTTON COMPARE",  0),
    "EXIT":     ("BUTTON EXIT",     0),
    "ENTER":    ("BUTTON ENTER",    0),
    "NUM0":     ("BUTTON NUM0",     0),
    "NUM1":     ("BUTTON NUM1",     0),
    "NUM2":     ("BUTTON NUM2",     0),
    "NUM3":     ("BUTTON NUM3",     0),
    "NUM4":     ("BUTTON NUM4",     0),
    "NUM5":     ("BUTTON NUM5",     0),
    "NUM6":     ("BUTTON NUM6",     0),
    "NUM7":     ("BUTTON NUM7",     0),
    "NUM8":     ("BUTTON NUM8",     0),
    "NUM9":     ("BUTTON NUM9",     0),
    "NUM_DASH": ("BUTTON NUM_DASH", 0),
    "NUM_DOT":  ("BUTTON NUM_DOT",  0),
}


def _numpad_btn(qt_key: int, modifiers) -> str | None:
    if not (modifiers & Qt.KeypadModifier):
        return None
    return _NUMPAD_MAP.get(qt_key)
_FRAME_W    = 800
_FRAME_H    = 600
_DRAG_START = 8    # px manhattan to start drag
_DRAG_MOVE  = 3    # px to send TOUCH_MOVE after drag starts
_CAL_NODE_R = 18.0
_CAL_MARGIN = 20
_TOUCH_FADE = 0.6  # seconds for touch marker fade
_MODE_POLL_INTERVAL_MS = 1000

# Kronos ADC coordinate mapping (pixel → ADC value)
def _px_to_adc_h(px: int) -> int:
    return max(10, min(246, round(10 + px * (246 - 10) / (_FRAME_W - 1))))

def _px_to_adc_v(py: int) -> int:
    return max(8,  min(245, round(8  + py * (245 - 8)  / (_FRAME_H - 1))))


def _paint_seq_icon(kind: str, color: str, size: int) -> QPixmap:
    """Flat, single-color footer icons for the two sequencer-transport glyphs that have
    no plain-text Unicode equivalent (metronome, floppy save) — painted rather than an
    emoji character so they match the rest of the row's monochrome style instead of
    falling back to the system's color emoji font (see _seq_icon_btn's own comment)."""
    scale = 4   # draw oversized, then let QPixmap's smooth scaling anti-alias the shape
    px = QPixmap(size * scale, size * scale)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor(color)))
    s = size * scale / 10.0   # 10x10 design grid, matching C#'s own icon Path geometry
    if kind == "metronome":
        # Port of MainWindow.xaml's Tap Tempo Path: "M3,0 L7,0 L9.5,10 L0.5,10 Z
        # M5.3,2 L6.2,2 L4.7,8 L3.8,8 Z" — trapezoid body, cut-out pendulum arm.
        p.drawPolygon(QPolygonF([QPointF(3*s, 0), QPointF(7*s, 0),
                                 QPointF(9.5*s, 10*s), QPointF(0.5*s, 10*s)]))
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        p.drawPolygon(QPolygonF([QPointF(5.3*s, 2*s), QPointF(6.2*s, 2*s),
                                 QPointF(4.7*s, 8*s), QPointF(3.8*s, 8*s)]))
    elif kind == "save":
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        p.drawRoundedRect(QRectF(0.3*s, 0.3*s, 9.4*s, 9.4*s), 1.2*s, 1.2*s)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        p.drawRect(QRectF(2.3*s, 0.3*s, 3.6*s, 3.2*s))          # write-protect notch
        p.drawRect(QRectF(2*s, 5.2*s, 6*s, 3.8*s))               # label window
    p.end()
    px.setDevicePixelRatio(scale)
    return px


class FrameWidget(QWidget):
    """Renders the Kronos frame and all overlays."""
    touch_down   = Signal(int, int)   # frame coords
    touch_move   = Signal(int, int)
    touch_up     = Signal(int, int)
    frame_clicked_for_kbd    = Signal()
    context_menu_requested   = Signal(QPoint)  # global position

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(400, 300)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setFocusPolicy(Qt.NoFocus)
        self.setMouseTracking(True)

        self._frame_image: Optional[QImage]   = None   # Format_Indexed8
        self._frame_pixmap: Optional[QPixmap] = None   # converted for drawPixmap
        self._lut: list[int] = [0] * 256               # packed 0xRRGGBB — UNADJUSTED (detection/editor)
        self._cached_ct: list[int] = []                # base (unadjusted) QImage color table
        self._display_ct: list[int] = []               # tone/saturation-adjusted table (display only)
        self._ct_dirty = True                          # rebuild color table on next frame
        # Image adjustments applied to the *displayed* frame. The detection LUT
        # (_lut) stays unadjusted so boot black-detection isn't skewed by them.
        self._img_bri   = 0
        self._img_con   = 0
        self._img_gam   = 1.0
        self._img_sat   = 0
        self._img_sharp = 0
        self._scale_mode = "HighQuality"   # Sharp | Smooth | HighQuality
        self._frame_rect     = QRectF()
        self._palette: list[PaletteEntry] = []

        self._drag_pending    = False
        self._drag_pending_pos: Optional[QPoint] = None
        self._drag_active     = False
        self._drag_last: Optional[QPoint] = None

        self._touch_marker_pos: Optional[Tuple[int, int]] = None
        self._touch_marker_time = 0.0

        self._zoom_on     = False
        self._zoom_level  = 2.5
        self._cursor_fx   = 0   # frame coords under cursor
        self._cursor_fy   = 0

        self._ed_open     = False
        self._cal_mode    = False
        self._kbd_capture = False
        self._boot_phase  = True
        self._is_connected = False
        self._disable_boot_screen = False
        self._frame_is_likely_boot_screen = False
        self._daemon_authoritative = False   # set True once the daemon STATE path answers

        self._renderer = OverlayRenderer()

        # Palette editor state
        self._ed_sel    = 0
        self._ed_ch     = 0
        self._ed_typed: Optional[str] = None
        self._hover_idx: Optional[int] = None
        self._overrides: Dict[int, PaletteEntry] = {}
        self._locked:    Set[int] = set()
        self._ed_history: list[HistEntry] = []
        self._ed_hist_pos = -1
        self._clipboard: Optional[PaletteEntry] = None

        # Panel geometry cache (set during paint, used for mouse hit-testing)
        self._panel_rect:   Optional[QRectF]  = None
        self._grid_origin:  Optional[QPointF] = None
        self._slider_top:   float = 0.0

        # Cal state
        self._cal_mesh       = CalMesh()
        self._cal_bias_dots: list[CalBiasDot] = []
        self._cal_dragging: Optional[Tuple[int, int]] = None
        self._cal_hover:    Optional[Tuple[int, int]] = None
        self._cal_dirty     = False
        self._cal_history:  list[CalHistEntry] = []
        self._cal_hist_pos  = -1

        # Boot splash
        self._disconnect_msg = ""

        # Render timer for touch marker fade
        self._fade_timer = QTimer(self)
        self._fade_timer.setInterval(30)
        self._fade_timer.timeout.connect(self.update)

    # ── Frame update ───────────────────────────────────────────────────────────

    def _rebuild_color_tables(self, palette: list[PaletteEntry],
                              overrides: Dict[int, PaletteEntry]):
        """Build the base (unadjusted) colour table + detection LUT, then the
        adjusted display table."""
        ct = []
        for i, e in enumerate(palette):
            entry = overrides.get(i, e)
            ct.append(0xFF000000 | (entry.r << 16) | (entry.g << 8) | entry.b)
        self._cached_ct = ct
        for i, c in enumerate(ct):
            self._lut[i] = c & 0xFFFFFF     # UNADJUSTED — boot black-detection reads this
        self._rebuild_display_ct()

    def _rebuild_display_ct(self):
        """Fold brightness/contrast/gamma/saturation into a display-only colour
        table (mirrors C# RebuildLut). Identity → reuse the base table."""
        if not self._cached_ct:
            self._display_ct = []
            return
        if (image_adjust.tone_is_identity(self._img_bri, self._img_con, self._img_gam)
                and self._img_sat == 0):
            self._display_ct = self._cached_ct
            return
        curve = image_adjust.build_tone_curve(self._img_bri, self._img_con, self._img_gam)
        sat   = image_adjust.saturation_factor(self._img_sat)
        self._display_ct = [
            0xFF000000 | image_adjust.apply_to_channel(
                (c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF, curve, sat)
            for c in self._cached_ct
        ]

    def _make_pixmap(self, indexed_img: QImage) -> QPixmap:
        """Indexed8 → RGB32, then optional unsharp-mask (spatial, once per frame)."""
        rgb = indexed_img.convertToFormat(QImage.Format_RGB32)
        if self._img_sharp > 0:
            rgb = image_adjust.sharpen_rgb32(
                rgb, self._img_sharp / 100.0 * image_adjust.MAX_SHARPEN)
        return QPixmap.fromImage(rgb)

    def on_frame(self, raw: bytes, palette: list[PaletteEntry],
                 overrides: Dict[int, PaletteEntry], locked: Set[int]):
        """Called from main thread with a new 8bpp frame and current palette."""
        self._overrides = overrides
        self._locked    = locked
        if self._ct_dirty or not self._cached_ct:
            self._rebuild_color_tables(palette, overrides)
            self._ct_dirty = False
        img = QImage(raw, _FRAME_W, _FRAME_H, _FRAME_W, QImage.Format_Indexed8)
        img.setColorTable(self._display_ct)
        self._frame_image  = img
        self._frame_pixmap = self._make_pixmap(img)
        self.update()

    def set_palette(self, palette: list[PaletteEntry],
                    overrides: Dict[int, PaletteEntry]):
        """Rebuild LUT after palette or override change without new frame."""
        if not self._frame_image:
            return
        self._rebuild_color_tables(palette, overrides)
        self._ct_dirty  = False
        self._frame_image.setColorTable(self._display_ct)
        self._frame_pixmap = self._make_pixmap(self._frame_image)
        self.update()

    def set_image_adjust(self, brightness: int, contrast: int, gamma: float,
                         saturation: int, sharpen: int):
        """Update image-adjust params and re-render the current frame (live)."""
        self._img_bri, self._img_con, self._img_gam = brightness, contrast, gamma
        self._img_sat, self._img_sharp = saturation, sharpen
        self._rebuild_display_ct()
        if self._frame_image and self._display_ct:
            self._frame_image.setColorTable(self._display_ct)
            self._frame_pixmap = self._make_pixmap(self._frame_image)
            self.update()

    # ── Paint ──────────────────────────────────────────────────────────────────

    def paintEvent(self, _event):
        p = QPainter(self)
        # Scaling quality: Sharp = nearest-neighbour (no smoothing); Smooth/HQ =
        # bilinear (Qt has no Fant equivalent, so those two render identically).
        p.setRenderHint(QPainter.SmoothPixmapTransform, self._scale_mode != "Sharp")
        p.fillRect(self.rect(), Qt.black)

        fr = self._compute_frame_rect()
        if self._cal_mode:
            m = _CAL_MARGIN
            fr = QRectF(fr.x() + m, fr.y() + m,
                        fr.width() - 2 * m, fr.height() - 2 * m)
        self._frame_rect = fr

        if (self._boot_phase and self._is_connected
                and not self._disable_boot_screen and self._frame_is_likely_boot_screen
                and not getattr(self, "_daemon_authoritative", False)):
            # Client-side boot splash overlay - only when the daemon is NOT compositing
            # one server-side (see _on_frame's daemon_authoritative flag). When the
            # daemon is authoritative it paints the splash + live progress bar into the
            # stream itself (KronosScreenRemoteDaemon/docs/api.md "Boot splash"), so a
            # local overlay would double-draw over it.
            self._renderer.draw_boot_splash(p, fr, self._boot_fill_fraction)
        elif self._frame_pixmap:
            p.drawPixmap(fr.toRect(), self._frame_pixmap)
        elif not self._is_connected:
            self._renderer.draw_disconnected(p, fr, self._disconnect_msg or "Not connected")

        if self._cal_mode:
            p.fillRect(fr, QColor(0, 0, 0, 100))

        # Touch marker
        if self._touch_marker_pos:
            elapsed = time.monotonic() - self._touch_marker_time
            alpha = max(0.0, 1.0 - elapsed / _TOUCH_FADE)
            if alpha > 0:
                nx, ny = self._touch_marker_pos
                self._renderer.draw_touch_marker(p, fr, nx, ny, alpha)
                if not self._fade_timer.isActive():
                    self._fade_timer.start()
            else:
                self._touch_marker_pos = None
                self._fade_timer.stop()

        # Zoom loupe
        if self._zoom_on and self._frame_pixmap:
            self._renderer.draw_zoom_loupe(
                p, self._frame_pixmap, fr,
                self._cursor_fx, self._cursor_fy, self._zoom_level)

        # Calibration overlay
        if self._cal_mode and self._frame_pixmap:
            self._renderer.draw_cal_overlay(
                p, fr, self._cal_mesh, self._cal_bias_dots,
                self._cal_hover, self._cal_dragging,
                self._cal_dirty)

        # Palette editor
        if self._ed_open and self._palette:
            panel, grid_org, sl_top = self._renderer.draw_palette_editor(
                p, fr, self._palette, self._lut,
                self._ed_sel, self._ed_ch,
                self._overrides, self._locked,
                self._hover_idx, self._ed_typed)
            self._panel_rect  = panel
            self._grid_origin = grid_org
            self._slider_top  = sl_top

        p.end()

    def _compute_frame_rect(self) -> QRectF:
        # Aspect lock is permanent — matches C#'s FrameImage.Stretch = Stretch.Uniform
        # (MainWindow.Streaming.cs): always letterboxed to the Kronos' native ratio,
        # never stretched to fill. There is no toggle for it.
        w, h = self.width(), self.height()
        aspect = _FRAME_W / _FRAME_H
        if w / h > aspect:
            fw = h * aspect
            return QRectF((w - fw) / 2, 0, fw, h)
        else:
            fh = w / aspect
            return QRectF(0, (h - fh) / 2, w, fh)

    # ── Coordinate mapping ─────────────────────────────────────────────────────

    def _widget_to_frame(self, pos: QPointF) -> Optional[QPoint]:
        fr = self._frame_rect
        if fr.width() <= 0 or fr.height() <= 0:
            return None
        fx = int((pos.x() - fr.x()) * _FRAME_W / fr.width())
        fy = int((pos.y() - fr.y()) * _FRAME_H / fr.height())
        if self._cal_mode:
            return QPoint(fx, fy)
        if fx < 0 or fy < 0 or fx >= _FRAME_W or fy >= _FRAME_H:
            return None
        return QPoint(fx, fy)

    def _apply_cal(self, fx: int, fy: int) -> Tuple[int, int]:
        """Apply inverse calibration mesh to get Kronos natural coords."""
        return self._cal_mesh.inverse_apply(fx, fy, _FRAME_W, _FRAME_H)

    # ── Mouse ──────────────────────────────────────────────────────────────────

    def mousePressEvent(self, event: QMouseEvent):
        # Right-click in cal mode: place or remove a bias dot
        if event.button() == Qt.RightButton and self._cal_mode:
            fp = self._widget_to_frame(event.position())
            if fp:
                self._cal_right_click(fp.x(), fp.y())
            return

        # Right-click in normal mode: emit for context menu
        if event.button() == Qt.RightButton:
            self.context_menu_requested.emit(event.globalPosition().toPoint())
            return

        if event.button() != Qt.LeftButton:
            return
        pos = event.position()
        fp  = self._widget_to_frame(pos)

        # Palette editor hit-test
        if self._ed_open and self._panel_rect:
            if self._panel_rect.contains(pos):
                self._ed_mouse_down(pos)
                return

        # Calibration node drag — hit node starts drag, miss falls through to touch
        if self._cal_mode:
            node = self._cal_hit_node(fp)
            if node:
                self._cal_dragging = node
                ox, oy = self._cal_mesh.get_offset(*node)
                self._cal_drag_start = (ox, oy)
                return

        # Click inside frame → capture keyboard (not in cal mode)
        if fp and self._frame_rect.contains(pos) and not self._cal_mode:
            self.frame_clicked_for_kbd.emit()

        # Track click for visual feedback (always) and touch injection (when connected)
        if fp:
            self._drag_pending      = True
            self._drag_pending_pos  = QPoint(fp.x(), fp.y())
            self._drag_active       = False
            self._drag_last         = None

    def mouseMoveEvent(self, event: QMouseEvent):
        pos = event.position()
        fp  = self._widget_to_frame(pos)
        if fp:
            self._cursor_fx, self._cursor_fy = fp.x(), fp.y()
            if self._zoom_on:
                self.update()

        # Palette editor hover
        if self._ed_open and self._grid_origin and fp:
            self._ed_hover(pos)

        # Cal node drag / hover
        if self._cal_mode and fp:
            if self._cal_dragging:
                col, row = self._cal_dragging
                nat_x = self._cal_mesh.nat_x(col, _FRAME_W)
                nat_y = self._cal_mesh.nat_y(row, _FRAME_H)
                self._cal_mesh.set_offset(col, row, fp.x() - nat_x, fp.y() - nat_y)
                self._cal_dirty = True
                self.update()
                return
            old_hover = self._cal_hover
            self._cal_hover = self._cal_hit_node(fp)
            if self._cal_hover != old_hover:
                self.update()

        # Touch drag
        if not self._drag_pending and not self._drag_active:
            return
        if not fp:
            return
        if self._drag_pending:
            pp = self._drag_pending_pos
            if (abs(fp.x() - pp.x()) + abs(fp.y() - pp.y())) >= _DRAG_START:
                # Start drag — always show marker; only send when connected
                self._show_touch_marker(pp.x(), pp.y())
                if self._is_connected:
                    nx, ny = self._apply_cal(pp.x(), pp.y())
                    self.touch_down.emit(nx, ny)
                self._drag_pending = False
                self._drag_active  = True
                self._drag_last    = QPoint(fp.x(), fp.y())
        if self._drag_active and self._drag_last:
            dl = self._drag_last
            if (abs(fp.x() - dl.x()) + abs(fp.y() - dl.y())) >= _DRAG_MOVE:
                self._show_touch_marker(fp.x(), fp.y())
                if self._is_connected:
                    nx, ny = self._apply_cal(fp.x(), fp.y())
                    self.touch_move.emit(nx, ny)
                self._drag_last = QPoint(fp.x(), fp.y())

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() != Qt.LeftButton:
            return
        pos = event.position()
        fp  = self._widget_to_frame(pos)

        if self._cal_mode and self._cal_dragging:
            col, row = self._cal_dragging
            self._cal_dragging = None
            # Record undo entry
            old_x, old_y = getattr(self, '_cal_drag_start', (0, 0))
            new_x, new_y = self._cal_mesh.get_offset(col, row)
            if (old_x, old_y) != (new_x, new_y):
                self._cal_push_hist(CalHistEntry(CalHistKind.NodeMove, col=col, row=row,
                                                  old_off_x=old_x, old_off_y=old_y,
                                                  new_off_x=new_x, new_off_y=new_y))
            return

        if self._drag_pending:
            # Simple click (no drag started) — always show marker
            self._drag_pending = False
            pp = self._drag_pending_pos
            if pp and fp:
                self._show_touch_marker(pp.x(), pp.y())
                if self._is_connected:
                    nx, ny = self._apply_cal(pp.x(), pp.y())
                    self.touch_down.emit(nx, ny)
                    self.touch_up.emit(nx, ny)

        if self._drag_active:
            self._drag_active = False
            if fp:
                self._show_touch_marker(fp.x(), fp.y())
                if self._is_connected:
                    nx, ny = self._apply_cal(fp.x(), fp.y())
                    self.touch_up.emit(nx, ny)

    def wheelEvent(self, event: QWheelEvent):
        # Pass to parent (main window handles wheel → WHEEL CW/CCW)
        event.ignore()

    def leaveEvent(self, _event):
        self._hover_idx = None
        if self._ed_open:
            self.update()

    # ── Touch marker ──────────────────────────────────────────────────────────

    def _show_touch_marker(self, nx: int, ny: int):
        self._touch_marker_pos  = (nx, ny)
        self._touch_marker_time = time.monotonic()
        self.update()

    # ── Palette editor mouse helpers ───────────────────────────────────────────

    def _ed_mouse_down(self, pos: QPointF):
        go = self._grid_origin
        if not go:
            return
        from overlay_renderer import _SWATCH_SIZE, _SWATCH_COLS, _SWATCH_ROWS, _SLIDER_W
        rx = pos.x() - go.x()
        ry = pos.y() - go.y()
        if 0 <= rx < _SWATCH_COLS * _SWATCH_SIZE and 0 <= ry < _SWATCH_ROWS * _SWATCH_SIZE:
            col = int(rx / _SWATCH_SIZE)
            row = int(ry / _SWATCH_SIZE)
            self._ed_sel   = row * _SWATCH_COLS + col
            self._ed_typed = None
            self.update()
            return
        # Slider area — channel selected by y position below the swatch grid
        ry2 = pos.y() - go.y() - _SWATCH_ROWS * _SWATCH_SIZE - 12
        if ry2 >= 0:
            ch = int(ry2 / 20)
            if 0 <= ch < 3:
                self._ed_ch    = ch
                self._ed_typed = None
                self.update()

    def _ed_hover(self, pos: QPointF):
        go = self._grid_origin
        if not go:
            return
        from overlay_renderer import _SWATCH_SIZE, _SWATCH_COLS, _SWATCH_ROWS
        rx = pos.x() - go.x()
        ry = pos.y() - go.y()
        if 0 <= rx < _SWATCH_COLS * _SWATCH_SIZE and 0 <= ry < _SWATCH_ROWS * _SWATCH_SIZE:
            col = int(rx / _SWATCH_SIZE)
            row = int(ry / _SWATCH_SIZE)
            new_idx = row * _SWATCH_COLS + col
            if new_idx != self._hover_idx:
                self._hover_idx = new_idx
                self.update()
        else:
            if self._hover_idx is not None:
                self._hover_idx = None
                self.update()

    # ── Cal helpers ───────────────────────────────────────────────────────────

    def _cal_hit_node(self, fp: Optional[QPoint]) -> Optional[Tuple[int, int]]:
        if not fp:
            return None
        fr = self._frame_rect
        scale_x = fr.width()  / _FRAME_W if fr.width()  > 0 else 1
        scale_y = fr.height() / _FRAME_H if fr.height() > 0 else 1
        best_d, best = 1e9, None
        for c in range(self._cal_mesh.cols):
            for r in range(self._cal_mesh.rows):
                nx, ny = self._cal_mesh.node_dst(c, r, _FRAME_W, _FRAME_H)
                dx = (nx - fp.x()) * scale_x
                dy = (ny - fp.y()) * scale_y
                d  = math.hypot(dx, dy)
                if d < _CAL_NODE_R and d < best_d:
                    best_d, best = d, (c, r)
        return best

    def _cal_push_hist(self, entry: CalHistEntry):
        self._cal_history = self._cal_history[:self._cal_hist_pos + 1]
        self._cal_history.append(entry)
        self._cal_hist_pos = len(self._cal_history) - 1

    def _cal_right_click(self, fx: int, fy: int):
        """Place or remove a bias dot at the clicked frame position."""
        _HIT_R = 12  # pixel hit radius for removal
        for i, d in enumerate(self._cal_bias_dots):
            disp_x, disp_y = self._cal_mesh.apply(d.nx, d.ny, _FRAME_W, _FRAME_H)
            if math.hypot(fx - disp_x, fy - disp_y) <= _HIT_R:
                removed = self._cal_bias_dots.pop(i)
                self._cal_push_hist(CalHistEntry(CalHistKind.DotRemoved,
                                                  dot_idx=i, dot=removed))
                self._cal_dirty = True
                self.update()
                return
        # Dots stored in natural (pre-mesh) coordinates so they follow the mesh
        nat_x, nat_y = self._cal_mesh.inverse_apply(fx, fy, _FRAME_W, _FRAME_H)
        dot = CalBiasDot(nat_x, nat_y)
        self._cal_bias_dots.append(dot)
        self._cal_push_hist(CalHistEntry(CalHistKind.DotAdded,
                                          dot_idx=len(self._cal_bias_dots) - 1,
                                          dot=dot))
        self._cal_dirty = True
        self.update()

    def cal_undo(self):
        if self._cal_hist_pos < 0:
            return
        e = self._cal_history[self._cal_hist_pos]
        if e.kind == CalHistKind.NodeMove:
            self._cal_mesh.set_offset(e.col, e.row, e.old_off_x, e.old_off_y)
        elif e.kind == CalHistKind.DotAdded:
            if 0 <= e.dot_idx < len(self._cal_bias_dots):
                self._cal_bias_dots.pop(e.dot_idx)
        elif e.kind == CalHistKind.DotRemoved and e.dot is not None:
            self._cal_bias_dots.insert(e.dot_idx, e.dot)
        self._cal_hist_pos -= 1
        self._cal_dirty = True
        self.update()

    def cal_redo(self):
        if self._cal_hist_pos >= len(self._cal_history) - 1:
            return
        self._cal_hist_pos += 1
        e = self._cal_history[self._cal_hist_pos]
        if e.kind == CalHistKind.NodeMove:
            self._cal_mesh.set_offset(e.col, e.row, e.new_off_x, e.new_off_y)
        elif e.kind == CalHistKind.DotAdded and e.dot is not None:
            self._cal_bias_dots.insert(e.dot_idx, e.dot)
        elif e.kind == CalHistKind.DotRemoved:
            if 0 <= e.dot_idx < len(self._cal_bias_dots):
                self._cal_bias_dots.pop(e.dot_idx)
        self._cal_dirty = True
        self.update()

    # ── Keyboard input for palette editor ─────────────────────────────────────

    def palette_key(self, event: QKeyEvent) -> bool:
        """Handle key event for palette editor. Returns True if consumed."""
        if not self._ed_open:
            return False
        key = event.key()
        if key == Qt.Key_Tab:
            self._ed_ch = (self._ed_ch + 1) % 3
            self._ed_typed = None
            self.update()
            return True
        if key in (Qt.Key_Left, Qt.Key_H):
            self._ed_sel = max(0, self._ed_sel - 1)
            self._ed_typed = None
            self.update()
            return True
        if key in (Qt.Key_Right, Qt.Key_L):
            self._ed_sel = min(255, self._ed_sel + 1)
            self._ed_typed = None
            self.update()
            return True
        if key in (Qt.Key_Up, Qt.Key_K):
            self._ed_sel = max(0, self._ed_sel - 16)
            self._ed_typed = None
            self.update()
            return True
        if key in (Qt.Key_Down, Qt.Key_J):
            self._ed_sel = min(255, self._ed_sel + 16)
            self._ed_typed = None
            self.update()
            return True
        # Digit entry for slider value
        if Qt.Key_0 <= key <= Qt.Key_9:
            digit = chr(key)
            cur   = self._ed_typed or ""
            nxt   = cur + digit
            if int(nxt) <= 255:
                self._ed_typed = nxt
                self.update()
            return True
        if key == Qt.Key_Return or key == Qt.Key_Enter:
            if self._ed_typed is not None:
                self._ed_commit_typed()
            return True
        if key == Qt.Key_Backspace:
            if self._ed_typed:
                self._ed_typed = self._ed_typed[:-1] or None
                self.update()
            return True
        return False

    def _ed_commit_typed(self):
        if not self._palette or self._ed_typed is None:
            return
        val = max(0, min(255, int(self._ed_typed)))
        self._ed_typed = None
        idx = self._ed_sel
        if idx in self._locked:
            return
        old = self._overrides.get(idx, self._palette[idx])
        if self._ed_ch == 0:
            new = PaletteEntry(val, old.g, old.b)
        elif self._ed_ch == 1:
            new = PaletteEntry(old.r, val, old.b)
        else:
            new = PaletteEntry(old.r, old.g, val)
        self._overrides[idx] = new
        self._ct_dirty = True
        self.update()


class _StatusDot(QWidget):
    """Colored circle indicating connection state."""
    _COLORS = {"disconnected": T.TEXT_FAINT, "connecting": T.WARN, "connected": T.OK}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(12, 12)
        self._state = "disconnected"

    def set_state(self, state: str):
        if state != self._state:
            self._state = state
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(self._COLORS.get(self._state, T.TEXT_FAINT)))
        p.drawEllipse(1, 1, 10, 10)
        p.end()


class _NotifyBubble(QWidget):
    """Chat-bubble notification indicator — a filled speech bubble that recolors
    by state (idle gray / info amber / error red). Vector shape mirrors the C#
    NotifyBubblePath. Click/right-click is handled by the main window's
    eventFilter (installed on this widget)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(18, 15)
        self._color = T.TEXT_FAINT
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_color(self, color: str):
        if color != self._color:
            self._color = color
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(self._color))
        # 14×13 bubble from the C# NotifyBubblePath, offset to centre in 18×15.
        ox, oy = 2.0, 1.0
        path = QPainterPath()
        path.moveTo(ox + 1.5, oy)
        path.lineTo(ox + 12, oy)
        path.quadTo(ox + 14, oy, ox + 14, oy + 2)
        path.lineTo(ox + 14, oy + 8.5)
        path.quadTo(ox + 14, oy + 10, ox + 12, oy + 10)
        path.lineTo(ox + 5.5, oy + 10)
        path.lineTo(ox + 2.5, oy + 13)
        path.lineTo(ox + 3.5, oy + 10)
        path.lineTo(ox + 2, oy + 10)
        path.quadTo(ox, oy + 10, ox, oy + 8.5)
        path.lineTo(ox, oy + 2)
        path.quadTo(ox, oy, ox + 1.5, oy)
        path.closeSubpath()
        p.drawPath(path)
        p.end()


class KronosValueSliderPanel(QWidget):
    """Left-side panel with INC/DEC buttons and a draggable value slider.

    Design space is 282×600, matching the C# LeftPanelViewbox.
    The background image (KronosLeftSide2EMPTY.png) is cropped/offset
    to match the XAML Margin="-33,-113,-34,0".
    """
    button_pressed = Signal(str)
    slider_changed = Signal(int)

    _DS_W = 282
    _DS_H = 600

    _BTN_W = 50
    _BTN_H = 24
    _BTN_X = 114
    _INC_Y = 72
    _DEC_Y = 146

    _CANVAS_LEFT   = 87
    _CANVAS_TOP    = 300
    _CANVAS_RIGHT  = 87
    _CANVAS_BOTTOM = 30
    _THUMB_W       = 72
    _THUMB_H       = 42

    _SLIDER_TRAVEL = 228.0
    _THUMB_HALF    = 21.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(60)
        self._bg_pixmap: Optional[QPixmap] = None
        self._btn_pixmap: Optional[QPixmap] = None
        self._thumb_pixmap: Optional[QPixmap] = None
        self._thumb_top = self._SLIDER_TRAVEL
        self._value = 0
        self._dragging = False
        self._pressed_btn: Optional[str] = None
        self._load_images()

    def _load_images(self):
        res = pathlib.Path(__file__).parent / "Resources" / "Images"
        bg = res / "KronosLeftSide2EMPTY.png"
        if bg.exists():
            self._bg_pixmap = QPixmap(str(bg))
        btn = res / "UnlitThinButton.png"
        if btn.exists():
            self._btn_pixmap = QPixmap(str(btn))
        thumb = res / "Slider.png"
        if thumb.exists():
            self._thumb_pixmap = QPixmap(str(thumb))

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.fillRect(self.rect(), Qt.black)

        scale = min(self.width() / self._DS_W, self.height() / self._DS_H)
        ox = (self.width() - self._DS_W * scale) / 2
        oy = (self.height() - self._DS_H * scale) / 2
        p.translate(ox, oy)
        p.scale(scale, scale)
        p.setClipRect(QRect(0, 0, self._DS_W, self._DS_H))

        if self._bg_pixmap:
            p.drawPixmap(QRect(-33, -113, 349, 713), self._bg_pixmap)
        else:
            p.fillRect(0, 0, self._DS_W, self._DS_H, QColor(T.PANEL_ALT))

        if self._btn_pixmap:
            inc_off = 2 if self._pressed_btn == "INC" else 0
            p.drawPixmap(QRect(self._BTN_X, self._INC_Y + inc_off,
                               self._BTN_W, self._BTN_H), self._btn_pixmap)
            dec_off = 2 if self._pressed_btn == "DEC" else 0
            p.drawPixmap(QRect(self._BTN_X, self._DEC_Y + dec_off,
                               self._BTN_W, self._BTN_H), self._btn_pixmap)

        if self._thumb_pixmap:
            tx = self._CANVAS_LEFT + 17
            ty = self._CANVAS_TOP + self._thumb_top
            p.drawPixmap(QRect(tx, int(ty), self._THUMB_W, self._THUMB_H),
                         self._thumb_pixmap)

        p.end()

    def _to_design(self, pos) -> QPointF:
        scale = min(self.width() / self._DS_W, self.height() / self._DS_H)
        ox = (self.width() - self._DS_W * scale) / 2
        oy = (self.height() - self._DS_H * scale) / 2
        return QPointF((pos.x() - ox) / scale, (pos.y() - oy) / scale)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        ds = self._to_design(event.position())
        inc_rect = QRect(self._BTN_X, self._INC_Y, self._BTN_W, self._BTN_H)
        dec_rect = QRect(self._BTN_X, self._DEC_Y, self._BTN_W, self._BTN_H)
        if inc_rect.contains(int(ds.x()), int(ds.y())):
            self._pressed_btn = "INC"
            self.update()
            self.button_pressed.emit("INC")
            return
        if dec_rect.contains(int(ds.x()), int(ds.y())):
            self._pressed_btn = "DEC"
            self.update()
            self.button_pressed.emit("DEC")
            return
        canvas_rect = QRect(self._CANVAS_LEFT, self._CANVAS_TOP,
                            self._DS_W - self._CANVAS_LEFT - self._CANVAS_RIGHT,
                            self._DS_H - self._CANVAS_TOP - self._CANVAS_BOTTOM)
        if canvas_rect.contains(int(ds.x()), int(ds.y())):
            self._dragging = True
            self.grabMouse()
            self._update_slider_from_mouse(ds.y())

    def mouseMoveEvent(self, event: QMouseEvent):
        if not self._dragging:
            return
        ds = self._to_design(event.position())
        self._update_slider_from_mouse(ds.y())

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self._thumb_top = self._SLIDER_TRAVEL * (127 - 64) / 127.0
            self._value = 64
            self.slider_changed.emit(64)
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            if self._dragging:
                self._dragging = False
                self.releaseMouse()
            if self._pressed_btn is not None:
                self._pressed_btn = None
                self.update()

    def _update_slider_from_mouse(self, mouse_y: float):
        local_y = mouse_y - self._CANVAS_TOP
        thumb_top = max(0.0, min(local_y - self._THUMB_HALF, self._SLIDER_TRAVEL))
        self._thumb_top = thumb_top
        new_val = round(127.0 * (self._SLIDER_TRAVEL - thumb_top) / self._SLIDER_TRAVEL)
        if new_val != self._value:
            self._value = new_val
            self.slider_changed.emit(new_val)
        self.update()


class _CollapseBar(QWidget):
    """Thin vertical bar on the edge used to expand/collapse adjacent panels."""
    clicked = Signal()

    def __init__(self, right_side: bool = True, parent=None):
        super().__init__(parent)
        self.setFixedWidth(14)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._expanded = True
        self._right_side = right_side

    def set_expanded(self, expanded: bool):
        self._expanded = expanded
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(T.BG))
        p.setPen(QColor(T.BORDER))
        if self._right_side:
            p.drawLine(0, 0, 0, self.height())
        else:
            p.drawLine(self.width() - 1, 0, self.width() - 1, self.height())
        if self._right_side:
            arrow = "‹" if self._expanded else "›"
        else:
            arrow = "›" if self._expanded else "‹"
        p.setPen(QColor(T.TEXT))
        f = p.font()
        f.setPixelSize(T.FS_BODY)
        p.setFont(f)
        p.drawText(self.rect(), Qt.AlignCenter, arrow)
        p.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()


class _ShutdownOverlay(QWidget):
    """Translucent overlay covering the entire window during teardown."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setGeometry(parent.rect())
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 180))
        p.setPen(QColor(200, 200, 200))
        f = p.font()
        f.setPixelSize(20)
        p.setFont(f)
        p.drawText(self.rect(), Qt.AlignCenter, "Shutting down…")
        p.end()


def _log_file_path() -> pathlib.Path:
    return storage.data_dir() / "kronos_screen_remote.log"


def _setup_logging(debug: bool):
    level = logging.DEBUG if debug else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
              for h in root.handlers):
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        root.addHandler(console)
    if not any(isinstance(h, logging.FileHandler) for h in root.handlers):
        try:
            file_handler = logging.FileHandler(_log_file_path(), encoding="utf-8")
            file_handler.setFormatter(fmt)
            root.addHandler(file_handler)
        except OSError as e:
            print(f"[log] could not open log file: {e}")
    for h in root.handlers:
        h.setLevel(level)


def _verify_ftp_login(host: str, port: int, user: str, password: str) -> Tuple[bool, str]:
    """Attempt a real FTP login (connect + login + disconnect). Reuses
    file_manager's ftplib wrapper rather than a bespoke client."""
    from file_manager import _FtpWorker
    worker = _FtpWorker(host, port, user, password)
    try:
        worker.connect()
        worker.disconnect()
        return True, ""
    except Exception as e:
        return False, str(e)


class _FtpLoginDialog(QDialog):
    """Kronos FTP login prompt — port of Views/LoginDialog.xaml(.cs).

    Verifies the entered credentials against the real FTP server on a
    background thread before accepting, and locks out after 3 failed
    attempts (matches KronosFtpSession's attempt-lockout constant).
    """

    _ATTEMPTS_ALLOWED = 3

    def __init__(self, host: str, port: int, existing_user: str, existing_pass: str,
                 parent=None):
        super().__init__(parent)
        self._host = host
        self._port = port
        self._attempts_failed = 0
        self.username = ""
        self.password = ""
        self.save_password = True
        self.exhausted_attempts = False

        self.setWindowTitle("Kronos FTP Login")
        self.setFixedWidth(340)
        self.setStyleSheet(f"QDialog {{ background-color: {T.BG}; color: {T.TEXT}; }}")

        layout = QVBoxLayout(self)
        subtitle = QLabel(f"FTP credentials for {host}:{port}:")
        subtitle.setStyleSheet(f"color: {T.TEXT_DIM}; font-size: {T.FS_SMALL}px;")
        layout.addWidget(subtitle)

        form = QFormLayout()
        self._user_edit = QLineEdit(existing_user)
        self._user_edit.setPlaceholderText("root")
        self._pass_edit = QLineEdit(existing_pass)
        self._pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Username:", self._user_edit)
        form.addRow("Password:", self._pass_edit)
        layout.addLayout(form)

        self._save_chk = QCheckBox("Save password")
        self._save_chk.setChecked(True)
        layout.addWidget(self._save_chk)

        self._error_label = QLabel("")
        self._error_label.setStyleSheet(f"color: {T.ERROR_TEXT};")
        self._error_label.setWordWrap(True)
        self._error_label.setVisible(False)
        layout.addWidget(self._error_label)

        btn_row = QHBoxLayout()
        self._clear_btn = QPushButton("Clear")
        self._clear_btn.setToolTip("Clear both fields so you can enter fresh credentials")
        self._clear_btn.clicked.connect(self._on_clear)
        btn_row.addWidget(self._clear_btn)
        btn_row.addStretch(1)
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self.reject)
        self._ok_btn = QPushButton("OK")
        self._ok_btn.setDefault(True)
        self._ok_btn.clicked.connect(self._on_ok)
        btn_row.addWidget(self._cancel_btn)
        btn_row.addWidget(self._ok_btn)
        layout.addLayout(btn_row)

        if existing_user:
            self._pass_edit.setFocus()
        else:
            self._user_edit.setFocus()

    def _on_clear(self):
        self._user_edit.clear()
        self._pass_edit.clear()
        self._user_edit.setFocus()

    def _show_error(self, msg: str):
        self._error_label.setText(msg)
        self._error_label.setVisible(True)

    def _on_ok(self):
        user = self._user_edit.text().strip()
        if not user:
            self._show_error("Username is required.")
            return

        self._ok_btn.setEnabled(False)
        self._cancel_btn.setEnabled(False)
        self._ok_btn.setText("Verifying…")
        self._error_label.setVisible(False)

        password = self._pass_edit.text()

        def worker():
            ok, error = _verify_ftp_login(self._host, self._port, user, password)
            QTimer.singleShot(0, self, lambda: self._on_verify_done(ok, error, user, password))

        threading.Thread(target=worker, daemon=True, name="FtpVerify").start()

    def _on_verify_done(self, ok: bool, error: str, user: str, password: str):
        if ok:
            self.username = user
            self.password = password
            self.save_password = self._save_chk.isChecked()
            self.accept()
            return

        self._ok_btn.setEnabled(True)
        self._cancel_btn.setEnabled(True)
        self._ok_btn.setText("OK")

        self._attempts_failed += 1
        if self._attempts_failed >= self._ATTEMPTS_ALLOWED:
            self.exhausted_attempts = True
            self.reject()
            return

        remaining = self._ATTEMPTS_ALLOWED - self._attempts_failed
        plural = "s" if remaining != 1 else ""
        self._show_error(f"{error} ({remaining} attempt{plural} remaining)")


class MainWindow(QMainWindow):
    def __init__(self, settings: AppSettings):
        super().__init__()
        self._settings    = settings
        self._conn_state  = "disconnected"
        self._host        = settings.kronos_host
        self._ctrl_port   = settings.ctrl_port
        self._stream_port = settings.stream_port
        self._pull_mode   = settings.pull_mode
        self._fps         = settings.max_fps

        _setup_logging(settings.debug_logging)

        self._receiver: Optional[StreamReceiver] = None
        self._palette:  list[PaletteEntry] = []
        self._overrides = storage.load_overrides()
        self._locked    = storage.load_locks()
        self._raw_frame: Optional[bytes] = None

        self._mode_detector  = ModeDetector()
        self._combi_detector = CombiProgramEditDetector()
        self._boot_detector  = BootPhaseDetector()
        self._current_mode   = 0
        self._prev_mode      = 0
        self._pending_mode   = 0   # user-requested mode awaiting detection confirmation
        # True from a user-initiated mode change until detection confirms it —
        # suppresses the footer performance name flashing the pre-change mode's
        # identity while the switch's MIDI is still in flight.
        self._mode_switch_pending = False
        self._combi_prog_edit_active = False
        self._combi_edit_origin: int = 0   # 1 = from Combi, 2 = from Sequence (daemon EDITCTX)
        self._combi_prog_flash_state = False
        self._combi_exit_gone_at: float = 0.0
        self._daemon_state_ok = False      # became True on the first STATE response
        self._daemon_booting  = True       # fail-safe default until the first STATE poll
        self._help_active    = False
        self._kbd_capture   = False
        self._kbd_send_en   = True
        self._shift_held    = False

        # Held-key auto-repeat while forwarding to the Kronos. OS auto-repeat is
        # ignored (as before); this drives re-triggers ourselves so a held key
        # (e.g. backspace) repeats — 400 ms initial delay, then ~40 ms rate,
        # mirroring the C# repeat timer.
        self._kbd_repeat_code  = 0
        self._kbd_repeat_key   = 0
        self._kbd_repeat_phase = False
        self._kbd_repeat_timer = QTimer(self)
        self._kbd_repeat_timer.timeout.connect(self._on_kbd_repeat_tick)

        self._mode_poll_timer = QTimer(self)
        self._mode_poll_timer.setInterval(_MODE_POLL_INTERVAL_MS)
        self._mode_poll_timer.timeout.connect(self._poll_mode)

        self._combi_flash_timer = QTimer(self)
        self._combi_flash_timer.setInterval(420)
        self._combi_flash_timer.timeout.connect(self._combi_flash_tick)

        # Boot phase state
        self._boot_phase         = False
        self._detected_mode_ever = False
        self._boot_first_frame: float   = 0.0
        self._boot_phase_start: float   = 0.0
        self._preload_timer_start: float = 0.0
        self._bank_data_detected_at: float = 0.0
        self._boot_load_phase    = BootPhase.NONE
        self._finishing_fill_frac = _BOOT_F_STATIC_END
        self._preload_schedule: Optional[list[tuple[float, float]]] = None

        self._boot_anim_timer = QTimer(self)
        self._boot_anim_timer.setInterval(30)
        self._boot_anim_timer.timeout.connect(self._boot_anim_tick)

        self._fps_count   = 0
        self._fps_time    = time.monotonic()
        self._measured_fps = 0.0

        self._connecting  = False
        self._auto_reconnect_enabled = True   # False only when user explicitly disconnects
        self._poll_in_progress = False        # guard: only one STATE query at a time
        self._layout_preset = settings.layout_preset
        self._is_fullscreen = False

        self._zoom_on      = False
        self._zoom_level   = settings.zoom_default_level
        self._mirror_state = False
        self._perf_window  = None   # PerformanceWindow singleton (lazy)
        self._file_manager_win = None
        self._sysex_tool_win = None
        self._librarian_shell_win = None
        self._sysex_service = SysExService(self)
        self._shutting_down = False
        self._tray_icon = None   # QSystemTrayIcon, set up in _init_tray_icon

        # Ping
        self._ping_inflight = False
        self._ping_timer = QTimer(self)
        self._ping_timer.setInterval(3000)
        self._ping_timer.timeout.connect(self._ping_once)

        # MIDI footer activity (RX/TX flash, coalesced by a 50 ms dim timer)
        self._midi_rx_at = 0.0
        self._midi_tx_at = 0.0
        self._midi_dim_timer = QTimer(self)
        self._midi_dim_timer.setInterval(50)
        self._midi_dim_timer.timeout.connect(self._update_midi_dots)

        # Notification state
        self._notify_count = 0
        self._notify_msgs: list[str] = []

        # VU meter audio capture
        self._audio_capture = None
        self._vu_device_id: Optional[str] = None

        self._setup_ui()
        self._wire_actions()
        self._apply_settings_to_ui()

        # Release keyboard capture when a click lands outside the frame widget
        QApplication.instance().installEventFilter(self)

        # Load cal
        self._frame_w._cal_mesh, self._frame_w._cal_bias_dots = storage.load_cal()
        if not self._frame_w._cal_mesh.is_identity():
            print(f"[cal] mesh loaded, {len(self._frame_w._cal_bias_dots)} bias dot(s)")

        self._ctrl = CtrlClient.get()

    # ── UI construction ────────────────────────────────────────────────────────

    def _setup_ui(self):
        self.setWindowTitle(_APP_TITLE)
        self.resize(1882, 650)
        self.setStyleSheet("QMainWindow { background: black; }")
        if self._settings.always_on_top:
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)

        # Central widget with horizontal layout
        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Focused-mode value rail (left edge)
        self._value_rail = _CollapseBar(right_side=False)
        self._value_rail.setToolTip("Expand/collapse value input")
        self._value_rail.clicked.connect(self._toggle_focused_value_expand)
        self._value_rail.setVisible(False)
        layout.addWidget(self._value_rail)

        # Left value-slider panel (282 design units wide)
        self._left_panel = KronosValueSliderPanel()
        self._left_panel.button_pressed.connect(self._on_left_panel_button)
        self._left_panel.slider_changed.connect(self._on_vslider_changed)
        layout.addWidget(self._left_panel, 282)

        # Frame widget (800 design units wide)
        self._frame_w = FrameWidget()
        layout.addWidget(self._frame_w, 800)

        # Control surface (800 design units wide)
        self._ctrl_surface = KronosControlSurface()
        self._ctrl_surface._reverse_scroll = self._settings.reverse_scrolling
        self._ctrl_surface.button_pressed.connect(self._on_ctrl_button)
        self._ctrl_surface.button_released.connect(self._on_ctrl_button_released)
        self._ctrl_surface.wheel_step.connect(self._on_wheel_step)
        self._ctrl_surface.wheel_settings_requested.connect(
            lambda: self._open_settings(initial_tab="View"))
        layout.addWidget(self._ctrl_surface, 800)

        # Focused-mode data rail (right edge)
        self._data_rail = _CollapseBar(right_side=True)
        self._data_rail.setToolTip("Expand/collapse data input")
        self._data_rail.clicked.connect(self._toggle_focused_data_expand)
        self._data_rail.setVisible(False)
        layout.addWidget(self._data_rail)

        # Signals from frame widget
        self._frame_w.touch_down.connect(self._on_touch_down)
        self._frame_w.touch_move.connect(self._on_touch_move)
        self._frame_w.touch_up.connect(self._on_touch_up)
        self._frame_w.frame_clicked_for_kbd.connect(self._set_kbd_capture)

        # Status bar (base look comes from the app-wide stylesheet; only the
        # per-instance content margins are set here)
        self._status_bar = QStatusBar()
        self._status_bar.setContentsMargins(0, 0, 0, 0)
        self.setStatusBar(self._status_bar)

        def _sep():
            """Vertical divider — a 1px widget coloured via background (a QFrame
            VLine's colour is not reliably settable through the CSS `color`)."""
            s = QWidget()
            s.setFixedWidth(1)
            s.setFixedHeight(14)
            s.setStyleSheet(f"background-color: {T.BORDER};")
            return s

        # Symbol font set once, in code — a real QFont resolves the glyph (with
        # Qt fallback) reliably, unlike a QSS font-family list. setFont is not
        # wiped by later setStyleSheet("color: …") calls, so the size survives.
        icon_font = QFont(T.FONT_SYMBOL)
        icon_font.setPixelSize(T.FS_ICON)

        def _icon(glyph: str, color: str, tooltip: str, clickable: bool = False) -> QLabel:
            """A footer glyph icon: enlarged/one-font via QFont so it reads as an
            icon; colour set via stylesheet (dynamic setters change colour only)."""
            lbl = QLabel(glyph)
            lbl.setObjectName("footerIcon")
            lbl.setFont(icon_font)
            lbl.setStyleSheet(f"color: {color};")
            lbl.setToolTip(tooltip)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            if clickable:
                lbl.setCursor(Qt.CursorShape.PointingHandCursor)
            return lbl

        def _text(color: str, tooltip: str = "", min_w: int = 0) -> QLabel:
            """A footer text/value label at the status-bar body size. A fixed
            min-width stops neighbours jittering as the value changes."""
            lbl = QLabel("")
            lbl.setStyleSheet(f"color: {color}; font-size: {T.FS_SMALL}px;")
            if tooltip:
                lbl.setToolTip(tooltip)
            if min_w:
                lbl.setMinimumWidth(min_w)
                lbl.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
            return lbl

        # ── Left cluster: connection health + input icons ───────────────────
        # Everything left-justified except the VU meter (right, below).
        self._conn_dot = _StatusDot()
        self._status_label = QLabel("Not connected")
        self._status_label.setStyleSheet(f"color: {T.TEXT_DIM}; font-size: {T.FS_SMALL}px;")
        self._status_label.setContentsMargins(T.PAD, 0, 0, 0)

        self._fps_label = _text(T.TEXT_DIM, "Live stream frame rate", min_w=54)
        self._ping_label = _text(T.TEXT_IDLE, "Round-trip latency to Kronos", min_w=56)
        self._ping_label.setText("⇄ —")

        self._kbd_label = _icon("⌨", T.TEXT_DIM,
                                 "Keyboard capture — click in frame to capture")
        self._notify_label = _NotifyBubble()
        self._notify_label.setToolTip("No notifications")
        self._kbd_info_btn = _icon("▦", T.TEXT_IDLE,
                                   "Keyboard Info / Performance Meter", clickable=True)
        self._conn_mode_label = _text(T.TEXT_DIM, "Streaming mode", min_w=48)
        self._mode_label = _text(T.ACCENT, "Current Kronos mode", min_w=88)

        # MIDI link badge — "TCP" when the MIDI bridge (:9875) is connected, else "—".
        self._midi_badge = QLabel("—")
        self._midi_badge.setToolTip("Active MIDI link — TCP: network daemon (:9875)")
        self._midi_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._set_midi_badge(False)

        # MIDI activity — RX (↓ + green dot), TX (red dot + ↑); flash on traffic,
        # dim at rest. Arrow + dot recolor together (mirrors the C# SysEx dots).
        arrow_font = QFont(T.FONT_SYMBOL); arrow_font.setPixelSize(T.FS_H2)
        dot_font   = QFont(T.FONT_SYMBOL); dot_font.setPixelSize(T.FS_SMALL)
        self._midi_rx_arrow = QLabel("↓")
        self._midi_rx_dot   = QLabel("●")
        self._midi_tx_dot   = QLabel("●")
        self._midi_tx_arrow = QLabel("↑")
        _rx_tip = "MIDI received (client ← Kronos)"
        _tx_tip = "MIDI transmitted (client → Kronos)"
        for w in (self._midi_rx_arrow, self._midi_tx_arrow):
            w.setFont(arrow_font)
        for w in (self._midi_rx_dot, self._midi_tx_dot):
            w.setFont(dot_font)
        for w, dim, tip in ((self._midi_rx_arrow, T.MIDI_RX_DIM, _rx_tip),
                            (self._midi_rx_dot,   T.MIDI_RX_DIM, _rx_tip),
                            (self._midi_tx_dot,   T.MIDI_TX_DIM, _tx_tip),
                            (self._midi_tx_arrow, T.MIDI_TX_DIM, _tx_tip)):
            w.setAlignment(Qt.AlignmentFlag.AlignCenter)
            w.setStyleSheet(f"color: {dim};")
            w.setToolTip(tip)
        _midi_io = QWidget()
        _midi_io_lay = QHBoxLayout(_midi_io)
        _midi_io_lay.setContentsMargins(0, 0, 0, 0)
        _midi_io_lay.setSpacing(1)
        for w in (self._midi_rx_arrow, self._midi_rx_dot,
                  self._midi_tx_dot, self._midi_tx_arrow):
            _midi_io_lay.addWidget(w)

        # Current performance (bank/number/name via SysEx). No min-width — it can
        # be long, so it takes remaining space at the end of the cluster.
        self._perf_label = QLabel("")
        self._perf_label.setStyleSheet(f"color: {T.ACCENT}; font-size: {T.FS_SMALL}px;")
        self._perf_label.setToolTip("Current performance — bank, number and name (via SysEx)")

        self._status_bar.addWidget(self._conn_dot)
        self._status_bar.addWidget(self._status_label)
        for w in (_sep(), self._fps_label, _sep(), self._ping_label,
                  _sep(), self._midi_badge, _sep(), _midi_io,
                  _sep(), self._kbd_label, _sep(), self._notify_label,
                  _sep(), self._kbd_info_btn, _sep(), self._perf_label):
            self._status_bar.addWidget(w)

        # Right-cluster containers for ModeText / ConnModeText (C# declares them
        # right-docked, so they sit at the far right of the status bar).
        def _wrap(label: QLabel) -> QWidget:
            box = QWidget()
            lay = QHBoxLayout(box)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.addWidget(label)
            return box

        self._conn_mode_box = _wrap(self._conn_mode_label)
        self._mode_box = _wrap(self._mode_label)

        # ── Right cluster: sequencer transport + tap tempo (mirrors C# footer row) ──
        # Locate/Rewind/Fast-Forward/Pause/Record/Start only mean anything in Sequence
        # mode; REC/WRITE doubles as Save in Setlist/Combi/Program/Global. Tap Tempo is
        # global. Enabled whenever connected (no per-mode gating in this port).
        _seq_font = QFont(T.FONT_SYMBOL)
        _seq_font.setPixelSize(T.FS_SMALL)

        def _seq_btn(glyph: str, tooltip: str, action: str) -> QLabel:
            lbl = QLabel(glyph)
            lbl.setObjectName("footerIcon")
            lbl.setFont(_seq_font)
            lbl.setStyleSheet(f"color: {T.TEXT_DIM}; padding: 0 3px;")
            lbl.setToolTip(tooltip)
            lbl.setCursor(Qt.CursorShape.PointingHandCursor)
            lbl.mousePressEvent = lambda ev, a=action, l=lbl: (
                None if ev.button() != Qt.MouseButton.LeftButton
                else (self._ctrl_send(f"BUTTON {a}"),
                      l.setStyleSheet(f"color: {T.ACCENT}; padding: 0 3px;"),
                      QTimer.singleShot(180, lambda: l.setStyleSheet(
                          f"color: {T.TEXT_DIM}; padding: 0 3px;"))))
            return lbl

        def _seq_icon_btn(kind: str, tooltip: str, action: str) -> QLabel:
            # Painted vector icon, not an emoji glyph — 💾 (U+1F4BE) and the metronome
            # shape have no Segoe UI Symbol coverage, so Qt falls back to the system's
            # COLOR emoji font for them, which is why Save used to render as a colored,
            # mismatched glyph next to the monochrome transport icons around it. Painting
            # our own keeps every footer icon the same flat, single-color style.
            size = T.FS_SMALL
            lbl = QLabel()
            lbl.setObjectName("footerIcon")
            lbl.setPixmap(_paint_seq_icon(kind, T.TEXT_DIM, size))
            lbl.setToolTip(tooltip)
            lbl.setCursor(Qt.CursorShape.PointingHandCursor)
            lbl.mousePressEvent = lambda ev, a=action, l=lbl, k=kind, sz=size: (
                None if ev.button() != Qt.MouseButton.LeftButton
                else (self._ctrl_send(f"BUTTON {a}"),
                      l.setPixmap(_paint_seq_icon(k, T.ACCENT, sz)),
                      QTimer.singleShot(180, lambda: l.setPixmap(_paint_seq_icon(k, T.TEXT_DIM, sz)))))
            return lbl

        self._seq_locate = _seq_btn("⏮", "Locate — return to the locate point", "SEQ_LOCATE")
        self._seq_rew    = _seq_btn("◀◀", "Rewind (<<)", "SEQ_REW")
        self._seq_ff     = _seq_btn("▶▶", "Fast-forward (>>)", "SEQ_FF")
        self._seq_pause  = _seq_btn("⏸", "Pause", "SEQ_PAUSE")
        self._seq_rec    = _seq_btn("⏺", "Record", "SEQ_REC")
        self._seq_start  = _seq_btn("▶", "Start / Stop", "SEQ_START")
        self._tap_tempo_lbl = _seq_icon_btn("metronome",
            "Tap Tempo — one tap per press; the Kronos averages", "TAP_TEMPO")
        self._seq_save_lbl  = _seq_icon_btn("save",
            "Write / Save — REC/WRITE (Setlist/Combi/Program/Global)", "SEQ_REC")
        # Transport row is one widget so it can be faded/enabled as a unit (req 12).
        _seq_box = QWidget()
        _seq_lay = QHBoxLayout(_seq_box)
        _seq_lay.setContentsMargins(0, 0, 0, 0)
        _seq_lay.setSpacing(2)
        for w in (self._seq_locate, self._seq_rew, self._seq_ff, self._seq_pause,
                  self._seq_rec, self._seq_start):
            _seq_lay.addWidget(w)
        self._seq_box = _seq_box
        # Save (REC/WRITE) is its own item in the C# status bar (SeqSaveBarItem).
        _save_box = QWidget()
        _save_lay = QHBoxLayout(_save_box)
        _save_lay.setContentsMargins(0, 0, 0, 0)
        _save_lay.addWidget(self._seq_save_lbl)
        self._seq_save_box = _save_box
        # Tap Tempo is its own item too (TapTempoBarItem).
        _tap_box = QWidget()
        _tap_lay = QHBoxLayout(_tap_box)
        _tap_lay.setContentsMargins(0, 0, 0, 0)
        _tap_lay.addWidget(self._tap_tempo_lbl)
        self._tap_tempo_box = _tap_box

        # ── Right cluster: audio VU meter (the only right-justified item) ────
        from vu_meter import VuMeterWidget
        self._vu_widget = VuMeterWidget()
        self._vu_picker_btn = _icon("▾", T.TEXT_IDLE,
                                    "Select audio monitoring device", clickable=True)
        _vu_box = QWidget()
        _vu_lay = QHBoxLayout(_vu_box)
        _vu_lay.setContentsMargins(T.PAD_TIGHT, 0, T.PAD_TIGHT, 0)
        _vu_lay.setSpacing(T.PAD)
        _vu_lay.addWidget(self._vu_widget)
        _vu_lay.addWidget(self._vu_picker_btn)

        # C# right-aligned status bar order (left→right, per MainWindow.xaml's
        # DockPanel.Dock=Right declarations): Save | TapTempo | SeqTransport | VU |
        # ConnMode | ModeText. QStatusBar.addPermanentWidget stacks from the right, so
        # add rightmost-first to reproduce that order.
        self._status_bar.addPermanentWidget(self._mode_box)
        self._status_bar.addPermanentWidget(_sep())
        self._status_bar.addPermanentWidget(self._conn_mode_box)
        self._status_bar.addPermanentWidget(_sep())
        self._status_bar.addPermanentWidget(_vu_box)
        self._status_bar.addPermanentWidget(_sep())
        self._status_bar.addPermanentWidget(self._seq_box)
        self._status_bar.addPermanentWidget(_sep())
        self._status_bar.addPermanentWidget(self._tap_tempo_box)
        self._status_bar.addPermanentWidget(_sep())
        self._status_bar.addPermanentWidget(self._seq_save_box)

        self._build_menu()
        self._init_tray_icon()

    def _build_menu(self):
        # Menu bar mirrors the C# MainWindow.xaml layout exactly:
        #   File | Connection | View | Tools | Mode Select | Bank Select | Help
        # Accelerator mnemonics (&) copied from the XAML '_' positions.
        mb = self.menuBar()

        # ── File (MENU_File) ────────────────────────────────────────────────
        file_menu = mb.addMenu("&File")
        self._act_settings_dlg = file_menu.addAction("&Settings…")
        file_menu.addSeparator()
        self._act_import_settings = file_menu.addAction("&Import Settings…")
        self._act_export_settings = file_menu.addAction("&Export Settings…")
        file_menu.addSeparator()
        self._act_screenshot  = file_menu.addAction("Sa&ve Screenshot…")
        self._act_quick_save  = file_menu.addAction("Q&uick Save Screenshot")
        self._act_copy_frame  = file_menu.addAction("&Copy Frame to Clipboard")
        self._act_open_ss_dir = file_menu.addAction("Open Screenshots &Folder")
        file_menu.addSeparator()
        self._act_open_log    = file_menu.addAction("Open &Log File")
        file_menu.addSeparator()
        self._act_quit        = file_menu.addAction("&Quit")

        # ── Connection (MENU_Connection) ────────────────────────────────────
        conn_menu = mb.addMenu("&Connection")
        self._act_connect    = conn_menu.addAction("&Connect")
        self._act_disconnect = conn_menu.addAction("&Disconnect")
        self._act_disconnect.setEnabled(False)
        conn_menu.addSeparator()
        self._recent_menu = conn_menu.addMenu("&Recent Connections")
        self._rebuild_recent_menu()
        self._act_copy_ip = conn_menu.addAction("Copy &IP Address")
        conn_menu.addSeparator()
        self._act_file_mgr   = conn_menu.addAction("File &Manager…")

        # ── View (MENU_View) ────────────────────────────────────────────────
        view_menu = mb.addMenu("&View")
        self._act_zoom     = view_menu.addAction("&Zoom Window")
        self._act_zoom.setCheckable(True)
        view_menu.addSeparator()
        self._act_full     = view_menu.addAction("&Fullscreen")
        self._act_hide_data  = view_menu.addAction("Hide &Data Input")
        self._act_hide_data.setCheckable(True)
        self._act_hide_value = view_menu.addAction("Hide &Value Input")
        self._act_hide_value.setCheckable(True)
        self._act_on_top   = view_menu.addAction("Always on &Top")
        self._act_on_top.setCheckable(True)
        self._act_on_top.setChecked(self._settings.always_on_top)
        self._act_refresh  = view_menu.addAction("Re&fresh Display")
        view_menu.addSeparator()
        scaling_menu = view_menu.addMenu("Scaling &Quality")
        self._act_scale_sharp  = scaling_menu.addAction("&Sharp (crisp pixels)")
        self._act_scale_smooth = scaling_menu.addAction("S&mooth (bilinear)")
        self._act_scale_hq     = scaling_menu.addAction("&High Quality (default)")
        # Exclusive group so exactly one quality is checked at a time. Held on
        # self so it isn't garbage-collected.
        self._scale_group = QActionGroup(self)
        self._scale_group.setExclusive(True)
        for a in (self._act_scale_sharp, self._act_scale_smooth, self._act_scale_hq):
            a.setCheckable(True)
            self._scale_group.addAction(a)
        self._act_image_adjust = view_menu.addAction("&Image Adjustments…")
        view_menu.addSeparator()
        preset_menu = view_menu.addMenu("Layout &Preset")
        self._act_preset_full    = preset_menu.addAction("&Full")
        self._act_preset_focused = preset_menu.addAction("F&ocused")
        for a in (self._act_preset_full, self._act_preset_focused):
            a.setCheckable(True)
        view_menu.addSeparator()
        size_menu = view_menu.addMenu("Window &Size")
        self._act_sz = {}
        for label, scale in (("&Small (75%)", 0.75), ("&Normal (100%)", 1.0),
                              ("&Large (125%)", 1.25), ("&Extra Large (150%)", 1.50),
                              ("&Huge (200%)", 2.00)):
            a = size_menu.addAction(label)
            a.setCheckable(True)
            self._act_sz[scale] = a

        # ── Tools (MENU_Tools) ──────────────────────────────────────────────
        tools_menu = mb.addMenu("&Tools")
        self._act_palette = tools_menu.addAction("&Palette Editor")
        self._act_palette.setCheckable(True)
        self._act_palette.setVisible(False)  # hidden at runtime — matches C# MainWindow.xaml.cs:615
        self._act_cal     = tools_menu.addAction("&Calibration")
        self._act_cal.setCheckable(True)
        cal_grid_menu = tools_menu.addMenu("Calibration &Grid Size")
        self._act_grid = {}
        for n in (3, 4, 5):
            a = cal_grid_menu.addAction(f"&{n}×{n}")
            a.setCheckable(True)
            self._act_grid[n] = a
        self._act_grid[5].setChecked(True)
        tools_menu.addSeparator()
        self._act_sysex_tool = tools_menu.addAction("Open &SysEx Tool…")
        self._act_librarian_shell = tools_menu.addAction("&Librarian…")
        tools_menu.addSeparator()
        self._act_keyboard_info = tools_menu.addAction("&Keyboard Info…")
        self._act_test_mode   = tools_menu.addAction("Enter Kronos &Test Mode")
        tools_menu.addSeparator()
        self._act_disable_kbd = tools_menu.addAction("&Disable Keyboard Send")
        self._act_disable_kbd.setCheckable(True)
        self._act_paste_clipboard = tools_menu.addAction("&Paste Clipboard to Kronos")

        # ── Mode select (MENU_ModeSelect) ───────────────────────────────────
        mode_menu = mb.addMenu("&Mode Select")
        self._act_modes: list[QAction] = []
        for label in _MODE_MENU_LABELS[1:]:
            a = mode_menu.addAction(label)
            self._act_modes.append(a)

        # ── Bank select (MENU_BankSelect) ───────────────────────────────────
        bank_menu = mb.addMenu("Ban&k Select")
        self._build_bank_menu(bank_menu)

        # ── Help ────────────────────────────────────────────────────────────
        help_menu = mb.addMenu("&Help")
        self._act_show_help   = help_menu.addAction("&Show Help")
        self._act_cmd_palette = help_menu.addAction("&Command Palette")
        help_menu.addSeparator()
        self._act_about       = help_menu.addAction("&About…")

    def _build_bank_menu(self, menu: QMenu):
        """3 sub-menus (Internal/User/U-User), matching C#'s MainWindow.xaml.cs
        BuildBankSelectMenu — keeps the top Bank Select popup a 3-item list
        instead of one flat 21-item dropdown."""
        letters = "ABCDEFG"
        internal_menu = menu.addMenu("&Internal (A-G)")
        for letter in letters:
            a = internal_menu.addAction(letter)
            a.triggered.connect(lambda checked, l=letter: self._ctrl_send(f"BUTTON BANK_I{l}"))
        user_menu = menu.addMenu("&User (A-G)")
        for letter in letters:
            a = user_menu.addAction(letter)
            a.triggered.connect(lambda checked, l=letter: self._ctrl_send(f"BUTTON BANK_U{l}"))
        uuser_menu = menu.addMenu("Us&er (AA–GG)")
        for letter in letters:
            a = uuser_menu.addAction(f"{letter}{letter}")
            a.triggered.connect(lambda checked, l=letter: self._ctrl_send(f"CHORD BANK_U{l} BANK_I{l}"))

    # ── Action wiring ──────────────────────────────────────────────────────────

    def _wire_actions(self):
        self._act_copy_ip.triggered.connect(self._copy_ip_address)
        self._act_file_mgr.triggered.connect(self._open_file_manager)
        self._act_connect.triggered.connect(self._trigger_reconnect)
        self._act_refresh.triggered.connect(lambda: self._ctrl_send("REFRESH"))
        self._act_disconnect.triggered.connect(self._disconnect)
        self._act_quit.triggered.connect(self._try_quit)

        self._act_zoom.toggled.connect(self._on_zoom_toggled)
        self._act_full.triggered.connect(self._toggle_fullscreen)
        self._act_on_top.toggled.connect(self._on_always_on_top_toggled)
        self._act_hide_data.toggled.connect(self._toggle_hide_data_input)
        self._act_hide_value.toggled.connect(self._toggle_hide_value_input)

        self._act_preset_full.triggered.connect(lambda: self._apply_layout("Full"))
        self._act_preset_focused.triggered.connect(lambda: self._apply_layout("Focused"))

        for scale, act in self._act_sz.items():
            act.triggered.connect(lambda checked, s=scale: self._set_window_size(s))

        self._act_palette.toggled.connect(self._on_palette_toggled)
        self._act_cal.toggled.connect(self._on_cal_toggled)
        for n, act in self._act_grid.items():
            act.triggered.connect(lambda checked, s=n: self._set_cal_grid(s))
        self._act_test_mode.triggered.connect(self._enter_test_mode)
        self._act_quick_save.triggered.connect(self._quick_save_screenshot)
        self._act_screenshot.triggered.connect(self._save_screenshot)
        self._act_copy_frame.triggered.connect(self._copy_frame_to_clipboard)
        self._act_open_ss_dir.triggered.connect(self._open_screenshots_folder)
        self._act_open_log.triggered.connect(self._open_log_file)
        self._act_keyboard_info.triggered.connect(self._open_keyboard_info)
        self._act_sysex_tool.triggered.connect(self._open_sysex_tool)
        self._act_librarian_shell.triggered.connect(self._open_librarian_shell)
        self._act_disable_kbd.toggled.connect(self._on_disable_kbd_toggled)
        self._act_paste_clipboard.triggered.connect(self._paste_clipboard_to_kronos)

        # Footer MIDI indicators + performance name — connect ONCE to the stable
        # sysex_service (the underlying bridge is rebuilt on every reconnect).
        self._sysex_service.rx_activity.connect(self._on_midi_rx)
        self._sysex_service.tx_activity.connect(self._on_midi_tx)
        self._sysex_service.link_changed.connect(self._on_midi_link_changed)
        self._sysex_service.performance_changed.connect(self._on_performance_changed)
        # Live MIDI-stream mode hint (func 0x4E, decoded the instant it's seen -
        # see sysex_service's own docstring) - triggers an immediate STATE
        # re-poll instead of waiting up to _MODE_POLL_INTERVAL_MS for the next
        # periodic tick, so a hardware-side mode change (not just an on-screen
        # button press) reaches the mode buttons/label as fast as the
        # information actually arrives. STATE stays authoritative (it also
        # carries EDITCTX/BOOT, which this bare hint doesn't) - this only
        # moves the confirmation earlier, via _apply_daemon_state as usual.
        self._sysex_service.mode_changed.connect(self._on_sysex_mode_hint)
        # Seed from current state (in case events fired before this wiring).
        br = self._sysex_service.bridge
        self._set_midi_badge(bool(br and br.is_connected))
        self._on_performance_changed(self._sysex_service.performance_display)

        # Frame widget context menu
        self._frame_w.context_menu_requested.connect(self._show_frame_context_menu)

        # Status bar label context menus
        self._status_label.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._status_label.customContextMenuRequested.connect(self._show_status_context_menu)
        self._kbd_label.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._kbd_label.customContextMenuRequested.connect(self._show_kbd_context_menu)
        self._fps_label.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._fps_label.customContextMenuRequested.connect(self._show_fps_context_menu)
        self._mode_label.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._mode_label.customContextMenuRequested.connect(self._show_mode_context_menu)

        # Clickable status bar widgets (event filter handles mouse press)
        for w in (self._notify_label, self._kbd_info_btn, self._vu_picker_btn):
            w.installEventFilter(self)

        # Mode buttons
        for i, (act, cmd) in enumerate(zip(self._act_modes, _MODE_CMDS[1:]), 1):
            act.triggered.connect(lambda checked, c=cmd, m=i:
                                  (self._ctrl_send(f"BUTTON {c}"), self._set_pending_mode(m)))

        self._act_settings_dlg.triggered.connect(self._open_settings)
        self._act_show_help.triggered.connect(self._toggle_help)
        self._act_cmd_palette.triggered.connect(self._open_command_palette)
        self._act_about.triggered.connect(self._open_about)

        # New C#-parity menu items whose backing features are not implemented
        # yet. Wired to _todo so testing surfaces exactly which are missing;
        # each is filled in one at a time per the feature-parity plan.
        self._act_import_settings.triggered.connect(self._import_settings)
        self._act_export_settings.triggered.connect(self._export_settings)
        self._act_scale_sharp.triggered.connect(lambda: self._set_scale_quality("Sharp"))
        self._act_scale_smooth.triggered.connect(lambda: self._set_scale_quality("Smooth"))
        self._act_scale_hq.triggered.connect(lambda: self._set_scale_quality("HighQuality"))
        self._act_image_adjust.triggered.connect(lambda: self._open_settings(initial_tab="Image"))

    def _todo(self, feature: str):
        """Placeholder for C#-parity menu items not yet ported. Makes a missing
        feature unmistakable during testing so it can be implemented next."""
        logging.info("Menu feature not yet implemented: %s", feature)
        QMessageBox.information(
            self, "Not Implemented Yet",
            f"“{feature}” is on the menu for C# parity but isn’t wired up yet.\n\n"
            "It will be implemented in an upcoming step.")

    def _paste_clipboard_to_kronos(self):
        """Port of MainWindow.Input.cs's PasteClipboardToKronos()."""
        clipboard = QApplication.clipboard()
        raw = clipboard.text()
        if not raw:
            return

        chars: list[str] = []
        skipped = 0
        for c in raw:
            if c in ("\r", "\n") or (ord(c) < 0x20 and c != "\t") or ord(c) >= 0x80:
                skipped += 1
                continue
            if char_map.get_commands(c) is None:
                skipped += 1
                continue
            chars.append(c)

        if not chars:
            logging.info("[paste] nothing sendable after filtering%s",
                         f" ({skipped} chars stripped)" if skipped else "")
            return

        char_count = len(chars)
        logging.info("[paste] typing %d chars via KEY%s", char_count,
                     f", {skipped} stripped" if skipped else "")

        def worker():
            for c in chars:
                cmds = char_map.get_commands(c)
                if cmds is None:
                    continue
                for cmd in cmds:
                    self._ctrl_send(cmd)
                time.sleep(0.05)
            logging.info("[paste] %d chars typed", char_count)

        threading.Thread(target=worker, daemon=True, name="PasteClipboard").start()

    def _macro_select_all(self):
        """Port of MainWindow.Input.cs's MacroSelectAll(): End, then Shift+Home,
        as raw KEY press/release pairs — a "select all" for whatever text field
        the Kronos currently has focused, since it has no native select-all key."""
        end_code = key_map.to_linux(Qt.Key_End)
        home_code = key_map.to_linux(Qt.Key_Home)
        shift_code = key_map.to_linux(Qt.Key_Shift)
        if end_code:
            self._ctrl_send(f"KEY {end_code} 1")
            self._ctrl_send(f"KEY {end_code} 0")
        if shift_code and home_code:
            self._ctrl_send(f"KEY {shift_code} 1")
            self._ctrl_send(f"KEY {home_code} 1")
            self._ctrl_send(f"KEY {home_code} 0")
            self._ctrl_send(f"KEY {shift_code} 0")

    def _apply_settings_to_ui(self):
        focused = self._layout_preset == "Focused"
        self._act_preset_full.setChecked(not focused)
        self._act_preset_focused.setChecked(focused)
        self._apply_layout(self._layout_preset)
        self._apply_image_adjust()
        self._apply_scale_quality()

    def _apply_image_adjust(self):
        """Push the saved image-adjustment settings into the frame widget."""
        s = self._settings
        self._frame_w.set_image_adjust(
            s.image_brightness, s.image_contrast, s.image_gamma,
            s.image_saturation, s.image_sharpen)

    def _apply_scale_quality(self):
        """Reflect the saved scaling quality in the menu check + frame widget."""
        mode = self._settings.scaling_quality
        if mode not in ("Sharp", "Smooth", "HighQuality"):
            mode = "HighQuality"
        {"Sharp": self._act_scale_sharp, "Smooth": self._act_scale_smooth,
         "HighQuality": self._act_scale_hq}[mode].setChecked(True)
        self._frame_w._scale_mode = mode
        self._frame_w.update()

    def _set_scale_quality(self, mode: str):
        """User picked a scaling quality from the menu."""
        self._settings.scaling_quality = mode
        storage.save_settings(self._settings)
        {"Sharp": self._act_scale_sharp, "Smooth": self._act_scale_smooth,
         "HighQuality": self._act_scale_hq}[mode].setChecked(True)  # exclusive group unchecks others
        self._frame_w._scale_mode = mode
        self._frame_w.update()

    # ── Connection ─────────────────────────────────────────────────────────────

    def _trigger_reconnect(self):
        host = self._settings.kronos_host
        if not host:
            text, ok = QInputDialog.getText(self, "Connect", "Kronos host/IP:", text="192.168.100.15")
            if not ok or not text.strip():
                return
            host = text.strip()
            self._settings.kronos_host = host
            storage.save_settings(self._settings)
        self._host = host
        self._connect_async()

    def _ensure_ftp_credentials(self) -> bool:
        """Prompt for FTP credentials if not saved, verifying them against the
        real FTP server before accepting. Returns False if the user cancelled
        or exhausted the 3-attempt lockout."""
        if self._settings.ftp_username:
            return True
        dlg = _FtpLoginDialog(self._host, self._settings.ftp_port,
                              self._settings.ftp_username, self._settings.ftp_password, self)
        if dlg.exec() != QDialog.Accepted:
            if dlg.exhausted_attempts:
                QMessageBox.critical(self, "Authentication Failed",
                                     "Too many failed FTP login attempts.")
            return False
        self._settings.ftp_username = dlg.username
        self._settings.ftp_password = dlg.password
        if dlg.save_password:
            storage.save_settings(self._settings)
        return True

    def _connect_async(self):
        if self._connecting or not self._host:
            return
        if not self._ensure_ftp_credentials():
            return
        self._connecting = True
        self._auto_reconnect_enabled = True
        self._disconnect(quiet=True)
        self._set_conn_state("connecting", f"Connecting to {self._host}…")
        self._act_disconnect.setEnabled(False)
        threading.Thread(target=self._connect_bg, daemon=True, name="Connect").start()

    def _connect_bg(self):
        """Background: performs the blocking handshake; marshals result to main thread.

        QTimer.singleShot calls all pass `self` as the context object so Qt
        delivers the callback on the main thread's event loop, not the caller's
        (threading.Thread has no Qt event loop and the callback would never fire).
        """
        rx = StreamReceiver(self._host, self._stream_port, self._pull_mode, self._fps,
                            self._settings.ftp_username, self._settings.ftp_password)
        try:
            rx.connect_to_host()
        except PermissionError as e:
            rx.deleteLater()
            self._settings.ftp_username = ""
            self._settings.ftp_password = ""
            self._connecting = False
            QTimer.singleShot(0, self, lambda: storage.save_settings(self._settings))
            QTimer.singleShot(0, self, lambda: self._set_conn_state(
                "disconnected", "Authentication failed — re-enter credentials in Settings"))
            return
        except Exception as e:
            rx.deleteLater()
            self._connecting = False
            msg = str(e)
            QTimer.singleShot(0, self, lambda m=msg: self._set_conn_state(
                "disconnected", f"Connection failed: {m}"))
            return
        QTimer.singleShot(0, self, lambda: self._apply_new_receiver(rx))

    def _disconnect(self, quiet: bool = False):
        if not quiet:
            self._auto_reconnect_enabled = False  # explicit user disconnect — no auto-reconnect
        self._reset_boot_state()
        self._mode_poll_timer.stop()
        self._poll_in_progress = False
        if self._combi_prog_edit_active:
            self._combi_prog_edit_active = False
            self._combi_flash_timer.stop()
        self._current_mode = 0
        self._prev_mode    = 0
        self._pending_mode = 0
        self._mode_switch_pending = False   # so the perf-clear below isn't suppressed
        if self._receiver:
            self._receiver.dispose()
            self._receiver = None
        self._ctrl.reset()
        self._sysex_service.stop()
        self._stop_ping()
        # Reset footer MIDI/performance indicators. sysex_service.stop() clears
        # its state by direct field assignment (no signal), and bridge teardown
        # may not emit connection_changed(False), so reset here explicitly.
        self._on_performance_changed("")
        self._set_midi_badge(False)
        self._midi_rx_at = self._midi_tx_at = 0.0
        self._update_midi_dots()          # dims RX/TX + stops the dim timer
        self._frame_w._is_connected = False
        self._frame_w._frame_pixmap = None
        self._frame_w._frame_image  = None
        if not quiet:
            self._frame_w._disconnect_msg = "Disconnected"
        self._frame_w.update()
        self._act_disconnect.setEnabled(False)
        # Stop the Performance window's SYSINFO polling — the on-Kronos daemon
        # is fragile and must not be probed during boot/offline (matches C#'s
        # KeyboardInfoWindow isParentConnected guard).
        if self._perf_window:
            self._perf_window.update_host("", self._ctrl_port)
        if not quiet:
            self._set_conn_state("disconnected", "Disconnected")
            self.setWindowTitle(f"{_APP_TITLE} — disconnected")

    # ── Frame handling ─────────────────────────────────────────────────────────

    @Slot(bytes)
    def _on_frame(self, raw: bytes):
        self._raw_frame = raw
        self._frame_w.on_frame(raw, self._palette, self._overrides, self._locked)

        # FPS counter
        self._fps_count += 1
        now = time.monotonic()
        if now - self._fps_time >= 1.0:
            self._measured_fps = self._fps_count / (now - self._fps_time)
            self._fps_count    = 0
            self._fps_time     = now
            self._fps_label.setText(f"{self._measured_fps:.1f} fps")

        # Per-frame black checks
        mostly_black = is_frame_mostly_black(raw, self._frame_w._lut)
        likely_boot = is_frame_mostly_black(
            raw, self._frame_w._lut, self._settings.boot_screen_threshold / 100.0)
        self._frame_w._frame_is_likely_boot_screen = likely_boot

        # Pixel detection is only a FALLBACK (req 13): the daemon's STATE poll is the
        # authoritative mode/boot source. While the daemon is answering (or still
        # reporting BOOT=1) the pixel detectors are skipped entirely; they only run
        # when the daemon STATE path has never produced a reading (daemon process
        # missing / network to ctrl port failing).
        daemon_authoritative = self._daemon_state_ok
        if not mostly_black:
            # Help overlay is still pixel-detected (the daemon exposes no help signal).
            if self._mode_detector.has_any():
                help_now = self._mode_detector.is_help_active(raw, _FRAME_W, self._frame_w._lut)
                if help_now != self._help_active:
                    self._help_active = help_now
                    self._ctrl_surface.set_active("Help", help_now)

            if not daemon_authoritative:
                # Last-case fallback: daemon STATE never answered - use the client-side
                # pixel mode detector.
                if self._mode_detector.has_any():
                    mode = self._mode_detector.identify(raw, _FRAME_W, self._frame_w._lut)
                    if mode != self._current_mode and mode > 0:
                        self._set_mode_button(mode)

                # Combi program-edit indicator - checked every frame (outside top-left region)
                indicator = self._combi_detector.is_active(raw, _FRAME_W, self._frame_w._lut)
                if (not self._combi_prog_edit_active
                        and self._current_mode == 3
                        and self._prev_mode in (2, 0)
                        and indicator):
                    self._combi_exit_gone_at = 0.0
                    self._enter_combi_program_edit()
                elif self._combi_prog_edit_active:
                    if self._current_mode != 3:
                        self._combi_exit_gone_at = 0.0
                        self._exit_combi_program_edit()
                    elif not indicator:
                        now = time.monotonic()
                        if self._combi_exit_gone_at == 0.0:
                            self._combi_exit_gone_at = now
                        elif now - self._combi_exit_gone_at >= 1.5:
                            self._combi_exit_gone_at = 0.0
                            self._exit_combi_program_edit()
                    else:
                        self._combi_exit_gone_at = 0.0

        # Boot phase entry (req 14): while the daemon reports BOOT=1 (or hasn't answered
        # yet) keep the boot splash up; pixel black-detection is the fallback path only.
        if self._boot_first_frame == 0.0:
            self._boot_first_frame = time.monotonic()
        if (not self._detected_mode_ever and not self._boot_phase
                and self._boot_first_frame > 0
                and time.monotonic() - self._boot_first_frame >= _BOOT_ENTRY_DELAY):
            if self._daemon_booting or not daemon_authoritative:
                self._enter_boot_phase()

        # Boot load-phase detection - advance phases strictly forward (fallback only;
        # the daemon's own progress bar is composited server-side into the stream).
        if self._boot_phase and not daemon_authoritative:
            detected = self._boot_detector.identify(raw, _FRAME_W, self._frame_w._lut)
            if (detected == BootPhase.FINISHING
                    and self._boot_load_phase < BootPhase.FINISHING):
                self._finishing_fill_frac = self._compute_boot_fill_fraction()
                self._boot_load_phase = BootPhase.FINISHING
            elif (detected == BootPhase.BANK_DATA
                    and self._boot_load_phase < BootPhase.BANK_DATA):
                self._boot_load_phase = BootPhase.BANK_DATA
                self._bank_data_detected_at = time.monotonic()
            elif (detected == BootPhase.PRELOAD_KSC
                    and self._boot_load_phase < BootPhase.PRELOAD_KSC):
                self._boot_load_phase = BootPhase.PRELOAD_KSC

    @Slot()
    def _on_disconnected(self):
        self._reset_boot_state()
        self._frame_w._is_connected    = False
        self._frame_w._frame_pixmap    = None
        self._frame_w._disconnect_msg  = "Connection lost"
        self._frame_w.update()
        self._mode_poll_timer.stop()
        self._poll_in_progress = False
        self._stop_ping()
        if self._combi_prog_edit_active:
            self._combi_prog_edit_active = False
            self._combi_flash_timer.stop()
        self._act_disconnect.setEnabled(False)
        # Stop the Performance window's SYSINFO polling while offline/reconnecting
        # — the on-Kronos daemon is fragile and must not be probed during
        # boot/offline (matches C#'s KeyboardInfoWindow isParentConnected guard).
        if self._perf_window:
            self._perf_window.update_host("", self._ctrl_port)
        # Clear receiver so _schedule_reconnect can proceed and _connect_async
        # guard works correctly.  The QThread has already exited (disconnected
        # is emitted from the run() finally block) so no dispose() needed here.
        self._receiver = None
        if self._auto_reconnect_enabled and self._host:
            self._set_conn_state("connecting", "Connection lost — reconnecting in 3 s…")
            self.setWindowTitle(f"{_APP_TITLE} — reconnecting")
            QTimer.singleShot(3000, self._schedule_reconnect)
        else:
            self._set_conn_state("disconnected", "Connection lost")
            self.setWindowTitle(f"{_APP_TITLE} — disconnected")

    def _schedule_reconnect(self):
        """Called 3 s after an unexpected disconnect; tries to reconnect in a background thread."""
        if self._receiver or self._connecting or not self._host or not self._auto_reconnect_enabled:
            return
        self._connecting = True
        self._set_conn_state("connecting", f"Reconnecting to {self._host}…")
        threading.Thread(target=self._reconnect_bg, daemon=True, name="Reconnect").start()

    def _reconnect_bg(self):
        """Background: performs the blocking handshake; marshals result to main thread."""
        rx = StreamReceiver(self._host, self._stream_port, self._pull_mode, self._fps,
                            self._settings.ftp_username, self._settings.ftp_password)
        try:
            rx.connect_to_host()
        except PermissionError:
            rx.deleteLater()
            self._settings.ftp_username = ""
            self._settings.ftp_password = ""
            self._connecting = False
            QTimer.singleShot(0, self, lambda: storage.save_settings(self._settings))
            QTimer.singleShot(0, self, lambda: self._set_conn_state(
                "disconnected", "Auth failed — re-enter credentials in Settings then reconnect"))
            return
        except Exception:
            rx.deleteLater()
            self._connecting = False
            QTimer.singleShot(0, self, lambda: self._set_conn_state(
                "connecting", "Reconnect failed — retrying in 5 s…"))
            QTimer.singleShot(5000, self, self._schedule_reconnect)
            return
        QTimer.singleShot(0, self, lambda: self._apply_new_receiver(rx))

    def _apply_new_receiver(self, rx: StreamReceiver):
        """Main thread: wire up the newly connected receiver (after a successful connect)."""
        print(f"[connect] _apply_new_receiver called, _receiver already set={self._receiver is not None}")
        self._connecting = False
        if self._receiver:  # user already reconnected manually between the two calls
            print("[connect] _apply_new_receiver: receiver already set — disposing new rx (causes 'client disconnected' on daemon)")
            rx.dispose()
            return
        self._receiver = rx
        self._palette  = list(rx.palette)
        self._frame_w._palette = self._palette
        rx.frame_received.connect(self._on_frame)
        rx.disconnected.connect(self._on_disconnected)
        rx.start()
        self._act_disconnect.setEnabled(True)
        self._set_conn_state("connected", f"Connected — {self._host}")
        self.setWindowTitle(f"{_APP_TITLE} — {self._host}")
        self._add_recent_host(self._host)
        self._reset_boot_state()
        self._frame_w._is_connected  = True
        self._frame_w._disable_boot_screen = self._settings.disable_boot_screen
        self._frame_w._frame_is_likely_boot_screen = True
        self._frame_w._disconnect_msg = ""
        self._frame_w.update()
        self._mode_poll_timer.start()
        self._update_conn_mode_label()
        self._start_ping()
        # Push mirror state and screensaver timeout to daemon on every connect
        self._mirror_state = self._settings.vga_mirror_enabled
        self._ctrl_send("MIRROR_ON" if self._mirror_state else "MIRROR_OFF")
        self._ctrl_send(f"SS_TIMEOUT {self._settings.screensaver_timeout}")
        # MIDI bridge (SysEx tool / Set List viewer / name caching) — port 9875
        if self._settings.midi_monitor_enabled:
            self._sysex_service.start(self._host)
        # Update perf window if open
        if self._perf_window:
            self._perf_window.update_host(self._host, self._ctrl_port)

    def _set_pending_mode(self, mode: int):
        """Record a user-requested mode without lighting the button immediately.
        Detection (daemon STATE, or pixel fallback) is authoritative; this falls back
        after 3 seconds. Mode changes are ignored until the board is verified booted
        (a real mode has been confirmed) - C# SendMode's own gate: the front panel
        ignores mode keys during boot anyway, and a pending mode whose timeout fires
        later would light the wrong button."""
        if not self._detected_mode_ever or self._daemon_booting:
            return
        self._pending_mode = mode
        # Boost UI responsiveness: fetch STATE soon instead of waiting up to
        # _MODE_POLL_INTERVAL_MS for the next periodic tick. api.md says a BUTTON
        # mode-select "update[s] the daemon's internal mode state so STATE queries
        # reflect the change immediately" - get_mode_state() prefers a live
        # eva_mode.ko read, which re-reads OA's real mode state that
        # HandleSwitchEvent (BUTTON's handler) just mutated. That's server-asserted,
        # not verified against hardware here, so this isn't fired with zero delay:
        # a short wait gives OA's own event handling room to settle before the poll,
        # while still landing in ~1/10th the time the periodic tick would take. If
        # it's still stale, the periodic timer remains the backstop.
        QTimer.singleShot(80, self._poll_mode)

    def _pending_mode_timeout(self, mode: int):
        """Fallback: if detection never confirmed within 3 s, apply the pending mode."""
        if self._pending_mode == mode:
            self._set_mode_button(mode)

    def _set_mode_button(self, mode: int):
        self._pending_mode = 0   # detection is authoritative — clear any pending request
        self._mode_switch_pending = False   # switch complete → allow perf updates again
        if mode != self._current_mode:
            self._prev_mode = self._current_mode
            # Mode changed → re-query the current performance identity so the
            # footer shows the NEW mode's program/combi, not the previous mode's
            # (a bank/patch switch alone sends a Program Change and updates it via
            # the stream; a bare mode switch does not). Mirrors C# RefreshNow().
            self._sysex_service.refresh_now()
        self._current_mode = mode
        self._detected_mode_ever = True
        if self._boot_phase:
            self._exit_boot_phase()
        self._ctrl_surface.set_mode(mode)
        self._mode_label.setText(_MODE_NAMES[mode] if 1 <= mode <= 7 else "")
        self._update_seq_enabled()

    # ── Boot phase ────────────────────────────────────────────────────────────

    def _reset_boot_state(self):
        self._boot_phase = False
        self._detected_mode_ever = False
        self._daemon_state_ok = False
        self._daemon_booting = True
        self._boot_first_frame = 0.0
        self._boot_phase_start = 0.0
        self._preload_timer_start = 0.0
        self._bank_data_detected_at = 0.0
        self._boot_load_phase = BootPhase.NONE
        self._finishing_fill_frac = _BOOT_F_STATIC_END
        self._preload_schedule = None
        self._boot_anim_timer.stop()
        self._frame_w._boot_phase = False
        self._frame_w._daemon_authoritative = False
        self._frame_w._boot_fill_fraction = 0.0
        self._frame_w._frame_is_likely_boot_screen = False

    def _enter_boot_phase(self):
        self._boot_phase = True
        self._boot_phase_start = time.monotonic()
        self._preload_timer_start = time.monotonic()
        self._boot_load_phase = BootPhase.NONE
        self._finishing_fill_frac = _BOOT_F_STATIC_END
        self._build_preload_schedule()
        self._frame_w._boot_phase = True
        self._boot_anim_timer.start()
        self._boot_anim_tick()

    def _exit_boot_phase(self):
        self._boot_phase = False
        self._boot_anim_timer.stop()
        self._frame_w._boot_phase = False
        self._frame_w.update()

    def _build_preload_schedule(self):
        import random
        pause_count = 25
        active_total = 20.0
        pause_duration = 1.0
        pts = sorted(random.random() * active_total for _ in range(pause_count))
        segs: list[tuple[float, float]] = []
        wall = 0.0
        prog = 0.0
        for p in pts:
            active = p - prog
            if active > 1e-9:
                wall += active
                prog += active
                segs.append((wall, prog))
            wall += pause_duration
            segs.append((wall, prog))
        tail = active_total - prog
        if tail > 1e-9:
            wall += tail
            prog = active_total
            segs.append((wall, prog))
        self._preload_schedule = segs

    def _get_preload_progress(self, elapsed: float) -> float:
        if self._preload_schedule is None:
            return max(0.0, min(1.0, elapsed / 20.0))
        prev_wall = 0.0
        prev_prog = 0.0
        for wall_end, prog_end in self._preload_schedule:
            if elapsed <= wall_end:
                wall_span = wall_end - prev_wall
                prog_span = prog_end - prev_prog
                if prog_span < 1e-9 or wall_span < 1e-9:
                    return prev_prog / 20.0
                return (prev_prog + (elapsed - prev_wall) / wall_span * prog_span) / 20.0
            prev_wall = wall_end
            prev_prog = prog_end
        return 1.0

    def _compute_boot_fill_fraction(self) -> float:
        if self._boot_load_phase == BootPhase.FINISHING:
            return self._finishing_fill_frac
        if self._boot_load_phase == BootPhase.BANK_DATA and self._bank_data_detected_at > 0:
            t = max(0.0, min(1.0, (time.monotonic() - self._bank_data_detected_at) / 5.0))
            raw = _BOOT_F_BANK_START + (_BOOT_F_BANK_END - _BOOT_F_BANK_START) * t
        elif self._preload_timer_start > 0:
            elapsed = time.monotonic() - self._preload_timer_start
            t = max(0.0, min(1.0, self._get_preload_progress(elapsed)))
            raw = _BOOT_F_STATIC_END + (_BOOT_F_PRELOAD_END - _BOOT_F_STATIC_END) * t
        else:
            raw = _BOOT_F_STATIC_END
        snapped = math.floor(raw / 0.01) * 0.01
        return max(snapped, _BOOT_F_STATIC_END)

    def _boot_anim_tick(self):
        if not self._boot_phase:
            self._boot_anim_timer.stop()
            return
        self._frame_w._boot_fill_fraction = self._compute_boot_fill_fraction()
        self._frame_w.update()

    def _combi_flash_tick(self):
        if not self._combi_prog_edit_active:
            self._combi_flash_timer.stop()
            return
        self._combi_prog_flash_state = not self._combi_prog_flash_state
        self._ctrl_surface.set_active("Program", self._combi_prog_flash_state)

    def _enter_combi_program_edit(self):
        """Pixel-fallback entry for program-edit-from-Combi (kept for when the daemon
        STATE path is unavailable)."""
        self._enter_program_edit_context(1)

    def _exit_combi_program_edit(self):
        self._combi_prog_edit_active = False
        self._combi_edit_origin = 0
        self._combi_flash_timer.stop()
        # Re-light whichever mode is actually current
        self._ctrl_surface.set_mode(self._current_mode)
        self._mode_label.setText(_MODE_NAMES[self._current_mode]
                                 if 1 <= self._current_mode <= 7 else "")
        self._update_seq_enabled()
        print("[mode] combi program-edit: exited")

    @Slot(int)
    def _on_sysex_mode_hint(self, _mode: int):
        """sysex_service.mode_changed fired (live MIDI-stream 0x4E decode) - pull STATE
        now rather than waiting for the next periodic tick. The signal's own mode value
        isn't used directly (no EDITCTX/BOOT), it's purely a "check now" trigger; STATE
        via _apply_daemon_state remains the sole source of truth for what gets applied."""
        self._poll_mode()

    def _poll_mode(self):
        """Daemon-first mode + boot detection (req 13/14): the daemon's STATE command is
        the authoritative source (its eva_mode.ko reading is exact per call, per
        KronosScreenRemoteDaemon/docs/api.md). The client-side pixel detectors are only a
        last-case fallback when STATE is unreachable or returns no mode (e.g. the daemon
        process is missing)."""
        if not self._host or not self._receiver:
            return
        if self._poll_in_progress:
            return  # previous query still in flight - skip this tick
        self._poll_in_progress = True
        host = self._host
        port = self._ctrl_port
        threading.Thread(target=self._poll_mode_bg, args=(host, port),
                         daemon=True, name="ModePoll").start()

    def _poll_mode_bg(self, host: str, port: int):
        try:
            resp = self._ctrl.query_state(host, port, timeout_ms=800)
            if not resp:
                self._daemon_state_ok = False
                return
            mode = 0
            edit_ctx = 0
            boot = 1
            for part in resp.split():
                if part.startswith("MODE="):
                    try:
                        mode = int(part[5:])
                    except ValueError:
                        mode = 0
                elif part.startswith("EDITCTX="):
                    try:
                        edit_ctx = int(part[8:])
                    except ValueError:
                        edit_ctx = 0
                elif part.startswith("BOOT="):
                    try:
                        boot = int(part[5:])
                    except ValueError:
                        boot = 1
            QTimer.singleShot(0, self, lambda m=mode, e=edit_ctx, b=boot:
                              self._apply_daemon_state(m, e, b))
        finally:
            self._poll_in_progress = False

    def _apply_daemon_state(self, mode: int, edit_ctx: int, boot: int) -> None:
        """Port of MainWindow.Streaming.cs's ApplyDaemonState + boot gate handling:
        BOOT=1 keeps the boot phase up (daemon's own authoritative gate); MODE/EDITCTX
        only apply once boot clears. EDITCTX (program-edit-from-Combi/Sequence) drives
        the flashing-Program/origin-button state."""
        if self._host is None or self._receiver is None:
            return
        self._daemon_state_ok = True
        self._frame_w._daemon_authoritative = True
        daemon_booting = boot != 0
        if daemon_booting:
            self._daemon_booting = True
            if not self._boot_phase and not self._detected_mode_ever:
                self._enter_boot_phase()
            return
        self._daemon_booting = False
        if self._boot_phase:
            self._exit_boot_phase()
        if edit_ctx != 0:
            # Program-edit-from-Combi (1) / -from-Sequence (2) - drive the flashing state.
            ctx = 1 if edit_ctx == 1 else 2
            if mode != self._current_mode and mode != 0:
                self._set_mode_button(mode)   # keep _current_mode bookkeeping correct
            if not self._combi_prog_edit_active or self._combi_edit_origin != ctx:
                self._enter_program_edit_context(ctx)
        else:
            if self._combi_prog_edit_active:
                self._exit_combi_program_edit()
            # STATE is authoritative and exact - apply it as soon as it disagrees with
            # what's currently shown, so a button press lights up as fast as the daemon
            # confirms the change (one poll interval, not an added grace).
            if mode != 0 and mode != self._current_mode:
                self._set_mode_button(mode)
            elif mode == 0 and not self._detected_mode_ever:
                # Daemon says unknown and nothing detected yet - keep the boot phase up.
                if not self._boot_phase:
                    self._enter_boot_phase()

    def _enter_program_edit_context(self, ctx: int) -> None:
        """Enter a program-edit-from-Combi/Sequence context: the origin mode button lights
        and the Program button flashes (C# EnterProgramEditContext)."""
        self._combi_prog_edit_active = True
        self._combi_edit_origin = ctx
        self._combi_prog_flash_state = False
        self._ctrl_surface.set_active("Combi", ctx == 1)
        self._ctrl_surface.set_active("Sequence", ctx == 2)
        self._ctrl_surface.set_active("Program", False)
        self._combi_flash_timer.start()
        self._mode_label.setText("Mode: Program (from "
                                 + ("Combi" if ctx == 1 else "Sequence") + ")")
        self._update_seq_enabled()
        print(f"[mode] program-edit-from-{'Combi' if ctx == 1 else 'Sequence'}: entered")

    # ── Touch ──────────────────────────────────────────────────────────────────

    def _ctrl_send(self, cmd: str):
        if self._host:
            self._ctrl.send(self._host, self._ctrl_port, cmd)

    @Slot(int, int)
    def _on_touch_down(self, nx: int, ny: int):
        self._ctrl_send(f"TOUCH_DOWN {nx} {ny}")

    @Slot(int, int)
    def _on_touch_move(self, nx: int, ny: int):
        self._ctrl_send(f"TOUCH_MOVE {nx} {ny}")

    @Slot(int, int)
    def _on_touch_up(self, nx: int, ny: int):
        self._ctrl_send(f"TOUCH_UP {nx} {ny}")

    # ── Control surface ────────────────────────────────────────────────────────

    @Slot(str)
    def _on_ctrl_button(self, name: str):
        entry = _CTRL_BTN_CMD.get(name)
        if not entry:
            return
        cmd, mode = entry
        if mode > 0:
            # Mode buttons act on mouse-UP (see _on_ctrl_button_released) so the
            # footer shows only the outcome, not the pre-change mode's identity
            # flashing while the switch is in flight. Re-light the current mode:
            # the surface's radio group unlit it on press, and a drag-off cancel
            # would otherwise strand it dark until the next real mode change.
            self._ctrl_surface.set_mode(self._current_mode)
            return
        self._ctrl_send(cmd)

    def _on_ctrl_button_released(self, name: str):
        entry = _CTRL_BTN_CMD.get(name)
        if not entry:
            return
        cmd, mode = entry
        if mode > 0:
            self._ctrl_send(cmd)
            self._set_pending_mode(mode)

    @Slot(int)
    def _on_wheel_step(self, delta: int):
        if delta > 0:
            for _ in range(delta):
                self._ctrl_send("WHEEL CW")
        else:
            for _ in range(-delta):
                self._ctrl_send("WHEEL CCW")
        # Animate the wheel widget (direction: +1=CW, -1=CCW)
        self._ctrl_surface.trigger_wheel_anim(1 if delta > 0 else -1)

    # ── Left panel (value slider) ─────────────────────────────────────────────

    def _on_left_panel_button(self, name: str):
        self._ctrl_send(f"BUTTON {name}")

    def _on_vslider_changed(self, value: int):
        self._ctrl_send(f"VSLIDER {value}")

    def _show_left_panel(self, show: bool):
        self._left_panel.setVisible(show)

    # ── Keyboard ───────────────────────────────────────────────────────────────

    @Slot()
    def _set_kbd_capture(self):
        self._kbd_capture = True
        self._update_kbd_indicator()

    def _release_kbd_capture(self):
        self._kbd_capture = False
        self._stop_kbd_repeat()   # never leave a repeat running after capture ends
        self._update_kbd_indicator()

    def _update_kbd_indicator(self):
        # Colour only — size/font come from the QLabel#footerIcon app rule.
        if not self._kbd_send_en:
            self._kbd_label.setStyleSheet(f"color: {T.ERROR};")
            self._kbd_label.setToolTip("Keyboard send disabled")
        elif self._kbd_capture:
            self._kbd_label.setStyleSheet(f"color: {T.OK};")
            self._kbd_label.setToolTip("Keyboard captured — keys forwarded to Kronos")
        else:
            self._kbd_label.setStyleSheet(f"color: {T.TEXT_DIM};")
            self._kbd_label.setToolTip("Keyboard capture — click in frame to capture")

    def keyPressEvent(self, event: QKeyEvent):
        key  = event.key()
        mods = event.modifiers()

        if event.isAutoRepeat():
            return

        # ── Kronos capture mode ─────────────────────────────────────────────
        # eventFilter already forwarded this key and consumed it; this branch
        # is a belt-and-suspenders fallback in case a key event slips through.
        # Ctrl+V/Ctrl+A are local actions even while capturing (C# gates them on
        # capture+send-enabled rather than forwarding them as raw keystrokes) —
        # eventFilter already let these two through, so fall into the normal
        # Ctrl-shortcut handling below instead of being swallowed here too.
        if self._kbd_capture and not (
                (mods & Qt.ControlModifier) and key in (Qt.Key_V, Qt.Key_A)):
            if self._kbd_send_en:
                self._forward_key(event, pressed=True)
            return  # always block local shortcuts when captured

        # ── Local UI shortcuts (only active when capture is off) ────────────

        # Palette editor gets first crack at keys
        if self._frame_w.palette_key(event):
            return

        # Calibration mode — handled by eventFilter, but belt-and-suspenders
        if self._frame_w._cal_mode:
            self._handle_cal_key(event)
            return

        # Escape: exit fullscreen → send BUTTON EXIT
        if key == Qt.Key_Escape:
            if self._is_fullscreen:
                self._toggle_fullscreen()
                return
            self._ctrl_send("BUTTON EXIT")
            return

        # Ctrl shortcuts
        if mods & Qt.ControlModifier:
            if key == Qt.Key_S:
                self._save_screenshot(); return
            if key == Qt.Key_K:
                self._open_command_palette(); return
            if key == Qt.Key_V and self._kbd_capture and self._kbd_send_en:
                self._paste_clipboard_to_kronos(); return
            if key in _WINDOW_SIZE_CTRL_KEYS:
                self._set_window_size(_WINDOW_SIZE_CTRL_KEYS[key]); return
            if key == Qt.Key_A and self._kbd_capture and self._kbd_send_en:
                self._macro_select_all(); return

        # Macro trigger check (triggers must include a modifier)
        macro = self._settings.get_macro_for_trigger(key, _mods_to_int(mods))
        if macro:
            threading.Thread(target=self._play_macro, args=(macro,), daemon=True).start()
            return

        # Numpad always routes to control surface regardless of capture mode
        name = _numpad_btn(key, mods)
        if name:
            self._ctrl_send(f"BUTTON {name}")
            self._ctrl_surface.press_button(name)
            return

        # Enter → BUTTON ENTER (main-keyboard Enter, not numpad)
        if key in (Qt.Key_Return, Qt.Key_Enter) and not (mods & Qt.KeypadModifier):
            self._ctrl_send("BUTTON ENTER")
            return

        # ~ (tilde/backtick) — show/hide the menu bar while fullscreen. Hardcoded,
        # not rebindable, matching the C# client's MainWindow.Input.cs.
        if key in (Qt.Key_QuoteLeft, Qt.Key_AsciiTilde) and self._is_fullscreen:
            mb = self.menuBar()
            mb.setVisible(not mb.isVisible())
            return

        # Global shortcuts
        if not (mods & Qt.ControlModifier):
            if self._matches_keybind(event, "Quit"):
                self._try_quit(); return
            if self._matches_keybind(event, "Zoom In"):
                self._zoom_step(+0.5); return
            if self._matches_keybind(event, "Zoom Out"):
                self._zoom_step(-0.5); return
            if self._matches_keybind(event, "Fullscreen"):
                self._toggle_fullscreen(); return
            if self._matches_keybind(event, "Zoom Window"):
                self._act_zoom.setChecked(not self._act_zoom.isChecked()); return
            if self._matches_keybind(event, "Mirror"):
                self._toggle_mirror(); return
            if self._matches_keybind(event, "Help"):
                self._toggle_help(); return
            if self._matches_keybind(event, "Calibrate"):
                self._act_cal.setChecked(not self._act_cal.isChecked()); return
            if self._matches_keybind(event, "HideDataInput"):
                self._toggle_hide_data_input(not self._settings.hide_data_input); return
            if self._matches_keybind(event, "HideValueInput"):
                self._toggle_hide_value_input(not self._settings.hide_value_input); return

        # Mode select keybinds
        for i in range(1, 8):
            if self._matches_keybind(event, f"Mode {_MODE_NAMES[i]}"):
                self._ctrl_send(f"BUTTON {_MODE_CMDS[i]}")
                self._set_pending_mode(i)
                return

        # Bank select keybinds (unassigned by default — see REBINDABLE_DEFS)
        for action, _label, _key in get_rebindable():
            bank_cmd = _bank_action_cmd(action)
            if bank_cmd and self._matches_keybind(event, action):
                self._ctrl_send(bank_cmd)
                return

        # Sequencer transport keybinds (unassigned by default — see REBINDABLE_DEFS)
        seq = {"Seq Locate": "BUTTON SEQ_LOCATE", "Seq Rewind": "BUTTON SEQ_REW",
               "Seq Forward": "BUTTON SEQ_FF", "Seq Pause": "BUTTON SEQ_PAUSE",
               "Seq Record": "BUTTON SEQ_REC", "Seq Start": "BUTTON SEQ_START",
               "Seq Save": "BUTTON SEQ_REC"}   # Save fires the same REC/WRITE press
        for action, cmd in seq.items():
            if self._matches_keybind(event, action):
                self._ctrl_send(cmd)
                return
        if self._matches_keybind(event, "Tap Tempo"):
            self._ctrl_send("BUTTON TAP_TEMPO")
            self._flash_tap_tempo()
            return

    def keyReleaseEvent(self, event: QKeyEvent):
        if event.isAutoRepeat():
            return
        if self._kbd_capture:
            if self._kbd_send_en:
                self._forward_key(event, pressed=False)
            return
        # Non-capture: release numpad button animation
        name = _numpad_btn(event.key(), event.modifiers())
        if name:
            self._ctrl_surface.release_button(name)

    # ── Tap tempo flash ──────────────────────────────────────────────────────
    # One tap = one front-panel TAP TEMPO press; the Kronos does its own averaging
    # (two taps set the tempo), so the client never computes BPM. The footer button
    # and the "Tap Tempo" keybind share this same press+flash path (mirrors C#
    # TapTempoOnce/FlashTapTempo).
    def _flash_tap_tempo(self):
        if hasattr(self, "_tap_tempo_lbl"):
            self._tap_tempo_lbl.setPixmap(_paint_seq_icon("metronome", T.ACCENT, T.FS_SMALL))
            QTimer.singleShot(250, lambda: self._tap_tempo_lbl.setPixmap(
                _paint_seq_icon("metronome", T.TEXT_DIM, T.FS_SMALL)))

    # ── Held-key auto-repeat ─────────────────────────────────────────────────
    def _start_kbd_repeat(self, qt_key: int, linux_code: int):
        self._kbd_repeat_key   = qt_key
        self._kbd_repeat_code  = linux_code
        self._kbd_repeat_phase = False
        self._kbd_repeat_timer.start(400)   # initial delay before repeat kicks in

    def _stop_kbd_repeat(self):
        self._kbd_repeat_timer.stop()
        self._kbd_repeat_code = 0
        self._kbd_repeat_key  = 0

    def _on_kbd_repeat_tick(self):
        if not self._kbd_repeat_phase:
            self._kbd_repeat_phase = True
            self._kbd_repeat_timer.setInterval(40)   # switch to fast repeat rate
        if self._kbd_repeat_code == 0 or not self._kbd_capture or not self._kbd_send_en:
            self._stop_kbd_repeat()
            return
        code = self._kbd_repeat_code
        self._ctrl_send(f"KEY {code} 1")
        self._ctrl_send(f"KEY {code} 0")

    def _forward_key(self, event: QKeyEvent, pressed: bool):
        key  = event.key()
        mods = event.modifiers()
        val  = 1 if pressed else 0

        # Releasing the key that started a repeat stops it (regardless of which
        # forward path below handles the release).
        if not pressed and key == self._kbd_repeat_key:
            self._stop_kbd_repeat()

        # Numpad digits → BUTTON NUM0..9 + control surface animation
        name = _numpad_btn(key, mods)
        if name:
            if pressed:
                self._ctrl_send(f"BUTTON {name}")
                self._ctrl_surface.press_button(name)
            else:
                self._ctrl_surface.release_button(name)
            return

        # Shift modifier keys — no raw map check for bare modifiers
        if key in (Qt.Key_Shift, Qt.Key_Control, Qt.Key_Alt, Qt.Key_Meta):
            lc = key_map.to_linux(key)
            if lc:
                self._ctrl_send(f"KEY {lc} {val}")
            return

        # Raw key map override (from Settings → Debug tab)
        raw = self._settings.get_raw_map(key, _mods_to_int(mods))
        if raw and raw.raw_code:
            if pressed:
                if raw.send_shift:
                    self._ctrl_send("KEY 42 1")
                self._ctrl_send(f"KEY {raw.raw_code} 1")
                self._ctrl_send(f"KEY {raw.raw_code} 0")
                if raw.send_shift:
                    self._ctrl_send("KEY 42 0")
                self._start_kbd_repeat(key, raw.raw_code)
            return

        # Shifted overrides
        if (mods & Qt.ShiftModifier) and pressed:
            override = key_map.to_linux_shifted(key)
            if override:
                lc, keep_shift = override
                if not keep_shift:
                    self._ctrl_send("KEY 42 0")  # release left shift
                self._ctrl_send(f"KEY {lc} 1")
                self._ctrl_send(f"KEY {lc} 0")
                if not keep_shift:
                    self._ctrl_send("KEY 42 1")  # re-press left shift
                return

        # Normal key
        lc = key_map.to_linux(key)
        if lc:
            self._ctrl_send(f"KEY {lc} {val}")
            if pressed and int(key) in _REPEATABLE_KEYS:
                self._start_kbd_repeat(key, lc)

    def _matches_keybind(self, event: QKeyEvent, action: str) -> bool:
        kb = self._settings.get_keybind(action)
        if kb.key == 0:
            return False
        return (event.key() == kb.key and
                _mods_to_int(event.modifiers()) == kb.modifiers)

    # ── Scroll wheel → Kronos data wheel ──────────────────────────────────────

    def wheelEvent(self, event: QWheelEvent):
        delta = event.angleDelta().y()
        if event.modifiers() & Qt.ControlModifier:
            if delta > 0:
                self._frame_w._zoom_level = min(8.0, self._frame_w._zoom_level + 0.25)
                if not self._zoom_on:
                    self._act_zoom.setChecked(True)
            elif delta < 0:
                self._frame_w._zoom_level = max(1.0, self._frame_w._zoom_level - 0.25)
            self._frame_w.update()
            event.accept()
            return
        if self._settings.reverse_scrolling:
            delta = -delta
        if delta > 0:
            self._ctrl_send("WHEEL CW")
            self._ctrl_surface.trigger_wheel_anim(1)
        elif delta < 0:
            self._ctrl_send("WHEEL CCW")
            self._ctrl_surface.trigger_wheel_anim(-1)
        event.accept()

    # ── View actions ───────────────────────────────────────────────────────────

    def _on_zoom_toggled(self, checked: bool):
        self._zoom_on = checked
        self._frame_w._zoom_on = checked
        self._frame_w.update()

    def _toggle_fullscreen(self):
        if self._is_fullscreen:
            self.showNormal()
            self.menuBar().setVisible(True)   # undo any '~' menu-bar hide from fullscreen
        else:
            self.showFullScreen()
        self._is_fullscreen = not self._is_fullscreen

    def _toggle_hide_data_input(self, checked: bool):
        if self._layout_preset != "Full":
            return
        self._settings.hide_data_input = checked
        self._ctrl_surface.setVisible(not checked)
        self._act_hide_data.blockSignals(True)
        self._act_hide_data.setChecked(checked)
        self._act_hide_data.blockSignals(False)
        storage.save_settings(self._settings)
        self._resize_to_fit()

    def _toggle_hide_value_input(self, checked: bool):
        if self._layout_preset != "Full":
            return
        self._settings.hide_value_input = checked
        self._show_left_panel(not checked)
        self._act_hide_value.blockSignals(True)
        self._act_hide_value.setChecked(checked)
        self._act_hide_value.blockSignals(False)
        storage.save_settings(self._settings)
        self._resize_to_fit()

    def _apply_layout(self, preset: str):
        self._layout_preset = preset
        self._settings.layout_preset = preset
        focused = preset == "Focused"
        self._act_preset_full.setChecked(not focused)
        self._act_preset_focused.setChecked(focused)

        # Menu enable state — data/value hiding only in Full
        self._act_hide_data.setEnabled(not focused)
        self._act_hide_value.setEnabled(not focused)

        if not focused:
            # Full layout
            self._value_rail.setVisible(False)
            self._data_rail.setVisible(False)
            self._ctrl_surface.setVisible(not self._settings.hide_data_input)
            self._show_left_panel(not self._settings.hide_value_input)
            self._act_hide_data.blockSignals(True)
            self._act_hide_data.setChecked(self._settings.hide_data_input)
            self._act_hide_data.blockSignals(False)
            self._act_hide_value.blockSignals(True)
            self._act_hide_value.setChecked(self._settings.hide_value_input)
            self._act_hide_value.blockSignals(False)
        else:
            # Focused layout — show rails on outermost edges
            self._value_rail.setVisible(True)
            self._data_rail.setVisible(True)
            data_exp  = self._settings.focused_data_expanded
            value_exp = self._settings.focused_value_expanded
            self._ctrl_surface.setVisible(data_exp)
            self._show_left_panel(value_exp)
            self._value_rail.set_expanded(value_exp)
            self._data_rail.set_expanded(data_exp)
        storage.save_settings(self._settings)
        self._resize_to_fit()

    def _toggle_focused_data_expand(self):
        if self._layout_preset != "Focused":
            return
        exp = not self._ctrl_surface.isVisible()
        self._settings.focused_data_expanded = exp
        self._ctrl_surface.setVisible(exp)
        self._data_rail.set_expanded(exp)
        storage.save_settings(self._settings)
        self._resize_to_fit()

    def _toggle_focused_value_expand(self):
        if self._layout_preset != "Focused":
            return
        exp = not self._left_panel.isVisible()
        self._settings.focused_value_expanded = exp
        self._show_left_panel(exp)
        self._value_rail.set_expanded(exp)
        storage.save_settings(self._settings)
        self._resize_to_fit()

    def _on_always_on_top_toggled(self, checked: bool):
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, checked)
        self.show()
        self._settings.always_on_top = checked
        storage.save_settings(self._settings)

    def _copy_ip_address(self):
        ip = self._host or self._settings.kronos_host
        if ip:
            QApplication.clipboard().setText(ip)
            self._status_label.setText(f"Copied: {ip}")
            QTimer.singleShot(2000, lambda: self._restore_conn_status())

    def _add_recent_host(self, host: str):
        hosts = self._settings.recent_hosts
        if host in hosts:
            hosts.remove(host)
        hosts.insert(0, host)
        self._settings.recent_hosts = hosts[:10]
        storage.save_settings(self._settings)
        self._rebuild_recent_menu()

    def _rebuild_recent_menu(self):
        self._recent_menu.clear()
        hosts = self._settings.recent_hosts
        if hosts:
            for h in hosts:
                a = self._recent_menu.addAction(h)
                a.triggered.connect(lambda checked, host=h: self._connect_to_recent(host))
            self._recent_menu.addSeparator()
            self._recent_menu.addAction("Clear All", self._clear_recent_hosts)
        else:
            a = self._recent_menu.addAction("(none)")
            a.setEnabled(False)

    def _connect_to_recent(self, host: str):
        self._settings.kronos_host = host
        self._host = host
        storage.save_settings(self._settings)
        self._connect_async()

    def _clear_recent_hosts(self):
        self._settings.recent_hosts.clear()
        storage.save_settings(self._settings)
        self._rebuild_recent_menu()

    def _set_window_size(self, scale: float):
        controls_visible = self._ctrl_surface.isVisible()
        left_visible = self._left_panel.isVisible()
        base_w = 800  # frame is always 800
        if controls_visible:
            base_w += 800  # control surface
        if left_visible:
            base_w += 282  # left value slider panel
        base_w = int(base_w * scale)
        base_h = int(600 * scale + 50)  # +50 for menu+statusbar
        self.resize(base_w, base_h)
        for s, a in self._act_sz.items():
            a.setChecked(abs(s - scale) < 0.01)

    def _resize_to_fit(self):
        """Resize the window to match the currently visible panels, preserving height."""
        if self._is_fullscreen or self.isMaximized() or not self.isVisible():
            return
        h = self.height()
        chrome = h - self._frame_w.height() if self._frame_w.height() > 0 else 50
        frame_h = max(h - chrome, 100)
        scale = frame_h / 600.0
        design_w = 800  # frame always present
        if self._ctrl_surface.isVisible():
            design_w += 800
        if self._left_panel.isVisible():
            design_w += 282
        # Rails add 14px each (fixed width, not scaled)
        rail_w = 0
        if self._value_rail.isVisible():
            rail_w += 14
        if self._data_rail.isVisible():
            rail_w += 14
        new_w = int(design_w * scale) + rail_w
        self.resize(new_w, h)

    # ── Tools ──────────────────────────────────────────────────────────────────

    def _on_palette_toggled(self, checked: bool):
        self._frame_w._ed_open  = checked
        self._frame_w._ed_typed = None
        self._frame_w._hover_idx = None
        self._frame_w.update()

    def _on_cal_toggled(self, checked: bool):
        self._frame_w._cal_mode = checked
        self._release_kbd_capture()
        if not checked:
            self._frame_w._cal_dragging = None
            self._frame_w._cal_hover    = None
            if self._frame_w._cal_dirty:
                storage.save_cal(self._frame_w._cal_mesh,
                                 self._frame_w._cal_bias_dots)
                self._frame_w._cal_dirty = False
        self._frame_w.update()

    def _handle_cal_key(self, event: QKeyEvent):
        key  = event.key()
        mods = event.modifiers()
        if key == Qt.Key_Escape:
            self._act_cal.setChecked(False)
            return
        if mods & Qt.ControlModifier and key == Qt.Key_Z:
            self._frame_w.cal_undo()
            return
        if mods & Qt.ControlModifier and key == Qt.Key_Y:
            self._frame_w.cal_redo()
            return
        if key == Qt.Key_S and not mods:
            storage.save_cal(self._frame_w._cal_mesh,
                             self._frame_w._cal_bias_dots)
            self._frame_w._cal_dirty = False
            self._frame_w.update()
            return
        if key == Qt.Key_X and not mods:
            self._frame_w._cal_bias_dots.clear()
            self._frame_w._cal_dirty = True
            self._frame_w.update()
            return
        if key == Qt.Key_R and not mods:
            self._frame_w._cal_mesh.reset()
            self._frame_w._cal_dirty = True
            self._frame_w.update()
            return

    def _set_cal_grid(self, n: int):
        self._frame_w._cal_mesh = CalMesh(n, n)
        self._frame_w._cal_dirty = True
        for size, act in self._act_grid.items():
            act.setChecked(size == n)
        self._frame_w.update()

    def _enter_test_mode(self):
        result = QMessageBox.warning(
            self,
            "Kronos Test Mode",
            "This will place you into the Kronos Test Mode. All unsaved changes "
            "will be lost, and your Kronos will need to be restarted after "
            "complete. Also, this is potentially a dangerous operation and should "
            "only be performed if you are aware of the risk.\n\n"
            "Do you wish to continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if result != QMessageBox.StandardButton.Yes:
            return
        self._set_kbd_capture()
        self._ctrl_send("BUTTON PROGRAM")
        QTimer.singleShot(500, lambda: self._ctrl_send(
            "CHORD 250 MIX_KNOBS RESET ENTER NUM5"))

    def _save_screenshot(self):
        if not self._frame_w._frame_pixmap:
            QMessageBox.information(self, "Screenshot", "No frame available yet.")
            return
        default = str(self._screenshot_default_path())
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Screenshot", default,
            "PNG Images (*.png);;JPEG Images (*.jpg)")
        if path:
            self._frame_w._frame_pixmap.save(path)

    def _quick_save_screenshot(self):
        if not self._frame_w._frame_pixmap:
            return
        dest = self._screenshot_default_path()
        self._frame_w._frame_pixmap.save(str(dest))
        logging.info(f"Screenshot saved: {dest}")
        self._status_label.setText(f"Screenshot saved: {dest.name}")
        QTimer.singleShot(3000, lambda: self._restore_conn_status())

    def _copy_frame_to_clipboard(self):
        if not self._frame_w._frame_pixmap:
            return
        QApplication.clipboard().setPixmap(self._frame_w._frame_pixmap)
        self._status_label.setText("Frame copied to clipboard")
        QTimer.singleShot(2000, lambda: self._restore_conn_status())

    def _open_screenshots_folder(self):
        d = self._settings.screenshot_dir.strip()
        if not d:
            d = str(pathlib.Path(__file__).parent)
        if sys.platform == "win32":
            os.startfile(d)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", d])
        else:
            subprocess.Popen(["xdg-open", d])

    def _open_log_file(self):
        path = str(_log_file_path())
        if not os.path.exists(path):
            QMessageBox.information(self, "Open Log File", "No log file has been written yet.")
            return
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    def _screenshot_default_path(self) -> pathlib.Path:
        base = pathlib.Path(self._settings.screenshot_dir.strip() or
                            str(pathlib.Path(__file__).parent))
        ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        return base / f"kronos_{ts}.png"

    def _on_disable_kbd_toggled(self, checked: bool):
        self._kbd_send_en = not checked
        self._update_kbd_indicator()
        self._frame_w.update()

    # ── Mirror ─────────────────────────────────────────────────────────────────

    def _toggle_mirror(self):
        self._mirror_state = not self._mirror_state
        self._ctrl_send("MIRROR_ON" if self._mirror_state else "MIRROR_OFF")

    # ── Help ───────────────────────────────────────────────────────────────────

    def _toggle_help(self):
        from help_window import HelpWindow
        dlg = HelpWindow(self)
        dlg.exec()

    # ── Settings dialog ────────────────────────────────────────────────────────

    def _open_settings(self, initial_tab: str = ""):
        from settings_window import SettingsWindow
        mirror_before  = self._settings.vga_mirror_enabled
        ss_before      = self._settings.screensaver_timeout
        debug_before   = self._settings.debug_logging
        hide_data_before  = self._settings.hide_data_input
        hide_value_before = self._settings.hide_value_input
        img_before = (self._settings.image_brightness, self._settings.image_contrast,
                      self._settings.image_gamma, self._settings.image_saturation,
                      self._settings.image_sharpen)
        dlg = SettingsWindow(self._settings, self, initial_tab=initial_tab,
                             on_image_preview=self._preview_image_adjust)
        if dlg.exec() == QDialog.Accepted:
            self._apply_settings_side_effects(mirror_before, ss_before, debug_before,
                                              hide_data_before, hide_value_before)
        else:
            # Cancelled — revert any live image-adjust preview to pre-dialog state.
            self._frame_w.set_image_adjust(*img_before)

    def _apply_settings_side_effects(self, mirror_before: bool, ss_before: int,
                                     debug_before: bool, hide_data_before: bool,
                                     hide_value_before: bool) -> None:
        """Everything that must happen after self._settings' fields change,
        whether from the Settings dialog's OK button or an Import Settings…
        round-trip — persists to disk and pushes each changed value out to the
        live connection/UI it affects."""
        storage.save_settings(self._settings)
        self._host        = self._settings.kronos_host
        self._ctrl_port   = self._settings.ctrl_port
        self._stream_port = self._settings.stream_port
        self._pull_mode   = self._settings.pull_mode
        self._fps         = self._settings.max_fps
        self._update_conn_mode_label()
        # Apply zoom default
        self._zoom_level = self._settings.zoom_default_level
        self._frame_w._zoom_level = self._zoom_level
        # Apply boot screen setting
        self._frame_w._disable_boot_screen = self._settings.disable_boot_screen
        # Apply image adjustments (brightness/contrast/gamma/saturation/sharpen)
        self._apply_image_adjust()
        # Apply hide data/value input only if visibility changed
        if (self._settings.hide_data_input != hide_data_before or
                self._settings.hide_value_input != hide_value_before):
            self._apply_layout(self._layout_preset)
        # Apply debug logging change immediately
        if self._settings.debug_logging != debug_before:
            _setup_logging(self._settings.debug_logging)
        if self._receiver:
            if self._settings.vga_mirror_enabled != mirror_before:
                self._mirror_state = self._settings.vga_mirror_enabled
                self._ctrl_send("MIRROR_ON" if self._mirror_state else "MIRROR_OFF")
            if self._settings.screensaver_timeout != ss_before:
                self._ctrl_send(f"SS_TIMEOUT {self._settings.screensaver_timeout}")
        # Update perf window host if open
        if self._perf_window:
            self._perf_window.update_host(self._host, self._ctrl_port)

    def _import_settings(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import Settings", "", "JSON Files (*.json)")
        if not path:
            return
        mirror_before     = self._settings.vga_mirror_enabled
        ss_before         = self._settings.screensaver_timeout
        debug_before      = self._settings.debug_logging
        hide_data_before  = self._settings.hide_data_input
        hide_value_before = self._settings.hide_value_input
        try:
            imported = storage.import_settings(path)
        except Exception as e:
            QMessageBox.warning(self, "Import Settings", f"Failed to import settings:\n{e}")
            return
        # Mutate self._settings in place field-by-field rather than replacing the
        # object outright — other components (SysExService, keybind lookups, etc.)
        # hold a reference to this same AppSettings instance.
        for f in dataclasses.fields(AppSettings):
            setattr(self._settings, f.name, getattr(imported, f.name))
        self._apply_settings_side_effects(mirror_before, ss_before, debug_before,
                                          hide_data_before, hide_value_before)
        QMessageBox.information(self, "Import Settings", "Settings imported successfully.")

    def _export_settings(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export Settings", "kronos_settings.json",
                                              "JSON Files (*.json)")
        if not path:
            return
        try:
            storage.export_settings(self._settings, path)
        except Exception as e:
            QMessageBox.warning(self, "Export Settings", f"Failed to export settings:\n{e}")
            return
        QMessageBox.information(self, "Export Settings", f"Settings exported to:\n{path}")

    def _preview_image_adjust(self, brightness: int, contrast: int, gamma: float,
                              saturation: int, sharpen: int):
        """Live preview from the Settings Image tab — frame widget only, never
        touches self._settings (so Cancel reverts cleanly)."""
        self._frame_w.set_image_adjust(brightness, contrast, gamma, saturation, sharpen)

    # ── Command palette ──────────────────────────────────────────────────────────

    def _open_command_palette(self):
        from command_palette import CommandEntry, CommandPalette
        entries = [
            CommandEntry(action, label, self._settings.get_key_name(action),
                        lambda a=action: self._run_action(a))
            for action, label, _ in get_rebindable()
        ]
        # C#'s BuildCommandRegistry() is a superset of the rebindable-action list
        # (which only backs the Key Bindings tab) — these are reachable from the
        # menu bar but had no Command Palette entry at all.
        entries.extend([
            CommandEntry("Reconnect", "Reconnect", "", self._trigger_reconnect),
            CommandEntry("RefreshDisplay", "Refresh Display", "",
                        lambda: self._ctrl_send("REFRESH")),
            CommandEntry("Disconnect", "Disconnect", "", self._disconnect),
            CommandEntry("Settings", "Settings…", "", self._open_settings),
            CommandEntry("WindowSize75", "Window Size: Small (75%)", "",
                        lambda: self._set_window_size(0.75)),
            CommandEntry("WindowSize100", "Window Size: Normal (100%)", "",
                        lambda: self._set_window_size(1.0)),
            CommandEntry("WindowSize125", "Window Size: Large (125%)", "",
                        lambda: self._set_window_size(1.25)),
            CommandEntry("WindowSize150", "Window Size: Extra Large (150%)", "",
                        lambda: self._set_window_size(1.50)),
            CommandEntry("WindowSize200", "Window Size: Huge (200%)", "",
                        lambda: self._set_window_size(2.00)),
            CommandEntry("LayoutFull", "Layout Preset: Full", "",
                        lambda: self._apply_layout("Full")),
            CommandEntry("LayoutFocused", "Layout Preset: Focused", "",
                        lambda: self._apply_layout("Focused")),
            CommandEntry("KeyboardInfo", "Keyboard Info…", "", self._open_keyboard_info),
            CommandEntry("SaveScreenshot", "Save Screenshot…", "", self._save_screenshot),
            CommandEntry("ToggleKeyboardSend", "Toggle Keyboard Send", "",
                        lambda: self._act_disable_kbd.setChecked(
                            not self._act_disable_kbd.isChecked())),
            CommandEntry("About", "About…", "", self._open_about),
        ])
        dlg = CommandPalette(entries, self)
        dlg.show()

    def _run_action(self, action: str):
        bank_cmd = _bank_action_cmd(action)
        if bank_cmd:
            self._ctrl_send(bank_cmd)
            return
        cmds: dict = {
            f"Mode {_MODE_NAMES[i]}": (
                lambda c=_MODE_CMDS[i], m=i: (self._ctrl_send(f"BUTTON {c}"), self._set_pending_mode(m))
            )
            for i in range(1, 8)
        }
        cmds.update({
            "Fullscreen":      self._toggle_fullscreen,
            "Zoom Window":     lambda: self._act_zoom.setChecked(not self._act_zoom.isChecked()),
            "Zoom In":         lambda: self._zoom_step(+0.5),
            "Zoom Out":        lambda: self._zoom_step(-0.5),
            "Mirror":          self._toggle_mirror,
            "Calibrate":       lambda: self._act_cal.setChecked(not self._act_cal.isChecked()),
            "Help":            self._toggle_help,
            "Quit":            self._try_quit,
            "HideDataInput":   lambda: self._toggle_hide_data_input(not self._settings.hide_data_input),
            "HideValueInput":  lambda: self._toggle_hide_value_input(not self._settings.hide_value_input),
            # Sequencer transport + tap tempo (shared with keybind handling)
            "Seq Locate":  lambda: self._ctrl_send("BUTTON SEQ_LOCATE"),
            "Seq Rewind":  lambda: self._ctrl_send("BUTTON SEQ_REW"),
            "Seq Forward": lambda: self._ctrl_send("BUTTON SEQ_FF"),
            "Seq Pause":   lambda: self._ctrl_send("BUTTON SEQ_PAUSE"),
            "Seq Record":  lambda: self._ctrl_send("BUTTON SEQ_REC"),
            "Seq Start":   lambda: self._ctrl_send("BUTTON SEQ_START"),
            "Seq Save":    lambda: self._ctrl_send("BUTTON SEQ_REC"),
            "Tap Tempo":   lambda: (self._ctrl_send("BUTTON TAP_TEMPO"), self._flash_tap_tempo()),
        })
        fn = cmds.get(action)
        if fn:
            fn()

    # ── About ──────────────────────────────────────────────────────────────────

    def _open_about(self):
        from about_dialog import AboutDialog
        dlg = AboutDialog(self._host, self._ctrl_port, self)
        dlg.exec()

    # ── File Manager ──────────────────────────────────────────────────────────

    def _open_file_manager(self):
        if self._file_manager_win is not None:
            self._file_manager_win.raise_()
            self._file_manager_win.activateWindow()
            return
        if not self._ensure_ftp_credentials():
            return
        host = self._host or self._settings.kronos_host
        if not host:
            QMessageBox.warning(self, "File Manager",
                                "No Kronos host configured. Set it in Settings first.")
            return
        from file_manager import FileManagerWindow
        self._file_manager_win = FileManagerWindow(
            host, self._settings.ftp_port,
            self._settings.ftp_username, self._settings.ftp_password, self)
        self._file_manager_win.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._file_manager_win.destroyed.connect(lambda: setattr(self, '_file_manager_win', None))
        self._file_manager_win.show()

    # ── Keyboard Info (Performance Meter) ─────────────────────────────────────

    def _open_keyboard_info(self):
        from perf_window import PerformanceWindow
        if self._perf_window is None:
            self._perf_window = PerformanceWindow(self._host, self._ctrl_port, self)
        self._perf_window.update_host(self._host, self._ctrl_port)
        self._perf_window.show()
        self._perf_window.raise_()

    # ── SysEx Tool ───────────────────────────────────────────────────────────────

    def _open_sysex_tool(self):
        if self._sysex_tool_win is not None:
            self._sysex_tool_win.raise_()
            self._sysex_tool_win.activateWindow()
            return
        if not self._host:
            QMessageBox.warning(self, "SysEx Tool",
                                "No Kronos host configured. Set it in Settings first.")
            return
        if not self._settings.midi_monitor_enabled or not self._sysex_service.can_dump:
            QMessageBox.warning(self, "SysEx Tool",
                                "MIDI monitoring is off or not connected. Enable "
                                "'MIDI bridge' and connect to the Kronos first.")
            return
        from sysex_tool_window import SysExToolWindow
        self._sysex_tool_win = SysExToolWindow(
            self._host, self._sysex_service.bridge, self._sysex_service, self)
        self._sysex_tool_win.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._sysex_tool_win.destroyed.connect(lambda: setattr(self, '_sysex_tool_win', None))
        self._sysex_tool_win.show()

    # ── Librarian ─────────────────────────────────────────────────────────────────

    def _open_librarian_shell(self):
        """Local Library / Merge / PCG three-pane shell — the only Librarian
        entry point (matches C#'s MNU_Librarian, which opens LibrarianShellWindow
        directly). Offline local-library curation (Erase, Rename, PCG/Merge
        staging, Local<->Local swap) needs only a host for the optional FTP
        pull; Sync Library/Commit Changes additionally need the live MIDI
        connection, gated inside the window itself."""
        if self._librarian_shell_win is not None:
            self._librarian_shell_win.raise_()
            self._librarian_shell_win.activateWindow()
            return
        if not self._host:
            QMessageBox.warning(self, "Librarian Shell",
                                "No Kronos host configured. Set it in Settings first.")
            return
        from librarian_shell_window import LibrarianShellWindow
        self._librarian_shell_win = LibrarianShellWindow(
            self._host, self._sysex_service,
            self._settings.ftp_port, self._settings.ftp_username, self._settings.ftp_password, self,
            settings=self._settings)
        self._librarian_shell_win.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._librarian_shell_win.destroyed.connect(lambda: setattr(self, '_librarian_shell_win', None))
        self._librarian_shell_win.show()

    # ── Macro playback ─────────────────────────────────────────────────────────

    def _play_macro(self, macro):
        """Run macro steps in a background thread, using the current delay setting."""
        import ctrl_client as CC
        host = self._host
        port = self._ctrl_port
        if not host:
            return
        for step in macro.steps:
            CC.get().send(host, port, step)
            time.sleep(macro.step_delay_ms / 1000.0)

    # ── Context menus ──────────────────────────────────────────────────────────

    def _show_frame_context_menu(self, global_pos: QPoint):
        menu = QMenu(self)

        menu.addAction("Copy Frame to Clipboard", self._copy_frame_to_clipboard)
        menu.addAction("Quick Save Screenshot",   self._quick_save_screenshot)
        menu.addAction("Save Screenshot As…",     self._save_screenshot)
        menu.addAction("Open Screenshots Folder", self._open_screenshots_folder)
        menu.addSeparator()

        a_zi = menu.addAction("Zoom In")
        a_zi.triggered.connect(lambda: self._zoom_step(+0.5))
        a_zo = menu.addAction("Zoom Out")
        a_zo.triggered.connect(lambda: self._zoom_step(-0.5))
        a_zr = menu.addAction("Reset Zoom")
        a_zr.triggered.connect(self._zoom_reset)
        menu.addSeparator()

        a_fs = menu.addAction("Fullscreen")
        a_fs.triggered.connect(self._toggle_fullscreen)
        # Reuses the exact same QActions as the View menu's Scaling Quality
        # submenu (a QAction can live in more than one menu), so checked-state
        # stays in sync automatically with no extra bookkeeping.
        scale_menu = menu.addMenu("Scaling Quality")
        scale_menu.addAction(self._act_scale_sharp)
        scale_menu.addAction(self._act_scale_smooth)
        scale_menu.addAction(self._act_scale_hq)
        menu.addAction("Image Adjustments…",
                       lambda: self._open_settings(initial_tab="Image"))
        menu.addSeparator()

        menu.addAction("Keyboard Info…", self._open_keyboard_info)
        menu.addAction("File Manager…", self._open_file_manager)
        menu.addSeparator()

        a_rec = menu.addAction("Reconnect")
        a_rec.triggered.connect(self._trigger_reconnect)
        a_dis = menu.addAction("Disconnect")
        a_dis.triggered.connect(self._disconnect)
        a_dis.setEnabled(self._receiver is not None)

        menu.exec(global_pos)

    def _zoom_step(self, delta: float):
        """Mirrors C#'s DoZoomIn/DoZoomOut: zooming in clamps at 10.0x and force-enables
        zoom; zooming out clamps at the configured default level and leaves the
        enabled/disabled state alone (mirrors DoZoomOut not touching _zoomOn)."""
        if delta > 0:
            self._frame_w._zoom_level = min(10.0, self._frame_w._zoom_level + delta)
            if not self._zoom_on:
                self._act_zoom.setChecked(True)
        else:
            self._frame_w._zoom_level = max(self._settings.zoom_default_level,
                                            self._frame_w._zoom_level + delta)
        self._frame_w.update()

    def _zoom_reset(self):
        self._frame_w._zoom_level = self._settings.zoom_default_level
        self._act_zoom.setChecked(False)
        self._frame_w.update()

    def _show_status_context_menu(self, local_pos):
        menu = QMenu(self)
        menu.addAction("Reconnect",  self._trigger_reconnect)
        a_dis = menu.addAction("Disconnect", self._disconnect)
        a_dis.setEnabled(self._receiver is not None)
        menu.addSeparator()
        a_copy = menu.addAction("Copy IP Address")
        a_copy.triggered.connect(lambda: QApplication.clipboard().setText(self._host or ""))
        a_copy.setEnabled(bool(self._host))
        menu.exec(self._status_label.mapToGlobal(local_pos))

    def _show_kbd_context_menu(self, local_pos):
        menu = QMenu(self)
        a_en = menu.addAction("Enable Keyboard Send")
        a_en.triggered.connect(lambda: self._act_disable_kbd.setChecked(False))
        a_en.setEnabled(not self._kbd_send_en)
        a_dis = menu.addAction("Disable Keyboard Send")
        a_dis.triggered.connect(lambda: self._act_disable_kbd.setChecked(True))
        a_dis.setEnabled(self._kbd_send_en)
        menu.exec(self._kbd_label.mapToGlobal(local_pos))

    def _show_fps_context_menu(self, local_pos):
        menu = QMenu(self)
        menu.addAction("Set Max FPS…", self._prompt_set_fps)
        menu.exec(self._fps_label.mapToGlobal(local_pos))

    def _prompt_set_fps(self):
        from PySide6.QtWidgets import QInputDialog
        val, ok = QInputDialog.getInt(self, "Max FPS", "Max frames per second (1–15):",
                                      self._fps, 1, 15)
        if ok:
            self._fps = val
            self._settings.max_fps = val
            storage.save_settings(self._settings)
            if self._receiver:
                self._ctrl_send(f"FPS {val}")

    def _show_mode_context_menu(self, local_pos):
        menu = QMenu(self)
        for i in range(1, 8):
            a = menu.addAction(_MODE_NAMES[i])
            a.triggered.connect(
                lambda checked, c=_MODE_CMDS[i], m=i:
                    (self._ctrl_send(f"BUTTON {c}"), self._set_pending_mode(m)))
        menu.exec(self._mode_label.mapToGlobal(local_pos))

    # ── Connection state ───────────────────────────────────────────────────────

    def _update_seq_enabled(self) -> None:
        """Gate the sequencer transport on Sequence mode and Save on the four
        write-capable modes, matching SeqTransportViewModel (IsTransportEnabled =
        CurrentMode == Sequence; IsSaveEnabled = Setlist/Combi/Program/Global).
        Tap Tempo is global (enabled whenever connected). When the transport is
        disabled it is faded (req 12) via a QGraphicsOpacityEffect so it reads as
        inactive rather than just greyed."""
        connected = self._conn_state == "connected"
        mode = self._current_mode
        transport_on = connected and mode == 4   # Sequence
        save_on = connected and mode in (1, 2, 3, 6)   # Setlist/Combi/Program/Global
        for w in (self._seq_locate, self._seq_rew, self._seq_ff, self._seq_pause,
                  self._seq_rec, self._seq_start):
            w.setEnabled(transport_on)
        self._tap_tempo_lbl.setEnabled(connected)
        self._seq_save_lbl.setEnabled(save_on)
        opacity = 1.0 if transport_on else 0.4
        eff = self._seq_box.graphicsEffect()
        if eff is None:
            eff = QGraphicsOpacityEffect(self._seq_box)
            self._seq_box.setGraphicsEffect(eff)
        eff.setOpacity(opacity)
        if not transport_on:
            self._seq_box.setToolTip("Sequencer transport - only available in Sequence mode")
        else:
            self._seq_box.setToolTip("Sequencer transport - sends the front-panel SEQUENCER "
                                     "buttons to the Kronos")

    def _set_conn_state(self, state: str, text: str):
        """Update connection dot + status label text together."""
        self._conn_state = state
        self._conn_dot.set_state(state)
        self._status_label.setText(text)
        color = {"disconnected": T.TEXT_DIM, "connecting": T.WARN,
                 "connected": T.OK_TEXT}.get(state, T.TEXT_DIM)
        # Left padding comes from the label's contentsMargins, not CSS.
        self._status_label.setStyleSheet(f"color: {color}; font-size: {T.FS_SMALL}px;")
        # Per-mode gating (req 12) - Tap Tempo stays connected-only, the rest follow mode.
        self._update_seq_enabled()

    def _restore_conn_status(self):
        """Restore status to actual connection state after a temporary message."""
        if self._receiver:
            self._set_conn_state("connected", f"Connected — {self._host}")
        elif self._connecting:
            self._set_conn_state("connecting", f"Connecting to {self._host}…")
        else:
            self._set_conn_state("disconnected", "Not connected")

    # ── Footer MIDI indicators ──────────────────────────────────────────────────

    def _set_midi_badge(self, connected: bool):
        txt, col = ("TCP", T.ACCENT) if connected else ("—", T.TEXT_IDLE)
        self._midi_badge.setText(txt)
        self._midi_badge.setStyleSheet(
            f"color: {col}; font-family: {T.FONT_MONO}; font-size: {T.FS_CAPTION}px; "
            f"font-weight: bold; border: 1px solid {col}; border-radius: 3px; padding: 0 4px;")

    def _on_midi_link_changed(self, connected: bool):
        self._set_midi_badge(connected)

    def _on_midi_rx(self):
        # Cheap slot — just stamp the time; the 50 ms timer recolors (coalesced).
        self._midi_rx_at = time.monotonic()
        if not self._midi_dim_timer.isActive():
            self._midi_dim_timer.start()

    def _on_midi_tx(self):
        self._midi_tx_at = time.monotonic()
        if not self._midi_dim_timer.isActive():
            self._midi_dim_timer.start()

    def _update_midi_dots(self):
        now = time.monotonic()
        rx_on = (now - self._midi_rx_at) < 0.25
        tx_on = (now - self._midi_tx_at) < 0.25
        rx = T.MIDI_RX if rx_on else T.MIDI_RX_DIM
        tx = T.MIDI_TX if tx_on else T.MIDI_TX_DIM
        self._midi_rx_arrow.setStyleSheet(f"color: {rx};")
        self._midi_rx_dot.setStyleSheet(f"color: {rx};")
        self._midi_tx_dot.setStyleSheet(f"color: {tx};")
        self._midi_tx_arrow.setStyleSheet(f"color: {tx};")
        if not rx_on and not tx_on:
            self._midi_dim_timer.stop()

    def _on_performance_changed(self, display: str):
        # While a user-initiated mode switch is in flight, hold the previous
        # value rather than flashing the pre-change mode's identity; the
        # authoritative refresh after _set_mode_button repopulates it.
        if self._mode_switch_pending:
            return
        self._perf_label.setText(display or "")

    def _update_conn_mode_label(self):
        if self._pull_mode:
            self._conn_mode_label.setText("Pull")
            self._conn_mode_label.setStyleSheet(f"color: {T.ACCENT}; font-size: {T.FS_SMALL}px;")
        else:
            self._conn_mode_label.setText("Change")
            self._conn_mode_label.setStyleSheet(f"color: {T.TEXT_DIM}; font-size: {T.FS_SMALL}px;")

    # ── Ping ───────────────────────────────────────────────────────────────────

    def _start_ping(self):
        self._ping_label.setText("⇄ —")
        self._ping_label.setStyleSheet(f"color: {T.TEXT_IDLE}; font-size: {T.FS_SMALL}px;")
        self._ping_inflight = False
        self._ping_timer.start()
        self._ping_once()

    def _stop_ping(self):
        self._ping_timer.stop()
        self._ping_inflight = False
        self._ping_label.setText("⇄ —")
        self._ping_label.setStyleSheet(f"color: {T.TEXT_IDLE}; font-size: {T.FS_SMALL}px;")

    def _ping_once(self):
        if self._ping_inflight or not self._host:
            return
        self._ping_inflight = True
        host, port = self._host, self._ctrl_port
        threading.Thread(target=self._ping_bg, args=(host, port),
                         daemon=True, name="Ping").start()

    def _ping_bg(self, host: str, port: int):
        ms = _icmp_ping(host)
        self._ping_inflight = False
        QTimer.singleShot(0, self, lambda m=ms: self._ping_result(m))

    def _ping_result(self, ms: float):
        if ms < 0:
            self._ping_label.setText("⇄ ×")
            self._ping_label.setStyleSheet(f"color: {T.ERROR}; font-size: {T.FS_SMALL}px;")
        else:
            if ms <= 15:
                color = T.OK
            elif ms <= 50:
                color = T.WARN
            else:
                color = T.ERROR
            self._ping_label.setText(f"⇄ {int(ms)}ms")
            self._ping_label.setStyleSheet(f"color: {color}; font-size: {T.FS_SMALL}px;")

    # ── Notifications ──────────────────────────────────────────────────────────

    def _notify(self, msg: str, is_error: bool = False):
        self._notify_msgs.append(msg)
        self._notify_count += 1
        self._notify_label.setToolTip(f"{self._notify_count} notification(s)\nLast: {msg}")
        self._notify_label.set_color(T.ERROR if is_error else T.WARN)

    def _clear_notification(self):
        self._notify_count = 0
        self._notify_msgs.clear()
        self._notify_label.set_color(T.TEXT_FAINT)
        self._notify_label.setToolTip("No notifications")

    def _open_notify_log(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Notifications")
        dlg.resize(460, 240)
        layout = QVBoxLayout(dlg)
        txt = QTextEdit()
        txt.setReadOnly(True)
        txt.setStyleSheet(f"background: {T.INSET}; color: {T.TEXT}; font-family: {T.FONT_MONO};")
        txt.setPlainText(
            "\n".join(self._notify_msgs) if self._notify_msgs else "(no notifications)")
        layout.addWidget(txt)
        from PySide6.QtWidgets import QDialogButtonBox
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        btns.accepted.connect(dlg.accept)
        layout.addWidget(btns)
        dlg.exec()
        self._clear_notification()

    def _show_notify_context_menu(self, global_pos: QPoint):
        menu = QMenu(self)
        menu.addAction("Open Log",           self._open_notify_log)
        menu.addAction("Clear Notification", self._clear_notification)
        menu.exec(global_pos)

    # ── VU meter / Audio ───────────────────────────────────────────────────────

    def _open_vu_device_picker(self):
        from vu_meter import list_audio_devices
        devices = list_audio_devices()
        menu = QMenu(self)
        a_none = menu.addAction("No monitoring")
        a_none.triggered.connect(lambda: self._set_vu_device(None))
        if devices:
            menu.addSeparator()
            for dev_id, dev_name in devices:
                a = menu.addAction(dev_name)
                a.triggered.connect(lambda checked, d=dev_id: self._set_vu_device(d))
        else:
            menu.addSeparator()
            menu.addAction("(no audio devices found)").setEnabled(False)
        btn_pos = QPoint(0, self._vu_picker_btn.height())
        menu.exec(self._vu_picker_btn.mapToGlobal(btn_pos))

    def _set_vu_device(self, device_id: Optional[str]):
        self._vu_device_id = device_id
        self._stop_audio_capture()
        if device_id is not None:
            self._start_audio_capture(device_id)
        else:
            self._vu_widget.reset()

    def _start_audio_capture(self, device_id: Optional[str] = None):
        from vu_meter import AudioCapture
        self._stop_audio_capture()
        self._audio_capture = AudioCapture(device_id, self)
        self._audio_capture.levels_updated.connect(self._vu_widget.update_levels)
        self._audio_capture.start()

    def _stop_audio_capture(self):
        if self._audio_capture is not None:
            self._audio_capture.stop()
            self._audio_capture = None
        self._vu_widget.reset()

    # ── Quit ───────────────────────────────────────────────────────────────────

    def _try_quit(self):
        self.close()

    def closeEvent(self, event):
        if self._shutting_down:
            event.accept()
            return
        if self._frame_w._cal_dirty:
            r = QMessageBox.warning(
                self, "Unsaved Calibration",
                "You have unsaved calibration changes.\n\n"
                "Do you want to save before quitting?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Save)
            if r == QMessageBox.Cancel:
                event.ignore()
                return
            if r == QMessageBox.Save:
                storage.save_cal(self._frame_w._cal_mesh,
                                 self._frame_w._cal_bias_dots)
                self._frame_w._cal_dirty = False
            else:
                self._frame_w._cal_dirty = False
        if self._settings.prompt_before_quitting:
            r = QMessageBox.question(self, "Quit?", "Disconnect and quit?",
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if r != QMessageBox.Yes:
                event.ignore()
                return
        event.ignore()
        self._shutting_down = True
        overlay = _ShutdownOverlay(self)
        overlay.show()
        overlay.raise_()
        overlay.repaint()
        QApplication.processEvents()
        QTimer.singleShot(0, self._do_shutdown)

    def _do_shutdown(self):
        if self._tray_icon:
            self._tray_icon.hide()
        self._stop_ping()
        self._mode_poll_timer.stop()
        if self._combi_prog_edit_active:
            self._combi_flash_timer.stop()
        if self._perf_window:
            self._perf_window.close()
        if self._file_manager_win:
            self._file_manager_win.close()

        receiver = self._receiver
        audio = self._audio_capture

        if receiver:
            receiver.stop()
        if audio:
            audio._running = False
            audio.quit()

        if receiver:
            receiver.wait(800)
            self._receiver = None
        if audio:
            audio.wait(500)
            self._audio_capture = None

        self._ctrl.reset()
        storage.save_settings(self._settings)
        if self._frame_w._cal_dirty:
            storage.save_cal(self._frame_w._cal_mesh, self._frame_w._cal_bias_dots)
        self.close()

    def eventFilter(self, watched, event):
        # Clickable status-bar widgets
        if event.type() == QEvent.Type.MouseButtonPress:
            btn = event.button()
            if watched is self._notify_label:
                if btn == Qt.MouseButton.LeftButton:
                    self._open_notify_log()
                elif btn == Qt.MouseButton.RightButton:
                    self._show_notify_context_menu(event.globalPosition().toPoint())
                return True
            if watched is self._kbd_info_btn and btn == Qt.MouseButton.LeftButton:
                self._open_keyboard_info()
                return True
            if watched is self._vu_picker_btn and btn == Qt.MouseButton.LeftButton:
                self._open_vu_device_picker()
                return True

        # Intercept all key events during calibration mode so menu
        # accelerators (e.g. &Settings) don't steal single-letter keys.
        if self._frame_w._cal_mode:
            t = event.type()
            if t == QEvent.Type.KeyPress:
                if isinstance(watched, QWidget) and watched.window() is self:
                    if not event.isAutoRepeat():
                        self._handle_cal_key(event)
                    return True
            if t == QEvent.Type.KeyRelease:
                if isinstance(watched, QWidget) and watched.window() is self:
                    return True

        # Intercept all key events at application level when captured so
        # menu accelerators and QShortcuts never fire.  Returning True
        # consumes the event before Qt's shortcut processing runs.
        if self._kbd_capture:
            t = event.type()
            if t in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
                # Only intercept events aimed at widgets inside our window
                # (not child dialogs, which have their own window()).
                if isinstance(watched, QWidget) and watched.window() is self:
                    # Ctrl+V/Ctrl+A are local actions even while capturing (C#
                    # gates them on capture+send-enabled instead of forwarding
                    # them as raw keystrokes) — let these two fall through to
                    # keyPressEvent's normal Ctrl-shortcut handling instead of
                    # being swallowed and forwarded here.
                    if (t == QEvent.Type.KeyPress and (event.modifiers() & Qt.ControlModifier)
                            and event.key() in (Qt.Key_V, Qt.Key_A)):
                        return False
                    if not event.isAutoRepeat():
                        if self._kbd_send_en:
                            self._forward_key(event,
                                              pressed=(t == QEvent.Type.KeyPress))
                    return True  # consume — blocks all shortcuts

        # Mouse: release capture when a click lands outside the frame widget
        if self._kbd_capture and event.type() == QEvent.Type.MouseButtonPress:
            gpos = event.globalPosition().toPoint()
            fw   = self._frame_w
            tl   = fw.mapToGlobal(QPoint(0, 0))
            br   = fw.mapToGlobal(QPoint(fw.width(), fw.height()))
            if not (tl.x() <= gpos.x() < br.x() and tl.y() <= gpos.y() < br.y()):
                self._release_kbd_capture()
        return False

    def changeEvent(self, event):
        if event.type() == QEvent.ActivationChange and not self.isActiveWindow():
            self._release_kbd_capture()
        if (event.type() == QEvent.Type.WindowStateChange and self.isMinimized()
                and self._tray_icon is not None):
            # Minimize-to-tray (matches C#'s MainWindow StateChanged handler) —
            # deferred a tick so the native minimize animation isn't fighting hide().
            QTimer.singleShot(0, self.hide)
        super().changeEvent(event)

    def _init_tray_icon(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return   # not every desktop environment provides one
        icon = QApplication.instance().windowIcon()
        tray = QSystemTrayIcon(icon, self)
        tray.setToolTip(_APP_TITLE)
        menu = QMenu()
        menu.addAction("Show", self._tray_show)
        menu.addSeparator()
        menu.addAction("Reconnect", self._trigger_reconnect)
        menu.addAction("Disconnect", self._disconnect)
        menu.addSeparator()
        menu.addAction("Quit", self._try_quit)
        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.show()
        self._tray_icon = tray

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:   # single left-click
            self._tray_show()

    def _tray_show(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()
