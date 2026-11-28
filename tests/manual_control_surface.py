"""Standalone Kronos/Nautilus Control Surface tester.

Launches the custom-painted control surface widget (Rendering/control_surface.py)
as a standalone window. Shows button presses, wheel steps, and active/pressed
state live in the console.

Usage:
    python tests/manual_control_surface.py                    # Kronos skin
    python tests/manual_control_surface.py --nautilus          # Nautilus skin
    python tests/manual_control_surface.py --device-family K   # Kronos (alias)
    python tests/manual_control_surface.py --device-family N   # Nautilus (alias)
"""
from __future__ import annotations
import sys
import os
from typing import Optional

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QVBoxLayout, QHBoxLayout,
    QWidget, QPushButton, QLabel, QGroupBox, QTextEdit,
    QComboBox, QCheckBox, QSlider, QSpinBox, QTabWidget
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from Rendering.control_surface import KronosControlSurface, _NAUT_BUTTON_DEFS


class ControlSurfaceTestWindow(QMainWindow):
    """Full control surface test window with logging and manual controls."""

    def __init__(self, device_family: str = "kronos"):
        super().__init__()
        self.setWindowTitle(f"Control Surface Test — {device_family.title()}")
        self.resize(1000, 700)
        self._device_family = device_family.lower()

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(8)

        # ── Top bar: device toggle + mode ─────────────────────────────────
        top = QHBoxLayout()
        top.addWidget(QLabel("Device family:"))
        self.cmb_family = QComboBox()
        self.cmb_family.addItems(["Kronos", "Nautilus"])
        self.cmb_family.setCurrentText(self._device_family.title())
        self.cmb_family.currentTextChanged.connect(self._switch_family)
        top.addWidget(self.cmb_family)
        top.addStretch()

        top.addWidget(QLabel("Mode:"))
        self.cmb_mode = QComboBox()
        self.cmb_mode.addItems(["Setlist", "Combi", "Program", "Sequence", "Sampling", "Global", "Disk"])
        self.cmb_mode.setCurrentText("Combi")
        self.cmb_mode.currentTextChanged.connect(self._on_mode_changed)
        top.addWidget(self.cmb_mode)
        top.addStretch()

        # Status bar label
        self.lbl_status = QLabel("Ready — click buttons or use manual controls below")
        self.lbl_status.setStyleSheet("color:gray; font-style:italic")
        top.addWidget(self.lbl_status)
        main_layout.addLayout(top)

        # ── Control surface widget ────────────────────────────────────────
        self.surface = KronosControlSurface()
        self.surface.set_device_family(self._device_family == "nautilus")
        self.surface.button_pressed.connect(self._on_button_pressed)
        self.surface.button_released.connect(self._on_button_released)
        self.surface.wheel_step.connect(self._on_wheel_step)
        self.surface.wheel_settings_requested.connect(self._on_wheel_settings)
        main_layout.addWidget(self.surface, 1)

        # ── Bottom: manual controls + log ─────────────────────────────────
        bottom = QTabWidget()

        # Manual buttons tab
        manual = QWidget()
        manual_layout = QVBoxLayout(manual)
        manual_layout.addWidget(QLabel("Manual button press/release:"))

        btn_grid = QHBoxLayout()
        # Common buttons
        common_btns = ["EXIT", "ENTER"]
        for name in common_btns:
            b = QPushButton(name)
            b.setCheckable(True)
            b.clicked.connect(lambda checked, n=name: self._manual_toggle(n, checked))
            btn_grid.addWidget(b)

        # Mode buttons (Kronos)
        mode_btns = ["Setlist", "Combi", "Program", "Sequence", "Sampling", "Global", "Disk"]
        self.mode_group = QGroupBox("Mode buttons")
        mode_layout = QHBoxLayout(self.mode_group)
        self._mode_btns = {}
        for name in mode_btns:
            b = QPushButton(name)
            b.setCheckable(True)
            b.clicked.connect(lambda checked, n=name: self._manual_toggle(n, checked))
            self._mode_btns[name] = b
            mode_layout.addWidget(b)
        btn_grid.addWidget(self.mode_group)

        # Nautilus-specific buttons
        self.naut_group = QGroupBox("Nautilus buttons")
        naut_layout = QHBoxLayout(self.naut_group)
        self._naut_btns = {}
        for name in ["A", "B", "C", "D", "E", "F", "MODE", "PAGE", "INC", "DEC"]:
            b = QPushButton(name)
            b.setCheckable(True)
            b.clicked.connect(lambda checked, n=name: self._manual_toggle(n, checked))
            self._naut_btns[name] = b
            naut_layout.addWidget(b)
        btn_grid.addWidget(self.naut_group)

        manual_layout.addLayout(btn_grid)

        # Volume + encoder sliders
        slider_layout = QHBoxLayout()
        slider_layout.addWidget(QLabel("Encoder value:"))
        self.spin_encoder = QSpinBox()
        self.spin_encoder.setRange(-64, 63)
        self.spin_encoder.setValue(0)
        slider_layout.addWidget(self.spin_encoder)
        slider_layout.addWidget(QLabel("VALUE slider:"))
        self.sld_value = QSlider(Qt.Orientation.Horizontal)
        self.sld_value.setRange(0, 127)
        self.sld_value.setValue(64)
        slider_layout.addWidget(self.sld_value)
        manual_layout.addLayout(slider_layout)

        # Reverse scrolling toggle
        rev_layout = QHBoxLayout()
        self.chk_reverse = QCheckBox("Reverse scrolling")
        self.chk_reverse.setChecked(self.surface._reverse_scroll)
        self.chk_reverse.toggled.connect(lambda checked: setattr(self.surface, "_reverse_scroll", checked))
        rev_layout.addWidget(self.chk_reverse)
        rev_layout.addStretch()
        manual_layout.addLayout(rev_layout)

        bottom.addTab(manual, "Manual Controls")

        # Log tab
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Consolas", 10))
        self.log_text.setMaximumHeight(200)
        bottom.addTab(self.log_text, "Event Log")

        main_layout.addWidget(bottom)

        self._apply_family()
        self.surface.set_mode(self.cmb_mode.currentIndex() + 1)
        self._log(f"Control surface started — {device_family.title()} skin loaded")

    # ── Signal handlers ───────────────────────────────────────────────────
    def _on_button_pressed(self, name: str):
        self._log(f"BTN_PRESS: {name}")
        self.lbl_status.setText(f"Pressed: {name}")

    def _on_button_released(self, name: str):
        self._log(f"BTN_RELEASE: {name}")

    def _on_wheel_step(self, delta: int):
        direction = "CW" if delta > 0 else "CCW"
        self._log(f"WHEEL: {direction} ({delta})")
        self.lbl_status.setText(f"Wheel: {direction} {abs(delta)}")

    def _on_wheel_settings(self):
        self._log("WHEEL_SETTINGS: Settings dialog requested")
        self.lbl_status.setText("Wheel: Settings requested")

    def _on_mode_changed(self, mode: str):
        self.surface.set_mode(self.cmb_mode.findText(mode) + 1)
        self._log(f"MODE: {mode}")

    def _switch_family(self, family: str):
        self._device_family = family.lower()
        self._apply_family()
        self._log(f"DEVICE_FAMILY: {self._device_family}")

    def _apply_family(self):
        self.surface.set_device_family(self._device_family == "nautilus")
        self.mode_group.setEnabled(self._device_family == "kronos")
        self.naut_group.setEnabled(self._device_family == "nautilus")

    def _manual_toggle(self, name: str, pressed: bool):
        if self._device_family == "nautilus":
            name = f"NAUT_{name}"
        if pressed:
            self.surface.press_button(name)
            self._log(f"MANUAL_PRESS: {name}")
        else:
            self.surface.release_button(name)
            self._log(f"MANUAL_RELEASE: {name}")

    def _log(self, msg: str):
        self.log_text.append(msg)
        # Auto-scroll
        self.log_text.verticalScrollBar().setValue(
            self.log_text.verticalScrollBar().maximum()
        )


# ── Entry point ─────────────────────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Standalone Control Surface tester"
    )
    parser.add_argument(
        "--device-family", "-d", choices=["kronos", "nautilus", "K", "N"],
        default="kronos", help="Device family skin"
    )
    parser.add_argument("--nautilus", action="store_true", help="Use Nautilus skin")
    args = parser.parse_args()

    family_map = {"kronos": "kronos", "nautilus": "nautilus", "K": "kronos", "N": "nautilus"}
    app = QApplication(sys.argv)
    window = ControlSurfaceTestWindow("nautilus" if args.nautilus else family_map[args.device_family])
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
