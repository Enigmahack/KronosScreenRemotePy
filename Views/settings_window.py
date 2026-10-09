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
    QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QMessageBox, QPushButton, QRadioButton, QScrollArea,
    QSlider, QSpinBox, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

import Models.storage
from Models.app_settings import AppSettings, MacroDef, RawKeyMap, get_rebindable
from Models.models import Keybind
import Utils.key_map as key_map
import Utils.theme as T
from Views.revealable_password_edit import RevealablePasswordEdit

_DIM  = T.TEXT_DIM
_HEAD = T.ACCENT
_RED  = T.ERROR


def _section(text: str) -> QLabel:
    lbl = QLabel(text)
    f = lbl.font()
    f.setBold(True)
    lbl.setFont(f)
    lbl.setStyleSheet(f"color: {_HEAD}; margin-top: 4px;")
    return lbl


_POLL_INTERVALS = (30, 45, 60, 120)


def _parse_value_slider_cc(text: str) -> int:
    """A valid MIDI controller (0-119), excluding 0 and 32 (Bank Select MSB/LSB, which drive
    program-change follow). Falls back to the Kronos default (18) on unparseable/out-of-range input."""
    try:
        v = int(text.strip())
    except ValueError:
        return 18
    return v if 0 <= v <= 119 and v not in (0, 32) else 18


def _hint(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color: {_DIM}; font-size: 10px;")
    lbl.setWordWrap(True)
    return lbl


class _ResettableSlider(QSlider):
    """QSlider that resets to `default_value` on double-click.

    Port of SettingsWindow.xaml.cs's OnSliderPreviewMouseLeftButtonDown,
    which is wired once via an implicit Slider style so every slider in the
    C# window gets this for free. Qt has no equivalent implicit-style hook,
    so this is a drop-in QSlider subclass instead — same effect, one call
    site per slider (`.default_value = ...`) rather than a per-slider
    handler.
    """

    default_value: Optional[int] = None

    def mouseDoubleClickEvent(self, event):
        if self.default_value is not None:
            self.setValue(self.default_value)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class SettingsWindow(QDialog):
    def __init__(self, settings: AppSettings, parent=None, initial_tab: str = "",
                 on_image_preview=None, on_button_injector=None, on_test_mode=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumSize(540, 580)
        self.resize(560, 620)
        self._settings = settings
        # Live preview callback for the Image tab (writes to the frame widget
        # only, never to settings — so Cancel can cleanly revert).
        self._on_image_preview = on_image_preview
        self._on_button_injector = on_button_injector
        self._on_test_mode = on_test_mode
        self._recording_macro_idx: Optional[int] = None
        self._recording_steps: List[str] = []
        self._editing_raw_idx: Optional[int] = None
        self._capture_raw_key_mode = False
        self._build_ui()
        self._load()
        if initial_tab:
            self.select_tab(initial_tab)

    def select_tab(self, name: str):
        for g in range(self._tabs.count()):
            sub = self._tabs.widget(g)
            for i in range(sub.count()):
                if sub.tabText(i) == name:
                    self._tabs.setCurrentIndex(g)
                    sub.setCurrentIndex(i)
                    return

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        # Two levels instead of one long scrolling tab row: group tabs on top,
        # the individual pages inside each group.
        self._tabs = QTabWidget()
        outer.addWidget(self._tabs, 1)
        groups = (
            ("General", (("General", self._build_general_tab),
                         ("Connection", self._build_connection_tab),
                         ("Streaming", self._build_streaming_tab))),
            ("Display & Input", (("View", self._build_view_tab),
                                 ("Image", self._build_image_tab),
                                 ("Key Bindings", self._build_keybinds_tab),
                                 ("Input Mapping", self._build_input_mapping_tab),
                                 ("Macros", self._build_macros_tab))),
            ("Tools", (("MIDI/SysEx", self._build_midi_tab),
                       ("Librarian", self._build_librarian_tab),
                       ("Sample Editor", self._build_sample_editor_tab),
                       ("Debug", self._build_debug_tab))),
        )
        for group, pages in groups:
            sub = QTabWidget()
            for title, build in pages:
                sub.addTab(build(), title)
            self._tabs.addTab(sub, group)

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

        self._quit_prompt    = QCheckBox("Prompt before quitting")
        self._hide_data_inp  = QCheckBox("Hide data input panel on startup")
        self._hide_data_inp.setToolTip("Hides the right-hand button panel at launch. Toggle from View → Hide Data Input.")
        self._hide_value_inp = QCheckBox("Hide value input panel on startup")
        self._hide_value_inp.setToolTip("Hides the left-hand slider panel at launch. Toggle from View → Hide Value Input.")
        self._reverse_scroll = QCheckBox("Reverse mouse scrolling direction")
        self._reverse_scroll.setToolTip("Swaps which wheel direction sends WHEEL CW vs WHEEL CCW to the instrument data wheel.")
        vb.addWidget(self._quit_prompt)
        vb.addWidget(self._hide_data_inp)
        vb.addWidget(self._hide_value_inp)
        vb.addWidget(self._reverse_scroll)

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
        self._ss_spin.setFixedWidth(80)
        ss_row2.addWidget(self._ss_spin)
        ss_row2.addWidget(QLabel("s  (0 = off)"))
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
        self._saved_combo = QComboBox()
        self._saved_combo.setToolTip("Choosing an entry fills in the IP address and FTP login below.")
        self._saved_combo.currentIndexChanged.connect(self._on_saved_selected)
        self._saved_add = QPushButton("+")
        self._saved_add.setFixedWidth(28)
        self._saved_add.setToolTip("Save the current settings as a new entry.")
        self._saved_add.clicked.connect(self._on_saved_add)
        self._saved_remove = QPushButton("-")
        self._saved_remove.setFixedWidth(28)
        self._saved_remove.setToolTip("Remove the selected entry.")
        self._saved_remove.clicked.connect(self._on_saved_remove)
        self._saved_edit = QPushButton("Edit...")
        self._saved_edit.setToolTip("Edit the selected entry.")
        self._saved_edit.clicked.connect(self._on_saved_edit)
        saved_row = QHBoxLayout()
        saved_row.addWidget(self._saved_combo, 1)
        saved_row.addWidget(self._saved_add)
        saved_row.addWidget(self._saved_remove)
        saved_row.addWidget(self._saved_edit)
        form.addRow("Saved connections:", saved_row)
        self._host_edit  = QLineEdit()
        form.addRow("Instrument IP address:", self._host_edit)

        note = _hint("Connection changes take effect on the next connect.")
        form.addRow(note)

        form.addRow(_section("FTP Credentials"))
        self._ftp_user = QLineEdit()
        self._ftp_pass = RevealablePasswordEdit()
        self._ftp_port_spin = QSpinBox(); self._ftp_port_spin.setRange(1, 65535)
        form.addRow("FTP Username:", self._ftp_user)
        form.addRow("FTP Password:", self._ftp_pass)
        form.addRow("FTP Port:",     self._ftp_port_spin)
        return w

    # ── Saved connections ──────────────────────────────────────────────────────
    # Edits go to self._saved (a working copy) and are written back on OK / Apply, so Cancel
    # discards them like every other setting.

    def _rebind_saved(self, select: Optional[int] = None) -> None:
        self._saved_combo.blockSignals(True)
        self._saved_combo.clear()
        for c in self._saved:
            self._saved_combo.addItem(str(c["name"]))
        self._saved_combo.setCurrentIndex(-1 if select is None else select)
        self._saved_combo.blockSignals(False)
        self._update_saved_buttons()

    def _update_saved_buttons(self) -> None:
        has = self._saved_combo.currentIndex() >= 0
        self._saved_remove.setEnabled(has)
        self._saved_edit.setEnabled(has)

    def _on_saved_selected(self, idx: int) -> None:
        self._update_saved_buttons()
        if not (0 <= idx < len(self._saved)):
            return
        c = self._saved[idx]
        self._host_edit.setText(str(c["host"]))
        self._ftp_user.setText(str(c["username"]))
        self._ftp_pass.setText(str(c["password"]))
        self._ftp_port_spin.setValue(int(c["ftp_port"]))

    def _on_saved_add(self) -> None:
        from Views.connection_dialogs import SavedConnectionDialog
        seed = {"name": "", "host": self._host_edit.text().strip(),
                "username": self._ftp_user.text().strip(), "password": self._ftp_pass.text(),
                "ftp_port": self._ftp_port_spin.value()}
        dlg = SavedConnectionDialog(seed, True, self)
        if dlg.exec() != QDialog.Accepted or dlg.result_entry is None:
            return
        self._saved.append(dlg.result_entry)
        self._rebind_saved(len(self._saved) - 1)
        self._on_saved_selected(len(self._saved) - 1)

    def _on_saved_edit(self) -> None:
        from Views.connection_dialogs import SavedConnectionDialog
        idx = self._saved_combo.currentIndex()
        if not (0 <= idx < len(self._saved)):
            return
        dlg = SavedConnectionDialog(self._saved[idx], False, self)
        if dlg.exec() != QDialog.Accepted or dlg.result_entry is None:
            return
        self._saved[idx] = dlg.result_entry
        self._rebind_saved(idx)
        self._on_saved_selected(idx)

    def _on_saved_remove(self) -> None:
        idx = self._saved_combo.currentIndex()
        if not (0 <= idx < len(self._saved)):
            return
        del self._saved[idx]
        self._rebind_saved(None)

    # ── Streaming tab ──────────────────────────────────────────────────────────

    def _build_streaming_tab(self) -> QWidget:
        w = QWidget()
        vb = QVBoxLayout(w)
        vb.setSpacing(6)

        vb.addWidget(_section("Stream mode"))
        self._stream_mode = QComboBox()
        self._stream_mode.setMinimumWidth(200)
        self._stream_mode.addItem("Change-driven - send frames only when the screen changes (recommended)")
        self._stream_mode.addItem("Pull - client requests each frame individually")
        self._stream_mode.setToolTip(
            "Change-driven (recommended): the daemon sends a frame only when the display "
            "changes. Pull: the client requests each frame individually — try if frames "
            "are occasionally missed.")
        vb.addWidget(self._stream_mode)

        vb.addSpacing(12)
        vb.addWidget(_section("Max frame rate"))
        fps_row = QHBoxLayout()
        self._fps_slider = _ResettableSlider(Qt.Horizontal)
        self._fps_slider.default_value = AppSettings().max_fps
        self._fps_slider.setRange(1, 15)
        self._fps_slider.setTickInterval(1)
        self._fps_slider.setSingleStep(1)
        self._fps_lbl = QLabel("15 fps")
        self._fps_lbl.setFixedWidth(50)
        self._fps_slider.valueChanged.connect(lambda v: self._fps_lbl.setText(f"{v} fps"))
        fps_row.addWidget(self._fps_slider)
        fps_row.addWidget(self._fps_lbl)
        vb.addLayout(fps_row)
        vb.addWidget(_hint("Change-driven mode consumes no CPU when the instrument screen is idle. "
                           "Streaming changes take effect on the next connect."))

        vb.addStretch()
        return w

    # ── View tab ───────────────────────────────────────────────────────────────

    def _build_view_tab(self) -> QWidget:
        w = QWidget()
        vb = QVBoxLayout(w)
        vb.setSpacing(6)

        vb.addWidget(_section("Window"))
        win_size_row = QHBoxLayout()
        win_size_row.addWidget(QLabel("Default window size:"))
        self._default_window_size = QComboBox()
        self._default_window_size.setToolTip(
            "Last Used keeps remembering the size/position you leave the window at.")
        self._default_window_size.addItem("Last Used", "LastUsed")
        self._default_window_size.addItem("Small", "Small")
        self._default_window_size.addItem("Medium", "Medium")
        self._default_window_size.addItem("Large", "Large")
        self._default_window_size.addItem("Maximized", "Maximized")
        win_size_row.addWidget(self._default_window_size, 1)
        vb.addLayout(win_size_row)
        vb.addWidget(_hint("Size applied to the main window at launch. Last Used remembers "
                           "the size and position you leave it at."))

        vb.addSpacing(12)
        vb.addWidget(_section("Zoom"))
        zoom_row = QHBoxLayout()
        zoom_row.addWidget(QLabel("Default zoom level (×):"))
        self._zoom_level_slider = _ResettableSlider(Qt.Horizontal)
        self._zoom_level_slider.default_value = round(AppSettings().zoom_default_level * 10)
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
        self._zoom_win_slider = _ResettableSlider(Qt.Horizontal)
        self._zoom_win_slider.default_value = round(AppSettings().zoom_window_size * 10)
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

    # ── Image tab ────────────────────────────────────────────────────────────────

    def _build_image_tab(self) -> QWidget:
        w = QWidget()
        vb = QVBoxLayout(w)
        vb.setSpacing(6)

        vb.addWidget(_section("Image Adjustments"))
        vb.addWidget(_hint("Applied to the streamed instrument screen in real time. "
                           "These affect only what you see here — not the instrument itself."))
        vb.addSpacing(6)

        def _adj_row(name: str, lo: int, hi: int, fmt, default: int) -> QSlider:
            row = QHBoxLayout()
            name_lbl = QLabel(name)
            name_lbl.setFixedWidth(84)
            sl = _ResettableSlider(Qt.Horizontal)
            sl.default_value = default
            sl.setRange(lo, hi)
            val = QLabel("")
            val.setFixedWidth(48)
            val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            sl.valueChanged.connect(lambda v: val.setText(fmt(v)))
            val.setText(fmt(sl.value()))
            row.addWidget(name_lbl)
            row.addWidget(sl, 1)
            row.addWidget(val)
            vb.addLayout(row)
            return sl

        _img_defaults = AppSettings()
        self._img_bri_slider = _adj_row("Brightness", -100, 100, lambda v: str(v),
                                         _img_defaults.image_brightness)
        self._img_con_slider = _adj_row("Contrast",   -100, 100, lambda v: str(v),
                                         _img_defaults.image_contrast)
        self._img_gam_slider = _adj_row("Gamma",        40, 250, lambda v: f"{v/100:.2f}",
                                         round(_img_defaults.image_gamma * 100))
        self._img_sat_slider = _adj_row("Saturation", -100, 100, lambda v: str(v),
                                         _img_defaults.image_saturation)
        self._img_shp_slider = _adj_row("Sharpen",       0, 100, lambda v: str(v),
                                         _img_defaults.image_sharpen)
        # Live preview: any slider change re-renders the frame immediately.
        for sl in (self._img_bri_slider, self._img_con_slider, self._img_gam_slider,
                   self._img_sat_slider, self._img_shp_slider):
            sl.valueChanged.connect(lambda _=0: self._emit_image_preview())

        vb.addWidget(_hint("Brightness / Contrast / Gamma / Saturation are folded into the "
                           "colour lookup (free per pixel). Sharpen is a 3×3 unsharp mask run "
                           "once per frame."))

        vb.addSpacing(8)
        reset_row = QHBoxLayout()
        btn_reset = QPushButton("Reset to defaults")
        btn_reset.setToolTip("Brightness 0, Contrast 0, Gamma 1.00, Saturation 0, Sharpen 0.")
        btn_reset.clicked.connect(self._reset_image_adjust)
        reset_row.addWidget(btn_reset)
        reset_row.addStretch()
        vb.addLayout(reset_row)

        vb.addStretch()
        return w

    def _reset_image_adjust(self):
        self._img_bri_slider.setValue(0)
        self._img_con_slider.setValue(0)
        self._img_gam_slider.setValue(100)
        self._img_sat_slider.setValue(0)
        self._img_shp_slider.setValue(0)

    def _emit_image_preview(self):
        if self._on_image_preview:
            self._on_image_preview(
                self._img_bri_slider.value(),
                self._img_con_slider.value(),
                self._img_gam_slider.value() / 100.0,
                self._img_sat_slider.value(),
                self._img_shp_slider.value())

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
        self._macro_delay_slider = _ResettableSlider(Qt.Horizontal)
        self._macro_delay_slider.default_value = MacroDef().step_delay_ms
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

    # ── Librarian tab ──────────────────────────────────────────────────────────
    # Port of SettingsWindow.xaml's Librarian tab: the Merge Window's persistence
    # behavior plus the per-type preserve-duplication policy for Merge->Local placement.

    def _build_midi_tab(self) -> QWidget:
        w = QWidget()
        vb = QVBoxLayout(w)
        vb.setSpacing(6)

        self._midi_monitor = QCheckBox("Monitor MIDI")
        self._midi_monitor.setToolTip(
            "When disabled, all incoming MIDI and SysEx messages are ignored and the MIDI Monitor "
            "window is unavailable.")
        vb.addWidget(self._midi_monitor)
        vb.addWidget(_hint("When disabled, the MIDI Monitor option is faded in the menu and no incoming "
                           "MIDI data is processed (the Librarian cannot sync without it)."))

        vb.addSpacing(10)
        self._poll_on_changes = QCheckBox("SysEx Poll on Changes")
        self._poll_on_changes.setToolTip(
            "Triggers a SysEx check-in when the instrument sends a Program Change or Bank Select message. "
            "Requires Monitor MIDI to be enabled.")
        vb.addWidget(self._poll_on_changes)
        vb.addWidget(_hint("Automatically updates the current performance display when a Program Change "
                           "or Bank Select is received from the instrument."))

        vb.addSpacing(10)
        self._pull_names = QCheckBox("Pull Names on Program Change")
        self._pull_names.setToolTip(
            "When you select a program/combi whose name isn't cached yet, fetch just that name over "
            "MIDI. Requires Monitor MIDI.")
        vb.addWidget(self._pull_names)
        vb.addWidget(_hint("Fills the performance name as you navigate, without a full Sync Names sweep. "
                           "Over the daemon/DIN path this can be slow and briefly flash the instrument display."))

        vb.addSpacing(10)
        self._proactive_poll = QCheckBox("Proactive SysEx Polling")
        self._proactive_poll.setToolTip(
            "Enabling this can cause the instrument to slow down during the check-in. Only enable if you "
            "require it.")
        vb.addWidget(self._proactive_poll)
        vb.addWidget(_hint("Periodically queries the instrument for the current performance name on a fixed "
                           "schedule, regardless of MIDI activity."))
        poll_row = QHBoxLayout()
        poll_row.addWidget(_hint("Poll interval"))
        self._poll_interval = QComboBox()
        for sec in _POLL_INTERVALS:
            self._poll_interval.addItem(f"{sec} seconds", sec)
        poll_row.addWidget(self._poll_interval)
        poll_row.addStretch(1)
        vb.addLayout(poll_row)
        self._poll_interval.setEnabled(False)
        self._proactive_poll.toggled.connect(self._poll_interval.setEnabled)

        vb.addSpacing(10)
        cc_row = QHBoxLayout()
        cc_row.addWidget(_hint("Value slider CC#"))
        self._slider_cc = QLineEdit()
        self._slider_cc.setMaxLength(3)
        self._slider_cc.setFixedWidth(60)
        self._slider_cc.setToolTip(
            "MIDI CC# the instrument VALUE slider transmits (default 18). The on-screen value slider "
            "follows this controller. Change it if your instrument assigns the value slider to a different CC.")
        cc_row.addWidget(self._slider_cc)
        cc_row.addStretch(1)
        vb.addLayout(cc_row)
        vb.addWidget(_hint("Keeps the on-screen value slider in sync with physical VALUE slider moves on "
                           "the instrument. Requires Monitor MIDI and SysEx transmit enabled on the instrument."))
        vb.addStretch()
        return w

    def _build_librarian_tab(self) -> QWidget:
        w = QWidget()
        vb = QVBoxLayout(w)
        vb.setSpacing(6)

        vb.addWidget(_section("Merge Window persistence"))
        self._merge_behavior = QComboBox()
        self._merge_behavior.addItem("Temporary Memory", "temporary_memory")
        self._merge_behavior.addItem("Local Storage", "local_storage")
        self._merge_behavior.setToolTip(
            "Controls whether the Librarian's Merge Window (a staging area for objects "
            "pulled from loaded PCG files, before they're placed into your Keyboard Library) "
            "survives an app restart or crash.")
        vb.addWidget(self._merge_behavior)
        vb.addWidget(_hint("Temporary Memory: cleared on restart, never touches disk. "
                           "Local Storage: survives a crash/reboot via a snapshot file."))

        vb.addSpacing(10)
        vb.addWidget(_section("Merge -> Local duplication"))
        self._preserve_dup_progs = QCheckBox("Programs: preserve duplication")
        self._preserve_dup_progs.setToolTip(
            "Checked: placing a Program from the Merge Window always copies it as-is into a "
            "fresh slot, even if byte-identical content already exists in your Keyboard Library. "
            "Unchecked (default): duplicates are detected and the existing copy is reused.")
        self._preserve_dup_combis = QCheckBox("Combis: preserve duplication")
        self._preserve_dup_combis.setToolTip(
            "Checked (default): placing a Combi from the Merge Window always copies it as-is "
            "into a fresh slot. Unchecked: duplicates are detected and the existing copy is "
            "reused — compared after its Program references are re-pointed, so a re-copied "
            "PCG still matches.")
        vb.addWidget(self._preserve_dup_progs)
        vb.addWidget(self._preserve_dup_combis)
        vb.addWidget(_hint("When placing from the Merge Window into Keyboard Library, choose per "
                           "object type whether duplicates are preserved (copied as-is) or "
                           "detected and reused."))

        vb.addSpacing(10)
        self._lib_sync_on_launch = QCheckBox("Full sync on launch")
        self._lib_sync_on_launch.setToolTip(
            "Pull every bank from the instrument as soon as the Librarian opens. Never writes to the instrument.")
        vb.addWidget(self._lib_sync_on_launch)
        vb.addWidget(_hint("Reads every program, combi, set list, drum kit, and wave sequence instead of "
                           "only the banks whose digest changed. Slow on a full library and is a pull "
                           "only: nothing is pushed to the instrument without user intervention."))

        vb.addSpacing(10)
        self._lib_force_destructive = QCheckBox("Force destructive write")
        self._lib_force_destructive.setToolTip(
            "2-Way Sync overwrites the instrument even where it changed since the last pull, without asking.")
        vb.addWidget(self._lib_force_destructive)
        warn = _hint("With this on, the keyboard library is treated as the source of truth and 2-Way "
                     "Sync overwrites the instrument to match it, without asking.")
        warn.setStyleSheet("color: #CC8888; font-size: 10px;")
        vb.addWidget(warn)

        vb.addStretch()
        return w

    def _build_sample_editor_tab(self) -> QWidget:
        from Core.sample_playback import list_playback_devices
        w = QWidget()
        vb = QVBoxLayout(w)
        vb.setSpacing(6)

        vb.addWidget(_section("Playback"))
        row = QHBoxLayout()
        lbl = QLabel("Output device")
        lbl.setStyleSheet(f"color: {_DIM}; font-size: 11px;")
        lbl.setFixedWidth(110)
        self._se_device = QComboBox()
        self._se_device.setMinimumWidth(220)
        self._se_device.addItem("(System Default)", "")
        for dev_id, name in list_playback_devices():
            self._se_device.addItem(name, dev_id)
        self._se_device.setToolTip("Which audio output the Sample Editor plays samples through.")
        row.addWidget(lbl)
        row.addWidget(self._se_device)
        row.addStretch()
        vb.addLayout(row)
        vb.addWidget(_hint("Takes effect the next time the Sample Editor window is opened."))

        vb.addSpacing(10)
        vb.addWidget(_section("Create Zone Preferences"))
        pos_lbl = QLabel("Position")
        pos_lbl.setStyleSheet(f"color: {_DIM}; font-size: 11px;")
        vb.addWidget(pos_lbl)
        self._se_pos_right = QRadioButton("Right")
        self._se_pos_right.setToolTip("A new zone is appended above the current top zone's Top Key without changing "
                                      "it. Falls back to shrinking the top zone only when it is already at 127.")
        self._se_pos_left = QRadioButton("Left")
        self._se_pos_left.setToolTip("A new zone takes the lower-key portion of the current last zone's range; "
                                     "the existing last zone keeps its Top Key.")
        # Separate groups: radios under one parent widget are otherwise ONE exclusive group.
        self._se_pos_group = QButtonGroup(w)
        self._se_pos_group.addButton(self._se_pos_right)
        self._se_pos_group.addButton(self._se_pos_left)
        pos_row = QHBoxLayout()
        pos_row.addWidget(self._se_pos_right)
        pos_row.addWidget(self._se_pos_left)
        pos_row.addStretch()
        vb.addLayout(pos_row)

        rng_row = QHBoxLayout()
        rng_lbl = QLabel("Zone Range")
        rng_lbl.setStyleSheet(f"color: {_DIM}; font-size: 11px;")
        rng_lbl.setFixedWidth(110)
        self._se_range = QLineEdit()
        self._se_range.setFixedWidth(50)
        self._se_range.setToolTip("How many keys (1-127) a new zone claims.")
        rng_row.addWidget(rng_lbl)
        rng_row.addWidget(self._se_range)
        rng_row.addWidget(_hint("keys  (1 to 127)"))
        rng_row.addStretch()
        vb.addLayout(rng_row)

        key_lbl = QLabel("Original Key Position")
        key_lbl.setStyleSheet(f"color: {_DIM}; font-size: 11px;")
        vb.addWidget(key_lbl)
        self._se_key_bottom = QRadioButton("Bottom")
        self._se_key_center = QRadioButton("Center")
        self._se_key_top = QRadioButton("Top")
        self._se_key_group = QButtonGroup(w)
        key_row = QHBoxLayout()
        for rb in (self._se_key_bottom, self._se_key_center, self._se_key_top):
            self._se_key_group.addButton(rb)
            key_row.addWidget(rb)
        key_row.addStretch()
        vb.addLayout(key_row)
        vb.addWidget(_hint("Where the Original Key (the root/tracking key, independent of the trigger range) "
                           "lands within a new zone's key range."))
        vb.addStretch()
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

        # Shown only while debug logging is ticked (C# SettingsWindow.OnDebugLoggingChanged).
        self._btn_button_injector = QPushButton("Button Injector…")
        self._btn_button_injector.setToolTip(
            "Send any front-panel button by name (sent as BTN <code>) and watch what happens on "
            "the mirrored screen - for mapping Nautilus's relabeled buttons.")
        self._btn_button_injector.setVisible(False)
        self._btn_button_injector.setEnabled(self._on_button_injector is not None)
        self._btn_button_injector.clicked.connect(lambda: self._on_button_injector())
        self._debug_logging.toggled.connect(self._btn_button_injector.setVisible)
        vb.addWidget(self._btn_button_injector, 0, Qt.AlignLeft)

        # Kronos-only (no Nautilus equivalent) — the caller passes None there.
        if self._on_test_mode is not None:
            vb.addSpacing(8)
            btn_test = QPushButton("Enter Instrument Test Mode…")
            btn_test.setToolTip("Puts the instrument into its built-in hardware test mode. "
                                "Unsaved changes are lost and the instrument must be restarted afterwards.")
            btn_test.clicked.connect(lambda: self._on_test_mode())
            vb.addWidget(btn_test, 0, Qt.AlignLeft)

        vb.addStretch()
        return w

    def _build_input_mapping_tab(self) -> QWidget:
        w = QWidget()
        vb = QVBoxLayout(w)
        vb.setSpacing(6)

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
        self._hide_data_inp.setChecked(s.hide_data_input)
        self._hide_value_inp.setChecked(s.hide_value_input)
        self._reverse_scroll.setChecked(s.reverse_scrolling)
        self._screenshot_dir.setText(s.screenshot_dir)
        self._vga_mirror.setChecked(s.vga_mirror_enabled)
        self._ss_spin.setValue(s.screensaver_timeout)

        # Connection
        self._host_edit.setText(s.kronos_host)
        self._saved = [dict(c) for c in s.saved_connections]
        self._rebind_saved(None)
        self._ftp_user.setText(s.ftp_username)
        self._ftp_pass.setText(s.ftp_password)
        self._ftp_port_spin.setValue(s.ftp_port)
        self._midi_monitor.setChecked(s.midi_monitor_enabled)
        self._poll_on_changes.setChecked(s.sysex_poll_on_changes)
        self._pull_names.setChecked(s.pull_names_on_change)
        self._proactive_poll.setChecked(s.proactive_sysex_polling)
        idx = self._poll_interval.findData(s.sysex_poll_interval_sec)
        self._poll_interval.setCurrentIndex(idx if idx >= 0 else _POLL_INTERVALS.index(60))
        self._poll_interval.setEnabled(s.proactive_sysex_polling)
        self._slider_cc.setText(str(s.value_slider_cc))

        # Librarian
        idx = self._merge_behavior.findData(s.merge_behavior)
        self._merge_behavior.setCurrentIndex(idx if idx >= 0 else 1)
        self._preserve_dup_progs.setChecked(s.merge_preserve_duplicate_programs)
        self._preserve_dup_combis.setChecked(s.merge_preserve_duplicate_combis)
        self._lib_sync_on_launch.setChecked(s.librarian_full_sync_on_launch)
        self._lib_force_destructive.setChecked(s.librarian_force_destructive_write)

        # Sample Editor
        i = self._se_device.findData(s.sample_editor_output_device_id)
        self._se_device.setCurrentIndex(i if i >= 0 else 0)
        (self._se_pos_left if s.sample_zone_create_position == "Left" else self._se_pos_right).setChecked(True)
        self._se_range.setText(str(s.sample_zone_create_range))
        {"Center": self._se_key_center, "Top": self._se_key_top}.get(
            s.sample_zone_original_key_position, self._se_key_bottom).setChecked(True)

        # Streaming
        self._stream_mode.setCurrentIndex(1 if s.pull_mode else 0)
        self._fps_slider.setValue(s.max_fps)
        self._fps_lbl.setText(f"{s.max_fps} fps")

        # View
        idx = self._default_window_size.findData(s.default_window_size)
        self._default_window_size.setCurrentIndex(idx if idx >= 0 else 0)
        self._zoom_level_slider.setValue(int(s.zoom_default_level * 10))
        self._zoom_level_lbl.setText(f"{s.zoom_default_level:.1f}×")
        self._zoom_win_slider.setValue(int(s.zoom_window_size * 10))
        self._zoom_win_lbl.setText(f"{s.zoom_window_size:.1f}×")

        # Image
        self._img_bri_slider.setValue(s.image_brightness)
        self._img_con_slider.setValue(s.image_contrast)
        self._img_gam_slider.setValue(int(round(s.image_gamma * 100)))
        self._img_sat_slider.setValue(s.image_saturation)
        self._img_shp_slider.setValue(s.image_sharpen)

        # Key bindings
        for action, edit in self._kb_edits.items():
            kb = s.get_keybind(action)
            edit.setText(kb.to_display_string() if kb.key != 0 else "")

        # Macros
        self._reload_macro_list()

        # Debug
        self._debug_logging.setChecked(s.debug_logging)
        self._reload_raw_list()

    def _save_sample_editor_fields(self, s) -> None:
        s.sample_editor_output_device_id = self._se_device.currentData() or ""
        s.sample_zone_create_position = "Left" if self._se_pos_left.isChecked() else "Right"
        try:
            s.sample_zone_create_range = max(1, min(127, int(self._se_range.text())))
        except ValueError:
            pass
        s.sample_zone_original_key_position = ("Center" if self._se_key_center.isChecked()
                                               else "Top" if self._se_key_top.isChecked() else "Bottom")

    def _save(self):
        s = self._settings

        # Commit any in-progress macro edit
        self._commit_macro_editor()

        # General
        s.prompt_before_quitting = self._quit_prompt.isChecked()
        s.hide_data_input        = self._hide_data_inp.isChecked()
        s.hide_value_input       = self._hide_value_inp.isChecked()
        s.reverse_scrolling      = self._reverse_scroll.isChecked()
        s.screenshot_dir         = self._screenshot_dir.text().strip()
        s.vga_mirror_enabled     = self._vga_mirror.isChecked()
        s.screensaver_timeout    = self._ss_spin.value()

        # Connection
        s.kronos_host  = self._host_edit.text().strip()
        s.saved_connections = [dict(c) for c in self._saved]
        # Not user-configurable: always the daemon defaults, so a value saved by an older
        # version cannot linger without any way to see or change it.
        s.stream_port  = AppSettings.stream_port
        s.ctrl_port    = AppSettings.ctrl_port
        s.ftp_username = self._ftp_user.text()
        s.ftp_password = self._ftp_pass.text()
        s.ftp_port     = self._ftp_port_spin.value()
        s.midi_monitor_enabled = self._midi_monitor.isChecked()
        s.sysex_poll_on_changes   = self._poll_on_changes.isChecked()
        s.pull_names_on_change    = self._pull_names.isChecked()
        s.proactive_sysex_polling = self._proactive_poll.isChecked()
        s.sysex_poll_interval_sec = self._poll_interval.currentData() or 60
        s.value_slider_cc         = _parse_value_slider_cc(self._slider_cc.text())

        # Librarian
        s.merge_behavior                  = self._merge_behavior.currentData()
        s.merge_preserve_duplicate_programs = self._preserve_dup_progs.isChecked()
        s.merge_preserve_duplicate_combis   = self._preserve_dup_combis.isChecked()
        s.librarian_full_sync_on_launch     = self._lib_sync_on_launch.isChecked()
        s.librarian_force_destructive_write = self._lib_force_destructive.isChecked()
        self._save_sample_editor_fields(s)

        # Streaming
        s.pull_mode              = self._stream_mode.currentIndex() == 1
        s.max_fps                = self._fps_slider.value()

        # View
        s.default_window_size = self._default_window_size.currentData()
        s.zoom_default_level = self._zoom_level_slider.value() / 10.0
        s.zoom_window_size   = self._zoom_win_slider.value()   / 10.0

        # Image
        s.image_brightness = self._img_bri_slider.value()
        s.image_contrast   = self._img_con_slider.value()
        s.image_gamma      = self._img_gam_slider.value() / 100.0
        s.image_saturation = self._img_sat_slider.value()
        s.image_sharpen    = self._img_shp_slider.value()

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
            Models.storage.reset_all()
            self._settings.__init__()  # reset in-place
            self._load()
            QMessageBox.information(self, "Reset", "Settings have been reset to defaults.")

    # ── Key binding helpers ────────────────────────────────────────────────────

    def _capture_key(self, action: str, edit: QLineEdit):
        edit.setText("Press a key…")
        edit.setFocus()

        def key_handler(event):
            from PySide6.QtCore import Qt as _Qt
            from Views.main_window import _mods_to_int
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
        import Core.ctrl_client as CC
        CC.play_macro(self._settings.kronos_host, self._settings.ctrl_port,
                      m.steps, m.step_delay_ms)

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
            from Views.main_window import _mods_to_int
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

        # Separate press/release hooks, matching C#'s SettingsWindow.xaml.cs
        # (OnMacroKeyDown/OnMacroKeyUp record a MacroStep{Code, Down} on each
        # real key event) — a held key or a chord (e.g. Shift held while
        # another key is pressed and released) needs its down and up as two
        # independently-timed steps to replay correctly. The previous version
        # only hooked keyPressEvent and synthesized an instant down+up pair
        # per keystroke, so a hold/chord could never be captured at all.
        def capture_press(event):
            from PySide6.QtCore import Qt as _Qt
            key = event.key()
            if key in (_Qt.Key_Escape,):
                self._stop_recording()
                return
            if event.isAutoRepeat():
                event.accept()
                return
            lc = key_map.to_linux(key)
            if lc:
                self._recording_steps.append(f"KEY {lc} 1")
                self._macro_steps_view.setPlainText("\n".join(self._recording_steps))
            event.accept()

        def capture_release(event):
            if event.isAutoRepeat():
                event.accept()
                return
            lc = key_map.to_linux(event.key())
            if lc:
                self._recording_steps.append(f"KEY {lc} 0")
                self._macro_steps_view.setPlainText("\n".join(self._recording_steps))
            event.accept()

        self._macro_steps_view.keyPressEvent = capture_press
        self._macro_steps_view.keyReleaseEvent = capture_release

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
        self._macro_steps_view.keyReleaseEvent = QTextEdit.keyReleaseEvent.__get__(
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
            from Views.main_window import _mods_to_int
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
            Models.storage.export_settings(self._settings, path)
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
        new_s = Models.storage.import_settings(path)
        # Replace fields in the shared settings object
        for field_name in self._settings.__dataclass_fields__:
            setattr(self._settings, field_name, getattr(new_s, field_name))
        self._load()
        QMessageBox.information(self, "Import", "Settings imported successfully.")

    def _save_to_settings_no_close(self):
        """Flush UI state into self._settings without calling accept()."""
        s = self._settings
        s.prompt_before_quitting = self._quit_prompt.isChecked()
        s.hide_data_input        = self._hide_data_inp.isChecked()
        s.hide_value_input       = self._hide_value_inp.isChecked()
        s.reverse_scrolling      = self._reverse_scroll.isChecked()
        s.screenshot_dir         = self._screenshot_dir.text().strip()
        s.vga_mirror_enabled     = self._vga_mirror.isChecked()
        s.screensaver_timeout    = self._ss_spin.value()
        s.kronos_host            = self._host_edit.text().strip()
        s.saved_connections      = [dict(c) for c in self._saved]
        s.stream_port            = AppSettings.stream_port
        s.ctrl_port              = AppSettings.ctrl_port
        s.ftp_username           = self._ftp_user.text()
        s.ftp_password           = self._ftp_pass.text()
        s.ftp_port               = self._ftp_port_spin.value()
        s.midi_monitor_enabled   = self._midi_monitor.isChecked()
        s.sysex_poll_on_changes   = self._poll_on_changes.isChecked()
        s.pull_names_on_change    = self._pull_names.isChecked()
        s.proactive_sysex_polling = self._proactive_poll.isChecked()
        s.sysex_poll_interval_sec = self._poll_interval.currentData() or 60
        s.value_slider_cc         = _parse_value_slider_cc(self._slider_cc.text())
        s.merge_behavior         = self._merge_behavior.currentData()
        s.merge_preserve_duplicate_programs = self._preserve_dup_progs.isChecked()
        s.merge_preserve_duplicate_combis   = self._preserve_dup_combis.isChecked()
        s.librarian_full_sync_on_launch     = self._lib_sync_on_launch.isChecked()
        s.librarian_force_destructive_write = self._lib_force_destructive.isChecked()
        self._save_sample_editor_fields(s)
        s.pull_mode              = self._stream_mode.currentIndex() == 1
        s.max_fps                = self._fps_slider.value()
        s.default_window_size    = self._default_window_size.currentData()
        s.zoom_default_level     = self._zoom_level_slider.value() / 10.0
        s.zoom_window_size       = self._zoom_win_slider.value()   / 10.0
        s.image_brightness       = self._img_bri_slider.value()
        s.image_contrast         = self._img_con_slider.value()
        s.image_gamma            = self._img_gam_slider.value() / 100.0
        s.image_saturation       = self._img_sat_slider.value()
        s.image_sharpen          = self._img_shp_slider.value()
        s.debug_logging          = self._debug_logging.isChecked()
