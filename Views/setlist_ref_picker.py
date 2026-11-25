"""Pick what a Set List slot points at: a Combi or Program from the Keyboard Library, or a Song number."""
from __future__ import annotations

from typing import Optional, Sequence, Tuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QSpinBox, QVBoxLayout)

TYPE_NAMES = {0: "Combi", 1: "Program", 2: "Song"}
SONG_COUNT = 200
Ref = Tuple[int, int, int]                 # (type 0/1/2, func-33 bank, index)


class RefPickerDialog(QDialog):
    """`entries`: (type, func33_bank, index, bank_label, name) of the Library's Combis and Programs. `result_ref` after OK."""

    def __init__(self, entries: Sequence[tuple], current: Optional[Ref], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Choose Set List slot target")
        self.resize(420, 480)
        self._entries = list(entries)
        self.result_ref: Optional[Ref] = None
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self._type = QComboBox()
        for t, n in TYPE_NAMES.items():
            self._type.addItem(n, t)
        self._find = QLineEdit()
        self._find.setPlaceholderText("Search name or bank")
        top.addWidget(self._type)
        top.addWidget(self._find, 1)
        lay.addLayout(top)
        self._list = QListWidget()
        lay.addWidget(self._list, 1)
        row = QHBoxLayout()
        self._song_lbl = QLabel("Song number")
        self._song = QSpinBox()
        self._song.setRange(0, SONG_COUNT - 1)
        row.addWidget(self._song_lbl)
        row.addWidget(self._song)
        row.addStretch(1)
        lay.addLayout(row)
        self._buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self._buttons.accepted.connect(self._accept)
        self._buttons.rejected.connect(self.reject)
        lay.addWidget(self._buttons)
        self._type.currentIndexChanged.connect(self._refill)
        self._find.textChanged.connect(self._refill)
        self._list.itemDoubleClicked.connect(lambda *_: self._accept())
        self._current = current
        if current is not None:
            self._type.setCurrentIndex(max(0, self._type.findData(current[0])))
            if current[0] == 2:
                self._song.setValue(min(current[2], SONG_COUNT - 1))
        self._refill()

    def _refill(self, *_) -> None:
        t = self._type.currentData()
        song = t == 2
        self._list.setVisible(not song)
        self._find.setVisible(not song)
        self._song.setVisible(song)
        self._song_lbl.setVisible(song)
        self._list.clear()
        if song:
            return
        needle = self._find.text().strip().lower()
        for et, bank, idx, bank_label, name in sorted(self._entries, key=lambda e: (e[1], e[2])):
            text = f"{bank_label}:{idx:03d}  {name}"
            if et != t or needle and needle not in text.lower():
                continue
            it = QListWidgetItem(text)
            it.setData(Qt.ItemDataRole.UserRole, (et, bank, idx))
            self._list.addItem(it)
            if self._current is not None and self._current == (et, bank, idx):
                self._list.setCurrentItem(it)

    def _accept(self) -> None:
        if self._type.currentData() == 2:
            self.result_ref = (2, 0, self._song.value())
        else:
            it = self._list.currentItem()
            if it is None:
                return
            self.result_ref = tuple(it.data(Qt.ItemDataRole.UserRole))
        self.accept()
