"""
Settings dialog — mirrors SettingsWindow.xaml behavior.
7 tabs: General, Connection, Streaming, View, Key Bindings, Macros, Debug.
Import/Export buttons + OK/Cancel at the bottom.
"""
from __future__ import annotations
import json
import pathlib
import threading
import time
from typing import List, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QScrollArea,
    QSlider, QSpinBox, QSplitter, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

import storage
from app_settings import AppSettings, MacroDef, RawKeyMap, get_rebindable
from models import Keybind

_DIM  = "#888888"
_HEAD = "#88AADD"
_RED  = "#CC4444"


def _section(text: str) -> QLabel:
    lbl = QLabel(text)
    f = lbl.font()
    f.setBold(True)
    lbl.setFont(f)
    lbl.setStyleSheet(f"color: {_HEAD}; margin-top: 4px;")
    return lbl


def _hint(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color: {_DIM}; font-size: 10px;")
    lbl.setWordWrap(True)
    return lbl


class SettingsWindow(QDialog):
    def __init__(self, settings: AppSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumSize(540, 580)
        self.resize(560, 620)
        self._settings = settings
        self._recording_macro_idx: Optional[int] = None
        self._recording_steps: List[str] = []
        self._editing_raw_idx: Optional[int] = None
        self._capture_raw_key_mode = False
        self._build_ui()
        self._load()

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        self._tabs = QTabWidget()
        outer.addWidget(self._tabs, 1)

        self._tabs.addTab(self._build_general_tab(),    "General")
        self._tabs.addTab(self._build_connection_tab(), "Connection")
        self._tabs.addTab(self._build_streaming_tab(),  "Streaming")
        self._tabs.addTab(self._build_view_tab(),       "View")
        self._tabs.addTab(self._build_keybinds_tab(),   "Key Bindings")
        self._tabs.addTab(self._build_macros_tab(),     "Macros")
        self._tabs.addTab(self._build_debug_tab(),      "Debug")

        # Bottom row: Import/Export left; OK/Cancel right
        foot = QHBoxLayout()
        btn_export = QPushButton("Export…")
        btn_export.setToolTip("Save all settings to a JSON file.")
        btn_export.clicked.connect(self._on_export)
        btn_import = QPushButton("Import…")
        btn_import.setToolTip("Load settings from a JSON file, replacing current configuration.")
        btn_import.clicked.connect(self._on_import)
        foot.addWidget(btn_export)
        foot.addWidget(btn_import)
        foot.addStretch()

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        foot.addWidget(btns)
        outer.addLayout(foot)

    # ── General tab ────────────────────────────────────────────────────────────

    def _build_general_tab(self) -> QWidget:
        w = QWidget()
        vb = QVBoxLayout(w)
        vb.setSpacing(6)

        self._quit_prompt = QCheckBox("Prompt before quitting")
        self._hide_ctrl   = QCheckBox("Hide the controls panel on startup")
        vb.addWidget(self._quit_prompt)
        vb.addWidget(self._hide_ctrl)

        vb.addSpacing(8)
        vb.addWidget(_section("Screenshots"))
        ss_row = QHBoxLayout()
        ss_row.addWidget(QLabel("Output folder:"))
        self._screenshot_dir = QLineEdit()
        self._screenshot_dir.setPlaceholderText("(next to app)")
        self._screenshot_dir.setToolTip("Quick-save screenshots go here. Leave empty to save next to the app.")
        ss_row.addWidget(self._screenshot_dir, 1)
        btn_browse = QPushButton("Browse…")
        btn_browse.clicked.connect(self._on_browse_screenshot_dir)
        ss_row.addWidget(btn_browse)
        vb.addLayout(ss_row)
        vb.addWidget(_hint("Folder for quick-save screenshots. Leave empty to save next to the app."))

        vb.addSpacing(8)
        vb.addWidget(_section("VGA output"))
        self._vga_mirror = QCheckBox("Enable VGA mirror on connect")
        vb.addWidget(self._vga_mirror)
        ss_row2 = QHBoxLayout()
        ss_row2.addWidget(QLabel("Screensaver timeout:"))
        self._ss_spin = QSpinBox()
        self._ss_spin.setRange(0, 3600)
        self._ss_spin.setSuffix("  s  (0 = off)")
        self._ss_spin.setFixedWidth(130)
        ss_row2.addWidget(self._ss_spin)
        ss_row2.addStretch()
        vb.addLayout(ss_row2)
        vb.addWidget(_hint("VGA and screensaver settings take effect immediately on OK while connected."))

        vb.addSpacing(8)
        lbl_reset = _section("Reset")
        lbl_reset.setStyleSheet(f"color: {_RED}; margin-top: 4px; font-weight: bold;")
        vb.addWidget(lbl_reset)
        btn_reset = QPushButton("Reset All Settings")
        btn_reset.setStyleSheet(f"QPushButton {{ background: #3A1E1E; color: #DD6666; border: 1px solid #883333; }}")
        btn_reset.setFixedWidth(160)
        btn_reset.setToolTip("Permanently deletes all settings, key maps, calibration, and customizations.")
        btn_reset.clicked.connect(self._on_reset)
        vb.addWidget(btn_reset)
        vb.addWidget(_hint("Removes all saved settings, key mappings, calibration, and customizations. "
                           "The app returns to its default state. This cannot be undone."))

        vb.addStretch()
        return w

    # ── Connection tab ─────────────────────────────────────────────────────────

    def _build_connection_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self._host_edit  = QLineEdit()
        self._sport_spin = QSpinBox(); self._sport_spin.setRange(1, 65535)
        self._cport_spin = QSpinBox(); self._cport_spin.setRange(1, 65535)
        form.addRow("Kronos IP address:", self._host_edit)
        form.addRow("Stream port:",       self._sport_spin)
        form.addRow("Control port:",      self._cport_spin)

        note = _hint("Connection changes take effect on the next connect.")
        form.addRow(note)

        form.addRow(_section("FTP Credentials"))
        self._ftp_user = QLineEdit()
        self._ftp_pass = QLineEdit()
        self._ftp_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self._ftp_port_spin = QSpinBox(); self._ftp_port_spin.setRange(1, 65535)
        form.addRow("FTP Username:", self._ftp_user)
        form.addRow("FTP Password:", self._ftp_pass)
        form.addRow("FTP Port:",     self._ftp_port_spin)
        return w

    # ── Streaming tab ──────────────────────────────────────────────────────────

    def _build_streaming_tab(self) -> QWidget:
        w = QWidget()
        vb = QVBoxLayout(w)
        vb.setSpacing(6)

        vb.addWidget(_section("Stream mode"))
        self._rb_change = QCheckBox("Change-driven — send frames only when screen changes (recommended)")
        self._rb_change.setToolTip("Recommended. The daemon sends a frame only when the display changes.")
        self._rb_pull   = QCheckBox("Pull — client requests each frame individually")
        self._rb_pull.setToolTip("The client requests each frame individually. Try if frames are occasionally missed.")
        # Mutually exclusive via click handlers
        self._rb_change.clicked.connect(lambda: self._rb_pull.setChecked(not self._rb_change.isChecked()))
        self._rb_pull.clicked.connect(lambda: self._rb_change.setChecked(not self._rb_pull.isChecked()))
        vb.addWidget(self._rb_change)
        vb.addWidget(self._rb_pull)

        vb.addSpacing(12)
        vb.addWidget(_section("Max frame rate"))
        fps_row = QHBoxLayout()
        self._fps_slider = QSlider(Qt.Horizontal)
        self._fps_slider.setRange(1, 15)
        self._fps_slider.setTickInterval(1)
        self._fps_slider.setSingleStep(1)
        self._fps_lbl = QLabel("15 fps")
        self._fps_lbl.setFixedWidth(50)
        self._fps_slider.valueChanged.connect(lambda v: self._fps_lbl.setText(f"{v} fps"))
        fps_row.addWidget(self._fps_slider)
        fps_row.addWidget(self._fps_lbl)
        vb.addLayout(fps_row)
        vb.addWidget(_hint("Change-driven mode consumes no CPU when the Kronos screen is idle. "
                           "Streaming changes take effect on the next connect."))
        vb.addStretch()
        return w

    # ── View tab ───────────────────────────────────────────────────────────────

    def _build_view_tab(self) -> QWidget:
        w = QWidget()
        vb = QVBoxLayout(w)
        vb.setSpacing(6)

        vb.addWidget(_section("Zoom"))
        zoom_row = QHBoxLayout()
        zoom_row.addWidget(QLabel("Default zoom level (×):"))
        self._zoom_level_slider = QSlider(Qt.Horizontal)
        self._zoom_level_slider.setRange(25, 50)  # 2.5–5.0 in tenths
        self._zoom_level_slider.setSingleStep(5)
        self._zoom_level_slider.setTickInterval(5)
        self._zoom_level_lbl = QLabel("2.5×")
        self._zoom_level_lbl.setFixedWidth(40)
        self._zoom_level_slider.valueChanged.connect(
            lambda v: self._zoom_level_lbl.setText(f"{v/10:.1f}×"))
        zoom_row.addWidget(self._zoom_level_slider, 1)
        zoom_row.addWidget(self._zoom_level_lbl)
        vb.addLayout(zoom_row)
        vb.addWidget(_hint("Zoom multiplier used when zoom is first activated and when reset. "
                           "Also the minimum — zooming out stops here."))

        vb.addSpacing(12)
        vb.addWidget(_section("Zoom Tool"))
        win_row = QHBoxLayout()
        win_row.addWidget(QLabel("Tool size:"))
        self._zoom_win_slider = QSlider(Qt.Horizontal)
        self._zoom_win_slider.setRange(10, 35)  # 1.0–3.5 in tenths
        self._zoom_win_slider.setSingleStep(5)
        self._zoom_win_slider.setTickInterval(5)
        self._zoom_win_lbl = QLabel("1.0×")
        self._zoom_win_lbl.setFixedWidth(40)
        self._zoom_win_slider.valueChanged.connect(
            lambda v: self._zoom_win_lbl.setText(f"{v/10:.1f}×"))
        win_row.addWidget(self._zoom_win_slider, 1)
        win_row.addWidget(self._zoom_win_lbl)
        vb.addLayout(win_row)
        vb.addWidget(_hint("Size of the zoom loupe overlay at 1× (200×150 px)."))

        vb.addStretch()
        return w

    # ── Key Bindings tab ───────────────────────────────────────────────────────

    def _build_keybinds_tab(self) -> QWidget:
        kb_tab  = QWidget()
        kb_vbox = QVBoxLayout(kb_tab)
        scroll  = QScrollArea(); scroll.setWidgetResizable(True)
        inner   = QWidget()
        kb_form = QFormLayout(inner)
        scroll.setWidget(inner)
        kb_vbox.addWidget(scroll)
        self._kb_edits: dict[str, QLineEdit] = {}
        for action, label, _ in get_rebindable():
            edit = QLineEdit()
            edit.setPlaceholderText("(none)")
            edit.setReadOnly(True)
            edit.setMaximumWidth(180)
            edit.mousePressEvent = lambda e, a=action, ed=edit: self._capture_key(a, ed)
            row_w = QWidget()
            row_h = QHBoxLayout(row_w)
            row_h.setContentsMargins(0, 0, 0, 0)
            row_h.addWidget(edit)
            clr = QPushButton("Clear")
            clr.setFixedWidth(50)
            clr.clicked.connect(lambda checked, a=action, ed=edit: self._clear_key(a, ed))
            row_h.addWidget(clr)
            kb_form.addRow(label + ":", row_w)
            self._kb_edits[action] = edit
        return kb_tab

    # ── Macros tab ─────────────────────────────────────────────────────────────

    def _build_macros_tab(self) -> QWidget:
        w = QWidget()
        vb = QVBoxLayout(w)
        vb.setSpacing(4)

        # Toolbar
        tb = QHBoxLayout()
        self._btn_macro_add    = QPushButton("Add")
        self._btn_macro_remove = QPushButton("Remove"); self._btn_macro_remove.setEnabled(False)
        self._btn_macro_play   = QPushButton("▶ Play"); self._btn_macro_play.setEnabled(False)
        for btn in (self._btn_macro_add, self._btn_macro_remove):
            tb.addWidget(btn)
        tb.addStretch()
        tb.addWidget(self._btn_macro_play)
        vb.addLayout(tb)

        self._macro_list = QListWidget()
        self._macro_list.setSelectionMode(QAbstractItemView.SingleSelection)
        vb.addWidget(self._macro_list, 1)

        # Separator
        sep = QWidget(); sep.setFixedHeight(1)
        sep.setStyleSheet("background: #444;")
        vb.addWidget(sep)

        # Editor panel (enabled only when a macro is selected)
        self._macro_editor = QWidget()
        self._macro_editor.setEnabled(False)
        ef = QFormLayout(self._macro_editor)
        ef.setSpacing(6)

        self._macro_desc = QLineEdit()
        self._macro_desc.setPlaceholderText("Macro description")
        ef.addRow("Description:", self._macro_desc)

        self._macro_trigger_btn = QPushButton("(none)")
        self._macro_trigger_btn.setToolTip("Click to assign a trigger key (must include Ctrl/Alt/Shift).")
        ef.addRow("Trigger:", self._macro_trigger_btn)

        delay_row = QHBoxLayout()
        self._macro_delay_slider = QSlider(Qt.Horizontal)
        self._macro_delay_slider.setRange(10, 500)
        self._macro_delay_slider.setSingleStep(10)
        self._macro_delay_slider.setTickInterval(50)
        self._macro_delay_lbl = QLabel("100 ms")
        self._macro_delay_lbl.setFixedWidth(55)
        self._macro_delay_slider.valueChanged.connect(
            lambda v: self._macro_delay_lbl.setText(f"{v} ms"))
        delay_row.addWidget(self._macro_delay_slider)
        delay_row.addWidget(self._macro_delay_lbl)
        ef.addRow("Step delay:", delay_row)

        self._macro_steps_view = QTextEdit()
        self._macro_steps_view.setReadOnly(True)
        self._macro_steps_view.setFixedHeight(56)
        self._macro_steps_view.setPlaceholderText("(no steps recorded)")
        self._macro_steps_view.setStyleSheet("background: #1E1E1E; color: #BBBBBB;")
        ef.addRow("Steps:", self._macro_steps_view)

        record_row = QHBoxLayout()
        self._btn_macro_record = QPushButton("● Record")
        self._btn_macro_record.setFixedWidth(90)
        self._btn_macro_clear  = QPushButton("Clear")
        self._btn_macro_clear.setFixedWidth(60)
        record_row.addWidget(self._btn_macro_record)
        record_row.addWidget(self._btn_macro_clear)
        record_hint = QLabel("Best with navigation/control keys. Trigger must include Ctrl/Alt/Shift.")
        record_hint.setStyleSheet(f"color: {_DIM}; font-size: 10px;")
        record_hint.setWordWrap(True)
        record_row.addWidget(record_hint, 1)
        ef.addRow("", record_row)

        vb.addWidget(self._macro_editor)

        # Wiring
        self._btn_macro_add.clicked.connect(self._on_macro_add)
        self._btn_macro_remove.clicked.connect(self._on_macro_remove)
        self._btn_macro_play.clicked.connect(self._on_macro_play)
        self._macro_list.currentRowChanged.connect(self._on_macro_selection_changed)
        self._macro_desc.textChanged.connect(self._on_macro_desc_changed)
        self._macro_trigger_btn.clicked.connect(self._on_macro_trigger_click)
        self._macro_delay_slider.valueChanged.connect(self._on_macro_delay_changed)
        self._btn_macro_record.clicked.connect(self._on_macro_record_toggle)
        self._btn_macro_clear.clicked.connect(self._on_macro_clear)

        return w

    # ── Debug tab ──────────────────────────────────────────────────────────────

    def _build_debug_tab(self) -> QWidget:
        w = QWidget()
        vb = QVBoxLayout(w)
        vb.setSpacing(6)

        self._debug_logging = QCheckBox("Enable debug logging")
        self._debug_logging.setToolTip("Writes verbose DEBUG entries to the log. Enable only when diagnosing issues.")
        vb.addWidget(self._debug_logging)
        vb.addWidget(_hint("When enabled, verbose DEBUG entries are written to the log. "
                           "Only Info, Warn, and Error are logged when this is off. Takes effect immediately."))

        vb.addSpacing(8)
        sep = QWidget(); sep.setFixedHeight(1); sep.setStyleSheet("background: #444;")
        vb.addWidget(sep)

        vb.addSpacing(6)
        vb.addWidget(_section("Keyboard Input Mapping"))

        raw_tb = QHBoxLayout()
        self._btn_raw_add    = QPushButton("Add")
        self._btn_raw_remove = QPushButton("Remove"); self._btn_raw_remove.setEnabled(False)
        raw_tb.addWidget(self._btn_raw_add)
        raw_tb.addWidget(self._btn_raw_remove)
        raw_tb.addStretch()
        vb.addLayout(raw_tb)

        self._raw_list = QListWidget()
        self._raw_list.setSelectionMode(QAbstractItemView.SingleSelection)
        vb.addWidget(self._raw_list, 1)

        vb.addWidget(_hint("Custom mappings override the default key map and take effect immediately. "
                           "Double-click a row to edit."))

        # Inline editor
        self._raw_editor = QWidget()
        self._raw_editor.setVisible(False)
        raw_ef = QFormLayout(self._raw_editor)
        raw_ef.setSpacing(4)

        raw_sep = QWidget(); raw_sep.setFixedHeight(1); raw_sep.setStyleSheet("background: #444;")
        raw_ef.addRow(raw_sep)

        self._raw_label_edit = QLineEdit(); self._raw_label_edit.setPlaceholderText("Label")
        raw_ef.addRow("Label:", self._raw_label_edit)

        self._raw_capture_btn = QPushButton("(none) — click to capture")
        self._raw_capture_btn.setToolTip("Click, then press the host key to map from.")
        raw_ef.addRow("Host key:", self._raw_capture_btn)

        raw_code_row = QHBoxLayout()
        self._raw_code_edit  = QLineEdit(); self._raw_code_edit.setFixedWidth(60)
        self._raw_code_edit.setPlaceholderText("e.g. 30")
        self._raw_shift_chk  = QCheckBox("Send Shift")
        raw_code_row.addWidget(self._raw_code_edit)
        raw_code_row.addWidget(self._raw_shift_chk)
        raw_code_row.addStretch()
        raw_ef.addRow("Raw code:", raw_code_row)

        raw_btns = QHBoxLayout()
        self._raw_save_btn   = QPushButton("Save")
        self._raw_cancel_btn = QPushButton("Cancel")
        for btn in (self._raw_save_btn, self._raw_cancel_btn):
            btn.setFixedWidth(70)
            raw_btns.addWidget(btn)
        raw_btns.addStretch()
        raw_ef.addRow("", raw_btns)

        vb.addWidget(self._raw_editor)

        # Wiring
        self._btn_raw_add.clicked.connect(self._on_raw_add)
        self._btn_raw_remove.clicked.connect(self._on_raw_remove)
        self._raw_list.currentRowChanged.connect(self._on_raw_selection_changed)
        self._raw_list.doubleClicked.connect(self._on_raw_double_click)
        self._raw_capture_btn.clicked.connect(self._on_raw_capture_key)
        self._raw_save_btn.clicked.connect(self._on_raw_save)
        self._raw_cancel_btn.clicked.connect(self._on_raw_cancel)

        return w

    # ── Load / Save ────────────────────────────────────────────────────────────

    def _load(self):
        s = self._settings

        # General
        self._quit_prompt.setChecked(s.prompt_before_quitting)
        self._hide_ctrl.setChecked(s.hide_controls)
        self._screenshot_dir.setText(s.screenshot_dir)
        self._vga_mirror.setChecked(s.vga_mirror_enabled)
        self._ss_spin.setValue(s.screensaver_timeout)

        # Connection
        self._host_edit.setText(s.kronos_host)
        self._sport_spin.setValue(s.stream_port)
        self._cport_spin.setValue(s.ctrl_port)
        self._ftp_user.setText(s.ftp_username)
        self._ftp_pass.setText(s.ftp_password)
        self._ftp_port_spin.setValue(s.ftp_port)

        # Streaming
        pull = s.pull_mode
        self._rb_pull.setChecked(pull)
        self._rb_change.setChecked(not pull)
        self._fps_slider.setValue(s.max_fps)
        self._fps_lbl.setText(f"{s.max_fps} fps")

        # View
        self._zoom_level_slider.setValue(int(s.zoom_default_level * 10))
        self._zoom_level_lbl.setText(f"{s.zoom_default_level:.1f}×")
        self._zoom_win_slider.setValue(int(s.zoom_window_size * 10))
        self._zoom_win_lbl.setText(f"{s.zoom_window_size:.1f}×")

        # Key bindings
        for action, edit in self._kb_edits.items():
            kb = s.get_keybind(action)
            edit.setText(kb.to_display_string() if kb.key != 0 else "")

        # Macros
        self._reload_macro_list()

        # Debug
        self._debug_logging.setChecked(s.debug_logging)
        self._reload_raw_list()

    def _save(self):
        s = self._settings

        # Commit any in-progress macro edit
        self._commit_macro_editor()

        # General
        s.prompt_before_quitting = self._quit_prompt.isChecked()
        s.hide_controls          = self._hide_ctrl.isChecked()
        s.screenshot_dir         = self._screenshot_dir.text().strip()
        s.vga_mirror_enabled     = self._vga_mirror.isChecked()
        s.screensaver_timeout    = self._ss_spin.value()

        # Connection
        s.kronos_host  = self._host_edit.text().strip()
        s.stream_port  = self._sport_spin.value()
        s.ctrl_port    = self._cport_spin.value()
        s.ftp_username = self._ftp_user.text()
        s.ftp_password = self._ftp_pass.text()
        s.ftp_port     = self._ftp_port_spin.value()

        # Streaming
        s.pull_mode = self._rb_pull.isChecked()
        s.max_fps   = self._fps_slider.value()

        # View
        s.zoom_default_level = self._zoom_level_slider.value() / 10.0
        s.zoom_window_size   = self._zoom_win_slider.value()   / 10.0

        # Debug
        s.debug_logging = self._debug_logging.isChecked()

        self.accept()

    # ── General actions ────────────────────────────────────────────────────────

    def _on_browse_screenshot_dir(self):
        current = self._screenshot_dir.text().strip() or str(pathlib.Path.home())
        path = QFileDialog.getExistingDirectory(self, "Screenshot Output Folder", current)
        if path:
            self._screenshot_dir.setText(path)

    def _on_reset(self):
        r = QMessageBox.warning(
            self, "Reset All Settings",
            "This will permanently delete all settings, key mappings, "
            "calibration data, and customizations.\n\nThis cannot be undone.",
            QMessageBox.Ok | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if r == QMessageBox.Ok:
            storage.reset_all()
            self._settings.__init__()  # reset in-place
            self._load()
            QMessageBox.information(self, "Reset", "Settings have been reset to defaults.")

    # ── Key binding helpers ────────────────────────────────────────────────────

    def _capture_key(self, action: str, edit: QLineEdit):
        edit.setText("Press a key…")
        edit.setFocus()

        def key_handler(event):
            from PySide6.QtCore import Qt as _Qt
            from main_window import _mods_to_int
            key = event.key()
            if key in (_Qt.Key_Shift, _Qt.Key_Control, _Qt.Key_Alt, _Qt.Key_Meta):
                return
            mods = _mods_to_int(event.modifiers())
            kb = Keybind(key, mods)
            self._settings.set_keybind(action, kb)
            edit.setText(kb.to_display_string())
            edit.keyPressEvent = QLineEdit.keyPressEvent.__get__(edit, type(edit))

        edit.keyPressEvent = key_handler

    def _clear_key(self, action: str, edit: QLineEdit):
        self._settings.set_keybind(action, Keybind.NONE)
        edit.setText("")

    # ── Macro helpers ──────────────────────────────────────────────────────────

    def _reload_macro_list(self):
        self._macro_list.clear()
        for m in self._settings.macros:
            self._macro_list.addItem(
                f"{m.description or '(unnamed)'}  [{m.trigger_display}]  "
                f"{m.step_count} steps  {m.delay_display}"
            )

    def _on_macro_add(self):
        m = MacroDef(description=f"Macro {len(self._settings.macros) + 1}")
        self._settings.macros.append(m)
        self._reload_macro_list()
        self._macro_list.setCurrentRow(len(self._settings.macros) - 1)

    def _on_macro_remove(self):
        idx = self._macro_list.currentRow()
        if idx < 0 or idx >= len(self._settings.macros):
            return
        if self._recording_macro_idx == idx:
            self._stop_recording()
        self._settings.macros.pop(idx)
        self._reload_macro_list()
        self._macro_editor.setEnabled(False)
        self._btn_macro_remove.setEnabled(False)
        self._btn_macro_play.setEnabled(False)

    def _on_macro_play(self):
        idx = self._macro_list.currentRow()
        if idx < 0 or idx >= len(self._settings.macros):
            return
        m = self._settings.macros[idx]
        if not m.steps:
            return
        self._commit_macro_editor()
        threading.Thread(target=self._play_macro, args=(m,), daemon=True).start()

    def _play_macro(self, m: MacroDef):
        import ctrl_client as CC
        host = self._settings.kronos_host
        port = self._settings.ctrl_port
        if not host:
            return
        for step in m.steps:
            CC.get().send(host, port, step)
            time.sleep(m.step_delay_ms / 1000.0)

    def _on_macro_selection_changed(self, idx: int):
        has = 0 <= idx < len(self._settings.macros)
        self._btn_macro_remove.setEnabled(has)
        self._btn_macro_play.setEnabled(has)
        self._macro_editor.setEnabled(has)
        if has:
            self._load_macro_editor(idx)

    def _load_macro_editor(self, idx: int):
        m = self._settings.macros[idx]
        self._macro_desc.blockSignals(True)
        self._macro_desc.setText(m.description)
        self._macro_desc.blockSignals(False)
        self._macro_trigger_btn.setText(m.trigger_display)
        self._macro_delay_slider.setValue(m.step_delay_ms)
        self._macro_delay_lbl.setText(f"{m.step_delay_ms} ms")
        self._macro_steps_view.setPlainText("\n".join(m.steps) if m.steps else "")

    def _commit_macro_editor(self):
        idx = self._macro_list.currentRow()
        if idx < 0 or idx >= len(self._settings.macros):
            return
        m = self._settings.macros[idx]
        m.description   = self._macro_desc.text()
        m.step_delay_ms = self._macro_delay_slider.value()

    def _on_macro_desc_changed(self, text: str):
        idx = self._macro_list.currentRow()
        if 0 <= idx < len(self._settings.macros):
            self._settings.macros[idx].description = text
            self._macro_list.item(idx).setText(
                f"{text or '(unnamed)'}  [{self._settings.macros[idx].trigger_display}]  "
                f"{self._settings.macros[idx].step_count} steps  "
                f"{self._settings.macros[idx].delay_display}"
            )

    def _on_macro_trigger_click(self):
        idx = self._macro_list.currentRow()
        if idx < 0:
            return
        self._macro_trigger_btn.setText("Press a key combo…")
        self._macro_trigger_btn.setFocus()

        def key_handler(event):
            from PySide6.QtCore import Qt as _Qt
            from main_window import _mods_to_int
            key  = event.key()
            mods = _mods_to_int(event.modifiers())
            if key in (_Qt.Key_Escape,):
                self._macro_trigger_btn.setText(self._settings.macros[idx].trigger_display)
                self._macro_trigger_btn.keyPressEvent = QPushButton.keyPressEvent.__get__(
                    self._macro_trigger_btn, type(self._macro_trigger_btn))
                return
            if key in (_Qt.Key_Shift, _Qt.Key_Control, _Qt.Key_Alt, _Qt.Key_Meta):
                return
            if not mods:
                QTimer.singleShot(0, lambda: QMessageBox.warning(
                    self, "Trigger", "Macro trigger must include Ctrl, Alt, or Shift."))
                self._macro_trigger_btn.setText(self._settings.macros[idx].trigger_display)
                self._macro_trigger_btn.keyPressEvent = QPushButton.keyPressEvent.__get__(
                    self._macro_trigger_btn, type(self._macro_trigger_btn))
                return
            m = self._settings.macros[idx]
            m.trigger_key  = key
            m.trigger_mods = mods
            self._macro_trigger_btn.setText(m.trigger_display)
            self._macro_trigger_btn.keyPressEvent = QPushButton.keyPressEvent.__get__(
                self._macro_trigger_btn, type(self._macro_trigger_btn))
            self._reload_macro_list()

        self._macro_trigger_btn.keyPressEvent = key_handler

    def _on_macro_delay_changed(self, val: int):
        idx = self._macro_list.currentRow()
        if 0 <= idx < len(self._settings.macros):
            self._settings.macros[idx].step_delay_ms = val

    def _on_macro_record_toggle(self):
        idx = self._macro_list.currentRow()
        if idx < 0:
            return
        if self._recording_macro_idx is not None:
            self._stop_recording()
        else:
            self._start_recording(idx)

    def _start_recording(self, idx: int):
        self._recording_macro_idx = idx
        self._recording_steps = []
        self._settings.macros[idx].steps = []
        self._macro_steps_view.setPlainText("")
        self._btn_macro_record.setText("■ Stop")
        self._btn_macro_record.setStyleSheet("QPushButton { color: #FF6666; }")
        self._macro_steps_view.setFocus()
        self._macro_steps_view.setReadOnly(False)
        self._macro_steps_view.setPlaceholderText("Recording… press keys here")

        orig_key = self._macro_steps_view.keyPressEvent

        def capture(event):
            from PySide6.QtCore import Qt as _Qt
            import key_map
            key  = event.key()
            if key in (_Qt.Key_Escape,):
                self._stop_recording()
                return
            lc = key_map.to_linux(key)
            if lc:
                step_dn = f"KEY {lc} 1"
                step_up = f"KEY {lc} 0"
                self._recording_steps.extend([step_dn, step_up])
                self._macro_steps_view.setPlainText("\n".join(self._recording_steps))
            event.accept()

        self._macro_steps_view.keyPressEvent = capture

    def _stop_recording(self):
        idx = self._recording_macro_idx
        if idx is None:
            return
        if 0 <= idx < len(self._settings.macros):
            self._settings.macros[idx].steps = list(self._recording_steps)
        self._recording_macro_idx = None
        self._recording_steps = []
        self._btn_macro_record.setText("● Record")
        self._btn_macro_record.setStyleSheet("")
        self._macro_steps_view.setReadOnly(True)
        self._macro_steps_view.setPlaceholderText("(no steps recorded)")
        self._macro_steps_view.keyPressEvent = QTextEdit.keyPressEvent.__get__(
            self._macro_steps_view, type(self._macro_steps_view))
        self._reload_macro_list()

    def _on_macro_clear(self):
        idx = self._macro_list.currentRow()
        if 0 <= idx < len(self._settings.macros):
            self._settings.macros[idx].steps = []
            self._macro_steps_view.setPlainText("")
            self._reload_macro_list()

    # ── Raw key mapping helpers ────────────────────────────────────────────────

    def _reload_raw_list(self):
        self._raw_list.clear()
        for r in self._settings.raw_key_maps:
            self._raw_list.addItem(
                f"{r.host_key_display}  →  {r.raw_display}  [{r.label}]"
            )

    def _on_raw_add(self):
        rm = RawKeyMap(label="New mapping")
        self._settings.raw_key_maps.append(rm)
        self._reload_raw_list()
        idx = len(self._settings.raw_key_maps) - 1
        self._raw_list.setCurrentRow(idx)
        self._open_raw_editor(idx)

    def _on_raw_remove(self):
        idx = self._raw_list.currentRow()
        if idx < 0:
            return
        self._settings.raw_key_maps.pop(idx)
        self._reload_raw_list()
        self._raw_editor.setVisible(False)
        self._btn_raw_remove.setEnabled(False)

    def _on_raw_selection_changed(self, idx: int):
        self._btn_raw_remove.setEnabled(idx >= 0)

    def _on_raw_double_click(self):
        idx = self._raw_list.currentRow()
        if idx >= 0:
            self._open_raw_editor(idx)

    def _open_raw_editor(self, idx: int):
        if idx < 0 or idx >= len(self._settings.raw_key_maps):
            return
        self._editing_raw_idx = idx
        r = self._settings.raw_key_maps[idx]
        self._raw_label_edit.setText(r.label)
        self._raw_capture_btn.setText(r.host_key_display)
        self._raw_code_edit.setText(str(r.raw_code) if r.raw_code else "")
        self._raw_shift_chk.setChecked(r.send_shift)
        self._raw_editor.setVisible(True)

    def _on_raw_capture_key(self):
        self._raw_capture_btn.setText("Press a key…")
        self._raw_capture_btn.setFocus()

        def key_handler(event):
            from PySide6.QtCore import Qt as _Qt
            from main_window import _mods_to_int
            key  = event.key()
            if key in (_Qt.Key_Escape,):
                idx = self._editing_raw_idx
                if idx is not None and 0 <= idx < len(self._settings.raw_key_maps):
                    self._raw_capture_btn.setText(
                        self._settings.raw_key_maps[idx].host_key_display)
                self._raw_capture_btn.keyPressEvent = QPushButton.keyPressEvent.__get__(
                    self._raw_capture_btn, type(self._raw_capture_btn))
                return
            if key in (_Qt.Key_Shift, _Qt.Key_Control, _Qt.Key_Alt, _Qt.Key_Meta):
                return
            mods = _mods_to_int(event.modifiers())
            kb = Keybind(key, mods)
            if self._editing_raw_idx is not None:
                r = self._settings.raw_key_maps[self._editing_raw_idx]
                r.host_key  = key
                r.host_mods = mods
            self._raw_capture_btn.setText(kb.to_display_string())
            self._raw_capture_btn.keyPressEvent = QPushButton.keyPressEvent.__get__(
                self._raw_capture_btn, type(self._raw_capture_btn))

        self._raw_capture_btn.keyPressEvent = key_handler

    def _on_raw_save(self):
        idx = self._editing_raw_idx
        if idx is None or idx >= len(self._settings.raw_key_maps):
            return
        r = self._settings.raw_key_maps[idx]
        r.label      = self._raw_label_edit.text()
        r.send_shift = self._raw_shift_chk.isChecked()
        try:
            r.raw_code = int(self._raw_code_edit.text())
        except ValueError:
            r.raw_code = 0
        self._raw_editor.setVisible(False)
        self._editing_raw_idx = None
        self._reload_raw_list()

    def _on_raw_cancel(self):
        self._raw_editor.setVisible(False)
        self._editing_raw_idx = None

    # ── Import / Export ────────────────────────────────────────────────────────

    def _on_export(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Settings", "kronos_settings.json",
            "JSON Files (*.json)")
        if path:
            self._commit_macro_editor()
            # Copy live settings into AppSettings before exporting
            self._save_to_settings_no_close()
            storage.export_settings(self._settings, path)
            QMessageBox.information(self, "Export", f"Settings exported to:\n{path}")

    def _on_import(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Settings", "", "JSON Files (*.json)")
        if not path:
            return
        r = QMessageBox.question(
            self, "Import Settings",
            "This will replace all current settings. Continue?",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
        if r != QMessageBox.Yes:
            return
        new_s = storage.import_settings(path)
        # Replace fields in the shared settings object
        for field_name in self._settings.__dataclass_fields__:
            setattr(self._settings, field_name, getattr(new_s, field_name))
        self._load()
        QMessageBox.information(self, "Import", "Settings imported successfully.")

    def _save_to_settings_no_close(self):
        """Flush UI state into self._settings without calling accept()."""
        s = self._settings
        s.prompt_before_quitting = self._quit_prompt.isChecked()
        s.hide_controls          = self._hide_ctrl.isChecked()
        s.screenshot_dir         = self._screenshot_dir.text().strip()
        s.vga_mirror_enabled     = self._vga_mirror.isChecked()
        s.screensaver_timeout    = self._ss_spin.value()
        s.kronos_host            = self._host_edit.text().strip()
        s.stream_port            = self._sport_spin.value()
        s.ctrl_port              = self._cport_spin.value()
        s.ftp_username           = self._ftp_user.text()
        s.ftp_password           = self._ftp_pass.text()
        s.ftp_port               = self._ftp_port_spin.value()
        s.pull_mode              = self._rb_pull.isChecked()
        s.max_fps                = self._fps_slider.value()
        s.zoom_default_level     = self._zoom_level_slider.value() / 10.0
        s.zoom_window_size       = self._zoom_win_slider.value()   / 10.0
        s.debug_logging          = self._debug_logging.isChecked()
