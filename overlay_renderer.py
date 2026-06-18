"""
Overlay drawing — QPainter routines that draw on top of the frame widget.

All drawing is in widget coordinates (frame rect + overlay rect passed in).
This mirrors OverlayRenderer.cs which draws directly onto a DrawingContext.
"""
from __future__ import annotations
import math
import pathlib
import time
from typing import Dict, List, Optional, Set, Tuple

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import (
    QBrush, QColor, QFont, QImage, QPainter, QPen, QPixmap, QPolygonF,
)

from models import CalBiasDot, CalMesh, PaletteEntry


# ── Colors / style constants ───────────────────────────────────────────────────
_COL_OVERLAY_BG    = QColor(0, 0, 0, 180)
_COL_PANEL_BG      = QColor(26, 26, 26, 230)
_COL_ACCENT        = QColor(0x88, 0xAA, 0xDD)
_COL_ACCENT2       = QColor(0x55, 0x88, 0xBB)
_COL_TEXT_PRIMARY  = QColor(0xCC, 0xCC, 0xCC)
_COL_TEXT_DIM      = QColor(0x66, 0x66, 0x66)
_COL_SEP           = QColor(0x44, 0x44, 0x44)
_COL_SLIDER_BG     = QColor(0x33, 0x33, 0x33)
_COL_LOCKED        = QColor(0xFF, 0x80, 0x00, 180)
_COL_GRID          = QColor(0x00, 0xFF, 0x80, 100)
_COL_NODE_IDLE     = QColor(0x00, 0xFF, 0x80, 180)
_COL_NODE_HOVER    = QColor(0xFF, 0xFF, 0x00, 220)
_COL_NODE_DRAG     = QColor(0xFF, 0x60, 0x00, 220)
_COL_BIAS_DOT      = QColor(0x00, 0xCC, 0xFF, 200)
_COL_TOUCH_MARKER  = QColor(0xAA, 0xAA, 0xAA, 210)
_COL_BOOT_BAR      = QColor(0xFF, 0x00, 0x00)
_COL_DISCONNECTED  = QColor(0xFF, 0x44, 0x44)

_SWATCH_SIZE   = 12   # palette editor swatch px
_SWATCH_COLS   = 16
_SWATCH_ROWS   = 16
_SLIDER_W      = 162
_PANEL_PADDING = 12

# Boot splash bar extents in 1600-px image space
_BAR_Y1, _BAR_Y2         = 859, 865
_BAR_X_STATIC_END        = 864
_BAR_X_END               = 1442

_FONT_MONO  = QFont("Courier New", 10)
_FONT_SMALL = QFont("Segoe UI", 9) if True else QFont("sans-serif", 9)


class OverlayRenderer:
    def __init__(self):
        self._boot_splash: Optional[QPixmap] = None
        self._boot_splash_path = (
            pathlib.Path(__file__).parent / "Resources" / "Images" / "BootSplash.png"
        )
        self._boot_splash_loaded = False

    # ── Boot splash ────────────────────────────────────────────────────────────

    def draw_boot_splash(self, p: QPainter, frame_rect: QRectF,
                         progress_x: int):
        """
        Draw the boot splash image scaled to frame_rect, with a red progress bar
        at the mapped position of _BAR_Y1.._BAR_Y2 in the 1600-px image space.
        """
        if not self._boot_splash_loaded:
            self._boot_splash_loaded = True
            if self._boot_splash_path.exists():
                self._boot_splash = QPixmap(str(self._boot_splash_path))

        if self._boot_splash and not self._boot_splash.isNull():
            p.drawPixmap(frame_rect.toRect(), self._boot_splash)
            # Map bar coords from 1600×1200 image space to frame_rect
            img_w, img_h = 1600, 1200
            x0 = frame_rect.x() + frame_rect.width()  * _BAR_X_STATIC_END / img_w
            x1 = frame_rect.x() + frame_rect.width()  * progress_x         / img_w
            y0 = frame_rect.y() + frame_rect.height() * _BAR_Y1            / img_h
            y1 = frame_rect.y() + frame_rect.height() * _BAR_Y2            / img_h
            bar = QRectF(x0, y0, max(0.0, x1 - x0), y1 - y0)
            p.fillRect(bar, _COL_BOOT_BAR)
        else:
            # No image — draw a simple black overlay with text
            p.fillRect(frame_rect, Qt.black)
            p.setPen(_COL_ACCENT)
            p.setFont(_FONT_SMALL)
            p.drawText(frame_rect.toRect(), Qt.AlignCenter, "Connecting to Kronos…")

    # ── Disconnected overlay ───────────────────────────────────────────────────

    def draw_disconnected(self, p: QPainter, frame_rect: QRectF, message: str):
        p.fillRect(frame_rect, QColor(0, 0, 0, 160))
        p.setPen(_COL_DISCONNECTED)
        p.setFont(QFont("Segoe UI", 16, QFont.Bold))
        p.drawText(frame_rect.toRect(), Qt.AlignCenter, message)

    # ── Touch marker ──────────────────────────────────────────────────────────

    def draw_touch_marker(self, p: QPainter, frame_rect: QRectF,
                          nx: int, ny: int, alpha: float):
        """Draw a fading circle at (nx, ny) in frame pixel coords (0–799, 0–599)."""
        scale_x = frame_rect.width()  / 800
        scale_y = frame_rect.height() / 600
        cx = frame_rect.x() + nx * scale_x
        cy = frame_rect.y() + ny * scale_y
        r  = 14 * min(scale_x, scale_y)
        col = QColor(_COL_TOUCH_MARKER)
        col.setAlphaF(alpha * col.alphaF())
        p.setPen(QPen(col, 2.0))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(cx, cy), r, r)

    # ── Zoom loupe ─────────────────────────────────────────────────────────────

    def draw_zoom_loupe(self, p: QPainter, frame_pixmap: QPixmap,
                        frame_rect: QRectF, cursor_frame_x: int, cursor_frame_y: int,
                        zoom: float, loupe_w: int = 200, loupe_h: int = 150):
        """Draw a magnified loupe window near the cursor."""
        # Source region in frame_pixmap coords (8→ARGB already applied)
        src_w = loupe_w / zoom
        src_h = loupe_h / zoom
        src_x = cursor_frame_x - src_w / 2
        src_y = cursor_frame_y - src_h / 2
        # Clamp to frame
        fw = frame_pixmap.width()
        fh = frame_pixmap.height()
        src_x = max(0.0, min(fw - src_w, src_x))
        src_y = max(0.0, min(fh - src_h, src_y))

        # Position loupe near the cursor but stay inside frame_rect
        scale_x = frame_rect.width()  / fw
        scale_y = frame_rect.height() / fh
        cx = frame_rect.x() + cursor_frame_x * scale_x
        cy = frame_rect.y() + cursor_frame_y * scale_y
        lx = cx + 20
        ly = cy - loupe_h - 20
        if lx + loupe_w > frame_rect.right():
            lx = cx - loupe_w - 20
        if ly < frame_rect.top():
            ly = cy + 20

        dst = QRectF(lx, ly, loupe_w, loupe_h)
        src = QRectF(src_x, src_y, src_w, src_h)
        p.save()
        p.setClipRect(dst)
        p.drawPixmap(dst, frame_pixmap, src)
        p.setClipping(False)
        p.setPen(QPen(_COL_ACCENT, 1.5))
        p.setBrush(Qt.NoBrush)
        p.drawRect(dst)
        # Crosshair at centre
        cx2, cy2 = dst.center().x(), dst.center().y()
        p.setPen(QPen(QColor(255, 255, 255, 120), 1.0))
        p.drawLine(QPointF(cx2 - 8, cy2), QPointF(cx2 + 8, cy2))
        p.drawLine(QPointF(cx2, cy2 - 8), QPointF(cx2, cy2 + 8))
        p.restore()

    # ── Help overlay ──────────────────────────────────────────────────────────

    def draw_help(self, p: QPainter, frame_rect: QRectF,
                  keybinds: list[tuple[str, str]]):
        """Two-column key/description table centered in frame_rect."""
        p.fillRect(frame_rect, QColor(0, 0, 0, 210))
        p.setFont(QFont("Courier New", 9))
        fm = p.fontMetrics()
        row_h = fm.height() + 4
        title_h = row_h + 8

        col_w = 280
        n_rows = (len(keybinds) + 1) // 2
        table_w = col_w * 2
        table_h = title_h + n_rows * row_h
        ox = frame_rect.x() + (frame_rect.width()  - table_w) / 2
        oy = frame_rect.y() + (frame_rect.height() - table_h) / 2

        p.setPen(_COL_ACCENT)
        p.drawText(QRectF(ox, oy, table_w, title_h), Qt.AlignCenter, "Keyboard Shortcuts")
        oy += title_h

        p.setPen(_COL_SEP)
        p.drawLine(QPointF(ox, oy), QPointF(ox + table_w, oy))

        for i, (key, desc) in enumerate(keybinds):
            col   = i % 2
            row   = i // 2
            cell_x = ox + col * col_w
            cell_y = oy + row * row_h
            p.setPen(_COL_ACCENT2)
            p.drawText(QRectF(cell_x + 4, cell_y, 70, row_h), Qt.AlignVCenter | Qt.AlignLeft, key)
            p.setPen(_COL_TEXT_PRIMARY)
            p.drawText(QRectF(cell_x + 78, cell_y, col_w - 82, row_h),
                       Qt.AlignVCenter | Qt.AlignLeft, desc)

    # ── Palette editor overlay ─────────────────────────────────────────────────

    def draw_palette_editor(self, p: QPainter, frame_rect: QRectF,
                            palette: list[PaletteEntry], lut: list[int],
                            selected: int, channel: int,
                            overrides: Dict[int, PaletteEntry], locked: Set[int],
                            hover_idx: Optional[int], typed: Optional[str]
                            ) -> Tuple[QRectF, QPointF, float]:
        """
        Draw the palette editor panel.
        Returns (panel_rect, grid_origin, slider_top) for hit-testing.
        """
        swatch_area_w = _SWATCH_COLS * _SWATCH_SIZE
        swatch_area_h = _SWATCH_ROWS * _SWATCH_SIZE
        panel_w = max(swatch_area_w, _SLIDER_W) + _PANEL_PADDING * 2
        slider_extra_h = 4 * (14 + 6) + 40   # 3 sliders + buttons
        panel_h = _PANEL_PADDING + swatch_area_h + _PANEL_PADDING + slider_extra_h + _PANEL_PADDING

        # Anchor to right side of frame
        px = frame_rect.right() - panel_w - 6
        py = frame_rect.top() + 6
        if py + panel_h > frame_rect.bottom() - 6:
            py = frame_rect.bottom() - panel_h - 6
        panel_rect = QRectF(px, py, panel_w, panel_h)

        p.fillRect(panel_rect, _COL_PANEL_BG)
        p.setPen(QPen(_COL_SEP, 1))
        p.drawRect(panel_rect)

        grid_x = px + _PANEL_PADDING
        grid_y = py + _PANEL_PADDING

        # Swatches
        for idx in range(256):
            row = idx // _SWATCH_COLS
            col = idx  % _SWATCH_COLS
            sx = grid_x + col * _SWATCH_SIZE
            sy = grid_y + row * _SWATCH_SIZE
            sw = QRectF(sx, sy, _SWATCH_SIZE, _SWATCH_SIZE)

            packed = lut[idx]
            r = (packed >> 16) & 0xFF
            g = (packed >> 8)  & 0xFF
            b =  packed        & 0xFF
            p.fillRect(sw, QColor(r, g, b))

            if idx in locked:
                p.fillRect(sw, _COL_LOCKED)
            if idx == selected:
                p.setPen(QPen(Qt.white, 1.5))
                p.drawRect(sw.adjusted(0.5, 0.5, -0.5, -0.5))
            elif idx == hover_idx:
                p.setPen(QPen(QColor(200, 200, 200, 180), 1.0))
                p.drawRect(sw.adjusted(0.5, 0.5, -0.5, -0.5))

        # Slider section
        slider_top = grid_y + swatch_area_h + _PANEL_PADDING
        sel_entry = palette[selected] if 0 <= selected < 256 else PaletteEntry(0, 0, 0)
        channels = [("R", sel_entry.r, QColor(180, 40, 40)),
                    ("G", sel_entry.g, QColor(40, 160, 40)),
                    ("B", sel_entry.b, QColor(40, 80, 180))]

        sy = slider_top
        for ci, (lbl, val, col) in enumerate(channels):
            # Label
            p.setPen(_COL_TEXT_PRIMARY if ci == channel else _COL_TEXT_DIM)
            p.setFont(QFont("Courier New", 9))
            p.drawText(QRectF(grid_x, sy, 14, 14), Qt.AlignVCenter, lbl)
            # Track
            track = QRectF(grid_x + 18, sy + 3, _SLIDER_W, 8)
            p.fillRect(track, _COL_SLIDER_BG)
            filled = QRectF(track.x(), track.y(), _SLIDER_W * val / 255, track.height())
            p.fillRect(filled, col)
            # Value text / typed input
            display = typed if (ci == channel and typed is not None) else str(val)
            p.setPen(_COL_TEXT_PRIMARY)
            p.drawText(QRectF(grid_x + 18 + _SLIDER_W + 4, sy, 36, 14),
                       Qt.AlignVCenter | Qt.AlignLeft, display)
            sy += 20

        # Hint
        p.setPen(_COL_TEXT_DIM)
        p.setFont(QFont("Courier New", 8))
        hint = f"#{selected:02X}  R{sel_entry.r} G{sel_entry.g} B{sel_entry.b}"
        if selected in overrides:
            hint += "  (override)"
        p.drawText(QRectF(grid_x, sy, panel_w - _PANEL_PADDING * 2, 14),
                   Qt.AlignVCenter, hint)

        return panel_rect, QPointF(grid_x, grid_y), slider_top

    # ── Calibration overlay ───────────────────────────────────────────────────

    def draw_cal_overlay(self, p: QPainter, frame_rect: QRectF,
                         mesh: CalMesh, bias_dots: list[CalBiasDot],
                         hover_node: Optional[Tuple[int, int]],
                         dragging_node: Optional[Tuple[int, int]],
                         cal_dirty: bool):
        fw = 800
        fh = 600
        scale_x = frame_rect.width()  / fw
        scale_y = frame_rect.height() / fh

        def to_screen(nx: float, ny: float) -> QPointF:
            return QPointF(frame_rect.x() + nx * scale_x,
                           frame_rect.y() + ny * scale_y)

        pen_grid = QPen(QColor(0x00, 0xBB, 0x50, 210), 1.8)
        p.setPen(pen_grid)
        for c in range(mesh.cols):
            for r in range(mesh.rows - 1):
                x1, y1 = mesh.node_dst(c, r,     fw, fh)
                x2, y2 = mesh.node_dst(c, r + 1, fw, fh)
                p.drawLine(to_screen(x1, y1), to_screen(x2, y2))
        for r in range(mesh.rows):
            for c in range(mesh.cols - 1):
                x1, y1 = mesh.node_dst(c,     r, fw, fh)
                x2, y2 = mesh.node_dst(c + 1, r, fw, fh)
                p.drawLine(to_screen(x1, y1), to_screen(x2, y2))

        # Nodes
        for c in range(mesh.cols):
            for r in range(mesh.rows):
                nx, ny = mesh.node_dst(c, r, fw, fh)
                sp = to_screen(nx, ny)
                if dragging_node == (c, r):
                    col = _COL_NODE_DRAG
                elif hover_node == (c, r):
                    col = _COL_NODE_HOVER
                else:
                    col = _COL_NODE_IDLE
                p.setPen(Qt.NoPen)
                p.setBrush(col)
                p.drawEllipse(sp, 5, 5)

        # Bias dots — displayed at their mesh-warped position so they follow the mesh
        p.setPen(QPen(_COL_BIAS_DOT, 1.5))
        p.setBrush(Qt.NoBrush)
        for d in bias_dots:
            disp_x, disp_y = mesh.apply(d.nx, d.ny, fw, fh)
            sp = to_screen(disp_x, disp_y)
            p.drawEllipse(sp, 5, 5)
            p.drawLine(QPointF(sp.x() - 3, sp.y()), QPointF(sp.x() + 3, sp.y()))
            p.drawLine(QPointF(sp.x(), sp.y() - 3), QPointF(sp.x(), sp.y() + 3))

        # Status bar
        dirty_flag = " [unsaved]" if cal_dirty else ""
        status_text = (f"Calibration{dirty_flag}  |  "
                       f"S: save  X: clear all dots  R: reset  Ctrl+Z/Y: undo/redo  "
                       f"RClick: dot  Esc: exit")
        bar_h = 18
        bar_rect = QRectF(frame_rect.x(), frame_rect.bottom() - bar_h,
                          frame_rect.width(), bar_h)
        p.fillRect(bar_rect, QColor(0, 0, 0, 180))
        bar_color = QColor(0xCC, 0x44, 0x44) if cal_dirty else QColor(0x44, 0xBB, 0x44)
        p.setPen(bar_color)
        p.setFont(QFont("Courier New", 8))
        p.drawText(bar_rect.toRect(), Qt.AlignVCenter | Qt.AlignHCenter, status_text)
