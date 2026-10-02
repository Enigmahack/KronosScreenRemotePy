"""Waveform display widget for sample editing.

Shows audio waveform visualization with:
- Stereo waveform display
- Sample-accurate zoom
- Selection regions
- Playhead position tracking
- Ruler and grid overlay
"""
from __future__ import annotations
from typing import Optional
import struct
import math

from PySide6.QtCore import Qt, QRect, QPoint, Signal
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QFont
from PySide6.QtWidgets import QWidget

import Utils.theme as T


class WaveformDisplay(QWidget):
    """Displays audio waveform with zoom and selection support."""

    # Signals
    selection_changed = Signal(float, float)  # start_sec, end_sec
    playhead_changed = Signal(float)  # position_sec

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(200)
        self.setStyleSheet(f"background-color: {T.BG};")

        # Audio data
        self.audio_data: Optional[bytes] = None
        self.sample_rate = 44100
        self.channels = 2
        self.duration_sec = 0.0

        # Display parameters
        self.zoom_level = 1.0  # Samples per pixel
        self.scroll_offset = 0.0  # Pixels scrolled
        self.playhead_pos = 0.0  # Current playback position in seconds

        # Selection
        self.selection_start = 0.0
        self.selection_end = 0.0
        self.is_selecting = False
        self.select_start_x = 0

        # Drawing parameters
        self.waveform_color = QColor(T.ACCENT)
        self.background_color = QColor(T.BG)
        self.grid_color = QColor(T.BG_DIM)
        self.playhead_color = QColor(100, 200, 255)
        self.selection_color = QColor(100, 150, 255, 50)

    def load_audio(self, audio_data: bytes, sample_rate: int, channels: int,
                   duration_sec: float):
        """Load audio data for waveform display.

        Args:
            audio_data: Raw 16-bit PCM audio
            sample_rate: Sample rate in Hz
            channels: Number of channels
            duration_sec: Duration in seconds
        """
        self.audio_data = audio_data
        self.sample_rate = sample_rate
        self.channels = channels
        self.duration_sec = duration_sec
        self.zoom_level = max(1.0, sample_rate / self.width()) if self.width() > 0 else 1.0
        self.playhead_pos = 0.0
        self.selection_start = 0.0
        self.selection_end = duration_sec
        self.update()

    def set_playhead(self, position_sec: float):
        """Set playhead position.

        Args:
            position_sec: Position in seconds
        """
        self.playhead_pos = max(0.0, min(self.duration_sec, position_sec))
        self.update()

    def get_selection(self) -> tuple[float, float]:
        """Get current selection range in seconds."""
        return (self.selection_start, self.selection_end)

    def set_selection(self, start_sec: float, end_sec: float):
        """Set selection range.

        Args:
            start_sec: Selection start in seconds
            end_sec: Selection end in seconds
        """
        self.selection_start = max(0.0, min(start_sec, self.duration_sec))
        self.selection_end = max(self.selection_start, min(end_sec, self.duration_sec))
        self.update()

    def zoom_in(self):
        """Zoom in (increase detail)."""
        self.zoom_level *= 0.8
        self.update()

    def zoom_out(self):
        """Zoom out (show more time)."""
        self.zoom_level *= 1.2
        self.update()

    def zoom_to_fit(self):
        """Zoom to fit entire waveform in view."""
        if self.width() > 0 and self.duration_sec > 0:
            self.zoom_level = (self.sample_rate * self.duration_sec) / self.width()
            self.scroll_offset = 0.0
            self.update()

    def paintEvent(self, event):
        """Paint the waveform display."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Background
        painter.fillRect(self.rect(), self.background_color)

        if not self.audio_data:
            self._draw_empty(painter)
            return

        # Draw components
        self._draw_grid(painter)
        self._draw_waveform(painter)
        self._draw_selection(painter)
        self._draw_playhead(painter)
        self._draw_ruler(painter)

    def _draw_empty(self, painter: QPainter):
        """Draw empty state message."""
        font = QFont()
        font.setPointSize(10)
        painter.setFont(font)
        painter.setPen(QColor(T.TEXT_DIM))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No audio loaded")

    def _draw_grid(self, painter: QPainter):
        """Draw time grid overlay."""
        if self.duration_sec <= 0:
            return

        # Calculate grid spacing
        grid_interval = self._get_grid_interval()
        pixels_per_second = self.sample_rate / self.zoom_level

        painter.setPen(QPen(self.grid_color, 1))

        # Draw vertical grid lines for each interval
        time = 0.0
        while time <= self.duration_sec:
            x = int((time * self.sample_rate / self.zoom_level) - self.scroll_offset)
            if 0 <= x < self.width():
                painter.drawLine(x, 0, x, self.height())
            time += grid_interval

    def _get_grid_interval(self) -> float:
        """Calculate appropriate grid interval in seconds."""
        # Aim for grid lines every ~100 pixels
        pixels_per_second = self.sample_rate / self.zoom_level
        target_pixels = 100
        seconds_per_line = target_pixels / pixels_per_second

        # Round to nice values (1, 2, 5, 10, 20, 50, 100 ms or seconds)
        intervals = [0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0]
        for interval in intervals:
            if interval >= seconds_per_line:
                return interval
        return 10.0

    def _draw_waveform(self, painter: QPainter):
        """Draw the actual waveform."""
        if not self.audio_data or len(self.audio_data) < 4:
            return

        painter.setPen(QPen(self.waveform_color, 1))
        center_y = self.height() / 2
        max_sample = 32768

        # Calculate display parameters
        samples_per_pixel = self.zoom_level
        width = self.width()

        # Draw waveform
        for x in range(width):
            sample_idx = int((x + self.scroll_offset) * samples_per_pixel * self.channels)
            if sample_idx + self.channels * 2 >= len(self.audio_data):
                break

            # Get peak sample for this pixel
            peak = 0.0
            for ch in range(self.channels):
                offset = sample_idx + ch * 2
                if offset + 2 <= len(self.audio_data):
                    sample = struct.unpack('<h', self.audio_data[offset:offset+2])[0]
                    peak = max(peak, abs(sample) / max_sample)

            # Draw peak line
            y_offset = int((center_y / 2) * peak)
            painter.drawLine(x, center_y - y_offset, x, center_y + y_offset)

    def _draw_selection(self, painter: QPainter):
        """Draw selection region."""
        if self.selection_start >= self.selection_end:
            return

        pixels_per_second = self.sample_rate / self.zoom_level

        start_x = int(self.selection_start * pixels_per_second - self.scroll_offset)
        end_x = int(self.selection_end * pixels_per_second - self.scroll_offset)

        painter.fillRect(start_x, 0, end_x - start_x, self.height(),
                        self.selection_color)

    def _draw_playhead(self, painter: QPainter):
        """Draw playhead position line."""
        pixels_per_second = self.sample_rate / self.zoom_level
        x = int(self.playhead_pos * pixels_per_second - self.scroll_offset)

        if 0 <= x < self.width():
            painter.setPen(QPen(self.playhead_color, 2))
            painter.drawLine(x, 0, x, self.height())

    def _draw_ruler(self, painter: QPainter):
        """Draw time ruler at the top."""
        if self.duration_sec <= 0:
            return

        painter.fillRect(0, 0, self.width(), 20, QColor(T.BG_DIM))
        painter.setPen(QColor(T.TEXT_DIM))

        grid_interval = self._get_grid_interval()
        pixels_per_second = self.sample_rate / self.zoom_level
        font = QFont()
        font.setPointSize(8)
        painter.setFont(font)

        # Draw time labels
        time = 0.0
        while time <= self.duration_sec:
            x = int((time * self.sample_rate / self.zoom_level) - self.scroll_offset)
            if 0 <= x < self.width():
                painter.drawLine(x, 15, x, 20)
                time_str = self._format_time(time)
                painter.drawText(x + 2, 12, time_str)
            time += grid_interval

    def _format_time(self, seconds: float) -> str:
        """Format time in seconds as MM:SS.ms."""
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        ms = int((seconds % 1) * 1000)
        return f"{minutes}:{secs:02d}.{ms:03d}"

    def mousePressEvent(self, event):
        """Handle mouse press for selection."""
        if event.button() == Qt.MouseButton.LeftButton:
            self.is_selecting = True
            self.select_start_x = event.pos().x()

    def mouseMoveEvent(self, event):
        """Handle mouse move for selection."""
        if self.is_selecting:
            # Update selection based on drag
            pixels_per_second = self.sample_rate / self.zoom_level
            start_sec = (self.select_start_x + self.scroll_offset) / pixels_per_second
            end_sec = (event.pos().x() + self.scroll_offset) / pixels_per_second

            if start_sec > end_sec:
                start_sec, end_sec = end_sec, start_sec

            self.selection_start = max(0.0, min(start_sec, self.duration_sec))
            self.selection_end = max(self.selection_start, min(end_sec, self.duration_sec))
            self.update()

    def mouseReleaseEvent(self, event):
        """Handle mouse release."""
        if event.button() == Qt.MouseButton.LeftButton:
            self.is_selecting = False
            self.selection_changed.emit(self.selection_start, self.selection_end)

    def wheelEvent(self, event):
        """Handle mouse wheel for zoom."""
        if event.angleDelta().y() > 0:
            self.zoom_in()
        else:
            self.zoom_out()
