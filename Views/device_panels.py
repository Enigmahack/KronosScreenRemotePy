"""Device-specific UI panels and adaptation."""
from __future__ import annotations
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel,
    QComboBox, QSpinBox, QSlider, QPushButton, QCheckBox,
)
from PySide6.QtGui import QFont

import Utils.theme as T
from Core.device_info import DeviceFamily


class DeviceAdaptationPanel(QWidget):
    """Panel that adapts to device capabilities."""

    def __init__(self, device_family: DeviceFamily, parent=None):
        super().__init__(parent)
        self.device_family = device_family
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Device-specific features
        if self.device_family == DeviceFamily.KRONOS:
            self._setup_kronos_features(layout)
        elif self.device_family == DeviceFamily.NAUTILUS:
            self._setup_nautilus_features(layout)

        layout.addStretch()

    def _setup_kronos_features(self, layout: QVBoxLayout):
        """Setup Kronos-specific feature panel."""
        # Mirror control
        mirror_group = QGroupBox("VGA Mirror")
        mirror_layout = QHBoxLayout(mirror_group)

        self.mirror_enabled = QCheckBox("Enable VGA Mirror")
        self.mirror_enabled.setChecked(False)
        mirror_layout.addWidget(self.mirror_enabled)
        mirror_layout.addStretch()

        layout.addWidget(mirror_group)

        # Audio mirror
        audio_group = QGroupBox("Audio")
        audio_layout = QHBoxLayout(audio_group)

        audio_layout.addWidget(QLabel("Master Volume:"))
        self.master_volume = QSlider(Qt.Orientation.Horizontal)
        self.master_volume.setMinimum(0)
        self.master_volume.setMaximum(127)
        self.master_volume.setValue(100)
        self.master_volume.setMaximumWidth(200)
        audio_layout.addWidget(self.master_volume)

        self.volume_label = QLabel("100")
        audio_layout.addWidget(self.volume_label)
        audio_layout.addStretch()

        self.master_volume.valueChanged.connect(
            lambda v: self.volume_label.setText(str(v))
        )

        layout.addWidget(audio_group)

    def _setup_nautilus_features(self, layout: QVBoxLayout):
        """Setup Nautilus-specific feature panel."""
        # Nautilus has different capabilities
        info_group = QGroupBox("Nautilus Features")
        info_layout = QVBoxLayout(info_group)

        info_text = QLabel(
            "Nautilus-specific features:\n"
            "• Enhanced color display (RGB565)\n"
            "• Larger touchscreen\n"
            "• Sample editing support\n"
            "• Improved performance"
        )
        info_text.setStyleSheet(f"color: {T.TEXT_DIM}; font-size: 9pt;")
        info_text.setWordWrap(True)
        info_layout.addWidget(info_text)

        layout.addWidget(info_group)

        # Audio settings (Nautilus version)
        audio_group = QGroupBox("Audio Output")
        audio_layout = QHBoxLayout(audio_group)

        audio_layout.addWidget(QLabel("Output Device:"))
        self.audio_device = QComboBox()
        self.audio_device.addItems(["Built-in", "USB Audio", "HDMI"])
        audio_layout.addWidget(self.audio_device)
        audio_layout.addStretch()

        layout.addWidget(audio_group)


class KronosControlPanel(QWidget):
    """Left panel with Kronos-specific controls."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # Tempo control
        tempo_group = QGroupBox("Tempo")
        tempo_layout = QHBoxLayout(tempo_group)

        tempo_layout.addWidget(QLabel("BPM:"))
        self.tempo = QSpinBox()
        self.tempo.setMinimum(30)
        self.tempo.setMaximum(300)
        self.tempo.setValue(120)
        self.tempo.setMaximumWidth(80)
        tempo_layout.addWidget(self.tempo)
        tempo_layout.addStretch()

        layout.addWidget(tempo_group)

        # Transport controls
        transport_group = QGroupBox("Transport")
        transport_layout = QHBoxLayout(transport_group)

        self.play_btn = QPushButton("▶ Play")
        self.stop_btn = QPushButton("⏹ Stop")
        self.rec_btn = QPushButton("● Record")

        transport_layout.addWidget(self.play_btn)
        transport_layout.addWidget(self.stop_btn)
        transport_layout.addWidget(self.rec_btn)

        layout.addWidget(transport_group)

        # Mode/bank selection
        bank_group = QGroupBox("Select")
        bank_layout = QHBoxLayout(bank_group)

        bank_layout.addWidget(QLabel("Mode:"))
        self.mode_select = QComboBox()
        self.mode_select.addItems(["Program", "Combi", "Sequencer"])
        bank_layout.addWidget(self.mode_select)

        bank_layout.addWidget(QLabel("Bank:"))
        self.bank_select = QSpinBox()
        self.bank_select.setMinimum(0)
        self.bank_select.setMaximum(7)
        self.bank_select.setMaximumWidth(60)
        bank_layout.addWidget(self.bank_select)

        bank_layout.addStretch()
        layout.addWidget(bank_group)

        layout.addStretch()


class NautilusControlPanel(QWidget):
    """Right panel with Nautilus-specific controls."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # Screen info
        screen_group = QGroupBox("Display")
        screen_layout = QVBoxLayout(screen_group)

        screen_info = QLabel("800 × 480 pixels\nRGB565 Native Format")
        screen_info.setStyleSheet(f"color: {T.TEXT_DIM};")
        screen_layout.addWidget(screen_info)

        layout.addWidget(screen_group)

        # Performance info
        perf_group = QGroupBox("Performance")
        perf_layout = QVBoxLayout(perf_group)

        perf_info = QLabel("4 CPU Cores\n4GB RAM\nUSB 2.0 Ethernet")
        perf_info.setStyleSheet(f"color: {T.TEXT_DIM};")
        perf_layout.addWidget(perf_info)

        layout.addWidget(perf_group)

        # Sample editor link
        sample_group = QGroupBox("Sample Editing")
        sample_layout = QVBoxLayout(sample_group)

        edit_btn = QPushButton("Open Sample Editor…")
        edit_btn.setEnabled(False)  # Placeholder for future implementation
        sample_layout.addWidget(edit_btn)

        layout.addWidget(sample_group)

        layout.addStretch()
