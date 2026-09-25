"""SampleKeymapControl — visual piano-keyboard keymap widget for the Sample
Editor's zone list. Port of C#'s `Views/SampleKeymapControl.cs` (745 lines).

Ported: the white-key-proportional piano layout, the zone-assignment bar
with its two-pass paint (see `_draw_zone_bar`'s docstring for the black-key-
boundary bug this avoids from the start), the 88/73/61-key range indicator
strips, and click-to-select (a piano key or a zone-bar segment). NOT ported
— deliberately out of scope for this pass, see CLAUDE.md: boundary-drag
zone resizing, zone-bar drag-to-reorder, and piano-key press-to-play (no
audio playback engine exists in this Sample Editor yet — see the standing
"Phase 4-6 fake audio/DSP code" note). Add Zone is a toolbar action in
`sample_editor_window.py`, not a drag gesture here, so none of C#'s
zone-creation flows actually depend on the missing interactions.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

from Data.kmp_multisample import KmpZone
import Utils.theme as T

_RAISED_LABEL_HEIGHT = 16
_ZONE_BAR_HEIGHT = 18
_RANGE_BAR_HEIGHT = 5
_RANGE_BARS_TOTAL_HEIGHT = _RANGE_BAR_HEIGHT * 3

# MIDI note numbers (C4=60 convention, matching the app's display elsewhere)
# for the three keyboard-range indicator strips — hardware-confirmed spans,
# see C#'s own Key88Low/High etc. comment: 88-key=A0..C8, 73-key=E1..E7,
# 61-key=C2..C7.
_KEY88_LOW, _KEY88_HIGH = 21, 108
_KEY73_LOW, _KEY73_HIGH = 28, 100
_KEY61_LOW, _KEY61_HIGH = 36, 96

_WHITE_SEMITONES = {0, 2, 4, 5, 7, 9, 11}   # C D E F G A B, within an octave


def _is_black_key(note: int) -> bool:
    return (note % 12) not in _WHITE_SEMITONES


def _note_name(note: int) -> str:
    """C4=60 convention, matching this app's other display code."""
    names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    octave = note // 12 - 1
    return f"{names[note % 12]}{octave}"


def _build_layout(width: float) -> Tuple[float, List[float], List[float]]:
    """(white_key_width, leftX[128], rightX[128]) for a proportional piano
    layout — white keys tile the full width edge-to-edge in ascending-note
    order; each black key is centered on the white/white boundary it falls
    between (leftX[n-1..n+1] all being white-key-contiguous, so that
    boundary is simply rightX of the white key below it), narrower than a
    white key. Matches C#'s own BuildLayout."""
    white_notes = [n for n in range(128) if not _is_black_key(n)]
    white_width = width / len(white_notes) if white_notes else 0.0
    left_x = [0.0] * 128
    right_x = [0.0] * 128
    for i, n in enumerate(white_notes):
        left_x[n] = i * white_width
        right_x[n] = (i + 1) * white_width
    black_width = white_width * 0.6
    for n in range(128):
        if not _is_black_key(n):
            continue
        # n-1 and n+1 are always white (no two black keys are ever
        # adjacent), and white keys tile with zero gap, so rightX[n-1] ==
        # leftX[n+1] — the boundary this black key straddles.
        boundary = right_x[n - 1] if n > 0 else 0.0
        left_x[n] = boundary - black_width / 2
        right_x[n] = boundary + black_width / 2
    return white_width, left_x, right_x


def _grid_edge(key: int, right: bool, left_x: List[float], right_x: List[float]) -> float:
    """A black key's "grid edge" collapses to its own center (the shared
    white/white boundary) so pass-1 zone fills tile with zero gap/overlap
    regardless of which neighboring zone owns that boundary key."""
    if _is_black_key(key):
        return (left_x[key] + right_x[key]) / 2
    return right_x[key] if right else left_x[key]


class SampleKeymapControl(QWidget):
    """Displays a multisample's zones as a piano-keyed range map. Read/select
    only — see module docstring for what's deliberately not implemented."""

    zone_clicked = Signal(int)   # emits the clicked zone's index into `zones`

    def __init__(self, parent=None):
        super().__init__(parent)
        self._zones: List[KmpZone] = []
        self._selected_index: Optional[int] = None
        self.setMinimumHeight(90)
        self.setMouseTracking(False)

    def set_zones(self, zones: List[KmpZone]):
        self._zones = zones
        self.update()

    def set_selected_index(self, index: Optional[int]):
        self._selected_index = index
        self.update()

    # ── Zone ranges ──────────────────────────────────────────────────────────

    def _ranges(self) -> List[Tuple[int, KmpZone, int, int]]:
        """(zone_index, zone, low_key, high_key) per zone — a zone's own low
        key is the previous zone's top_key + 1 (or 0 for the first zone);
        KmpZone itself has no separate bottom-key field, see its own
        docstring in Data/kmp_multisample.py."""
        out = []
        low = 0
        for i, z in enumerate(self._zones):
            high = max(low, min(127, z.top_key))
            out.append((i, z, low, high))
            low = high + 1
        return out

    def _zone_index_at(self, key: int) -> Optional[int]:
        for i, _z, low, high in self._ranges():
            if low <= key <= high:
                return i
        return None

    # ── Painting ─────────────────────────────────────────────────────────────

    def paintEvent(self, _event):
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.fillRect(self.rect(), QColor(T.PANEL))

        _white_width, left_x, right_x = _build_layout(w)
        ranges = self._ranges()
        piano_top = _RAISED_LABEL_HEIGHT + _ZONE_BAR_HEIGHT
        piano_height = max(1, h - piano_top - _RANGE_BARS_TOTAL_HEIGHT)

        selected = None
        if self._selected_index is not None:
            for i, z, low, high in ranges:
                if i == self._selected_index:
                    selected = (z, low, high)
                    break

        self._draw_label(p, w, selected)
        self._draw_zone_bar(p, ranges, left_x, right_x)
        self._draw_piano(p, left_x, right_x, piano_top, piano_height)
        self._draw_range_bars(p, left_x, right_x, piano_top + piano_height)
        p.end()

    def _draw_label(self, p: QPainter, w: float, selected):
        p.setPen(QColor(T.TEXT))
        font = QFont(T.FONT_UI_FAMILY, 9)
        p.setFont(font)
        if selected is None:
            text = "No zone selected" if self._zones else "No zones"
            p.setPen(QColor(T.TEXT_DIM))
        else:
            zone, low, high = selected
            name = "(no sample)" if zone.is_skipped else zone.filename
            text = f"{_note_name(low)}–{_note_name(high)}  root {_note_name(zone.original_key)}  {name}"
        p.drawText(QRectF(4, 0, w - 8, _RAISED_LABEL_HEIGHT),
                   Qt.AlignVCenter | Qt.AlignLeft, text)

    def _draw_zone_bar(self, p: QPainter, ranges, left_x, right_x):
        """Two-pass paint — port of C#'s DrawZoneFill/GridEdge fix.

        A black key is drawn WIDER than its own white-grid slot (centered
        on the white/white boundary it falls between). Painting one rect
        per zone straight from leftX[low] to rightX[high] makes adjacent
        zones' rects overlap by that margin whenever a boundary key is
        black — painter's order then decides which zone's fill wins,
        wrongly favoring whichever zone paints LAST regardless of which one
        actually owns that key. Fixed in two passes: pass 1 fills every
        zone using edges snapped to the shared grid boundary (_grid_edge)
        so segments tile with zero gap/overlap; pass 2 repaints only a
        zone's own boundary key, when that key is black, using its true
        wider rect — matching the piano's own black-key overlay drawn
        immediately below. Borders are drawn once per real boundary
        afterward, at the same x the (currently unimplemented) draggable
        boundary line would use — never per-rect, which would disagree
        with a black key's wider true edge by this same margin."""
        y = _RAISED_LABEL_HEIGHT
        if not ranges:
            return

        # Pass 1: grid-snapped fills, zero gap/overlap by construction.
        for i, zone, low, high in ranges:
            gx0 = _grid_edge(low, False, left_x, right_x)
            gx1 = _grid_edge(high, True, left_x, right_x)
            self._fill_zone_rect(p, i, zone, QRectF(gx0, y, max(1.0, gx1 - gx0), _ZONE_BAR_HEIGHT))

        # Pass 2: repaint only a zone's own low/high edge when it's black,
        # with its true (wider) rect. Interior black keys need no repaint —
        # their true span already sits fully inside that zone's pass-1 rect.
        for i, zone, low, high in ranges:
            if _is_black_key(low):
                self._fill_zone_rect(
                    p, i, zone, QRectF(left_x[low], y, right_x[low] - left_x[low], _ZONE_BAR_HEIGHT))
            if high != low and _is_black_key(high):
                self._fill_zone_rect(
                    p, i, zone, QRectF(left_x[high], y, right_x[high] - left_x[high], _ZONE_BAR_HEIGHT))

        # Borders: outer frame + one separator per real zone boundary.
        pen = QPen(QColor(T.BORDER))
        pen.setWidth(1)
        p.setPen(pen)
        x_end = right_x[127] if right_x else 0.0
        p.drawRect(QRectF(0, y, x_end, _ZONE_BAR_HEIGHT))
        for i, _zone, _low, high in ranges[:-1]:
            bx = right_x[high] if not _is_black_key(high) else (left_x[high] + right_x[high]) / 2
            p.drawLine(int(bx), int(y), int(bx), int(y + _ZONE_BAR_HEIGHT))

    def _fill_zone_rect(self, p: QPainter, index: int, zone: KmpZone, rect: QRectF):
        if zone.is_skipped:
            fill = QColor(T.TEXT_FAINT)
        elif index % 2 == 0:
            fill = QColor(T.PANEL_ALT)
        else:
            fill = QColor(T.PANEL)
        p.fillRect(rect, fill)
        if index == self._selected_index:
            sel = QColor(T.ACCENT_DEEP)
            sel.setAlpha(160)
            p.fillRect(rect, sel)

    def _draw_piano(self, p: QPainter, left_x, right_x, top: float, height: float):
        white_fill = QColor(0xE8, 0xE8, 0xE8)
        black_fill = QColor(0x18, 0x18, 0x18)
        border = QPen(QColor(T.BORDER))
        border.setWidth(1)

        for n in range(128):
            if _is_black_key(n):
                continue
            rect = QRectF(left_x[n], top, right_x[n] - left_x[n], height)
            p.fillRect(rect, white_fill)
            p.setPen(border)
            p.drawRect(rect)

        black_height = height * 0.6
        for n in range(128):
            if not _is_black_key(n):
                continue
            rect = QRectF(left_x[n], top, right_x[n] - left_x[n], black_height)
            p.fillRect(rect, black_fill)

    def _draw_range_bars(self, p: QPainter, left_x, right_x, top: float):
        specs = [
            (_KEY88_LOW, _KEY88_HIGH, QColor(0xCC, 0x33, 0x33)),
            (_KEY73_LOW, _KEY73_HIGH, QColor(0x33, 0x66, 0xCC)),
            (_KEY61_LOW, _KEY61_HIGH, QColor(0x33, 0xAA, 0x55)),
        ]
        for row, (low, high, color) in enumerate(specs):
            y = top + row * _RANGE_BAR_HEIGHT
            x0, x1 = left_x[low], right_x[high]
            p.fillRect(QRectF(x0, y, max(1.0, x1 - x0), _RANGE_BAR_HEIGHT), color)

    # ── Interaction: click-to-select only ───────────────────────────────────

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        w = self.width()
        _white_width, left_x, right_x = _build_layout(w)
        x, y = event.position().x(), event.position().y()
        piano_top = _RAISED_LABEL_HEIGHT + _ZONE_BAR_HEIGHT

        key = self._key_at_x(x, left_x, right_x, y >= piano_top)
        if key is None:
            return
        idx = self._zone_index_at(key)
        if idx is not None:
            self.zone_clicked.emit(idx)

    @staticmethod
    def _key_at_x(x: float, left_x: List[float], right_x: List[float], prefer_black: bool) -> Optional[int]:
        """Which of the 128 keys x falls under. When prefer_black (a click
        within the piano's own drawing area), a black key's narrower true
        rect takes priority over the white key visually behind it."""
        if prefer_black:
            for n in range(128):
                if _is_black_key(n) and left_x[n] <= x <= right_x[n]:
                    return n
        for n in range(128):
            if not _is_black_key(n) and left_x[n] <= x <= right_x[n]:
                return n
        return None
