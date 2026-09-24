"""MIDI device selection and management UI."""
from __future__ import annotations
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
    QPushButton, QGroupBox, QListWidget, QListWidgetItem,
    QMessageBox, QSpinBox, QTabWidget, QWidget,
)
from PySide6.QtGui import QFont

import Utils.theme as T
from Core.midi_devices import get_midi_devices, MidiDevice


class MidiDeviceDialog(QDialog):
    """Dialog for selecting MIDI input/output devices."""

    def __init__(self, input_device: Optional[str] = None,
                 output_device: Optional[str] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("MIDI Device Configuration")
        self.input_device = input_device
        self.output_device = output_device
        self._setup_ui()
        self.resize(500, 300)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Get available devices
        midi_mgr = get_midi_devices()
        input_names = [d.name for d in midi_mgr.get_input_devices()]
        output_names = [d.name for d in midi_mgr.get_output_devices()]

        # MIDI Input
        input_group = QGroupBox("MIDI Input Device")
        input_layout = QVBoxLayout(input_group)

        input_layout.addWidget(QLabel("Select MIDI input device:"))
        self.input_combo = QComboBox()
        self.input_combo.addItem("(None)")
        self.input_combo.addItems(input_names)
        if self.input_device and self.input_device in input_names:
            self.input_combo.setCurrentText(self.input_device)
        input_layout.addWidget(self.input_combo)

        layout.addWidget(input_group)

        # MIDI Output
        output_group = QGroupBox("MIDI Output Device")
        output_layout = QVBoxLayout(output_group)

        output_layout.addWidget(QLabel("Select MIDI output device:"))
        self.output_combo = QComboBox()
        self.output_combo.addItem("(None)")
        self.output_combo.addItems(output_names)
        if self.output_device and self.output_device in output_names:
            self.output_combo.setCurrentText(self.output_device)
        output_layout.addWidget(self.output_combo)

        layout.addWidget(output_group)

        # Device info
        info_group = QGroupBox("Device Information")
        info_layout = QVBoxLayout(info_group)

        self.info_display = QLabel(self._get_device_info())
        self.info_display.setStyleSheet(f"color: {T.TEXT_DIM}; font-size: 9pt;")
        self.info_display.setWordWrap(True)
        info_layout.addWidget(self.info_display)

        self.input_combo.currentTextChanged.connect(self._on_device_changed)
        self.output_combo.currentTextChanged.connect(self._on_device_changed)

        layout.addWidget(info_group)
        layout.addStretch()

        # Buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch()

        test_btn = QPushButton("Test")
        test_btn.clicked.connect(self._on_test)
        button_layout.addWidget(test_btn)

        ok_btn = QPushButton("OK")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self._on_ok)
        button_layout.addWidget(ok_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(cancel_btn)

        layout.addLayout(button_layout)

    def _get_device_info(self) -> str:
        """Get information about selected devices."""
        midi_mgr = get_midi_devices()
        info_parts = []

        input_name = self.input_combo.currentText()
        if input_name != "(None)":
            device = midi_mgr.get_device_by_name(input_name, is_input=True)
            if device:
                info_parts.append(f"Input: {device.description}")

        output_name = self.output_combo.currentText()
        if output_name != "(None)":
            device = midi_mgr.get_device_by_name(output_name, is_input=False)
            if device:
                info_parts.append(f"Output: {device.description}")

        if not info_parts:
            return "No devices selected. MIDI will be disabled."

        return "\n".join(info_parts)

    def _on_device_changed(self):
        """Update device info when selection changes."""
        self.info_display.setText(self._get_device_info())

    def _on_test(self):
        """Test the selected MIDI devices."""
        input_name = self.input_combo.currentText()
        output_name = self.output_combo.currentText()

        devices = []
        if input_name != "(None)":
            devices.append(f"Input: {input_name}")
        if output_name != "(None)":
            devices.append(f"Output: {output_name}")

        if not devices:
            QMessageBox.warning(self, "Test", "Please select at least one device.")
            return

        QMessageBox.information(self, "MIDI Test",
                              f"Testing devices:\n" + "\n".join(devices) +
                              "\n\nNote: Full testing not yet implemented.")

    def _on_ok(self):
        self.input_device = self.input_combo.currentText()
        self.output_device = self.output_combo.currentText()

        if self.input_device == "(None)":
            self.input_device = None
        if self.output_device == "(None)":
            self.output_device = None

        self.accept()

    def get_devices(self) -> tuple[Optional[str], Optional[str]]:
        """Return (input_device, output_device)."""
        return (self.input_device, self.output_device)


class MidiDeviceStatusWidget(QWidget):
    """Status display showing MIDI device connections."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(0, 0, 0, 0)

        # MIDI input status
        self.input_label = QLabel("MIDI In: (None)")
        self.input_label.setStyleSheet(f"color: {T.TEXT_DIM}; font-size: 8pt;")
        layout.addWidget(self.input_label)

        # MIDI output status
        self.output_label = QLabel("MIDI Out: (None)")
        self.output_label.setStyleSheet(f"color: {T.TEXT_DIM}; font-size: 8pt;")
        layout.addWidget(self.output_label)

        layout.addStretch()

        # Configure button
        self.config_btn = QPushButton("Configure…")
        self.config_btn.setMaximumWidth(100)
        layout.addWidget(self.config_btn)

    def set_devices(self, input_device: Optional[str], output_device: Optional[str]):
        """Update display with selected devices."""
        input_text = input_device if input_device else "(None)"
        output_text = output_device if output_device else "(None)"

        self.input_label.setText(f"MIDI In: {input_text}")
        self.output_label.setText(f"MIDI Out: {output_text}")

    def set_config_callback(self, callback):
        """Set callback for configure button."""
        self.config_btn.clicked.connect(callback)
