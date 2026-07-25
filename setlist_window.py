"""
Set List viewer — port of Views/SetListWindow.xaml + .xaml.cs.

Layout matches the XAML Grid exactly: top bar (selector + Load/Refresh +
inline status text), bold Set List name, a slot table with a dedicated color
swatch column, and a hint line at the bottom. No live sync with the Kronos's
current set list position — purely on-demand, matching the Windows original.
"""
from __future__ import annotations

import threading
from typing import Dict, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

import storage
from setlist_data import SetListData, MAX_COUNT
from sysex_service import SysExService
import theme as T

_WIN_BG = T.BG
_WIN_FG = T.TEXT

# 16-color slot palette — authentic Kronos Set List slot colors, ported from
# Tools/SetListColors.cs (same role as the C# DataGridTemplateColumn color
# swatch). Order: Default, Charcoal, Brick, Burgundy, Ivy, Olive, Gold, Cacao,
# Indigo, Navy, Rose, Lavender, Azure, Denim, Silver, Slate.
_SLOT_COLORS = [
    "#4D4D4D", "#2F2F2F", "#B23F3F", "#691B1B", "#91A730", "#374520",
    "#AA842A", "#7F4236", "#5360A5", "#1A2B88", "#AB81A2", "#9267BA",
    "#88A4C5", "#6A7F96", "#808080", "#626262",
]

_HINT_TEXT = ("Dumps are read from the Kronos once and cached. Requires MIDI "
              "monitoring on and SysEx transmit enabled on the Kronos. Empty "
              "slots are hidden.")


class SetListWindow(QDialog):
    _load_done = Signal(int, object, str)   # number, SetListData|None, status

    def __init__(self, host: str, service: Optional[SysExService], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Set List Viewer")
        self.resize(900, 620)
        self.setStyleSheet(f"QDialog {{ background-color: {_WIN_BG}; color: {_WIN_FG}; }}")
        self._host = host
        self._service = service
        self._cache: Dict[int, SetListData] = storage.load_setlists(host)
        self._current: Optional[SetListData] = None

        self._build_ui()
        self._load_done.connect(self._on_load_done)
        self._reload_cache()

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)

        # Top bar: selector + actions + inline status (all one row, matches XAML)
        top = QHBoxLayout()
        lbl = QLabel("Set List")
        lbl.setStyleSheet("color: #999999;")
        top.addWidget(lbl)
        self._combo = QComboBox()
        self._combo.setFixedWidth(180)
        self._combo.setStyleSheet("border: 1px solid #555555;")
        top.addWidget(self._combo)
        self._btn_load = QPushButton("Load")
        self._btn_load.setFixedWidth(80)
        self._btn_load.setToolTip("Dump the selected Set List from the Kronos (or show it from cache).")
        self._btn_load.clicked.connect(lambda: self._load(force=False))
        top.addWidget(self._btn_load)
        self._btn_refresh = QPushButton("Refresh")
        self._btn_refresh.setFixedWidth(80)
        self._btn_refresh.setToolTip("Re-dump the selected Set List, replacing the cached copy.")
        self._btn_refresh.clicked.connect(lambda: self._load(force=True))
        top.addWidget(self._btn_refresh)
        self._status_label = QLabel("Not loaded — press Load")
        self._status_label.setStyleSheet("color: #88AADD;")
        top.addWidget(self._status_label)
        top.addStretch(1)
        root.addLayout(top)

        # Set List name
        self._name_label = QLabel("")
        self._name_label.setStyleSheet("color: #E0E0E0; font-size: 16px; font-weight: bold;")
        self._name_label.setContentsMargins(0, 8, 0, 8)
        root.addWidget(self._name_label)

        # Slot grid: #, color swatch, Name, Type, Performance, Notes
        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(["#", "", "Name", "Type", "Performance", "Notes"])
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setSelectionMode(QTableWidget.SingleSelection)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setColumnWidth(0, 42)
        self._table.setColumnWidth(1, 26)
        self._table.setColumnWidth(2, 200)
        self._table.setColumnWidth(3, 60)
        self._table.setColumnWidth(4, 120)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setDefaultSectionSize(24)
        self._table.setStyleSheet(
            "QTableWidget { background-color: #141414; color: #D0D0D0; "
            "border: 1px solid #333333; gridline-color: #2A2A2A; }"
            "QTableWidget::item { padding: 2px; }"
            "QHeaderView::section { background-color: #262626; color: #BBBBBB; "
            "padding: 6px 3px; border: none; border-right: 1px solid #333333; "
            "border-bottom: 1px solid #333333; font-weight: 600; }"
            "QTableWidget::item:alternate { background-color: #1B1B1B; }")
        root.addWidget(self._table, stretch=1)

        # Hint line at the bottom
        hint = QLabel(_HINT_TEXT)
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #777777; font-size: 11px;")
        hint.setContentsMargins(0, 8, 0, 0)
        root.addWidget(hint)

    def _reload_cache(self):
        """Re-read the on-disk cache (e.g. after a Sync All elsewhere) and repaint."""
        self._cache = storage.load_setlists(self._host)
        cur_idx = self._combo.currentIndex()
        self._combo.blockSignals(True)
        self._combo.clear()
        for n in range(MAX_COUNT):
            data = self._cache.get(n)
            label = f"{n:03d}: {data.name}" if data and data.name.strip() else f"Set List {n:03d}"
            self._combo.addItem(label, n)
        self._combo.blockSignals(False)
        if cur_idx >= 0:
            self._combo.setCurrentIndex(cur_idx)
        self._combo.currentIndexChanged.connect(self._on_selection_changed)
        if self._combo.count() > 0:
            self._on_selection_changed(self._combo.currentIndex())

    def _on_selection_changed(self, _idx: int):
        number = self._combo.currentData()
        if number is None:
            return
        data = self._cache.get(number)
        if data is not None:
            self._render(data)
            self._status_label.setText("Cached")
        else:
            self._current = None
            self._name_label.setText("")
            self._table.setRowCount(0)
            self._status_label.setText("Not loaded — press Load")

    # ── Load / Refresh ───────────────────────────────────────────────────────

    def _load(self, force: bool):
        number = self._combo.currentData()
        if number is None:
            return
        if not force and number in self._cache:
            self._render(self._cache[number])
            self._status_label.setText("Cached")
            return
        if self._service is None or not self._service.can_dump:
            self._status_label.setText("MIDI monitoring is off — enable it in Settings first")
            return

        self._status_label.setText("Loading…")
        self._btn_load.setEnabled(False)
        self._btn_refresh.setEnabled(False)
        threading.Thread(target=self._load_worker, args=(number,), daemon=True,
                          name="SetListLoad").start()

    def _load_worker(self, number: int):
        data = self._service.dump_set_list(number)
        if data is not None:
            self._cache[number] = data
            storage.save_setlists(self._host, self._cache)
            self._load_done.emit(number, data, "Loaded")
        else:
            self._load_done.emit(number, None, "No response — check MIDI Enable Exclusive on the Kronos")

    def _on_load_done(self, number: int, data: Optional[SetListData], status: str):
        self._btn_load.setEnabled(True)
        self._btn_refresh.setEnabled(True)
        if self._combo.currentData() != number:
            return
        if data is not None:
            label = f"{number:03d}: {data.name}" if data.name.strip() else f"Set List {number:03d}"
            self._combo.setItemText(self._combo.currentIndex(), label)
            self._render(data)
        self._status_label.setText(status)

    # ── Rendering ────────────────────────────────────────────────────────────

    def _render(self, data: SetListData):
        self._current = data
        self._name_label.setText(data.name or f"Set List {data.number:03d}")
        self._table.setRowCount(0)
        for slot in data.slots:
            if slot.is_empty:
                continue
            row = self._table.rowCount()
            self._table.insertRow(row)
            self._table.setItem(row, 0, QTableWidgetItem(str(slot.number)))

            swatch = QWidget()
            swatch_layout = QHBoxLayout(swatch)
            swatch_layout.setContentsMargins(0, 0, 0, 0)
            swatch_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            box = QLabel()
            box.setFixedSize(14, 14)
            color = _SLOT_COLORS[slot.color % len(_SLOT_COLORS)]
            box.setStyleSheet(f"background-color: {color}; border: 1px solid #000000; border-radius: 2px;")
            swatch_layout.addWidget(box)
            self._table.setCellWidget(row, 1, swatch)

            self._table.setItem(row, 2, QTableWidgetItem(slot.name))
            self._table.setItem(row, 3, QTableWidgetItem(slot.type_label))
            self._table.setItem(row, 4, QTableWidgetItem(slot.performance_label))
            notes = slot.comments.replace("\n", " ").strip()
            self._table.setItem(row, 5, QTableWidgetItem(notes))
