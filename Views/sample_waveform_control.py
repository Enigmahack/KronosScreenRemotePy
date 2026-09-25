"""SampleWaveformControl — waveform display with draggable Sample-Start/
Loop-Start/Loop-End markers. Port of C#'s `Views/SampleWaveformControl.cs`,
scoped to the rendering + marker-drag behavior (fade/DSP preview, ruler
sub-control, and pan/zoom-window sync across a stereo pair are C#-only
extras not ported this pass — see CLAUDE.md).

Uses Core/waveform_pyramid.py (min/max mip-map, built once per loaded
sample) so zoom/pan never rescans the raw PCM — see that module for why.
Renders one filled envelope span per pixel column via plain QPainter calls;
C#'s own version additionally bypassed WPF's vector rasterizer with a
hand-written pixel buffer for performance reasons specific to WPF's
geometry cost model (see that file's own comment) — Qt's QPainter doesn't
have the same bottleneck, so this port skips that specific optimization
and keeps the simpler per-column draw calls.

Marker drags are live-preview: the dragged value only updates the on-
screen line (and the label) until mouse-release, when `markers_changed` is
emitted once with the committed (sample_start, loop_start, loop_end)
triple — never repaints/writes on every mouse-move. An interrupted drag
(the widget's implicit mouse grab taken away mid-drag — e.g. a modal
dialog appearing, matching C#'s OnLostMouseCapture/Alt-Tab-mid-drag case)
reverts to the last committed values instead of leaving the drag "stuck
armed", via the QEvent.UngrabMouse override below — Qt's own hook for the
same class of event WPF's OnLostMouseCapture handles.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
from PySide6.QtCore import QEvent, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

from Core.waveform_pyramid import WaveformPyramid
from Data.ksf_sample import KsfSample
import Utils.theme as T

_MAX_TRACE_COLUMNS = 1920
_HIT_PX = 6   # marker-line drag hit-test tolerance, in pixels

_MARKER_COLORS = {
    "sample_start": QColor(0xCC, 0x33, 0x33),   # red
    "loop_start":   QColor(0x33, 0xAA, 0x55),   # green
    "loop_end":     QColor(0x33, 0x66, 0xCC),   # blue
}


class SampleWaveformControl(QWidget):
    markers_changed = Signal(int, int, int)   # sample_start, loop_start, loop_end

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(120)
        self.setMouseTracking(True)

        self._samples: Optional[np.ndarray] = None
        self._pyramid: Optional[WaveformPyramid] = None
        self._view_start = 0
        self._view_end = 0

        self._sample_start = 0
        self._loop_start = 0
        self._loop_end = 0

        self._dragging: Optional[str] = None   # "sample_start" | "loop_start" | "loop_end" | "pan"
        self._drag_preview: int = 0
        self._pan_anchor_x = 0
        self._pan_anchor_view_start = 0

    # ── Loading ──────────────────────────────────────────────────────────────

    def load_ksf(self, ksf: Optional[KsfSample]):
        if ksf is None or ksf.is_header_only:
            self._samples = None
            self._pyramid = None
            self.update()
            return
        self._samples = ksf.samples()
        self._pyramid = WaveformPyramid(self._samples)
        self._view_start = 0
        self._view_end = len(self._samples)
        self._sample_start = ksf.sample_start
        self._loop_start = ksf.loop_start
        self._loop_end = ksf.loop_end
        self._dragging = None
        self.update()

    def set_markers(self, sample_start: int, loop_start: int, loop_end: int):
        """Reflect externally-changed marker values (e.g. the field panel's
        loop-start/loop-end spinboxes) without touching the loaded sample or
        view — call after load_ksf if the caller edits fields separately."""
        self._sample_start, self._loop_start, self._loop_end = sample_start, loop_start, loop_end
        self.update()

    def zoom_to_fit(self):
        if self._samples is not None:
            self._view_start = 0
            self._view_end = len(self._samples)
            self.update()

    # ── Painting ─────────────────────────────────────────────────────────────

    def paintEvent(self, _event):
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(T.INSET))
        if self._samples is None or len(self._samples) == 0:
            p.setPen(QColor(T.TEXT_DIM))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No audio loaded")
            p.end()
            return

        self._draw_trace(p, w, h)
        self._draw_markers(p, w, h)
        p.end()

    def _draw_trace(self, p: QPainter, w: int, h: int):
        samples = self._samples
        view_start, view_end = self._view_start, self._view_end
        view_len = max(1, view_end - view_start)
        pixel_count = max(1, min(w, _MAX_TRACE_COLUMNS))
        column_width = w / pixel_count
        level = self._pyramid.pick(view_len / pixel_count) if self._pyramid else None

        mid_y = h / 2
        y_scale = (h / 2) / 32768.0
        p.setPen(QPen(QColor(T.ACCENT), 1))

        n = len(samples)
        for px in range(pixel_count):
            start = view_start + px * view_len // pixel_count
            if start >= view_end:
                break
            end = view_start + (px + 1) * view_len // pixel_count
            end = max(start + 1, min(end, view_end))
            if start >= n:
                break
            read_end = min(end, n)
            if read_end <= start:
                break

            if level is not None:
                b0 = start // level.bucket
                b1 = min((read_end + level.bucket - 1) // level.bucket, len(level.min))
                if b1 <= b0:
                    continue
                lo = int(level.min[b0:b1].min())
                hi = int(level.max[b0:b1].max())
            else:
                chunk = samples[start:read_end]
                lo = int(chunk.min())
                hi = int(chunk.max())

            y_top = mid_y - hi * y_scale
            y_bot = mid_y - lo * y_scale
            if y_bot - y_top < 1:
                y_bot = y_top + 1   # silence still reads as the centre line
            x = px * column_width
            p.fillRect(QRectF(x, y_top, max(1.0, column_width), y_bot - y_top), QColor(T.ACCENT))

    def _marker_value(self, name: str) -> int:
        if self._dragging == name:
            return self._drag_preview
        return getattr(self, f"_{name}")

    def _frame_to_x(self, frame: int, w: int) -> float:
        view_len = max(1, self._view_end - self._view_start)
        return (frame - self._view_start) / view_len * w

    def _x_to_frame(self, x: float, w: int) -> int:
        view_len = max(1, self._view_end - self._view_start)
        frame = self._view_start + int(round(x / w * view_len))
        return max(0, min(len(self._samples) - 1 if self._samples is not None else 0, frame))

    def _draw_markers(self, p: QPainter, w: int, h: int):
        for name in ("sample_start", "loop_start", "loop_end"):
            frame = self._marker_value(name)
            x = self._frame_to_x(frame, w)
            if -1 <= x <= w + 1:
                pen = QPen(_MARKER_COLORS[name], 2 if self._dragging == name else 1)
                p.setPen(pen)
                p.drawLine(int(x), 0, int(x), h)
        # Loop region tint, using whichever values are live (preview or committed).
        ls = self._frame_to_x(self._marker_value("loop_start"), w)
        le = self._frame_to_x(self._marker_value("loop_end"), w)
        if le > ls:
            tint = QColor(_MARKER_COLORS["loop_start"])
            tint.setAlpha(30)
            p.fillRect(QRectF(ls, 0, le - ls, h), tint)

    # ── Interaction ──────────────────────────────────────────────────────────

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or self._samples is None:
            return
        x = event.position().x()
        nearest = self._nearest_marker(x)
        if nearest is not None:
            self._dragging = nearest
            self._drag_preview = self._marker_value(nearest)
        else:
            self._dragging = "pan"
            self._pan_anchor_x = x
            self._pan_anchor_view_start = self._view_start

    def mouseMoveEvent(self, event):
        if self._dragging is None or self._samples is None:
            return
        x = event.position().x()
        if self._dragging == "pan":
            view_len = self._view_end - self._view_start
            dx_frames = int((x - self._pan_anchor_x) / max(1, self.width()) * view_len)
            new_start = max(0, min(len(self._samples) - view_len, self._pan_anchor_view_start - dx_frames))
            self._view_start = new_start
            self._view_end = new_start + view_len
        else:
            self._drag_preview = self._x_to_frame(x, self.width())
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or self._dragging is None:
            return
        self._commit_drag()

    def _commit_drag(self):
        if self._dragging in ("sample_start", "loop_start", "loop_end"):
            setattr(self, f"_{self._dragging}", self._drag_preview)
            self.markers_changed.emit(self._sample_start, self._loop_start, self._loop_end)
        self._dragging = None
        self.update()

    def _revert_drag(self):
        """Abandoned-drag recovery (lost mouse grab mid-drag) — discard the
        preview value without committing or emitting, matching C#'s
        OnLostMouseCapture."""
        self._dragging = None
        self.update()

    def event(self, ev):
        # Qt's equivalent of WPF's OnLostMouseCapture: fires when this
        # widget's implicit press-and-hold mouse grab is taken away out
        # from under it (e.g. a modal dialog appears mid-drag), not just on
        # a normal release.
        if ev.type() == QEvent.Type.UngrabMouse and self._dragging is not None:
            self._revert_drag()
        return super().event(ev)

    def _nearest_marker(self, x: float) -> Optional[str]:
        best_name = None
        best_dist = _HIT_PX + 1
        for name in ("sample_start", "loop_start", "loop_end"):
            mx = self._frame_to_x(self._marker_value(name), self.width())
            dist = abs(mx - x)
            if dist <= _HIT_PX and dist < best_dist:
                best_name, best_dist = name, dist
        return best_name

    def wheelEvent(self, event):
        if self._samples is None:
            return
        factor = 0.8 if event.angleDelta().y() > 0 else 1.25
        view_len = self._view_end - self._view_start
        new_len = max(64, min(len(self._samples), int(view_len * factor)))
        # Zoom centered on the cursor.
        cursor_frame = self._x_to_frame(event.position().x(), self.width())
        frac = (cursor_frame - self._view_start) / max(1, view_len)
        new_start = max(0, min(len(self._samples) - new_len, int(cursor_frame - frac * new_len)))
        self._view_start = new_start
        self._view_end = new_start + new_len
        self.update()
