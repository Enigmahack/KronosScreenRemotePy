"""Audio control UI components - VU meter, device selection, volume controls."""
from __future__ import annotations
from typing import Optional, Callable
import math

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPainter, QColor, QFont, QPainterPath
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel,
    QComboBox, QSlider, QPushButton, QDialog, QSpinBox,
)

import Utils.theme as T
from Core.audio_engine import get_audio_devices, AudioDevice, AudioConfig


class VuMeter(QWidget):
    """Vertical stereo VU meter display."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(100)
        self.setMinimumWidth(80)
        self.setStyleSheet(f"background-color: {T.BG};")

        # Levels: left and right channels (0.0 - 1.0)
        self.left_level = 0.0
        self.right_level = 0.0

        # Peak indicators
        self.left_peak = 0.0
        self.right_peak = 0.0
        self.peak_hold_ms = 2000  # Hold peak for 2 seconds

        # Peak reset timer
        self.peak_timer = QTimer(self)
        self.peak_timer.timeout.connect(self._reset_peaks)
        self.peak_timer.start(self.peak_hold_ms)

    def set_levels(self, left: float, right: float):
        """Set the current audio levels (0.0 - 1.0)."""
        left = max(0.0, min(1.0, left))
        right = max(0.0, min(1.0, right))

        self.left_level = left
        self.right_level = right

        # Update peaks
        if left > self.left_peak:
            self.left_peak = left
            self.peak_timer.stop()
            self.peak_timer.start(self.peak_hold_ms)

        if right > self.right_peak:
            self.right_peak = right
            self.peak_timer.stop()
            self.peak_timer.start(self.peak_hold_ms)

        self.update()

    def _reset_peaks(self):
        """Reset peak indicators."""
        self.left_peak = max(0.0, self.left_peak - 0.1)
        self.right_peak = max(0.0, self.right_peak - 0.1)
        self.update()

    def paintEvent(self, event):
        """Paint the VU meter."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()
        meter_height = h - 30

        # Draw left and right meters side by side
        meter_width = w // 2 - 4

        # Left channel
        self._draw_meter(painter, 4, 20, meter_width, meter_height,
                        self.left_level, self.left_peak, "L")

        # Right channel
        self._draw_meter(painter, w // 2 + 4, 20, meter_width, meter_height,
                        self.right_level, self.right_peak, "R")

    def _draw_meter(self, painter: QPainter, x: int, y: int, w: int, h: int,
                   level: float, peak: float, label: str):
        """Draw a single meter channel."""
        # Background
        painter.fillRect(x, y, w, h, QColor(T.BG_DIM))

        # Level bar - gradient from green to red
        level_h = int(h * level)
        if level_h > 0:
            for i in range(level_h):
                ratio = i / h
                if ratio < 0.7:
                    # Green to yellow
                    color_val = int(255 * (ratio / 0.7))
                    color = QColor(255 - color_val, 255, 0)
                else:
                    # Yellow to red
                    color_val = int(255 * ((ratio - 0.7) / 0.3))
                    color = QColor(255, 255 - color_val, 0)

                painter.fillRect(x, y + h - i - 1, w, 1, color)

        # Peak indicator
        if peak > 0.0:
            peak_y = y + h - int(h * peak)
            painter.fillRect(x, peak_y - 1, w, 2, QColor(T.ACCENT))

        # Border
        painter.drawRect(x, y, w, h)

        # Label
        font = QFont()
        font.setPointSize(8)
        painter.setFont(font)
        painter.drawText(x, y + h + 15, w, 15, Qt.AlignmentFlag.AlignCenter, label)

    def sizeHint(self):
        """Return preferred size."""
        return self.minimumSize()


class AudioDeviceSelector(QWidget):
    """Audio device selection UI with device list."""

    device_changed = Signal(str)  # Emits device_id

    def __init__(self, parent=None):
        super().__init__(parent)
        self.audio_mgr = get_audio_devices()
        self.selected_device_id = None
        self._setup_ui()
        self._refresh_devices()

    def _setup_ui(self):
        """Setup UI layout."""
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        layout.addWidget(QLabel("Audio Output:"))

        self.device_combo = QComboBox()
        self.device_combo.currentTextChanged.connect(self._on_device_selected)
        layout.addWidget(self.device_combo)

        self.refresh_btn = QPushButton("🔄")
        self.refresh_btn.setMaximumWidth(32)
        self.refresh_btn.clicked.connect(self._refresh_devices)
        layout.addWidget(self.refresh_btn)

        layout.addStretch()

    def _refresh_devices(self):
        """Refresh list of available audio devices."""
        self.device_combo.blockSignals(True)
        self.device_combo.clear()

        devices = self.audio_mgr.get_output_devices()
        for device in devices:
            self.device_combo.addItem(device.name, device.device_id)

        # Select default device
        default = self.audio_mgr.get_default_output()
        if default:
            idx = self.device_combo.findData(default.device_id)
            if idx >= 0:
                self.device_combo.setCurrentIndex(idx)

        self.device_combo.blockSignals(False)

    def _on_device_selected(self):
        """Handle device selection change."""
        device_id = self.device_combo.currentData()
        if device_id:
            self.selected_device_id = device_id
            self.device_changed.emit(device_id)

    def get_selected_device(self) -> Optional[AudioDevice]:
        """Get the currently selected audio device."""
        if self.selected_device_id:
            return self.audio_mgr.get_device_by_id(self.selected_device_id)
        return self.audio_mgr.get_default_output()


class AudioControlPanel(QWidget):
    """Complete audio control panel with device selector, volume, and VU meter."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self):
        """Setup audio control panel."""
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Device selector
        device_group = QGroupBox("Output Device")
        device_layout = QVBoxLayout(device_group)
        self.device_selector = AudioDeviceSelector(self)
        device_layout.addWidget(self.device_selector)
        layout.addWidget(device_group)

        # Volume control
        volume_group = QGroupBox("Master Volume")
        volume_layout = QHBoxLayout(volume_group)

        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setMinimum(0)
        self.volume_slider.setMaximum(127)
        self.volume_slider.setValue(100)
        self.volume_slider.valueChanged.connect(self._on_volume_changed)
        volume_layout.addWidget(self.volume_slider)

        self.volume_label = QLabel("100")
        self.volume_label.setMaximumWidth(40)
        volume_layout.addWidget(self.volume_label)

        layout.addWidget(volume_group)

        # VU Meter
        meter_group = QGroupBox("Level Meter")
        meter_layout = QVBoxLayout(meter_group)
        self.vu_meter = VuMeter(self)
        meter_layout.addWidget(self.vu_meter)
        layout.addWidget(meter_group)

        layout.addStretch()

    def _on_volume_changed(self, value: int):
        """Handle volume slider change."""
        self.volume_label.setText(str(value))

    def set_levels(self, left: float, right: float):
        """Set VU meter levels."""
        self.vu_meter.set_levels(left, right)

    def get_config(self) -> AudioConfig:
        """Get current audio configuration."""
        device = self.device_selector.get_selected_device()
        return AudioConfig(
            device_id=device.device_id if device else "default",
            sample_rate=device.sample_rate if device else 44100,
            channels=device.channels if device else 2,
            buffer_size=2048,
            latency_ms=device.latency_ms if device else 0.0
        )


class AudioDeviceDialog(QDialog):
    """Dialog for selecting audio device and configuring audio settings."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Audio Configuration")
        self.setMinimumWidth(400)
        self.setMinimumHeight(300)
        self._setup_ui()

    def _setup_ui(self):
        """Setup dialog UI."""
        layout = QVBoxLayout(self)

        # Device selection
        device_group = QGroupBox("Output Device")
        device_layout = QVBoxLayout(device_group)

        audio_mgr = get_audio_devices()

        device_layout.addWidget(QLabel("Select output device:"))
        self.device_list = QComboBox()

        for device in audio_mgr.get_output_devices():
            self.device_list.addItem(device.name, device.device_id)

        device_layout.addWidget(self.device_list)

        default = audio_mgr.get_default_output()
        if default:
            idx = self.device_list.findData(default.device_id)
            if idx >= 0:
                self.device_list.setCurrentIndex(idx)

        layout.addWidget(device_group)

        # Audio settings
        settings_group = QGroupBox("Audio Settings")
        settings_layout = QVBoxLayout(settings_group)

        # Sample rate
        sr_layout = QHBoxLayout()
        sr_layout.addWidget(QLabel("Sample Rate:"))
        self.sample_rate = QComboBox()
        self.sample_rate.addItems(["44100 Hz", "48000 Hz", "96000 Hz"])
        self.sample_rate.setCurrentText("44100 Hz")
        sr_layout.addWidget(self.sample_rate)
        sr_layout.addStretch()
        settings_layout.addLayout(sr_layout)

        # Channels
        ch_layout = QHBoxLayout()
        ch_layout.addWidget(QLabel("Channels:"))
        self.channels = QSpinBox()
        self.channels.setMinimum(1)
        self.channels.setMaximum(8)
        self.channels.setValue(2)
        ch_layout.addWidget(self.channels)
        ch_layout.addStretch()
        settings_layout.addLayout(ch_layout)

        # Buffer size
        buf_layout = QHBoxLayout()
        buf_layout.addWidget(QLabel("Buffer Size:"))
        self.buffer_size = QComboBox()
        self.buffer_size.addItems(["512", "1024", "2048", "4096"])
        self.buffer_size.setCurrentText("2048")
        buf_layout.addWidget(self.buffer_size)
        buf_layout.addStretch()
        settings_layout.addLayout(buf_layout)

        layout.addWidget(settings_group)

        # Test button
        self.test_btn = QPushButton("Test Audio…")
        self.test_btn.clicked.connect(self._on_test_audio)
        layout.addWidget(self.test_btn)

        layout.addStretch()

        # OK/Cancel
        button_layout = QHBoxLayout()
        ok_btn = QPushButton("OK")
        cancel_btn = QPushButton("Cancel")
        ok_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)
        button_layout.addStretch()
        button_layout.addWidget(ok_btn)
        button_layout.addWidget(cancel_btn)
        layout.addLayout(button_layout)

    def _on_test_audio(self):
        """Test audio playback."""
        # TODO: Implement audio test (play a tone or sample)
        pass

    def get_config(self) -> AudioConfig:
        """Get selected audio configuration."""
        sr_text = self.sample_rate.currentText()
        sample_rate = int(sr_text.split()[0])
        buffer_size = int(self.buffer_size.currentText())

        return AudioConfig(
            device_id=self.device_list.currentData(),
            sample_rate=sample_rate,
            channels=self.channels.value(),
            buffer_size=buffer_size,
        )
