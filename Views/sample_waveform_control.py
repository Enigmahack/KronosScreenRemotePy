"""Waveform pane for the Sample Editor — port of Views/SampleWaveformControl.cs.

Min/max-bucketed trace of one PCM buffer with a zoom/pan view window, crop-selection drag, draggable Sample
Start / Loop Start / Loop End markers (Kronos's own red / green / blue), a scrub line and a playhead. This
control owns the horizontal view window; the ruler and scrollbar beside it are separate widgets kept in sync
through `view_changed` / `set_view`.

Drags only ever move a PREVIEW (selection and markers alike); the committed values are written once at
mouse-up. Abandoning a drag (alt-tab, focus loss) therefore costs nothing to undo — dropping the previews IS the
revert.
"""
from __future__ import annotations

import math
import time
from typing import List, Optional

import numpy as np
from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QImage, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from Core.sample_editor_model.markers import SampleMarkerKind
from Core.waveform_pyramid import WaveformPyramid

HIT_TEST_PIXELS = 5
MAX_TRACE_COLUMNS = 1920
WHEEL_ACCEL_WINDOW_S = 0.150

# Theme values from C# Themes/Dark.xaml.
PANEL_BG = QColor("#232323")
GRID = QColor(255, 255, 255, 0x1A)
TRACE = QColor("#D8D8D8")
SELECTION = QColor(0xE0, 0xA0, 0x30, 0x55)
LOOP_REGION = QColor(0x2E, 0x7F, 0xD6, 0x33)
LOOP_LOCKED = QColor(0x33, 0xCC, 0x55, 0x55)
SAMPLE_START = QColor("#FF0000")
LOOP_START = QColor("#00FF00")
LOOP_END = QColor("#0000FF")
ACCENT = QColor("#88AADD")
MUTED = QColor("#888888")


def nice_interval(frames_per_pixel: float, target_pixels: float) -> int:
    """A '1/2/5 x 10^n' frame interval landing roughly `target_pixels` apart — graph-paper axis spacing."""
    rough = frames_per_pixel * target_pixels
    if rough < 1:
        return 1
    magnitude = 10 ** math.floor(math.log10(rough))
    residual = rough / magnitude
    nice = 1 if residual < 1.5 else 2 if residual < 3.5 else 5 if residual < 7.5 else 10
    return max(1, int(nice * magnitude))


def trace_columns(samples: np.ndarray, pyramid: WaveformPyramid, view_start: int, view_end: int,
                  columns: int):
    """(mins, maxs, valid_count) for `columns` equal slices of [view_start, view_end), clamped to the buffer.
    Columns past the end of THIS buffer end the envelope (valid_count stops there) — a pane in a stereo pair can
    be shorter than the shared view. Over-inclusion at bucket edges only ever shows a peak a column early or
    late, never loses one."""
    view_len = max(1, view_end - view_start)
    n = len(samples)
    cols = np.arange(columns, dtype=np.int64)
    starts = view_start + cols * view_len // columns
    valid = int(np.searchsorted(starts, n, side="left"))          # columns whose start is inside the buffer
    if valid <= 0:
        return None, None, 0
    starts = starts[:valid]
    read_end = min(view_end, n)
    level = pyramid.pick(view_len / columns)
    if level is None:
        src_min = src_max = samples[:read_end]
        idx = starts
    else:
        b = level.bucket
        nb = min((read_end + b - 1) // b, len(level.min))
        src_min, src_max = level.min[:nb], level.max[:nb]
        idx = np.minimum(starts // b, nb - 1)
    idx = np.minimum(idx, len(src_min) - 1)
    mins = np.minimum.reduceat(src_min, idx)
    maxs = np.maximum.reduceat(src_max, idx)
    return mins.astype(np.int32), maxs.astype(np.int32), valid


class SampleWaveformControl(QWidget):
    selection_changed = Signal()
    selection_preview_changed = Signal()
    view_changed = Signal()
    loop_region_changed = Signal(int, int)
    marker_dragged = Signal(object, int)
    markers_changing = Signal()
    scrub_requested = Signal(int)
    waveform_moved = Signal(int)
    channel_double_clicked = Signal()
    drag_started = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setMinimumHeight(60)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self._samples: Optional[np.ndarray] = None
        self._pyramid: Optional[WaveformPyramid] = None
        self._selection_start = 0
        self._selection_end = 0
        self._sample_start = 0
        self._loop_start = 0
        self._loop_end = 0
        self._playhead = -1
        self._scrub = -1

        self.loop_enabled = False
        self.loop_lock_enabled = False
        self.move_tool_active = False
        self.scroll_to_zoom = True
        self.can_move_waveform = False
        self.is_split_channel_pane = False
        self.is_active_channel = False
        self.view_frame_count = 0

        self._view_start = 0
        self._view_end = 0
        self._last_frame_count = -1

        self._prev_sel: Optional[tuple] = None
        self._prev_sample_start: Optional[int] = None
        self._prev_loop_start: Optional[int] = None
        self._prev_loop_end: Optional[int] = None

        self._drag_anchor = -1
        self._drag_moved = False
        self._dragging_marker: Optional[SampleMarkerKind] = None
        self._dragging_loop = False
        self._loop_drag_moved = False
        self._loop_anchor = self._loop_start_at_anchor = self._loop_end_at_anchor = 0
        self._moving_selection = False
        self._sel_move_moved = False
        self._sel_anchor = self._sel_start_at_anchor = self._sel_end_at_anchor = 0
        self._moving_waveform = False
        self._wave_anchor_x = 0.0
        self._wave_dx = 0.0
        self._grabbed = False

        self._last_wheel = 0.0
        self._wheel_accel = 1.0
        self._trace_key = None
        self._trace_image: Optional[QImage] = None

    # ── committed state (setters clear the matching drag preview, like the DP callbacks) ──

    @property
    def samples(self) -> Optional[np.ndarray]:
        return self._samples

    @samples.setter
    def samples(self, value: Optional[np.ndarray]) -> None:
        if value is self._samples:
            return
        self._samples = value
        self._pyramid = None
        self._trace_key = None
        self._on_samples_changed()
        self.update()

    @property
    def selection_start_frame(self) -> int:
        return self._selection_start

    @selection_start_frame.setter
    def selection_start_frame(self, v: int) -> None:
        if v != self._selection_start:
            self._selection_start = v
            self.clear_preview_selection()
            self.update()

    @property
    def selection_end_frame(self) -> int:
        return self._selection_end

    @selection_end_frame.setter
    def selection_end_frame(self, v: int) -> None:
        if v != self._selection_end:
            self._selection_end = v
            self.clear_preview_selection()
            self.update()

    @property
    def sample_start_frame(self) -> int:
        return self._sample_start

    @sample_start_frame.setter
    def sample_start_frame(self, v: int) -> None:
        if v != self._sample_start:
            self._sample_start = v
            self.clear_preview_markers()
            self.update()

    @property
    def loop_start_frame(self) -> int:
        return self._loop_start

    @loop_start_frame.setter
    def loop_start_frame(self, v: int) -> None:
        if v != self._loop_start:
            self._loop_start = v
            self.clear_preview_markers()
            self.update()

    @property
    def loop_end_frame(self) -> int:
        return self._loop_end

    @loop_end_frame.setter
    def loop_end_frame(self, v: int) -> None:
        if v != self._loop_end:
            self._loop_end = v
            self.clear_preview_markers()
            self.update()

    @property
    def playhead_frame(self) -> int:
        return self._playhead

    @playhead_frame.setter
    def playhead_frame(self, v: int) -> None:
        if v != self._playhead:
            self._playhead = v
            self.update()

    @property
    def scrub_frame(self) -> int:
        return self._scrub

    @scrub_frame.setter
    def scrub_frame(self, v: int) -> None:
        if v != self._scrub:
            self._scrub = v
            self.update()

    # ── previews ───────────────────────────────────────────────────────────────────────────

    @property
    def effective_selection_start(self) -> int:
        return self._prev_sel[0] if self._prev_sel is not None else self._selection_start

    @property
    def effective_selection_end(self) -> int:
        return self._prev_sel[1] if self._prev_sel is not None else self._selection_end

    @property
    def effective_sample_start(self) -> int:
        return self._sample_start if self._prev_sample_start is None else self._prev_sample_start

    @property
    def effective_loop_start(self) -> int:
        return self._loop_start if self._prev_loop_start is None else self._prev_loop_start

    @property
    def effective_loop_end(self) -> int:
        return self._loop_end if self._prev_loop_end is None else self._prev_loop_end

    @property
    def has_marker_preview(self) -> bool:
        return (self._prev_sample_start is not None or self._prev_loop_start is not None
                or self._prev_loop_end is not None)

    def set_preview_selection(self, start: int, end: int) -> None:
        self._prev_sel = (start, end)
        self.update()

    def clear_preview_selection(self) -> None:
        if self._prev_sel is not None:
            self._prev_sel = None
            self.update()

    def set_preview_markers(self, sample_start: int, loop_start: int, loop_end: int) -> None:
        self._prev_sample_start, self._prev_loop_start, self._prev_loop_end = sample_start, loop_start, loop_end
        self.update()

    def clear_preview_markers(self) -> None:
        if self.has_marker_preview:
            self._prev_sample_start = self._prev_loop_start = self._prev_loop_end = None
            self.update()

    # ── view window ────────────────────────────────────────────────────────────────────────

    @property
    def frame_count(self) -> int:
        return 0 if self._samples is None else len(self._samples)

    @property
    def view_span_frame_count(self) -> int:
        return max(self.frame_count, self.view_frame_count)

    @property
    def view_start_frame(self) -> int:
        return self._view_start

    @property
    def view_end_frame(self) -> int:
        return self.view_span_frame_count if self._view_end == 0 else self._view_end

    @property
    def _min_view_len(self) -> int:
        return max(1, int(self.width() * 1.25))

    def _on_samples_changed(self) -> None:
        """Reset pan/zoom only when the FRAME COUNT changed — that is what distinguishes 'a different sample was
        loaded' (or a length-changing edit) from 'the same sample was re-decoded': a field-only edit reassigns
        the buffer too, and must not snap the view back to full."""
        n = self.frame_count
        span = self.view_span_frame_count
        if n != self._last_frame_count:
            self._view_start = 0
            self._view_end = span
            self._scrub = -1
        else:
            self._view_start = max(0, min(self._view_start, max(0, span - 1)))
            self._view_end = max(self._view_start + 1, min(self._view_end, span))
        self._last_frame_count = n
        self.view_changed.emit()

    def set_view(self, start: int, end: int) -> None:
        span = max(1, self.view_span_frame_count)
        length = max(min(self._min_view_len, span), min(end - start, span))
        start = max(0, min(start, max(0, span - length)))
        if start == self._view_start and start + length == self._view_end:
            return
        self._view_start, self._view_end = start, start + length
        self.update()
        self.view_changed.emit()

    def reset_view(self) -> None:
        self._view_start, self._view_end = 0, self.view_span_frame_count
        self.update()
        self.view_changed.emit()

    def _pixel_to_frame(self, x: float) -> int:
        w = self.width()
        if w <= 0 or self.frame_count == 0:
            return 0
        view_len = max(1, self.view_end_frame - self._view_start)
        return max(0, min(self._view_start + int(x / w * view_len), self.frame_count))

    def _frame_to_pixel(self, frame: int) -> float:
        view_len = max(1, self.view_end_frame - self._view_start)
        return (frame - self._view_start) * self.width() / view_len

    def _marker_x(self, frame: int) -> float:
        return max(0.75, min(self._frame_to_pixel(frame), self.width() - 0.75))

    def _near(self, x: float, frame: int) -> bool:
        return abs(x - self._frame_to_pixel(frame)) <= HIT_TEST_PIXELS

    def _in_loop(self, frame: int) -> bool:
        return self._has_loop and self._loop_start <= frame < self._loop_end

    @property
    def _has_loop(self) -> bool:
        return self.loop_enabled and self._loop_end > self._loop_start

    # ── mouse ──────────────────────────────────────────────────────────────────────────────

    def _grab(self) -> None:
        self._grabbed = True
        self.grabMouse()

    def _release(self) -> None:
        if self._grabbed:
            self._grabbed = False
            self.releaseMouse()

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.RightButton:
            self._on_right_press(e)
            return
        if e.button() != Qt.MouseButton.LeftButton or self.frame_count == 0:
            return
        self.setFocus()
        self.drag_started.emit()
        x = e.position().x()
        click_frame = self._pixel_to_frame(x)
        self._press_x = x
        self._press_frame = click_frame

        if e.type() == QEvent.Type.MouseButtonDblClick:
            self._on_double_click(click_frame)
            return

        # Marker edges take priority over the loop body / crop selection, in draw order.
        if self._near(x, self._sample_start):
            self._dragging_marker = SampleMarkerKind.SAMPLE_START
            self._grab()
            return
        if self._has_loop and self._near(x, self._loop_start):
            self._dragging_marker = SampleMarkerKind.LOOP_START
            self._grab()
            return
        if self._has_loop and self._near(x, self._loop_end):
            self._dragging_marker = SampleMarkerKind.LOOP_END
            self._grab()
            return

        # Move mode ONLY: Select mode must never move anything. A loop spanning the entire sample can never move
        # (the clamp pins it to 0), so it falls through to a plain selection.
        if (self.move_tool_active and self._in_loop(click_frame)
                and self._loop_end - self._loop_start < self.frame_count):
            self._dragging_loop = True
            self._loop_drag_moved = False
            self._loop_anchor = click_frame
            self._loop_start_at_anchor, self._loop_end_at_anchor = self._loop_start, self._loop_end
            self._grab()
            return

        if self.move_tool_active:
            if self._selection_end > self._selection_start and self._selection_start <= click_frame < self._selection_end:
                self._moving_selection = True
                self._sel_move_moved = False
                self._sel_anchor = click_frame
                self._sel_start_at_anchor, self._sel_end_at_anchor = self._selection_start, self._selection_end
                self._grab()
                return
            if self.can_move_waveform and self.frame_count > 0:
                self._moving_waveform = True
                self._wave_dx = 0.0
                self._wave_anchor_x = x
                self._grab()
                return
            # Move mode with nothing here to move: an ordinary selection drag rather than a dead click.

        self._drag_anchor = click_frame
        self._drag_moved = False
        self.set_preview_selection(click_frame, click_frame)
        self._grab()

    def mouseDoubleClickEvent(self, e) -> None:
        self.mousePressEvent(e)

    def _on_double_click(self, click_frame: int) -> None:
        # Inside the loop region it selects the loop (a selection, not a move, so it isn't gated on the tool).
        if self._has_loop and self._in_loop(click_frame):
            self.clear_preview_selection()
            self.selection_start_frame = self._loop_start
            self.selection_end_frame = self._loop_end
            self.selection_changed.emit()
            return
        # Split L/R: outside the loop, double-click picks a channel instead of resetting zoom.
        if self.is_split_channel_pane:
            self.channel_double_clicked.emit()
            return
        self.reset_view()

    def mouseMoveEvent(self, e) -> None:
        x = e.position().x()
        if self._dragging_marker is not None:
            frame = self._pixel_to_frame(x)
            kind = self._dragging_marker
            if kind is SampleMarkerKind.SAMPLE_START:
                self._prev_sample_start = frame
            elif kind is SampleMarkerKind.LOOP_START:
                self._prev_loop_start = min(frame, self._loop_end)
            else:
                self._prev_loop_end = max(frame, self._loop_start)
            self.update()
            self.markers_changing.emit()
            return

        if self._dragging_loop:
            frame = self._pixel_to_frame(x)
            delta = frame - self._loop_anchor
            if delta != 0:
                self._loop_drag_moved = True
            length = self._loop_end_at_anchor - self._loop_start_at_anchor
            new_start = max(0, min(self._loop_start_at_anchor + delta, max(0, self.frame_count - length)))
            self._prev_loop_start, self._prev_loop_end = new_start, new_start + length
            self.update()
            self.markers_changing.emit()
            return

        if self._moving_selection:
            frame = self._pixel_to_frame(x)
            delta = frame - self._sel_anchor
            if delta != 0:
                self._sel_move_moved = True
            length = self._sel_end_at_anchor - self._sel_start_at_anchor
            new_start = max(0, min(self._sel_start_at_anchor + delta, max(0, self.frame_count - length)))
            self.set_preview_selection(new_start, new_start + length)
            self.selection_preview_changed.emit()
            return

        if self._moving_waveform:
            self._wave_dx = x - self._wave_anchor_x
            self.update()
            return

        if self._drag_anchor < 0:
            self._update_hover_cursor(x)
            return

        frame = self._pixel_to_frame(x)
        if frame != self._drag_anchor:
            self._drag_moved = True
        self.set_preview_selection(min(self._drag_anchor, frame), max(self._drag_anchor, frame))
        self.selection_preview_changed.emit()

    def _update_hover_cursor(self, x: float) -> None:
        hover = self._pixel_to_frame(x)
        loop_grab = (self.move_tool_active and self._in_loop(hover)
                     and self._loop_end - self._loop_start < self.frame_count)
        sel_grab = (self.move_tool_active and self._selection_end > self._selection_start
                    and self._selection_start <= hover < self._selection_end)
        if (self._near(x, self._sample_start)
                or (self._has_loop and (self._near(x, self._loop_start) or self._near(x, self._loop_end)))):
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        elif loop_grab or sel_grab or (self.move_tool_active and self.can_move_waveform):
            self.setCursor(Qt.CursorShape.SizeAllCursor)
        else:
            self.unsetCursor()

    def mouseReleaseEvent(self, e) -> None:
        if e.button() != Qt.MouseButton.LeftButton:
            return
        # Each branch clears its own drag state BEFORE releasing the grab: the release re-enters the
        # ungrab handler, which would otherwise discard the very preview being read here.
        if self._dragging_marker is not None:
            marker = self._dragging_marker
            self._dragging_marker = None
            self._release()
            dragged = {SampleMarkerKind.SAMPLE_START: self._prev_sample_start,
                       SampleMarkerKind.LOOP_START: self._prev_loop_start}.get(marker, self._prev_loop_end)
            self._prev_sample_start = self._prev_loop_start = self._prev_loop_end = None
            if dragged is not None:
                if marker is SampleMarkerKind.SAMPLE_START:
                    self.sample_start_frame = dragged
                elif marker is SampleMarkerKind.LOOP_START:
                    self.loop_start_frame = dragged
                else:
                    self.loop_end_frame = dragged
            # Reported even when the press never moved: in Split L/R that is what makes clicking a marker
            # activate its channel, and set_marker's no-op guard keeps an unchanged value from dirtying anything.
            self.update()
            value = {SampleMarkerKind.SAMPLE_START: self._sample_start,
                     SampleMarkerKind.LOOP_START: self._loop_start}.get(marker, self._loop_end)
            self.marker_dragged.emit(marker, value)
            return

        if self._dragging_loop:
            self._dragging_loop = False
            self._release()
            moved_start = self._prev_loop_start if self._prev_loop_start is not None else self._loop_start
            moved_end = self._prev_loop_end if self._prev_loop_end is not None else self._loop_end
            self._prev_loop_start = self._prev_loop_end = None
            if self._loop_drag_moved:
                self.loop_start_frame, self.loop_end_frame = moved_start, moved_end
                self.loop_region_changed.emit(moved_start, moved_end)
            else:
                self.scrub_frame = self._loop_anchor
                self.scrub_requested.emit(self._loop_anchor)
            self.update()
            return

        if self._moving_selection:
            self._moving_selection = False
            self._release()
            if self._sel_move_moved and self._prev_sel is not None:
                s, en = self._prev_sel
                self.selection_start_frame, self.selection_end_frame = s, en
                self.selection_changed.emit()
            else:
                self.clear_preview_selection()
                self.scrub_frame = self._sel_anchor
                self.scrub_requested.emit(self._sel_anchor)
            self.update()
            return

        if self._moving_waveform:
            self._moving_waveform = False
            self._release()
            pixel_delta, self._wave_dx = self._wave_dx, 0.0
            if pixel_delta != 0:
                view_len = max(1, self.view_end_frame - self._view_start)
                frame_delta = int(round(pixel_delta * view_len / max(1, self.width())))
                if frame_delta != 0:
                    self.waveform_moved.emit(frame_delta)
            self.update()
            return

        if self._drag_anchor < 0:
            return
        clicked = self._drag_anchor
        self._drag_anchor = -1
        self._release()
        if not self._drag_moved:
            # A plain click: drop any existing highlight (end > start is the "there is a selection" convention
            # everywhere), then treat it as a scrub point.
            self.clear_preview_selection()
            had = self._selection_end > self._selection_start
            self.selection_start_frame = clicked
            self.selection_end_frame = clicked
            if had:
                self.selection_changed.emit()
            self.scrub_frame = clicked
            self.scrub_requested.emit(clicked)
            return
        new_start, new_end = self._prev_sel
        self.selection_start_frame, self.selection_end_frame = new_start, new_end
        self.selection_changed.emit()

    def _on_right_press(self, e) -> None:
        """Right-click on/inside the selection keeps it (the context menu acts on it); outside it collapses to a
        point at the click, matching most editors."""
        if self.frame_count == 0:
            return
        frame = self._pixel_to_frame(e.position().x())
        if self._selection_start <= frame < self._selection_end:
            return
        self.selection_start_frame = frame
        self.selection_end_frame = frame
        self.selection_changed.emit()

    def event(self, ev) -> bool:
        t = ev.type()
        if t == QEvent.Type.UngrabMouse or t == QEvent.Type.WindowDeactivate or t == QEvent.Type.FocusOut:
            self._abandon_drags()
        return super().event(ev)

    def _abandon_drags(self) -> None:
        """Alt-tab, a focus-stealing dialog, or anything else that takes the grab mid-drag: ABANDON every drag
        (only a real mouse-up commits). The previews were never committed, so dropping them is the revert."""
        if (self._dragging_marker is None and not self._dragging_loop and not self._moving_selection
                and not self._moving_waveform and self._drag_anchor < 0):
            return
        self._dragging_marker = None
        self._dragging_loop = False
        self._moving_selection = False
        self._moving_waveform = False
        self._wave_dx = 0.0
        self._drag_anchor = -1
        self._prev_sample_start = self._prev_loop_start = self._prev_loop_end = None
        self._prev_sel = None
        self._grabbed = False
        self.markers_changing.emit()
        self.update()

    def keyPressEvent(self, e) -> None:
        """Left/Right nudge the loop region by one frame — gated on Loop Lock, the same condition the mouse
        whole-region drag uses (nudging and dragging are the same action)."""
        if (e.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right) and self.loop_lock_enabled and self._has_loop):
            delta = 1 if e.key() == Qt.Key.Key_Right else -1
            length = self._loop_end - self._loop_start
            new_start = max(0, min(self._loop_start + delta, max(0, self.frame_count - length)))
            if new_start != self._loop_start:
                self.loop_start_frame, self.loop_end_frame = new_start, new_start + length
                self.loop_region_changed.emit(self._loop_start, self._loop_end)
            e.accept()
            return
        super().keyPressEvent(e)

    def wheelEvent(self, e) -> None:
        if not self.scroll_to_zoom or self.frame_count == 0:
            e.ignore()                                   # bubbles to the outer scroll area, like scrolling elsewhere
            return
        e.accept()
        now = time.monotonic()
        since = now - self._last_wheel if self._last_wheel else 1e9
        self._last_wheel = now
        # A fast spin compounds (up to 0.8^4 per notch); a deliberate single notch keeps the fine 0.8 step.
        self._wheel_accel = min(self._wheel_accel + 0.4, 4.0) if since < WHEEL_ACCEL_WINDOW_S else 1.0

        view_len = self.view_end_frame - self._view_start
        cursor_frame = self._pixel_to_frame(e.position().x())
        factor = (0.8 if e.angleDelta().y() > 0 else 1.25) ** self._wheel_accel
        span = self.view_span_frame_count
        new_len = max(min(self._min_view_len, span), min(int(view_len * factor), span))
        t = 0.5 if view_len == 0 else (cursor_frame - self._view_start) / view_len
        new_start = max(0, min(cursor_frame - int(new_len * t), span - new_len))
        self._view_start, self._view_end = new_start, new_start + new_len
        self.update()
        self.view_changed.emit()

    # ── painting ───────────────────────────────────────────────────────────────────────────

    def _build_trace(self, view_start: int, view_end: int, w: int, h: int) -> Optional[QImage]:
        key = (id(self._samples), view_start, view_end, w, h)
        if key == self._trace_key:
            return self._trace_image
        samples = self._samples
        if self._pyramid is None:
            self._pyramid = WaveformPyramid(samples)
        columns = max(1, min(w, MAX_TRACE_COLUMNS))
        mins, maxs, valid = trace_columns(samples, self._pyramid, view_start, view_end, columns)
        img = None
        if valid > 0:
            mid = h / 2
            scale = h / 2 / 32768.0
            # Quantised to whole rows; a silent column collapses to zero height, so it keeps a single row.
            row_top = np.clip((mid - maxs * scale).astype(np.int32), 0, h - 1)
            row_bot = np.clip((mid - mins * scale).astype(np.int32), 0, h - 1)
            lo, hi = np.minimum(row_top, row_bot), np.maximum(row_top, row_bot)
            rows = np.arange(h, dtype=np.int32)[:, None]
            mask = (rows >= lo[None, :]) & (rows <= hi[None, :])
            buf = np.zeros((h, columns, 4), np.uint8)
            buf[:, :valid][mask] = (TRACE.blue(), TRACE.green(), TRACE.red(), TRACE.alpha())   # BGRA
            img = QImage(buf.data, columns, h, columns * 4, QImage.Format.Format_ARGB32).copy()
        self._trace_key, self._trace_image = key, img
        return img

    def paintEvent(self, _e) -> None:
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        p = QPainter(self)
        p.fillRect(0, 0, w, h, PANEL_BG)
        samples = self._samples
        if samples is None or len(samples) == 0:
            p.setPen(MUTED)
            p.drawText(QRectF(0, 0, w, h), Qt.AlignmentFlag.AlignCenter, "No audio data in this file")
            p.end()
            return

        if self._moving_waveform:
            p.translate(self._wave_dx, 0)
        span = self.view_span_frame_count
        view_start = max(0, min(self._view_start, span))
        view_end = max(view_start + 1, min(self.view_end_frame, span))
        view_len = view_end - view_start

        interval = nice_interval(view_len / w, 90)
        p.setPen(QPen(GRID, 1))
        f = ((view_start + interval - 1) // interval) * interval
        while f < view_end:
            x = round(self._frame_to_pixel(f)) + 0.5
            p.drawLine(QPointF(x, 0), QPointF(x, h))
            f += interval

        ls, le = self.effective_loop_start, self.effective_loop_end
        eff_loop = self.loop_enabled and le > ls
        if eff_loop:
            x0, x1 = self._frame_to_pixel(max(ls, view_start)), self._frame_to_pixel(min(le, view_end))
            if x1 > x0:
                p.fillRect(QRectF(x0, 0, x1 - x0, h), LOOP_LOCKED if self.loop_lock_enabled else LOOP_REGION)

        ss, se = self.effective_selection_start, self.effective_selection_end
        if se > ss:
            x0, x1 = self._frame_to_pixel(max(ss, view_start)), self._frame_to_pixel(min(se, view_end))
            if x1 > x0:
                p.fillRect(QRectF(x0, 0, x1 - x0, h), SELECTION)

        trace = self._build_trace(view_start, view_end, w, h)
        if trace is not None:
            p.drawImage(QRectF(0, 0, w, h), trace)

        if eff_loop:
            if view_start <= ls <= view_end:
                p.setPen(QPen(LOOP_START, 1.5))
                x = self._marker_x(ls)
                p.drawLine(QPointF(x, 0), QPointF(x, h))
            if view_start <= le <= view_end:
                p.setPen(QPen(LOOP_END, 1.5))
                x = self._marker_x(le)
                p.drawLine(QPointF(x, 0), QPointF(x, h))
        sst = self.effective_sample_start
        if view_start <= sst <= view_end:
            p.setPen(QPen(SAMPLE_START, 1.5))
            x = self._marker_x(sst)
            p.drawLine(QPointF(x, 0), QPointF(x, h))
        if view_start <= self._scrub <= view_end:
            p.setPen(QPen(QColor("#808080"), 1))
            x = self._marker_x(self._scrub)
            p.drawLine(QPointF(x, 0), QPointF(x, h))
        if self.is_active_channel:
            p.setPen(QPen(ACCENT, 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(QRectF(1, 1, max(0, w - 2), max(0, h - 2)))
        if self._playhead >= 0 and view_start <= self._playhead <= view_end:
            p.setPen(QPen(QColor("white"), 1))
            x = self._marker_x(self._playhead)
            p.drawLine(QPointF(x, 0), QPointF(x, h))
        p.end()
