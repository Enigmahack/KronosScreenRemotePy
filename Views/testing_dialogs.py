"""Testing and calibration dialogs.

Both windows below inject real control-port commands against the connected
Kronos/Nautilus (docs/api.md §7) through the app's own CtrlClient — they are
diagnostic tools for confirming what a given button/control token actually
does on real hardware, not local-only widgets. `main_window` supplies the
live host/ctrl-port/CtrlClient the app is already using for its own input
handling (see Views/main_window.py's `_ctrl_send`), so a command sent here
behaves identically to one sent by clicking the real UI.
"""
from __future__ import annotations
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QGroupBox,
    QGridLayout, QTabWidget, QLineEdit, QSpinBox, QDoubleSpinBox,
    QTextEdit, QScrollArea, QWidget, QCheckBox, QComboBox,
)
from PySide6.QtGui import QFont, QColor

import Utils.theme as T
from Views.dialog_base import BaseDialog

# Complete catalog of every button the daemon's BUTTON command accepts
# (docs/api.md §9 "Button name reference"), ported verbatim from the C#
# reference's Core/ButtonReference.cs — keep in sync if that table changes.
# (category, token, code, description)
BUTTON_REFERENCE: list[tuple[str, str, int, str]] = [
    ("Navigation", "EXIT", 8, "Exit button"),
    ("Navigation", "ENTER", 23, "Enter / confirm button"),
    ("Value control", "INC", 51, "Increment the currently selected value"),
    ("Value control", "DEC", 52, "Decrement the currently selected value"),
    ("Mode select", "SETLIST", 7, "Setlist mode"),
    ("Mode select", "COMBI", 1, "Combi mode"),
    ("Mode select", "PROGRAM", 2, "Program mode"),
    ("Mode select", "SEQUENCE", 3, "Sequence mode"),
    ("Mode select", "SAMPLING", 4, "Sampling mode"),
    ("Mode select", "GLOBAL", 5, "Global mode"),
    ("Mode select", "DISK", 6, "Disk mode"),
    ("Utility", "HELP", 9, "Help button"),
    ("Utility", "COMPARE", 10, "Compare button"),
    ("Utility", "RESET", 75, "Reset Controls button"),
    *[("Numeric pad", f"NUM{d}", 11 + d, f"Numeric key {d}") for d in range(10)],
    ("Numeric pad", "NUM_DASH", 21, "Numeric dash / minus"),
    ("Numeric pad", "NUM_DOT", 22, "Numeric dot / decimal"),
    *[("Mix Play", f"MP{i + 1}", 58 + i, f"Mix Play {i + 1}") for i in range(8)],
    *[("Mix Select", f"MS{i + 1}", 66 + i, f"Mix Select {i + 1}") for i in range(8)],
    *[("Bank", f"BANK_I{chr(ord('A') + i)}", 24 + i, f"Internal bank {chr(ord('A') + i)}")
      for i in range(7)],
    *[("Bank", f"BANK_U{chr(ord('A') + i)}", 31 + i, f"User bank {chr(ord('A') + i)}")
      for i in range(7)],
    ("Sequencer", "SEQ_PAUSE", 38, "Pause"),
    ("Sequencer", "SEQ_REW", 39, "Rewind"),
    ("Sequencer", "SEQ_FF", 40, "Fast forward"),
    ("Sequencer", "SEQ_LOCATE", 41, "Locate / return to start"),
    ("Sequencer", "SEQ_REC", 42, "Sequencer record"),
    ("Sequencer", "SEQ_START", 43, "Sequencer start / stop"),
    ("Sequencer", "TAP_TEMPO", 44, "Tap tempo"),
    ("Sampling", "SMPL_REC", 45, "Sampling record"),
    ("Sampling", "SMPL_START", 46, "Sampling start"),
    ("Channel strip", "MIX_KNOBS", 74, "Mixer Knobs selector"),
    ("Channel strip", "SOLO", 76, "Solo (fires on release)"),
    ("Channel strip", "MODULE_CONTROL", 47, "Module Control"),
    ("Channel strip", "KARMA_ONOFF", 48, "Karma On/Off"),
    ("Channel strip", "KARMA_LATCH", 49, "Karma Latch"),
    ("Channel strip", "DRUM_TRACK", 50, "Drum Track select"),
    ("Channel strip", "TIMBRE_TRACK", 53, "Timbre/Track select"),
    ("Channel strip", "AUDIO_TRACK", 54, "Audio select"),
    ("Channel strip", "EXT_TRACK", 55, "Ext select"),
    ("Channel strip", "RTKNOBS_KARMA", 56, "RT Knobs/Karma page select"),
    ("Channel strip", "TONE_ADJUST", 57, "Tone Adjust"),
    ("Channel strip", "SW1", 77, "Front-panel switch 1"),
    ("Channel strip", "SW2", 78, "Front-panel switch 2"),
]
BUTTON_REFERENCE.sort(key=lambda b: b[2])
_BUTTON_TOKENS = [b[1] for b in BUTTON_REFERENCE]


class InputTesterWindow(BaseDialog):
    """Window for testing input devices and injection against the connected unit."""

    def __init__(self, main_window, parent=None):
        super().__init__("Input Tester", parent or main_window)
        self._mw = main_window
        self._touch_timer: Optional[QTimer] = None
        self._touch_poll_inflight = False
        self._setup_ui()
        self.resize(600, 500)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        tabs = QTabWidget()
        tabs.addTab(self._create_touch_tab(), "Touch")
        tabs.addTab(self._create_button_tab(), "Buttons")
        tabs.addTab(self._create_control_tab(), "Controls")
        tabs.addTab(self._create_midi_tab(), "MIDI")
        layout.addWidget(tabs, 1)

        log_group = QGroupBox("Command Log")
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

        button_layout = QHBoxLayout()
        button_layout.addStretch()
        clear_btn = QPushButton("Clear Log")
        clear_btn.clicked.connect(self.log_display.clear)
        button_layout.addWidget(clear_btn)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        button_layout.addWidget(close_btn)
        layout.addLayout(button_layout)

    # ── Touch tab: polls the real LASTTOUCH read-only command ──────────────────

    def _create_touch_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        info = QLabel(
            "Polls LASTTOUCH (docs/api.md §7) — the daemon's record of the most\n"
            "recent touch it saw, from any client or the physical panel."
        )
        info.setStyleSheet(f"color: {T.TEXT_DIM};")
        info.setWordWrap(True)
        layout.addWidget(info)

        coord_group = QGroupBox("Last Touch")
        coord_layout = QGridLayout(coord_group)
        coord_layout.addWidget(QLabel("X:"), 0, 0)
        self.touch_x = QLabel("—")
        coord_layout.addWidget(self.touch_x, 0, 1)
        coord_layout.addWidget(QLabel("Y:"), 0, 2)
        self.touch_y = QLabel("—")
        coord_layout.addWidget(self.touch_y, 0, 3)
        coord_layout.addWidget(QLabel("Status:"), 1, 0)
        self.touch_status = QLabel("Idle")
        self.touch_status.setStyleSheet(f"color: {T.TEXT_DIM};")
        coord_layout.addWidget(self.touch_status, 1, 1, 1, 3)
        layout.addWidget(coord_group)

        btn_layout = QHBoxLayout()
        enable_btn = QPushButton("Start Polling")
        enable_btn.clicked.connect(self._start_touch_polling)
        btn_layout.addWidget(enable_btn)
        disable_btn = QPushButton("Stop Polling")
        disable_btn.clicked.connect(self._stop_touch_polling)
        btn_layout.addWidget(disable_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        layout.addStretch()
        return widget

    def _start_touch_polling(self):
        if self._touch_timer is not None:
            return
        if not self._mw._host:
            self._log("[TOUCH] not connected — nothing to poll")
            return
        self.touch_status.setText("Polling…")
        self._touch_timer = QTimer(self)
        self._touch_timer.setInterval(500)
        self._touch_timer.timeout.connect(self._poll_touch_once)
        self._touch_timer.start()
        self._log("[TOUCH] polling started")

    def _stop_touch_polling(self):
        if self._touch_timer is not None:
            self._touch_timer.stop()
            self._touch_timer = None
        self.touch_status.setText("Idle")
        self._log("[TOUCH] polling stopped")

    def _poll_touch_once(self):
        if self._touch_poll_inflight or not self._mw._host:
            return
        self._touch_poll_inflight = True
        import threading
        import Core.ctrl_client as CC
        host, port = self._mw._host, self._mw._ctrl_port

        def fetch():
            resp = CC.get().query(host, port, "LASTTOUCH", timeout_ms=800)
            try:
                QTimer.singleShot(0, self, lambda: self._apply_touch(resp))
            except RuntimeError:
                pass  # window closed before the poll finished

        threading.Thread(target=fetch, daemon=True, name="LastTouchPoll").start()

    def _apply_touch(self, resp: Optional[str]):
        self._touch_poll_inflight = False
        if not resp:
            return
        kv = {}
        for part in resp.split():
            if "=" in part:
                k, _, v = part.partition("=")
                kv[k] = v
        if "X" in kv and "Y" in kv:
            self.touch_x.setText(kv["X"])
            self.touch_y.setText(kv["Y"])

    # ── Button tab: real BUTTON <token> injection ───────────────────────────────

    def _create_button_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        info = QLabel("Send a named front-panel button press (docs/api.md §9).")
        info.setStyleSheet(f"color: {T.TEXT_DIM};")
        info.setWordWrap(True)
        layout.addWidget(info)

        inject_group = QGroupBox("Button Injection")
        inject_layout = QVBoxLayout(inject_group)

        button_select_layout = QHBoxLayout()
        button_select_layout.addWidget(QLabel("Button:"))
        self.button_select = QComboBox()
        self.button_select.addItems(_BUTTON_TOKENS)
        button_select_layout.addWidget(self.button_select, 1)

        inject_btn = QPushButton("Inject")
        inject_btn.clicked.connect(self._inject_button)
        button_select_layout.addWidget(inject_btn)
        inject_layout.addLayout(button_select_layout)

        layout.addWidget(inject_group)
        layout.addStretch()
        return widget

    def _inject_button(self):
        token = self.button_select.currentText()
        if token:
            self._mw._ctrl_send(f"BUTTON {token}")
            self._log(f"[BTN] BUTTON {token}")

    # ── Controls tab: real SLIDER/KNOB/VSLIDER/WHEEL injection ─────────────────

    def _create_control_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        info = QLabel("Send a slider, RT knob, value-slider, or data-wheel move (docs/api.md §7).")
        info.setStyleSheet(f"color: {T.TEXT_DIM};")
        info.setWordWrap(True)
        layout.addWidget(info)

        control_group = QGroupBox("Control Injection")
        control_layout = QGridLayout(control_group)

        control_layout.addWidget(QLabel("Control Type:"), 0, 0)
        self.control_type = QComboBox()
        self.control_type.addItems(["SLIDER", "KNOB", "VSLIDER", "WHEEL"])
        self.control_type.currentTextChanged.connect(self._update_control_fields)
        control_layout.addWidget(self.control_type, 0, 1)

        control_layout.addWidget(QLabel("Index (1-8):"), 1, 0)
        self.control_index = QSpinBox()
        self.control_index.setMinimum(1)
        self.control_index.setMaximum(8)
        self.control_index.setValue(1)
        control_layout.addWidget(self.control_index, 1, 1)

        control_layout.addWidget(QLabel("Value (0-127):"), 2, 0)
        self.control_value = QSpinBox()
        self.control_value.setMinimum(0)
        self.control_value.setMaximum(127)
        control_layout.addWidget(self.control_value, 2, 1)

        inject_btn = QPushButton("Inject Control")
        inject_btn.clicked.connect(self._inject_control)
        control_layout.addWidget(inject_btn, 3, 0, 1, 2)

        layout.addWidget(control_group)
        layout.addStretch()
        self._update_control_fields("SLIDER")
        return widget

    def _update_control_fields(self, ctype: str):
        # WHEEL has no index/value — CW/CCW only; VSLIDER has no index (a
        # single fixed control, docs/api.md §7 VSLIDER).
        self.control_index.setEnabled(ctype in ("SLIDER", "KNOB"))
        self.control_value.setEnabled(ctype != "WHEEL")

    def _inject_control(self):
        ctype = self.control_type.currentText()
        index = self.control_index.value()
        value = self.control_value.value()
        if ctype in ("SLIDER", "KNOB"):
            cmd = f"{ctype} {index} {value}"
        elif ctype == "VSLIDER":
            cmd = f"VSLIDER {value}"
        else:  # WHEEL — no numeric value on the wire, just a direction
            cmd = f"WHEEL {'CW' if value >= 64 else 'CCW'}"
        self._mw._ctrl_send(cmd)
        self._log(f"[CTL] {cmd}")

    # ── MIDI tab: points at the real monitor instead of faking one ─────────────

    def _create_midi_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        info = QLabel(
            "Live MIDI monitoring already exists — use Tools → MIDI Monitor…\n"
            "(the MIDI bridge, docs/api.md §8, is shared with the rest of the app;\n"
            "duplicating it here would just be a second, unsynced listener)."
        )
        info.setStyleSheet(f"color: {T.TEXT_DIM};")
        info.setWordWrap(True)
        layout.addWidget(info)
        layout.addStretch()
        return widget

    def _log(self, message: str):
        self.log_display.append(message)
        scrollbar = self.log_display.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def closeEvent(self, event):
        self._stop_touch_polling()
        super().closeEvent(event)


class ButtonInjectorWindow(BaseDialog):
    """Dedicated window for button injection testing against the connected unit."""

    def __init__(self, main_window, parent=None):
        super().__init__("Button Injector", parent or main_window)
        self._mw = main_window
        self._setup_ui()
        self.resize(420, 560)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        info = QLabel(
            "Sends BUTTON <token> to the connected unit (docs/api.md §7/§9).\n"
            "Watch the mirrored screen to confirm which physical button fires."
        )
        info.setStyleSheet(f"color: {T.TEXT_DIM};")
        info.setWordWrap(True)
        layout.addWidget(info)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet(f"color: {T.TEXT_DIM}; font-size: 9pt;")
        layout.addWidget(self.status_label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        table_widget = QWidget()
        table_layout = QVBoxLayout(table_widget)

        by_category: dict[str, list[tuple[str, str, int, str]]] = {}
        for entry in BUTTON_REFERENCE:
            by_category.setdefault(entry[0], []).append(entry)

        for category, entries in by_category.items():
            group = QGroupBox(category)
            grid = QGridLayout(group)
            for row, (_, token, code, desc) in enumerate(entries):
                label = QLabel(f"{token}  ({code})")
                label.setToolTip(desc)
                grid.addWidget(label, row, 0)
                btn = QPushButton("Send")
                btn.clicked.connect(lambda checked, t=token: self._inject_button(t))
                grid.addWidget(btn, row, 1)
            table_layout.addWidget(group)

        table_layout.addStretch()
        scroll.setWidget(table_widget)
        layout.addWidget(scroll, 1)

        custom_group = QGroupBox("Custom Token")
        custom_layout = QHBoxLayout(custom_group)
        self.button_input = QLineEdit()
        self.button_input.setPlaceholderText("Enter a raw button token")
        custom_layout.addWidget(self.button_input)
        inject_btn = QPushButton("Inject")
        inject_btn.clicked.connect(self._inject_custom)
        custom_layout.addWidget(inject_btn)
        layout.addWidget(custom_group)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn)

    def _inject_button(self, token: str):
        import time
        self._mw._ctrl_send(f"BUTTON {token}")
        self.status_label.setText(f"Sent BUTTON {token}  ({time.strftime('%H:%M:%S')})")

    def _inject_custom(self):
        token = self.button_input.text().strip()
        if token:
            self._inject_button(token)
            self.button_input.clear()
