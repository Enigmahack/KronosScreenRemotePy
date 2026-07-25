"""
Input Tester window — port of Views/InputTesterWindow.xaml(.cs).

Dev/power-user tool: a full printable-ASCII + control-char grid where each
row can be sent to the Kronos via char_map's KEY sequences, with the actual
observed result on real hardware recorded next to it. Also supports capturing
an arbitrary host key and binding it to a raw Kronos keycode (persisted via
AppSettings.raw_key_maps — the same store Settings -> Debug already uses, so
no separate mapping format is introduced). Observed results are saved/loaded
to input_test_results.json in this project's existing data-file directory
(storage.data_dir()).
"""
from __future__ import annotations

import json
import logging
from typing import Callable, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QKeyEvent
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

import char_map
import storage
import theme as T
from app_settings import AppSettings, RawKeyMap
from models import Keybind

_RESULTS_FILE = "input_test_results.json"

_OBSERVED_OPTIONS = [
    "N/A",
    "Space", "Tab", "Enter", "Backspace", "Delete", "Escape",
    "Up", "Down", "Left", "Right", "Home", "End", "Page Up", "Page Down",
    "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12",
]


def _mods_to_int(mods) -> int:
    result = 0
    if mods & Qt.ControlModifier:
        result |= Qt.ControlModifier.value
    if mods & Qt.AltModifier:
        result |= Qt.AltModifier.value
    if mods & Qt.ShiftModifier:
        result |= Qt.ShiftModifier.value
    if mods & Qt.MetaModifier:
        result |= Qt.MetaModifier.value
    return result


_MODIFIER_KEYS = (
    Qt.Key_Shift, Qt.Key_Control, Qt.Key_Alt, Qt.Key_Meta, Qt.Key_AltGr,
    Qt.Key_CapsLock, Qt.Key_NumLock, Qt.Key_ScrollLock,
)


class _TestEntry:
    __slots__ = ("ch", "display", "code_hex", "send_seq", "cmds", "observed")

    def __init__(self, ch: str, display: str):
        self.ch = ch
        self.display = display
        self.code_hex = f"0x{ord(ch):02X}"
        self.cmds = char_map.get_commands(ch)
        self.send_seq = char_map.get_description(ch)
        self.observed = ""

    @property
    def has_mapping(self) -> bool:
        return self.cmds is not None


class _KeyCaptureEdit(QLineEdit):
    """Click then press a host key; records it on the owning window."""

    def __init__(self, owner: "InputTesterWindow"):
        super().__init__()
        self._owner = owner
        self.setReadOnly(True)
        self.setText("[click, then press key]")
        self.setStyleSheet(
            f"background-color: {T.INSET}; color: {T.TEXT}; "
            f"border: 1px solid {T.BORDER_STRONG}; padding: 3px;")

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.setText("Press a key…")
        self._owner.captured_key = 0
        self._owner.captured_mods = 0

    def keyPressEvent(self, event: QKeyEvent):
        key = event.key()
        if key in _MODIFIER_KEYS or key == 0:
            return
        mods = _mods_to_int(event.modifiers())
        self._owner.captured_key = key
        self._owner.captured_mods = mods
        self.setText(Keybind(key, mods).to_display_string())


class InputTesterWindow(QDialog):
    def __init__(self, settings: AppSettings, send_cmd: Callable[[str], None], parent=None):
        super().__init__(parent)
        self._settings = settings
        self._send_cmd = send_cmd
        self.captured_key = 0
        self.captured_mods = 0

        self.setWindowTitle("Input Tester — Kronos Keyboard Mapper")
        self.resize(900, 620)
        self.setMinimumSize(640, 400)
        self.setStyleSheet(f"QDialog {{ background-color: {T.BG}; color: {T.TEXT}; }}")

        self._entries: List[_TestEntry] = self._build_entries()

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._grid = self._build_entry_grid()
        root.addWidget(self._grid, stretch=1)

        bottom = QWidget()
        bottom.setStyleSheet(f"background-color: {T.PANEL}; border-top: 1px solid {T.BORDER};")
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(10, 8, 10, 8)
        bl.setSpacing(7)

        bl.addLayout(self._build_send_row())
        bl.addLayout(self._build_status_row())
        bl.addWidget(self._build_mappings_panel())

        root.addWidget(bottom)

        self._grid.itemSelectionChanged.connect(self._on_selection_changed)
        if self._entries:
            self._grid.selectRow(0)
        self._update_count()

    # ── Entry grid ───────────────────────────────────────────────────────────

    def _build_entries(self) -> List[_TestEntry]:
        entries = [
            _TestEntry('\b', "Backspace"),
            _TestEntry('\t', "Tab"),
            _TestEntry('\n', "Enter"),
        ]
        c = 0x20
        while c <= 0x7E:
            ch = chr(c)
            entries.append(_TestEntry(ch, "Space" if ch == " " else ch))
            c += 1
        return entries

    def _build_entry_grid(self) -> QTableWidget:
        grid = QTableWidget(len(self._entries), 5)
        grid.setHorizontalHeaderLabels(["", "Char", "Code", "Send Sequence", "Observed on Kronos"])
        grid.verticalHeader().setVisible(False)
        grid.setEditTriggers(QAbstractItemView.NoEditTriggers)
        grid.setSelectionBehavior(QAbstractItemView.SelectRows)
        grid.setSelectionMode(QAbstractItemView.SingleSelection)
        grid.horizontalHeader().setStretchLastSection(True)
        grid.setColumnWidth(0, 22)
        grid.setColumnWidth(1, 80)
        grid.setColumnWidth(2, 55)
        grid.setStyleSheet(
            f"QTableWidget {{ background-color: {T.INSET}; color: {T.TEXT}; border: none; "
            f"font-family: {T.FONT_MONO}; font-size: {T.FS_BODY}px; }}"
            f"QTableWidget::item:selected {{ background-color: {T.ACCENT_DEEP}; color: #FFFFFF; }}"
            f"QHeaderView::section {{ background-color: {T.PANEL_ALT}; color: {T.TEXT}; "
            f"border: 1px solid {T.BORDER_STRONG}; padding: 3px; }}")

        for row, e in enumerate(self._entries):
            dot = QTableWidgetItem("●")
            dot.setTextAlignment(Qt.AlignCenter)
            dot.setForeground(self._status_color(e))
            grid.setItem(row, 0, dot)
            grid.setItem(row, 1, QTableWidgetItem(e.display))
            grid.setItem(row, 2, QTableWidgetItem(e.code_hex))
            grid.setItem(row, 3, QTableWidgetItem(e.send_seq))
            grid.setItem(row, 4, QTableWidgetItem(e.observed))

        return grid

    def _status_color(self, e: _TestEntry):
        if not e.has_mapping:
            return QColor(T.WARN)
        return QColor(T.OK) if e.observed else QColor(T.TEXT_IDLE)

    def _refresh_row(self, row: int):
        e = self._entries[row]
        self._grid.item(row, 0).setForeground(self._status_color(e))
        self._grid.item(row, 4).setText(e.observed)

    def _selected_entry(self) -> Optional[_TestEntry]:
        row = self._grid.currentRow()
        if 0 <= row < len(self._entries):
            return self._entries[row]
        return None

    def _on_selection_changed(self):
        entry = self._selected_entry()
        self._send_btn.setEnabled(bool(entry and entry.has_mapping))
        self._observed_combo.setEditText(entry.observed if entry else "")

    # ── Send row ─────────────────────────────────────────────────────────────

    def _build_send_row(self) -> QHBoxLayout:
        row = QHBoxLayout()

        row.addWidget(QLabel("Observed:"))
        self._observed_combo = QComboBox()
        self._observed_combo.setEditable(True)
        self._observed_combo.addItems(_OBSERVED_OPTIONS)
        self._observed_combo.setCurrentText("")
        self._observed_combo.setFixedWidth(180)
        self._observed_combo.activated.connect(lambda _i: self._commit_observed())
        self._observed_combo.lineEdit().editingFinished.connect(self._commit_observed)
        row.addWidget(self._observed_combo)

        self._send_btn = QPushButton("Send to Kronos  (F5)")
        self._send_btn.setEnabled(False)
        self._send_btn.clicked.connect(self._send_selected)
        row.addWidget(self._send_btn)

        row.addSpacing(16)
        row.addWidget(QLabel("Raw KEY:"))
        self._raw_code_edit = QLineEdit("30")
        self._raw_code_edit.setFixedWidth(50)
        row.addWidget(self._raw_code_edit)
        self._raw_shift_chk = QCheckBox("Shift")
        row.addWidget(self._raw_shift_chk)
        send_raw_btn = QPushButton("Send Raw")
        send_raw_btn.clicked.connect(self._send_raw)
        row.addWidget(send_raw_btn)

        row.addSpacing(10)
        row.addWidget(QLabel("→ host key:"))
        self._host_key_edit = _KeyCaptureEdit(self)
        self._host_key_edit.setFixedWidth(120)
        row.addWidget(self._host_key_edit)
        add_mapping_btn = QPushButton("Add Mapping")
        add_mapping_btn.clicked.connect(self._add_mapping)
        row.addWidget(add_mapping_btn)

        row.addStretch(1)
        return row

    def _commit_observed(self):
        entry = self._selected_entry()
        if entry is None:
            return
        entry.observed = self._observed_combo.currentText()
        self._refresh_row(self._grid.currentRow())
        self._update_count()

    def _send_selected(self):
        entry = self._selected_entry()
        if entry is None or entry.cmds is None:
            return
        for cmd in entry.cmds:
            self._send_cmd(cmd)

    def _send_raw(self):
        try:
            code = int(self._raw_code_edit.text().strip())
        except ValueError:
            return
        if not (1 <= code <= 767):
            return
        shift = self._raw_shift_chk.isChecked()
        if shift:
            self._send_cmd("KEY 42 1")
        self._send_cmd(f"KEY {code} 1")
        self._send_cmd(f"KEY {code} 0")
        if shift:
            self._send_cmd("KEY 42 0")

    # ── Host key capture / raw mappings ──────────────────────────────────────

    def _build_status_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self._status_label = QLabel("")
        self._status_label.setStyleSheet(f"color: {T.TEXT_DIM}; font-size: {T.FS_SMALL}px;")
        row.addWidget(self._status_label, stretch=1)
        load_btn = QPushButton("Load Results")
        load_btn.clicked.connect(self._load_results)
        row.addWidget(load_btn)
        save_btn = QPushButton("Save Results")
        save_btn.clicked.connect(self._save_results)
        row.addWidget(save_btn)
        return row

    def _build_mappings_panel(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.setSpacing(3)
        label = QLabel("Custom Key Mappings  (host key → raw keycode sent to Kronos)")
        label.setStyleSheet(f"color: {T.TEXT_IDLE}; font-size: {T.FS_SMALL}px;")
        lay.addWidget(label)

        self._mappings_table = QTableWidget(0, 4)
        self._mappings_table.setHorizontalHeaderLabels(["Host Key", "Sends", "Label", ""])
        self._mappings_table.verticalHeader().setVisible(False)
        self._mappings_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._mappings_table.setSelectionMode(QAbstractItemView.NoSelection)
        self._mappings_table.setMaximumHeight(120)
        self._mappings_table.horizontalHeader().setStretchLastSection(False)
        self._mappings_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self._mappings_table.setColumnWidth(0, 130)
        self._mappings_table.setColumnWidth(1, 120)
        self._mappings_table.setColumnWidth(3, 28)
        self._mappings_table.setStyleSheet(
            f"QTableWidget {{ background-color: {T.PANEL}; color: {T.TEXT}; "
            f"font-family: {T.FONT_MONO}; font-size: {T.FS_SMALL}px; }}"
            f"QHeaderView::section {{ background-color: {T.PANEL_ALT}; color: {T.TEXT}; "
            f"border: 1px solid {T.BORDER_STRONG}; padding: 3px; }}")
        lay.addWidget(self._mappings_table)

        self._refresh_mappings_table()
        return panel

    def _refresh_mappings_table(self):
        maps = self._settings.raw_key_maps
        self._mappings_table.setRowCount(len(maps))
        for row, rm in enumerate(maps):
            self._mappings_table.setItem(row, 0, QTableWidgetItem(rm.host_key_display))
            self._mappings_table.setItem(row, 1, QTableWidgetItem(rm.raw_display))
            self._mappings_table.setItem(row, 2, QTableWidgetItem(rm.label))
            del_btn = QPushButton("×")
            del_btn.setFixedWidth(24)
            del_btn.setStyleSheet(f"color: {T.ERROR_TEXT}; background: transparent; border: none;")
            del_btn.clicked.connect(lambda _c=False, r=rm: self._delete_mapping(r))
            self._mappings_table.setCellWidget(row, 3, del_btn)

    def _add_mapping(self):
        if self.captured_key == 0:
            self._status_label.setText("Click the host-key field, then press a key.")
            return
        try:
            code = int(self._raw_code_edit.text().strip())
        except ValueError:
            self._status_label.setText("Enter a valid raw KEY code.")
            return
        if not (1 <= code <= 767):
            self._status_label.setText("Enter a valid raw KEY code.")
            return

        entry = self._selected_entry()
        label = entry.observed if entry else ""

        maps = self._settings.raw_key_maps
        for i, rm in enumerate(maps):
            if rm.host_key == self.captured_key and rm.host_mods == self.captured_mods:
                maps[i] = RawKeyMap(label=label, host_key=self.captured_key,
                                     host_mods=self.captured_mods, raw_code=code,
                                     send_shift=self._raw_shift_chk.isChecked())
                break
        else:
            maps.append(RawKeyMap(label=label, host_key=self.captured_key,
                                   host_mods=self.captured_mods, raw_code=code,
                                   send_shift=self._raw_shift_chk.isChecked()))
        storage.save_settings(self._settings)
        self._refresh_mappings_table()

        key_str = Keybind(self.captured_key, self.captured_mods).to_display_string()
        self._status_label.setText(f"Mapped {key_str} → KEY {code}")
        self.captured_key = 0
        self.captured_mods = 0
        self._host_key_edit.setText("[click, then press key]")

    def _delete_mapping(self, rm: RawKeyMap):
        maps = self._settings.raw_key_maps
        if rm in maps:
            maps.remove(rm)
            storage.save_settings(self._settings)
            self._refresh_mappings_table()

    # ── Save / Load ───────────────────────────────────────────────────────────

    def _results_path(self):
        return storage.data_dir() / _RESULTS_FILE

    def _save_results(self):
        obj = {str(ord(e.ch)): e.observed for e in self._entries if e.observed}
        try:
            self._results_path().write_text(json.dumps(obj, indent=2), encoding="utf-8")
            self._status_label.setText(f"Saved to {self._results_path()}")
        except Exception as ex:
            logging.warning("[input tester] save failed: %s", ex)
            self._status_label.setText(f"Save failed: {ex}")

    def _load_results(self):
        p = self._results_path()
        if not p.exists():
            self._status_label.setText("No results file found.")
            return
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except Exception as ex:
            logging.warning("[input tester] load failed: %s", ex)
            self._status_label.setText(f"Could not parse results file: {ex}")
            return

        loaded = 0
        by_code = {int(k): v for k, v in obj.items() if str(k).lstrip("-").isdigit()}
        for row, e in enumerate(self._entries):
            if ord(e.ch) in by_code:
                e.observed = by_code[ord(e.ch)]
                self._refresh_row(row)
                loaded += 1

        entry = self._selected_entry()
        if entry is not None:
            self._observed_combo.setEditText(entry.observed)

        self._update_count()
        self._status_label.setText(f"Loaded {loaded} result(s) from {p}")

    def _update_count(self):
        tested = sum(1 for e in self._entries if e.observed)
        self._status_label.setText(f"{tested} / {len(self._entries)} tested")

    # ── Keyboard shortcuts ────────────────────────────────────────────────────

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key_F5 and not event.isAutoRepeat():
            self._send_selected()
            event.accept()
            return
        super().keyPressEvent(event)
