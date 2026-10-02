"""Piano-strip keymap for one multisample — port of Views/SampleKeymapControl.cs.

A white-key-proportional piano (full 0-127) with a zone-assignment bar above it. Each zone boundary is a
draggable handle (KmpZone.TopKey); dragging a zone's bar segment onto another reorders them; clicking a piano key
auditions that key's zone (held, not latched). The control mutates nothing itself — it reports gestures and the
model decides what they mean.

Layout, top to bottom: a raised label strip (full sample name of the SELECTED zone), the zone bar, the piano, and
three thin 88/73/61-key range indicator strips.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from Data.kmp_multisample import KmpZone
from Utils.midi_note_name import to_name, try_parse

RAISED_LABEL_HEIGHT = 14
ZONE_BAR_HEIGHT = 16
RANGE_BAR_HEIGHT = 4
RANGE_BARS_TOTAL = RANGE_BAR_HEIGHT * 3
HIT_TEST_PIXELS = 5

KEY88 = (try_parse("A0"), try_parse("C8"))
KEY73 = (try_parse("E1"), try_parse("E7"))
KEY61 = (try_parse("C2"), try_parse("C7"))

PANEL_BG = QColor("#232323")
TEXT = QColor("#E0E0E0")
DIVIDER = QColor("#444444")
HOVER = QColor("#363636")
DISABLED = QColor("#606060")
SELECTED = QColor(0xE0, 0xA0, 0x30, 0x55)
DRAG_ORIGIN = QColor(0xB8, 0x86, 0x0B)
DRAG_HOVER = QColor("yellow")
WHITE_KEY = QColor(0xE8, 0xE8, 0xE8)
BLACK_KEY = QColor(0x18, 0x18, 0x18)
KEY_HIGHLIGHT = QColor(140, 140, 140, 100)


def is_black_key(midi: int) -> bool:
    return (midi % 12) in (1, 3, 6, 8, 10)


def build_layout(width: float) -> Tuple[float, List[float], List[float]]:
    """Per-key left/right pixel edges. White keys get equal slots; a black key is 60 % of a white key's width,
    centred on the boundary between the two white keys it falls between."""
    white_index = []
    white_count = 0
    for k in range(128):
        white_index.append(white_count)
        if not is_black_key(k):
            white_count += 1
    white_w = max(0.01, width / max(1, white_count))
    left, right = [0.0] * 128, [0.0] * 128
    for k in range(128):
        if is_black_key(k):
            center = white_index[k] * white_w
            bw = white_w * 0.6
            left[k], right[k] = center - bw / 2, center + bw / 2
        else:
            left[k], right[k] = white_index[k] * white_w, (white_index[k] + 1) * white_w
    return white_w, left, right


def compute_ranges(zones: Optional[List[KmpZone]]) -> List[Tuple[KmpZone, int, int]]:
    """Each zone's trigger range: (previous zone's TopKey + 1) .. its own TopKey."""
    if not zones:
        return []
    out, prev_top = [], -1
    for z in zones:
        low = max(0, min(prev_top + 1, 127))
        high = max(low, min(z.top_key, 127))
        out.append((z, low, high))
        prev_top = z.top_key
    return out


def zone_at(key: int, ranges) -> Optional[KmpZone]:
    for z, low, high in ranges:
        if low <= key <= high:
            return z
    return None


def pixel_to_boundary_key(x: float, right: List[float], min_key: int, max_key: int) -> int:
    """Nearest boundary position by SCAN, not x / whiteWidth: the layout isn't linear in MIDI number (black keys
    are interspersed), so dividing capped the drag short of the right side and desynced direction of travel."""
    best, best_d = min_key, float("inf")
    for k in range(min_key, max_key + 1):
        d = abs(x - right[k])
        if d < best_d:
            best, best_d = k, d
    return best


class SampleKeymapControl(QWidget):
    zone_clicked = Signal(object)
    piano_key_clicked = Signal(object, int)
    piano_key_released = Signal()
    piano_key_ctrl_clicked = Signal(int)
    boundary_moved = Signal(object, int)
    zone_reordered = Signal(object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setMinimumHeight(104)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._zones: Optional[List[KmpZone]] = None
        self.selected_zone: Optional[KmpZone] = None
        self._hover_boundary = -1
        self._drag_boundary = -1
        self._drag_pending_key = -1
        self._drag_anchor_x = 0.0
        self._drag_zone_origin: Optional[KmpZone] = None
        self._drag_zone_hover: Optional[KmpZone] = None
        self._piano_key_down = False
        self._grabbed = False

    @property
    def zones(self) -> Optional[List[KmpZone]]:
        return self._zones

    @zones.setter
    def zones(self, value: Optional[List[KmpZone]]) -> None:
        if value is not self._zones:
            self._hover_boundary = self._drag_boundary = -1
            self._drag_zone_origin = self._drag_zone_hover = None
        self._zones = value
        self.update()

    def _in_zone_bar(self, y: float) -> bool:
        return RAISED_LABEL_HEIGHT <= y < RAISED_LABEL_HEIGHT + ZONE_BAR_HEIGHT

    def _hit_boundary(self, right, ranges, x) -> int:
        for i, (_z, _lo, hi) in enumerate(ranges):
            if abs(x - right[hi]) <= HIT_TEST_PIXELS:
                return i
        return -1

    def _pixel_to_key(self, x: float, y: Optional[float], left, right) -> int:
        """Black keys first (they sit on top near a boundary) but only in the band they are DRAWN in, so a click
        in a black key's x-range below its bottom hits the white key visibly under the cursor."""
        if y is not None:
            piano_top = RAISED_LABEL_HEIGHT + ZONE_BAR_HEIGHT
            black_bottom = piano_top + max(1, self.height() - piano_top - RANGE_BARS_TOTAL) * 0.6
            if y < black_bottom:
                for k in range(128):
                    if is_black_key(k) and left[k] <= x < right[k]:
                        return k
        for k in range(128):
            if not is_black_key(k) and left[k] <= x < right[k]:
                return k
        return 0 if x < 0 else 127

    def _grab(self) -> None:
        self._grabbed = True
        self.grabMouse()

    def _release(self) -> None:
        if self._grabbed:
            self._grabbed = False
            self.releaseMouse()

    def mouseMoveEvent(self, e) -> None:
        _w, left, right = build_layout(self.width())
        ranges = compute_ranges(self._zones)
        x, y = e.position().x(), e.position().y()

        if self._drag_boundary >= 0:
            # Floor only: the previous zone's Top Key can't be invaded, but dragging past the NEXT zone's pushes
            # it (and everything after it) up — the model owns the shift, applied once on mouse-up.
            min_key = ranges[self._drag_boundary - 1][2] + 1 if self._drag_boundary > 0 else 0
            self._drag_pending_key = pixel_to_boundary_key(x, right, min_key, 127)
            self.update()
            return
        if self._drag_zone_origin is not None:
            hover = zone_at(self._pixel_to_key(x, None, left, right), ranges)
            if hover is not self._drag_zone_hover:
                self._drag_zone_hover = hover
                self.update()
            self.setCursor(Qt.CursorShape.SizeAllCursor)
            return
        # The resize cursor lives in the zone-bar strip only; over the piano a boundary's x can sit under a key
        # the user wants to click.
        hb = self._hit_boundary(right, ranges, x) if self._in_zone_bar(y) else -1
        if hb != self._hover_boundary:
            self._hover_boundary = hb
            if hb >= 0:
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            else:
                self.unsetCursor()
            self.update()

    def mousePressEvent(self, e) -> None:
        if e.button() != Qt.MouseButton.LeftButton:
            return
        x, y = e.position().x(), e.position().y()
        piano_top = RAISED_LABEL_HEIGHT + ZONE_BAR_HEIGHT
        # Ctrl+Click on the piano writes a key into the focused Orig./Top Key field — checked BEFORE taking focus
        # so the field the user armed is still the focused widget when the window reads it.
        if (e.modifiers() & Qt.KeyboardModifier.ControlModifier) and y >= piano_top:
            _w, left, right = build_layout(self.width())
            self.piano_key_ctrl_clicked.emit(self._pixel_to_key(x, y, left, right))
            return

        self.setFocus()
        _w, left, right = build_layout(self.width())
        ranges = compute_ranges(self._zones)

        if self._in_zone_bar(y):
            hit = self._hit_boundary(right, ranges, x)
            if hit >= 0:
                self._drag_boundary = hit
                self._drag_pending_key = ranges[hit][2]
                self._drag_anchor_x = x
                self._grab()
                self.update()
                return

        key = self._pixel_to_key(x, y, left, right)
        hit_zone = zone_at(key, ranges)
        if hit_zone is None:
            return
        if self._in_zone_bar(y):
            # A POTENTIAL reorder: whether it becomes one or a plain select is decided on release.
            self._drag_zone_origin = hit_zone
            self._drag_zone_hover = hit_zone
            self._grab()
            self.update()
            return
        # The raised label strip only selects. A PIANO click must never change the selection or any Top Key — it
        # only auditions; Ctrl+Click is the only piano gesture that writes a key.
        if y < RAISED_LABEL_HEIGHT:
            self.zone_clicked.emit(hit_zone)
            return
        self._piano_key_down = True
        self._grab()
        self.piano_key_clicked.emit(hit_zone, key)

    def mouseReleaseEvent(self, e) -> None:
        if e.button() != Qt.MouseButton.LeftButton:
            return
        if self._drag_boundary >= 0 and self._drag_pending_key >= 0:
            # A plain click that merely landed near a boundary must not nudge a Top Key: the pointer has to move
            # at least the hit tolerance to count as a drag.
            dragged = abs(e.position().x() - self._drag_anchor_x) > HIT_TEST_PIXELS
            zone = compute_ranges(self._zones)[self._drag_boundary][0]
            pending = self._drag_pending_key
            self._drag_boundary = -1
            self._drag_pending_key = -1
            if dragged and pending != zone.top_key:
                self.boundary_moved.emit(zone, pending)
        self._drag_boundary = -1
        self._drag_pending_key = -1

        if self._drag_zone_origin is not None:
            _w, left, right = build_layout(self.width())
            ranges = compute_ranges(self._zones)
            drop = zone_at(self._pixel_to_key(e.position().x(), None, left, right), ranges)
            origin = self._drag_zone_origin
            self._drag_zone_origin = self._drag_zone_hover = None
            if drop is not None and drop is not origin:
                self.zone_reordered.emit(origin, drop)
            else:
                self.zone_clicked.emit(origin)
        self._release()
        self._notify_key_released()
        self.update()

    def _notify_key_released(self) -> None:
        if self._piano_key_down:
            self._piano_key_down = False
            self.piano_key_released.emit()

    def event(self, ev) -> bool:
        # Capture lost without a mouse-up (window deactivated mid-hold): the key is no longer held.
        if ev.type() in (QEvent.Type.UngrabMouse, QEvent.Type.WindowDeactivate):
            self._grabbed = False
            self._drag_boundary = -1
            self._drag_zone_origin = self._drag_zone_hover = None
            self._notify_key_released()
        return super().event(ev)

    def leaveEvent(self, _e) -> None:
        if self._hover_boundary != -1 and self._drag_boundary < 0:
            self._hover_boundary = -1
            self.update()

    # ── painting ───────────────────────────────────────────────────────────────────────────

    def paintEvent(self, _e) -> None:
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        p = QPainter(self)
        p.fillRect(0, 0, w, h, PANEL_BG)
        _ww, left, right = build_layout(w)
        ranges = compute_ranges(self._zones)
        piano_top = RAISED_LABEL_HEIGHT + ZONE_BAR_HEIGHT
        piano_h = max(1, h - piano_top - RANGE_BARS_TOTAL)
        sel_range = next((r for r in ranges if r[0] is self.selected_zone), None) if self.selected_zone is not None else None

        f = QFont(p.font())
        f.setPixelSize(9)
        p.setFont(f)
        if not ranges:
            p.setPen(QColor("#888888"))
            p.drawText(QPointF(4, RAISED_LABEL_HEIGHT + 11), "No zones")
        else:
            border = QPen(DIVIDER, 1)

            def grid_edge(key: int, is_right: bool) -> float:
                return (left[key] + right[key]) / 2 if is_black_key(key) else (right[key] if is_right else left[key])

            def fill(i: int, zone: KmpZone, rect: QRectF) -> None:
                color = DISABLED if zone.is_skipped else HOVER if i % 2 == 0 else PANEL_BG
                p.fillRect(rect, color)
                if zone is self.selected_zone:
                    p.fillRect(rect, SELECTED)

            # Two passes: a black key is drawn WIDER than its grid slot, so one rect per zone would overlap its
            # neighbour at a black boundary and painter's order would hand that key to the wrong zone. Pass 1
            # tiles on the shared white-grid edges; pass 2 repaints a boundary black key with its true owner.
            for i, (z, lo, hi) in enumerate(ranges):
                gx0, gx1 = grid_edge(lo, False), grid_edge(hi, True)
                fill(i, z, QRectF(gx0, RAISED_LABEL_HEIGHT, max(1, gx1 - gx0), ZONE_BAR_HEIGHT))
            for i, (z, lo, hi) in enumerate(ranges):
                if is_black_key(lo):
                    fill(i, z, QRectF(left[lo], RAISED_LABEL_HEIGHT, max(1, right[lo] - left[lo]), ZONE_BAR_HEIGHT))
                if hi != lo and is_black_key(hi):
                    fill(i, z, QRectF(left[hi], RAISED_LABEL_HEIGHT, max(1, right[hi] - left[hi]), ZONE_BAR_HEIGHT))

            p.setPen(border)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(QRectF(0, RAISED_LABEL_HEIGHT, w - 1, ZONE_BAR_HEIGHT))
            for i in range(len(ranges) - 1):
                bx = right[ranges[i][2]]
                p.drawLine(QPointF(bx, RAISED_LABEL_HEIGHT), QPointF(bx, RAISED_LABEL_HEIGHT + ZONE_BAR_HEIGHT))

            for z, lo, hi in ranges:
                rect = QRectF(left[lo], RAISED_LABEL_HEIGHT, max(1, right[hi] - left[lo]), ZONE_BAR_HEIGHT)
                if self._drag_zone_origin is not None and z is self._drag_zone_origin:
                    p.setPen(QPen(DRAG_ORIGIN, 2))
                    p.drawRect(rect)
                if (self._drag_zone_hover is not None and z is self._drag_zone_hover
                        and z is not self._drag_zone_origin):
                    p.setPen(QPen(DRAG_HOVER, 2))
                    p.drawRect(rect)
                # Clipped to its own segment: KMP/KSF names have no break points, so an unclipped long
                # name bleeds into the next zone.
                if rect.width() > 12:
                    p.save()
                    p.setClipRect(rect)
                    p.setPen(TEXT)
                    p.drawText(QPointF(rect.x() + 1, RAISED_LABEL_HEIGHT + 12), "-" if z.is_skipped else z.filename)
                    p.restore()

        # Raised label: the selected zone's FULL name, so a narrow segment's clipped label needn't carry it.
        if sel_range is not None and not sel_range[0].is_skipped:
            x0, x1 = left[sel_range[1]], right[sel_range[2]]
            fm = p.fontMetrics()
            tw = fm.horizontalAdvance(sel_range[0].filename)
            label_x = max(0.0, min(x0 + (x1 - x0 - tw) / 2, max(0.0, w - tw)))
            p.fillRect(QRectF(label_x - 3, 0, tw + 6, RAISED_LABEL_HEIGHT), SELECTED)
            p.setPen(QColor("white"))
            p.drawText(QPointF(label_x, RAISED_LABEL_HEIGHT - 3), sel_range[0].filename)

        # Piano: white keys, their highlight, then black keys on top and theirs — interleaved so a black key's
        # opaque fill covers a neighbour white key's highlight where they overlap.
        def in_sel(k: int) -> bool:
            return sel_range is not None and sel_range[1] <= k <= sel_range[2]

        p.setPen(QPen(QColor("black"), 0.5))
        for k in range(128):
            if is_black_key(k):
                continue
            r = QRectF(left[k], piano_top, right[k] - left[k], piano_h)
            p.setBrush(WHITE_KEY)
            p.drawRect(r)
            if in_sel(k):
                p.fillRect(r, KEY_HIGHLIGHT)
        black_h = piano_h * 0.6
        p.setPen(Qt.PenStyle.NoPen)
        for k in range(128):
            if not is_black_key(k):
                continue
            r = QRectF(left[k], piano_top, right[k] - left[k], black_h)
            p.setBrush(BLACK_KEY)
            p.drawRect(r)
            if in_sel(k):
                p.fillRect(r, KEY_HIGHLIGHT)

        if piano_h > 14:
            f8 = QFont(p.font())
            f8.setPixelSize(8)
            p.setFont(f8)
            p.setPen(QColor("black"))
            for k in range(0, 128, 12):
                x = left[k] + 1
                if x + p.fontMetrics().horizontalAdvance(to_name(k)) <= w:
                    p.drawText(QPointF(x, piano_top + piano_h - 3), to_name(k))

        # Boundary handle: drawn only while hovered or dragged (the always-visible dividers were clutter).
        for i, (_z, _lo, hi) in enumerate(ranges):
            if not (self._drag_boundary == i or self._hover_boundary == i):
                continue
            x = right[max(0, min(self._drag_pending_key, 127))] if self._drag_boundary == i else right[hi]
            p.setPen(QPen(QColor("yellow"), 2))
            p.drawLine(QPointF(x, piano_top), QPointF(x, piano_top + piano_h))

        # 88 / 73 / 61-key range indicators, closest to the keys first.
        ry = piano_top + piano_h
        for j, ((lo, hi), color) in enumerate(((KEY88, "#CC3333"), (KEY73, "#3366CC"), (KEY61, "#33AA55"))):
            p.fillRect(QRectF(left[lo], ry + RANGE_BAR_HEIGHT * j, right[hi] - left[lo], RANGE_BAR_HEIGHT), QColor(color))
        p.end()
