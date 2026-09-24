"""Testing and calibration dialogs."""
from __future__ import annotations
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QGroupBox,
    QGridLayout, QTabWidget, QLineEdit, QSpinBox, QDoubleSpinBox,
    QTextEdit, QScrollArea, QWidget, QCheckBox,
)
from PySide6.QtGui import QFont, QColor

import Utils.theme as T
from Views.dialog_base import BaseDialog


class InputTesterWindow(BaseDialog):
    """Window for testing input devices and injection."""

    def __init__(self, parent=None):
        super().__init__("Input Tester", parent)
        self.last_input = ""
        self._setup_ui()
        self.resize(600, 500)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Tabs for different input types
        tabs = QTabWidget()

        # Touch input tab
        touch_tab = self._create_touch_tab()
        tabs.addTab(touch_tab, "Touch")

        # Button input tab
        button_tab = self._create_button_tab()
        tabs.addTab(button_tab, "Buttons")

        # Control input tab
        control_tab = self._create_control_tab()
        tabs.addTab(control_tab, "Controls")

        # MIDI tab
        midi_tab = self._create_midi_tab()
        tabs.addTab(midi_tab, "MIDI")

        layout.addWidget(tabs, 1)

        # Log display
        log_group = QGroupBox("Input Log")
        log_layout = QVBoxLayout(log_group)
        self.log_display = QTextEdit()
        self.log_display.setReadOnly(True)
        self.log_display.setMaximumHeight(120)
        self.log_display.setStyleSheet(f"""
            QTextEdit {{
                background-color: {T.INSET};
                color: {T.TEXT_DIM};
                border: 1px solid {T.BORDER};
                font-family: monospace;
                font-size: 8pt;
            }}
        """)
        log_layout.addWidget(self.log_display)
        layout.addWidget(log_group)

        # Clear button
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        clear_btn = QPushButton("Clear Log")
        clear_btn.clicked.connect(self.log_display.clear)
        button_layout.addWidget(clear_btn)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        button_layout.addWidget(close_btn)
        layout.addLayout(button_layout)

    def _create_touch_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        info = QLabel(
            "Click 'Enable Touch Testing' and then touch or click anywhere on the\n"
            "Kronos display. Touch coordinates and pressure will be shown below."
        )
        info.setStyleSheet(f"color: {T.TEXT_DIM};")
        info.setWordWrap(True)
        layout.addWidget(info)

        # Touch coordinates display
        coord_group = QGroupBox("Current Touch")
        coord_layout = QGridLayout(coord_group)

        coord_layout.addWidget(QLabel("X:"), 0, 0)
        self.touch_x = QLabel("—")
        coord_layout.addWidget(self.touch_x, 0, 1)

        coord_layout.addWidget(QLabel("Y:"), 0, 2)
        self.touch_y = QLabel("—")
        coord_layout.addWidget(self.touch_y, 0, 3)

        coord_layout.addWidget(QLabel("Pressure:"), 1, 0)
        self.touch_p = QLabel("—")
        coord_layout.addWidget(self.touch_p, 1, 1)

        coord_layout.addWidget(QLabel("Status:"), 1, 2)
        self.touch_status = QLabel("Idle")
        self.touch_status.setStyleSheet(f"color: {T.TEXT_DIM};")
        coord_layout.addWidget(self.touch_status, 1, 3)

        layout.addWidget(coord_group)

        # Control buttons
        btn_layout = QHBoxLayout()
        enable_btn = QPushButton("Enable Touch Testing")
        enable_btn.clicked.connect(lambda: self._log("Touch testing enabled"))
        btn_layout.addWidget(enable_btn)

        disable_btn = QPushButton("Disable Touch Testing")
        disable_btn.clicked.connect(lambda: self._log("Touch testing disabled"))
        btn_layout.addWidget(disable_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        layout.addStretch()
        return widget

    def _create_button_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        info = QLabel(
            "Press hardware buttons and they will appear below.\n"
            "Also allows manual button injection for testing."
        )
        info.setStyleSheet(f"color: {T.TEXT_DIM};")
        info.setWordWrap(True)
        layout.addWidget(info)

        # Button injection
        inject_group = QGroupBox("Button Injection")
        inject_layout = QVBoxLayout(inject_group)

        button_select_layout = QHBoxLayout()
        button_select_layout.addWidget(QLabel("Button:"))
        self.button_select = QLineEdit()
        self.button_select.setPlaceholderText("e.g., MODE, PLAY, STOP")
        button_select_layout.addWidget(self.button_select)

        inject_btn = QPushButton("Inject")
        inject_btn.clicked.connect(self._inject_button)
        button_select_layout.addWidget(inject_btn)
        inject_layout.addLayout(button_select_layout)

        layout.addWidget(inject_group)
        layout.addStretch()
        return widget

    def _create_control_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        info = QLabel("Test control elements (wheels, sliders, knobs, etc.)")
        info.setStyleSheet(f"color: {T.TEXT_DIM};")
        layout.addWidget(info)

        # Control injection
        control_group = QGroupBox("Control Injection")
        control_layout = QGridLayout(control_group)

        control_layout.addWidget(QLabel("Control Type:"), 0, 0)
        self.control_type = QLineEdit()
        self.control_type.setPlaceholderText("e.g., WHEEL, SLIDER, KNOB")
        control_layout.addWidget(self.control_type, 0, 1)

        control_layout.addWidget(QLabel("Index:"), 1, 0)
        self.control_index = QSpinBox()
        control_layout.addWidget(self.control_index, 1, 1)

        control_layout.addWidget(QLabel("Value:"), 2, 0)
        self.control_value = QSpinBox()
        self.control_value.setMinimum(0)
        self.control_value.setMaximum(127)
        control_layout.addWidget(self.control_value, 2, 1)

        inject_btn = QPushButton("Inject Control")
        inject_btn.clicked.connect(self._inject_control)
        control_layout.addWidget(inject_btn, 3, 0, 1, 2)

        layout.addWidget(control_group)
        layout.addStretch()
        return widget

    def _create_midi_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        info = QLabel(
            "Monitor and test MIDI I/O.\n"
            "Requires MIDI bridge to be running."
        )
        info.setStyleSheet(f"color: {T.TEXT_DIM};")
        layout.addWidget(info)

        # MIDI monitoring
        monitor_group = QGroupBox("MIDI Monitor")
        monitor_layout = QVBoxLayout(monitor_group)

        self.midi_log = QTextEdit()
        self.midi_log.setReadOnly(True)
        self.midi_log.setMaximumHeight(150)
        self.midi_log.setStyleSheet(f"""
            QTextEdit {{
                background-color: {T.INSET};
                color: {T.TEXT_DIM};
                font-family: monospace;
                font-size: 8pt;
            }}
        """)
        monitor_layout.addWidget(self.midi_log)

        monitor_btn_layout = QHBoxLayout()
        start_btn = QPushButton("Start Monitoring")
        start_btn.clicked.connect(lambda: self._log("[MIDI] Monitoring started"))
        monitor_btn_layout.addWidget(start_btn)

        stop_btn = QPushButton("Stop Monitoring")
        stop_btn.clicked.connect(lambda: self._log("[MIDI] Monitoring stopped"))
        monitor_btn_layout.addWidget(stop_btn)

        monitor_btn_layout.addStretch()
        monitor_layout.addLayout(monitor_btn_layout)

        layout.addWidget(monitor_group)
        layout.addStretch()
        return widget

    def _inject_button(self):
        button = self.button_select.text().strip()
        if button:
            self._log(f"[BTN] Injecting: {button}")

    def _inject_control(self):
        ctype = self.control_type.text().strip()
        index = self.control_index.value()
        value = self.control_value.value()
        if ctype:
            self._log(f"[CTL] {ctype} #{index} = {value}")

    def _log(self, message: str):
        self.log_display.append(message)
        # Auto-scroll to bottom
        scrollbar = self.log_display.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())


class ButtonInjectorWindow(BaseDialog):
    """Dedicated window for button injection testing."""

    def __init__(self, parent=None):
        super().__init__("Button Injector", parent)
        self._setup_ui()
        self.resize(400, 500)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        info = QLabel("Inject button presses for testing.\nSelect buttons by name or test common buttons.")
        info.setStyleSheet(f"color: {T.TEXT_DIM};")
        layout.addWidget(info)

        # Common buttons grid
        common_group = QGroupBox("Common Buttons")
        common_layout = QGridLayout(common_group)

        common_buttons = [
            ("MODE", 0, 0), ("PLAY", 0, 1), ("STOP", 0, 2),
            ("ENTER", 1, 0), ("EXIT", 1, 1), ("BACK", 1, 2),
            ("FUNC", 2, 0), ("YES", 2, 1), ("NO", 2, 2),
        ]

        for btn_name, row, col in common_buttons:
            btn = QPushButton(btn_name)
            btn.clicked.connect(lambda checked, name=btn_name: self._inject_button(name))
            common_layout.addWidget(btn, row, col)

        layout.addWidget(common_group)

        # Custom button section
        custom_group = QGroupBox("Custom Button")
        custom_layout = QHBoxLayout(custom_group)

        self.button_input = QLineEdit()
        self.button_input.setPlaceholderText("Enter button name")
        custom_layout.addWidget(self.button_input)

        inject_btn = QPushButton("Inject")
        inject_btn.clicked.connect(self._inject_custom)
        custom_layout.addWidget(inject_btn)

        layout.addWidget(custom_group)
        layout.addStretch()

        # Close button
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn)

    def _inject_button(self, button_name: str):
        # TODO: Implement button injection
        pass

    def _inject_custom(self):
        button = self.button_input.text().strip()
        if button:
            self._inject_button(button)
            self.button_input.clear()
