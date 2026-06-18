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
    QAction, QClipboard, QColor, QFont, QIcon, QImage, QKeyEvent, QMouseEvent,
    QPainter, QPixmap, QResizeEvent, QWheelEvent,
)
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QFrame, QHBoxLayout, QInputDialog,
    QLabel, QMainWindow, QMenu, QMenuBar, QMessageBox, QSizePolicy,
    QStatusBar, QTextEdit, QVBoxLayout, QWidget,
)

import ctrl_client as CtrlClient
import key_map
import storage
from app_settings import AppSettings, get_rebindable
from control_surface import KronosControlSurface
from mode_detector import CombiProgramEditDetector, ModeDetector, is_frame_mostly_black
from models import CalBiasDot, CalHistEntry, CalHistKind, CalMesh, HistEntry, PaletteEntry
from overlay_renderer import OverlayRenderer
from stream_receiver import StreamReceiver


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
        self._lut: list[int] = [0] * 256               # packed 0xRRGGBB
        self._cached_ct: list[int] = []                # cached QImage color table
        self._ct_dirty = True                          # rebuild color table on next frame
        self._aspect_lock    = True
        self._frame_rect     = QRectF()
        self._palette: list[PaletteEntry] = []
        self._help_rows: list[tuple[str, str]] = []

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
        self._help_open   = False
        self._kbd_capture = False
        self._boot_phase  = True
        self._is_connected = False

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
        self._boot_bar_x    = 864
        self._disconnect_msg = ""

        # Render timer for touch marker fade
        self._fade_timer = QTimer(self)
        self._fade_timer.setInterval(30)
        self._fade_timer.timeout.connect(self.update)

    # ── Frame update ───────────────────────────────────────────────────────────

    def on_frame(self, raw: bytes, palette: list[PaletteEntry],
                 overrides: Dict[int, PaletteEntry], locked: Set[int]):
        """Called from main thread with a new 8bpp frame and current palette."""
        self._overrides = overrides
        self._locked    = locked
        if self._ct_dirty or not self._cached_ct:
            ct = []
            for i, e in enumerate(palette):
                entry = overrides.get(i, e)
                ct.append(0xFF000000 | (entry.r << 16) | (entry.g << 8) | entry.b)
            self._cached_ct = ct
            for i, c in enumerate(ct):
                self._lut[i] = c & 0xFFFFFF
            self._ct_dirty = False
        img = QImage(raw, _FRAME_W, _FRAME_H, _FRAME_W, QImage.Format_Indexed8)
        img.setColorTable(self._cached_ct)
        self._frame_image  = img
        self._frame_pixmap = QPixmap.fromImage(img.convertToFormat(QImage.Format_RGB32))
        self.update()

    def set_palette(self, palette: list[PaletteEntry],
                    overrides: Dict[int, PaletteEntry]):
        """Rebuild LUT after palette or override change without new frame."""
        if not self._frame_image:
            return
        ct = []
        for i, e in enumerate(palette):
            entry = overrides.get(i, e)
            ct.append(0xFF000000 | (entry.r << 16) | (entry.g << 8) | entry.b)
            self._lut[i] = ct[-1] & 0xFFFFFF
        self._cached_ct = ct
        self._ct_dirty  = False
        self._frame_image.setColorTable(ct)
        self._frame_pixmap = QPixmap.fromImage(
            self._frame_image.convertToFormat(QImage.Format_RGB32))
        self.update()

    # ── Paint ──────────────────────────────────────────────────────────────────

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.fillRect(self.rect(), Qt.black)

        fr = self._compute_frame_rect()
        if self._cal_mode:
            m = _CAL_MARGIN
            fr = QRectF(fr.x() + m, fr.y() + m,
                        fr.width() - 2 * m, fr.height() - 2 * m)
        self._frame_rect = fr

        if self._frame_pixmap:
            p.drawPixmap(fr.toRect(), self._frame_pixmap)
        elif self._boot_phase and self._is_connected:
            self._renderer.draw_boot_splash(p, fr, self._boot_bar_x)
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

        # Help overlay
        if self._help_open:
            self._renderer.draw_help(p, fr, self._help_rows)

        p.end()

    def _compute_frame_rect(self) -> QRectF:
        w, h = self.width(), self.height()
        if self._aspect_lock:
            aspect = _FRAME_W / _FRAME_H
            if w / h > aspect:
                fw = h * aspect
                return QRectF((w - fw) / 2, 0, fw, h)
            else:
                fh = w / aspect
                return QRectF(0, (h - fh) / 2, w, fh)
        return QRectF(0, 0, w, h)

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
    _COLORS = {"disconnected": "#444444", "connecting": "#CCAA00", "connected": "#44BB44"}

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
        p.setBrush(QColor(self._COLORS.get(self._state, "#444444")))
        p.drawEllipse(1, 1, 10, 10)
        p.end()


class _CollapseBar(QWidget):
    """Thin vertical bar between frame and controls with a clickable arrow."""
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(14)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Show/hide Data Controls")
        self._expanded = True

    def set_expanded(self, expanded: bool):
        self._expanded = expanded
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor("#1A1A1A"))
        p.setPen(QColor("#888"))
        p.drawLine(0, 0, 0, self.height())
        arrow = "◀" if self._expanded else "▶"
        p.setPen(QColor("#AAA"))
        f = p.font()
        f.setPixelSize(12)
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


def _setup_logging(debug: bool):
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger().setLevel(level)


class MainWindow(QMainWindow):
    def __init__(self, settings: AppSettings):
        super().__init__()
        self._settings    = settings
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
        self._current_mode   = 0
        self._prev_mode      = 0
        self._pending_mode   = 0   # user-requested mode awaiting detection confirmation
        self._combi_prog_edit_active = False
        self._combi_prog_flash_state = False
        self._combi_exit_gone_at: float = 0.0
        self._help_active    = False
        self._kbd_capture   = False
        self._kbd_send_en   = True
        self._shift_held    = False

        self._mode_poll_timer = QTimer(self)
        self._mode_poll_timer.setInterval(_MODE_POLL_INTERVAL_MS)
        self._mode_poll_timer.timeout.connect(self._poll_mode)

        self._combi_flash_timer = QTimer(self)
        self._combi_flash_timer.setInterval(420)
        self._combi_flash_timer.timeout.connect(self._combi_flash_tick)

        self._fps_count   = 0
        self._fps_time    = time.monotonic()
        self._measured_fps = 0.0

        self._connecting  = False
        self._auto_reconnect_enabled = True   # False only when user explicitly disconnects
        self._poll_in_progress = False        # guard: only one STATE query at a time
        self._layout_preset = settings.layout_preset
        self._is_fullscreen = False

        self._aspect_lock  = True
        self._zoom_on      = False
        self._zoom_level   = settings.zoom_default_level
        self._mirror_state = False
        self._perf_window  = None   # PerformanceWindow singleton (lazy)
        self._file_manager_win = None
        self._shutting_down = False

        # Ping
        self._ping_inflight = False
        self._ping_timer = QTimer(self)
        self._ping_timer.setInterval(3000)
        self._ping_timer.timeout.connect(self._ping_once)

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
        self.resize(1590, 650)
        self.setStyleSheet("QMainWindow { background: black; }")
        if self._settings.always_on_top:
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)

        # Central widget with horizontal layout
        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Frame widget (left, 2/3 of width)
        self._frame_w = FrameWidget()
        layout.addWidget(self._frame_w, 1)

        # Collapse bar between frame and control surface
        self._collapse_bar = _CollapseBar()
        self._collapse_bar.clicked.connect(self._toggle_controls_from_bar)
        layout.addWidget(self._collapse_bar)

        # Control surface (right — same design-space width as the frame: 800 units)
        self._ctrl_surface = KronosControlSurface()
        self._ctrl_surface.button_pressed.connect(self._on_ctrl_button)
        self._ctrl_surface.wheel_step.connect(self._on_wheel_step)
        layout.addWidget(self._ctrl_surface, 1)

        # Signals from frame widget
        self._frame_w.touch_down.connect(self._on_touch_down)
        self._frame_w.touch_move.connect(self._on_touch_move)
        self._frame_w.touch_up.connect(self._on_touch_up)
        self._frame_w.frame_clicked_for_kbd.connect(self._set_kbd_capture)

        # Status bar
        self._status_bar = QStatusBar()
        self._status_bar.setStyleSheet(
            "QStatusBar { background: #1A1A1A; color: #888; font-size: 11px; }"
            "QStatusBar::item { border: none; }"
        )
        self.setStatusBar(self._status_bar)

        # Left: connection dot + status text
        self._conn_dot = _StatusDot()
        self._status_label = QLabel("Not connected")
        self._status_label.setStyleSheet("color: #888; padding-left: 4px;")
        self._status_bar.addWidget(self._conn_dot)
        self._status_bar.addWidget(self._status_label)

        def _sep():
            s = QFrame()
            s.setFrameShape(QFrame.Shape.VLine)
            s.setStyleSheet("color: #333; margin: 2px 0;")
            return s

        # Permanent widgets (left → right)
        self._kbd_label = QLabel("⌨︎")
        self._kbd_label.setStyleSheet("color: #888; font-family: 'Segoe UI Symbol';")
        self._kbd_label.setToolTip("Keyboard capture — click in frame to capture")

        self._fps_label = QLabel("")
        self._fps_label.setStyleSheet("color: #666;")

        self._ping_label = QLabel("⇄ —")
        self._ping_label.setStyleSheet("color: #555;")
        self._ping_label.setToolTip("Round-trip latency to Kronos")

        self._notify_label = QLabel("●")
        self._notify_label.setStyleSheet("color: #444;")
        self._notify_label.setToolTip("No notifications")
        self._notify_label.setCursor(Qt.CursorShape.PointingHandCursor)

        self._kbd_info_btn = QLabel("⊞")
        self._kbd_info_btn.setStyleSheet("color: #556; padding: 0 2px;")
        self._kbd_info_btn.setToolTip("Keyboard Info / Performance Meter")
        self._kbd_info_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        self._conn_mode_label = QLabel("")
        self._conn_mode_label.setStyleSheet("color: #888;")
        self._conn_mode_label.setToolTip("Streaming mode")

        from vu_meter import VuMeterWidget
        self._vu_widget = VuMeterWidget()
        self._vu_picker_btn = QLabel("▾")
        self._vu_picker_btn.setStyleSheet("color: #555; padding: 0 2px;")
        self._vu_picker_btn.setToolTip("Select audio monitoring device")
        self._vu_picker_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        _vu_box = QWidget()
        _vu_lay = QHBoxLayout(_vu_box)
        _vu_lay.setContentsMargins(2, 0, 2, 0)
        _vu_lay.setSpacing(2)
        _vu_lay.addWidget(self._vu_widget)
        _vu_lay.addWidget(self._vu_picker_btn)

        self._mode_label = QLabel("")
        self._mode_label.setStyleSheet("color: #88AADD;")

        for w in (_sep(), self._kbd_label, _sep(),
                  self._fps_label, _sep(),
                  self._ping_label, _sep(),
                  self._notify_label, _sep(),
                  self._kbd_info_btn):
            self._status_bar.addWidget(w)

        for w in (_vu_box, _sep(),
                  self._conn_mode_label, _sep(),
                  self._mode_label):
            self._status_bar.addPermanentWidget(w)

        self._build_menu()

    def _build_menu(self):
        mb = self.menuBar()

        # Connection
        conn_menu = mb.addMenu("&Connection")
        self._act_connect    = conn_menu.addAction("&Connect")
        self._act_refresh    = conn_menu.addAction("Re&fresh Display")
        conn_menu.addSeparator()
        self._act_disconnect = conn_menu.addAction("&Disconnect")
        conn_menu.addSeparator()
        self._recent_menu = conn_menu.addMenu("Recent C&onnections")
        self._rebuild_recent_menu()
        self._act_copy_ip = conn_menu.addAction("Copy &IP Address")
        conn_menu.addSeparator()
        self._act_file_mgr   = conn_menu.addAction("File &Manager…")
        conn_menu.addSeparator()
        self._act_quit       = conn_menu.addAction("&Quit")
        self._act_disconnect.setEnabled(False)

        # View
        view_menu = mb.addMenu("&View")
        self._act_aspect   = view_menu.addAction("&Aspect Lock")
        self._act_aspect.setCheckable(True)
        self._act_aspect.setChecked(True)
        self._act_zoom     = view_menu.addAction("&Zoom Window")
        self._act_zoom.setCheckable(True)
        view_menu.addSeparator()
        self._act_full     = view_menu.addAction("&Fullscreen")
        self._act_on_top   = view_menu.addAction("Always on &Top")
        self._act_on_top.setCheckable(True)
        self._act_on_top.setChecked(self._settings.always_on_top)
        self._act_hide_ctrl = view_menu.addAction("&Hide Controls")
        self._act_hide_ctrl.setCheckable(True)
        view_menu.addSeparator()
        preset_menu = view_menu.addMenu("Layout &Preset")
        self._act_preset_full    = preset_menu.addAction("&Full")
        self._act_preset_focused = preset_menu.addAction("F&ocused")
        for a in (self._act_preset_full, self._act_preset_focused):
            a.setCheckable(True)
        view_menu.addSeparator()
        size_menu = view_menu.addMenu("Window &Size")
        self._act_sz = {}
        for label, scale in (("Small (75%)", 0.75), ("Normal (100%)", 1.0),
                              ("Large (125%)", 1.25), ("Extra Large (150%)", 1.50),
                              ("Huge (200%)", 2.00)):
            a = size_menu.addAction(label)
            a.setCheckable(True)
            self._act_sz[scale] = a

        # Tools
        tools_menu = mb.addMenu("&Tools")
        self._act_palette = tools_menu.addAction("&Palette Editor")
        self._act_palette.setCheckable(True)
        self._act_palette.setVisible(False)  # palette editor disabled — matches C# version
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
        self._act_quick_save  = tools_menu.addAction("&Quick Save Screenshot")
        self._act_screenshot  = tools_menu.addAction("Save Screenshot &As…")
        self._act_copy_frame  = tools_menu.addAction("&Copy Frame to Clipboard")
        self._act_open_ss_dir = tools_menu.addAction("&Open Screenshots Folder")
        tools_menu.addSeparator()
        self._act_keyboard_info = tools_menu.addAction("&Keyboard Info…")
        tools_menu.addSeparator()
        self._act_disable_kbd = tools_menu.addAction("&Disable Keyboard Send")
        self._act_disable_kbd.setCheckable(True)

        # Mode select
        mode_menu = mb.addMenu("&Mode Select")
        self._act_modes: list[QAction] = []
        for name in _MODE_NAMES[1:]:
            a = mode_menu.addAction(name)
            self._act_modes.append(a)

        # Bank select
        bank_menu = mb.addMenu("Ban&k Select")
        self._build_bank_menu(bank_menu)

        # Settings
        settings_menu = mb.addMenu("&Settings")
        self._act_settings_dlg = settings_menu.addAction("&Settings…")

        # Help
        help_menu = mb.addMenu("&Help")
        self._act_show_help   = help_menu.addAction("&Show Help")
        self._act_cmd_palette = help_menu.addAction("&Command Palette")
        help_menu.addSeparator()
        self._act_about       = help_menu.addAction("&About…")

    def _build_bank_menu(self, menu: QMenu):
        letters = "ABCDEFG"
        for letter in letters:
            a = menu.addAction(f"I-{letter}")
            a.triggered.connect(lambda checked, l=letter: self._ctrl_send(f"BUTTON BANK_I{l}"))
        menu.addSeparator()
        for letter in letters:
            a = menu.addAction(f"U-{letter}")
            a.triggered.connect(lambda checked, l=letter: self._ctrl_send(f"BUTTON BANK_U{l}"))
        menu.addSeparator()
        for letter in letters:
            a = menu.addAction(f"U-{letter}{letter}")
            a.triggered.connect(lambda checked, l=letter: self._ctrl_send(f"CHORD BANK_U{l} BANK_I{l}"))

    # ── Action wiring ──────────────────────────────────────────────────────────

    def _wire_actions(self):
        self._act_copy_ip.triggered.connect(self._copy_ip_address)
        self._act_file_mgr.triggered.connect(self._open_file_manager)
        self._act_connect.triggered.connect(self._trigger_reconnect)
        self._act_refresh.triggered.connect(lambda: self._ctrl_send("REFRESH"))
        self._act_disconnect.triggered.connect(self._disconnect)
        self._act_quit.triggered.connect(self._try_quit)

        self._act_aspect.toggled.connect(self._on_aspect_toggled)
        self._act_zoom.toggled.connect(self._on_zoom_toggled)
        self._act_full.triggered.connect(self._toggle_fullscreen)
        self._act_on_top.toggled.connect(self._on_always_on_top_toggled)
        self._act_hide_ctrl.toggled.connect(self._on_hide_controls_toggled)

        self._act_preset_full.triggered.connect(lambda: self._apply_layout("Full"))
        self._act_preset_focused.triggered.connect(lambda: self._apply_layout("Focused"))

        for scale, act in self._act_sz.items():
            act.triggered.connect(lambda checked, s=scale: self._set_window_size(s))

        self._act_palette.toggled.connect(self._on_palette_toggled)
        self._act_cal.toggled.connect(self._on_cal_toggled)
        for n, act in self._act_grid.items():
            act.triggered.connect(lambda checked, s=n: self._set_cal_grid(s))
        self._act_quick_save.triggered.connect(self._quick_save_screenshot)
        self._act_screenshot.triggered.connect(self._save_screenshot)
        self._act_copy_frame.triggered.connect(self._copy_frame_to_clipboard)
        self._act_open_ss_dir.triggered.connect(self._open_screenshots_folder)
        self._act_keyboard_info.triggered.connect(self._open_keyboard_info)
        self._act_disable_kbd.toggled.connect(self._on_disable_kbd_toggled)

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

    def _apply_settings_to_ui(self):
        self._act_aspect.setChecked(self._aspect_lock)
        self._frame_w._aspect_lock = self._aspect_lock
        focused = self._layout_preset == "Focused"
        self._act_preset_full.setChecked(not focused)
        self._act_preset_focused.setChecked(focused)
        self._collapse_bar.setVisible(focused)
        ctrl_hidden = focused and self._settings.hide_controls
        self._ctrl_surface.setVisible(not ctrl_hidden)
        self._collapse_bar.set_expanded(not ctrl_hidden)
        self._act_hide_ctrl.setChecked(ctrl_hidden)

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
        """Prompt for FTP credentials if not saved. Returns False if user cancelled."""
        if self._settings.ftp_username:
            return True
        from PySide6.QtWidgets import (
            QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QVBoxLayout)
        dlg = QDialog(self)
        dlg.setWindowTitle("Kronos Login")
        dlg.setMinimumWidth(320)
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel(f"FTP credentials for {self._host}:"))
        form = QFormLayout()
        user_edit = QLineEdit()
        user_edit.setPlaceholderText("root")
        pass_edit = QLineEdit()
        pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Username:", user_edit)
        form.addRow("Password:", pass_edit)
        layout.addLayout(form)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        layout.addWidget(btns)
        if dlg.exec() != QDialog.Accepted:
            return False
        self._settings.ftp_username = user_edit.text()
        self._settings.ftp_password = pass_edit.text()
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
        self._mode_poll_timer.stop()
        self._poll_in_progress = False
        if self._combi_prog_edit_active:
            self._combi_prog_edit_active = False
            self._combi_flash_timer.stop()
        self._current_mode = 0
        self._prev_mode    = 0
        self._pending_mode = 0
        if self._receiver:
            self._receiver.dispose()
            self._receiver = None
        self._ctrl.reset()
        self._stop_ping()
        self._frame_w._is_connected = False
        self._frame_w._frame_pixmap = None
        self._frame_w._frame_image  = None
        self._frame_w._boot_phase   = False
        if not quiet:
            self._frame_w._disconnect_msg = "Disconnected"
        self._frame_w.update()
        self._act_disconnect.setEnabled(False)
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

        # Suppress detection while the framebuffer is still mostly black (boot)
        mostly_black = is_frame_mostly_black(raw, self._frame_w._lut)

        if not mostly_black:
            # Mode + help detection from top-left 140×55 region
            if self._mode_detector.has_any():
                mode = self._mode_detector.identify(raw, _FRAME_W, self._frame_w._lut)
                if mode != self._current_mode and mode > 0:
                    self._set_mode_button(mode)
                help_now = self._mode_detector.is_help_active(raw, _FRAME_W, self._frame_w._lut)
                if help_now != self._help_active:
                    self._help_active = help_now
                    self._ctrl_surface.set_active("Help", help_now)

            # Combi program-edit indicator — checked every frame (outside top-left region)
            indicator = self._combi_detector.is_active(raw, _FRAME_W, self._frame_w._lut)
            if (not self._combi_prog_edit_active
                    and self._current_mode == 3
                    and self._prev_mode in (2, 0)
                    and indicator):
                self._combi_exit_gone_at = 0.0
                self._enter_combi_program_edit()
            elif self._combi_prog_edit_active:
                if self._current_mode != 3:
                    # Mode change is definitive — exit immediately
                    self._combi_exit_gone_at = 0.0
                    self._exit_combi_program_edit()
                elif not indicator:
                    # Indicator absent but mode still 3 — holdoff before exiting
                    # (covers cases like a menu briefly covering the indicator pixel)
                    now = time.monotonic()
                    if self._combi_exit_gone_at == 0.0:
                        self._combi_exit_gone_at = now
                    elif now - self._combi_exit_gone_at >= 1.5:
                        self._combi_exit_gone_at = 0.0
                        self._exit_combi_program_edit()
                else:
                    self._combi_exit_gone_at = 0.0  # indicator back — reset holdoff

        # Exit boot phase once a mode is detected
        if self._frame_w._boot_phase and self._current_mode > 0:
            self._frame_w._boot_phase = False

    @Slot()
    def _on_disconnected(self):
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
        self._frame_w._is_connected  = True
        self._frame_w._boot_phase    = True
        self._frame_w._disconnect_msg = ""
        self._frame_w.update()
        self._mode_poll_timer.start()
        self._update_conn_mode_label()
        self._start_ping()
        # Push mirror state and screensaver timeout to daemon on every connect
        self._mirror_state = self._settings.vga_mirror_enabled
        self._ctrl_send("MIRROR_ON" if self._mirror_state else "MIRROR_OFF")
        self._ctrl_send(f"SS_TIMEOUT {self._settings.screensaver_timeout}")
        # Update perf window if open
        if self._perf_window:
            self._perf_window.update_host(self._host, self._ctrl_port)

    def _set_pending_mode(self, mode: int):
        """Record a user-requested mode without lighting the button immediately.
        Detection in _on_frame is authoritative; this falls back after 3 seconds."""
        self._pending_mode = mode
        QTimer.singleShot(3000, lambda m=mode: self._pending_mode_timeout(m))

    def _pending_mode_timeout(self, mode: int):
        """Fallback: if detection never confirmed within 3 s, apply the pending mode."""
        if self._pending_mode == mode:
            self._set_mode_button(mode)

    def _set_mode_button(self, mode: int):
        self._pending_mode = 0   # detection is authoritative — clear any pending request
        if mode != self._current_mode:
            self._prev_mode = self._current_mode
        self._current_mode = mode
        self._ctrl_surface.set_mode(mode)
        self._mode_label.setText(_MODE_NAMES[mode] if 1 <= mode <= 7 else "")

    def _combi_flash_tick(self):
        if not self._combi_prog_edit_active:
            self._combi_flash_timer.stop()
            return
        self._combi_prog_flash_state = not self._combi_prog_flash_state
        self._ctrl_surface.set_active("Program", self._combi_prog_flash_state)

    def _enter_combi_program_edit(self):
        self._combi_prog_edit_active = True
        self._combi_prog_flash_state = False
        self._ctrl_surface.set_active("Combi",   True)
        self._ctrl_surface.set_active("Program", False)
        self._combi_flash_timer.start()
        self._mode_label.setText("Program (from Combi)")
        print("[mode] combi program-edit: entered")

    def _exit_combi_program_edit(self):
        self._combi_prog_edit_active = False
        self._combi_flash_timer.stop()
        # Re-light whichever mode is actually current
        self._ctrl_surface.set_mode(self._current_mode)
        self._mode_label.setText(_MODE_NAMES[self._current_mode]
                                 if 1 <= self._current_mode <= 7 else "")
        print("[mode] combi program-edit: exited")

    def _poll_mode(self):
        if not self._host or not self._receiver:
            return
        if self._mode_detector.has_any():
            return  # frame-based detection is working — no need to poll STATE
        if self._poll_in_progress:
            return  # previous query still in flight — skip this tick
        self._poll_in_progress = True
        host = self._host
        port = self._ctrl_port
        threading.Thread(target=self._poll_mode_bg, args=(host, port),
                         daemon=True, name="ModePoll").start()

    def _poll_mode_bg(self, host: str, port: int):
        try:
            resp = self._ctrl.query(host, port, "STATE", timeout_ms=800)
            if not resp:
                return
            for part in resp.split():
                if part.startswith("MODE="):
                    try:
                        m = int(part[5:])
                        if m != self._current_mode and self._pending_mode == 0:
                            QTimer.singleShot(0, self, lambda mode=m: self._set_mode_button(mode))
                    except ValueError:
                        pass
        finally:
            self._poll_in_progress = False

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
        if entry:
            cmd, mode = entry
            self._ctrl_send(cmd)
            if mode > 0:
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

    # ── Keyboard ───────────────────────────────────────────────────────────────

    @Slot()
    def _set_kbd_capture(self):
        self._kbd_capture = True
        self._update_kbd_indicator()

    def _release_kbd_capture(self):
        self._kbd_capture = False
        self._update_kbd_indicator()

    def _update_kbd_indicator(self):
        _kbd_font = "font-family: 'Segoe UI Symbol';"
        if not self._kbd_send_en:
            self._kbd_label.setStyleSheet(f"color: #CC4444; {_kbd_font}")
            self._kbd_label.setToolTip("Keyboard send disabled")
        elif self._kbd_capture:
            self._kbd_label.setStyleSheet(f"color: #44BB44; {_kbd_font}")
            self._kbd_label.setToolTip("Keyboard captured — keys forwarded to Kronos")
        else:
            self._kbd_label.setStyleSheet(f"color: #888; {_kbd_font}")
            self._kbd_label.setToolTip("Keyboard capture — click in frame to capture")

    def keyPressEvent(self, event: QKeyEvent):
        key  = event.key()
        mods = event.modifiers()

        if event.isAutoRepeat():
            return

        # ── Kronos capture mode ─────────────────────────────────────────────
        # eventFilter already forwarded this key and consumed it; this branch
        # is a belt-and-suspenders fallback in case a key event slips through.
        if self._kbd_capture:
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

        # Escape: close help overlay → exit fullscreen → send BUTTON EXIT
        if key == Qt.Key_Escape:
            if self._frame_w._help_open:
                self._toggle_help()
                return
            if self._is_fullscreen:
                self._toggle_fullscreen()
                return
            self._ctrl_send("BUTTON EXIT")
            return

        # Ctrl shortcuts
        if mods & Qt.ControlModifier:
            if key == Qt.Key_S and not (mods & Qt.ShiftModifier):
                self._quick_save_screenshot(); return
            if key == Qt.Key_S and (mods & Qt.ShiftModifier):
                self._save_screenshot(); return
            if key == Qt.Key_K:
                self._open_command_palette(); return

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

        # Global shortcuts
        if not (mods & Qt.ControlModifier):
            if self._matches_keybind(event, "Quit"):
                self._try_quit(); return
            if self._matches_keybind(event, "Fullscreen"):
                self._toggle_fullscreen(); return
            if self._matches_keybind(event, "Zoom Window"):
                self._act_zoom.setChecked(not self._act_zoom.isChecked()); return
            if self._matches_keybind(event, "AspectLock"):
                self._act_aspect.setChecked(not self._act_aspect.isChecked()); return
            if self._matches_keybind(event, "Mirror"):
                self._toggle_mirror(); return
            if self._matches_keybind(event, "Help"):
                self._toggle_help(); return
            if self._matches_keybind(event, "Calibrate"):
                self._act_cal.setChecked(not self._act_cal.isChecked()); return

        # Mode select keybinds
        for i in range(1, 8):
            if self._matches_keybind(event, f"Mode {_MODE_NAMES[i]}"):
                self._ctrl_send(f"BUTTON {_MODE_CMDS[i]}")
                self._set_pending_mode(i)
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

    def _forward_key(self, event: QKeyEvent, pressed: bool):
        key  = event.key()
        mods = event.modifiers()
        val  = 1 if pressed else 0

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
        if delta > 0:
            self._ctrl_send("WHEEL CW")
            self._ctrl_surface.trigger_wheel_anim(1)
        elif delta < 0:
            self._ctrl_send("WHEEL CCW")
            self._ctrl_surface.trigger_wheel_anim(-1)
        event.accept()

    # ── View actions ───────────────────────────────────────────────────────────

    def _on_aspect_toggled(self, checked: bool):
        self._aspect_lock = checked
        self._frame_w._aspect_lock = checked
        self._frame_w.update()

    def _on_zoom_toggled(self, checked: bool):
        self._zoom_on = checked
        self._frame_w._zoom_on = checked
        self._frame_w.update()

    def _toggle_fullscreen(self):
        if self._is_fullscreen:
            self.showNormal()
        else:
            self.showFullScreen()
        self._is_fullscreen = not self._is_fullscreen

    def _on_hide_controls_toggled(self, checked: bool):
        self._set_controls_hidden(checked)

    def _apply_layout(self, preset: str):
        self._layout_preset = preset
        self._settings.layout_preset = preset
        focused = preset == "Focused"
        self._act_preset_full.setChecked(not focused)
        self._act_preset_focused.setChecked(focused)
        self._collapse_bar.setVisible(focused)
        if not focused:
            self._ctrl_surface.setVisible(True)
            self._collapse_bar.set_expanded(True)
            self._settings.hide_controls = False
            self._act_hide_ctrl.blockSignals(True)
            self._act_hide_ctrl.setChecked(False)
            self._act_hide_ctrl.blockSignals(False)
        else:
            ctrl_hidden = self._settings.hide_controls
            self._ctrl_surface.setVisible(not ctrl_hidden)
            self._collapse_bar.set_expanded(not ctrl_hidden)
            self._act_hide_ctrl.blockSignals(True)
            self._act_hide_ctrl.setChecked(ctrl_hidden)
            self._act_hide_ctrl.blockSignals(False)
        storage.save_settings(self._settings)

    def _toggle_controls_from_bar(self):
        self._set_controls_hidden(self._ctrl_surface.isVisible())

    def _set_controls_hidden(self, hidden: bool):
        self._ctrl_surface.setVisible(not hidden)
        self._collapse_bar.set_expanded(not hidden)
        self._settings.hide_controls = hidden
        self._act_hide_ctrl.blockSignals(True)
        self._act_hide_ctrl.setChecked(hidden)
        self._act_hide_ctrl.blockSignals(False)
        storage.save_settings(self._settings)

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
        base_w = int(1600 * scale)
        base_h = int(600 * scale + 50)  # +50 for menu+statusbar
        self.resize(base_w, base_h)
        for s, a in self._act_sz.items():
            a.setChecked(abs(s - scale) < 0.01)

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

    def _open_settings(self):
        from settings_window import SettingsWindow
        mirror_before  = self._settings.vga_mirror_enabled
        ss_before      = self._settings.screensaver_timeout
        debug_before   = self._settings.debug_logging
        dlg = SettingsWindow(self._settings, self)
        if dlg.exec() == QDialog.Accepted:
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

    # ── Command palette (simplified) ───────────────────────────────────────────

    def _open_command_palette(self):
        # Simple implementation: show QInputDialog with action list
        actions = [(a, l) for a, l, _ in get_rebindable()]
        items   = [f"{l}  [{self._settings.get_key_name(a)}]" for a, l in actions]
        item, ok = QInputDialog.getItem(self, "Command Palette", "Action:", items, 0, False)
        if ok and item:
            idx = items.index(item)
            action = actions[idx][0]
            # Trigger the action
            self._run_action(action)

    def _run_action(self, action: str):
        cmds: dict = {
            f"Mode {_MODE_NAMES[i]}": (
                lambda c=_MODE_CMDS[i], m=i: (self._ctrl_send(f"BUTTON {c}"), self._set_pending_mode(m))
            )
            for i in range(1, 8)
        }
        cmds.update({
            "Fullscreen": self._toggle_fullscreen,
            "Mirror":     self._toggle_mirror,
            "Calibrate":  lambda: self._act_cal.setChecked(not self._act_cal.isChecked()),
            "Help":       self._toggle_help,
            "Quit":       self._try_quit,
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
        a_zi.triggered.connect(lambda: self._zoom_step(+0.25))
        a_zo = menu.addAction("Zoom Out")
        a_zo.triggered.connect(lambda: self._zoom_step(-0.25))
        a_zr = menu.addAction("Reset Zoom")
        a_zr.triggered.connect(self._zoom_reset)
        menu.addSeparator()

        a_asp = menu.addAction("Aspect Lock")
        a_asp.setCheckable(True)
        a_asp.setChecked(self._aspect_lock)
        a_asp.triggered.connect(lambda chk: self._act_aspect.setChecked(chk))
        a_fs = menu.addAction("Fullscreen")
        a_fs.triggered.connect(self._toggle_fullscreen)
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
        self._frame_w._zoom_level = max(1.0, min(8.0, self._frame_w._zoom_level + delta))
        if not self._zoom_on:
            self._act_zoom.setChecked(True)
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

    def _set_conn_state(self, state: str, text: str):
        """Update connection dot + status label text together."""
        self._conn_dot.set_state(state)
        self._status_label.setText(text)
        color = {"disconnected": "#888888", "connecting": "#CCAA00", "connected": "#88DD88"}.get(
            state, "#888888")
        self._status_label.setStyleSheet(f"color: {color}; padding-left: 4px;")

    def _restore_conn_status(self):
        """Restore status to actual connection state after a temporary message."""
        if self._receiver:
            self._set_conn_state("connected", f"Connected — {self._host}")
        elif self._connecting:
            self._set_conn_state("connecting", f"Connecting to {self._host}…")
        else:
            self._set_conn_state("disconnected", "Not connected")

    def _update_conn_mode_label(self):
        if self._pull_mode:
            self._conn_mode_label.setText("Pull")
            self._conn_mode_label.setStyleSheet("color: #88AADD;")
        else:
            self._conn_mode_label.setText("Change")
            self._conn_mode_label.setStyleSheet("color: #888;")

    # ── Ping ───────────────────────────────────────────────────────────────────

    def _start_ping(self):
        self._ping_label.setText("⇄ —")
        self._ping_label.setStyleSheet("color: #555;")
        self._ping_inflight = False
        self._ping_timer.start()
        self._ping_once()

    def _stop_ping(self):
        self._ping_timer.stop()
        self._ping_inflight = False
        self._ping_label.setText("⇄ —")
        self._ping_label.setStyleSheet("color: #555;")

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
            self._ping_label.setStyleSheet("color: #CC3333;")
        else:
            if ms <= 15:
                color = "#44CC44"
            elif ms <= 50:
                color = "#CCCC44"
            else:
                color = "#CC4444"
            self._ping_label.setText(f"⇄ {int(ms)}ms")
            self._ping_label.setStyleSheet(f"color: {color};")

    # ── Notifications ──────────────────────────────────────────────────────────

    def _notify(self, msg: str, is_error: bool = False):
        self._notify_msgs.append(msg)
        self._notify_count += 1
        self._notify_label.setToolTip(f"{self._notify_count} notification(s)\nLast: {msg}")
        self._notify_label.setStyleSheet("color: #CC3333;" if is_error else "color: #CCAA00;")

    def _clear_notification(self):
        self._notify_count = 0
        self._notify_msgs.clear()
        self._notify_label.setStyleSheet("color: #444;")
        self._notify_label.setToolTip("No notifications")

    def _open_notify_log(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Notifications")
        dlg.resize(460, 240)
        layout = QVBoxLayout(dlg)
        txt = QTextEdit()
        txt.setReadOnly(True)
        txt.setStyleSheet("background: #111; color: #CCC; font-family: monospace;")
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
        super().changeEvent(event)
