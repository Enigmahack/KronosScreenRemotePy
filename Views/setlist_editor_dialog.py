"""Set List editor: the 128 slots of one Set List with their name, colour, pitch offset (transpose) and comments, the
order of the slots, and the Set List's own EQ. Works on a copy of the body; `new_body` holds the result after OK."""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QGridLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget, QPlainTextEdit, QPushButton, QSpinBox,
                               QVBoxLayout)

from Objects import object_body as ob
from Objects import setlist_edit as se
from Utils.setlist_colors import ALL_COLORS, get_by_index_or_default


def _swatch(hex_color: str) -> QIcon:
    pix = QPixmap(28, 12)
    pix.fill(QColor(hex_color))
    return QIcon(pix)


class SetListEditorDialog(QDialog):
    def __init__(self, title: str, body: bytes, label_for: Callable[[bytes, int], str], parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(820, 620)
        self._body = body
        self._label_for = label_for
        self._loading = False
        self.new_body: Optional[bytes] = None

        root = QVBoxLayout(self)
        main = QHBoxLayout()
        root.addLayout(main, 1)

        left = QVBoxLayout()
        self._list = QListWidget()
        self._list.setMinimumWidth(300)
        left.addWidget(self._list, 1)
        order = QHBoxLayout()
        self._up = QPushButton("Move Up")
        self._down = QPushButton("Move Down")
        self._up.clicked.connect(lambda: self._shift(-1))
        self._down.clicked.connect(lambda: self._shift(1))
        order.addWidget(self._up)
        order.addWidget(self._down)
        left.addLayout(order)
        main.addLayout(left, 1)

        right = QVBoxLayout()
        self._slot_box = QGroupBox("Slot")
        g = QGridLayout(self._slot_box)
        self._name = QLineEdit()
        self._name.setMaxLength(ob.NAME_LEN)
        self._color = QComboBox()
        for c in ALL_COLORS:
            self._color.addItem(_swatch(c.hex), c.display_name)
        self._transpose = QSpinBox()
        self._transpose.setRange(*se.TRANSPOSE_RANGE)
        self._transpose.setSuffix(" semitones")
        self._comments = QPlainTextEdit()
        g.addWidget(QLabel("Name"), 0, 0)
        g.addWidget(self._name, 0, 1)
        g.addWidget(QLabel("Color"), 1, 0)
        g.addWidget(self._color, 1, 1)
        g.addWidget(QLabel("Pitch offset"), 2, 0)
        g.addWidget(self._transpose, 2, 1)
        g.addWidget(QLabel("Comments"), 3, 0, Qt.AlignmentFlag.AlignTop)
        g.addWidget(self._comments, 3, 1)
        right.addWidget(self._slot_box, 1)

        self._eq_box = QGroupBox("Set List EQ")
        e = QGridLayout(self._eq_box)
        self._eq_bypass = QCheckBox("Bypass")
        self._eq_trim = QSpinBox()
        self._eq_trim.setRange(0, 99)
        e.addWidget(self._eq_bypass, 0, 0)
        e.addWidget(QLabel("Input trim"), 0, 1)
        e.addWidget(self._eq_trim, 0, 2)
        self._bands = []
        for i in range(se.BAND_COUNT):
            sp = QDoubleSpinBox()
            sp.setRange(-18.0, 18.0)
            sp.setSingleStep(0.5)
            sp.setDecimals(1)
            sp.setSuffix(" dB")
            self._bands.append(sp)
            row, col = 1 + i // 3, (i % 3) * 2
            e.addWidget(QLabel(f"Band {i + 1}"), row, col)
            e.addWidget(sp, row, col + 1)
        right.addWidget(self._eq_box)
        main.addLayout(right, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._load_eq()
        self._fill_list()
        self._list.currentRowChanged.connect(self._load_slot)
        self._name.textEdited.connect(self._commit_name)
        self._color.currentIndexChanged.connect(self._commit_color)
        self._transpose.valueChanged.connect(self._commit_transpose)
        self._comments.textChanged.connect(self._commit_comments)
        self._eq_bypass.toggled.connect(self._commit_eq)
        self._eq_trim.valueChanged.connect(self._commit_eq)
        for sp in self._bands:
            sp.valueChanged.connect(self._commit_eq)
        self._list.setCurrentRow(0)

    # -- list / selection --
    def _row_text(self, i: int) -> str:
        s = ob.parse_setlist_slot(self._body, i)
        return f"{i:03d}  (empty)" if s is None or s.is_empty else f"{i:03d}  {s.name.strip()}  -  {self._label_for(self._body, i)}"

    def _row_icon(self, i: int) -> QIcon:
        s = ob.parse_setlist_slot(self._body, i)
        return _swatch(get_by_index_or_default(s.color).hex) if s is not None else QIcon()

    def _fill_list(self) -> None:
        self._list.blockSignals(True)
        self._list.clear()
        for i in range(ob.SLOT_COUNT):
            if ob.parse_setlist_slot(self._body, i) is None:
                break
            self._list.addItem(self._row_text(i))
            self._list.item(i).setIcon(self._row_icon(i))
        self._list.blockSignals(False)

    def _refresh_row(self, i: int) -> None:
        it = self._list.item(i)
        if it is not None:
            it.setText(self._row_text(i))
            it.setIcon(self._row_icon(i))

    def _load_slot(self, row: int) -> None:
        s = ob.parse_setlist_slot(self._body, row) if row >= 0 else None
        self._slot_box.setEnabled(s is not None)
        self._up.setEnabled(row > 0)
        self._down.setEnabled(0 <= row < self._list.count() - 1)
        if s is None:
            return
        self._loading = True
        self._name.setText(s.name.strip())
        self._color.setCurrentIndex(get_by_index_or_default(s.color).index)
        self._transpose.setValue(se.slot_transpose(self._body, row))
        self._comments.setPlainText(s.comments.rstrip())
        self._loading = False

    # -- edits (each writes into the working copy at once) --
    def _commit_name(self, *_) -> None:
        row = self._list.currentRow()
        if self._loading or row < 0:
            return
        self._body = ob.write_setlist_slot_name(self._body, row, self._name.text())
        self._refresh_row(row)

    def _commit_color(self, idx: int) -> None:
        row = self._list.currentRow()
        if self._loading or row < 0:
            return
        self._body = ob.write_setlist_slot_color(self._body, row, ALL_COLORS[idx].index)
        self._refresh_row(row)

    def _commit_transpose(self, v: int) -> None:
        row = self._list.currentRow()
        if not self._loading and row >= 0:
            self._body = se.write_slot_transpose(self._body, row, v)

    def _commit_comments(self) -> None:
        row = self._list.currentRow()
        if not self._loading and row >= 0:
            self._body = ob.write_setlist_slot_comments(self._body, row, self._comments.toPlainText())

    def _shift(self, delta: int) -> None:
        row, dst = self._list.currentRow(), self._list.currentRow() + delta
        if row < 0 or not 0 <= dst < self._list.count():
            return
        self._commit_name()
        self._body = se.move_slot(self._body, row, dst)
        for i in (row, dst):
            self._refresh_row(i)
        self._list.setCurrentRow(dst)

    def _load_eq(self) -> None:
        self._eq_box.setEnabled(se.has_eq(self._body))
        if not se.has_eq(self._body):
            return
        eq = se.read_eq(self._body)
        self._loading = True
        self._eq_bypass.setChecked(eq.bypass)
        self._eq_trim.setValue(eq.input_trim)
        for sp, v in zip(self._bands, eq.bands):
            sp.setValue(v)
        self._loading = False

    def _commit_eq(self, *_) -> None:
        if self._loading or not se.has_eq(self._body):
            return
        self._body = se.write_eq(self._body, se.SetListEq(
            self._eq_bypass.isChecked(), self._eq_trim.value(), tuple(sp.value() for sp in self._bands)))

    def _accept(self) -> None:
        self._commit_name()
        self.new_body = self._body
        self.accept()
