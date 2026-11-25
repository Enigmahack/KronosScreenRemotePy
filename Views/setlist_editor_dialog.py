"""Set List editor: the slots of one Set List as the instruments show them, pages of a 4x4 grid. Drag a slot onto another to
swap them (Ctrl-drag copies); Copy / Cut / Paste / Delete work from the menu and keys. The selected slot's name, colour,
pitch offset, comments and target (a Combi, Program or Song, chosen from the Keyboard Library) are edited beside the grid, and
so is the Set List's own EQ. Works on a copy of the body (either instrument's); `new_body` holds the result after OK."""
from __future__ import annotations

from typing import Callable, Optional, Tuple

from PySide6.QtCore import QMimeData, Qt
from PySide6.QtGui import QColor, QDrag, QIcon, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QGridLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QMenu, QPlainTextEdit, QPushButton, QSpinBox, QVBoxLayout)

from Objects import object_body as ob
from Objects import setlist_edit as se
from Utils.setlist_colors import ALL_COLORS, get_by_index_or_default

COLS = ROWS = 4
PER_PAGE = COLS * ROWS
PAGES = ob.SLOT_COUNT // PER_PAGE
_MIME = "application/x-setlist-slot"
Ref = Tuple[int, int, int]
_clip: Optional[bytes] = None             # one slot, shared by every open editor so it pastes across Set Lists


def _swatch(hex_color: str) -> QIcon:
    pix = QPixmap(28, 12)
    pix.fill(QColor(hex_color))
    return QIcon(pix)


class _Cell(QPushButton):
    def __init__(self, dlg: "SetListEditorDialog", slot: int):
        super().__init__()
        self._dlg, self.slot = dlg, slot
        self.setMinimumSize(120, 64)
        self.setAcceptDrops(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(lambda p: dlg._menu(self.slot, self.mapToGlobal(p)))
        self.clicked.connect(lambda: dlg._select(self.slot))
        self.doubleClicked_cb = None
        self._press = None

    def mousePressEvent(self, e):
        self._press = e.position().toPoint()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._press is not None and (e.position().toPoint() - self._press).manhattanLength() > 8:
            self._press = None
            self._dlg._select(self.slot)
            md = QMimeData()
            md.setData(_MIME, str(self.slot).encode())
            drag = QDrag(self)
            drag.setMimeData(md)
            drag.exec(Qt.DropAction.MoveAction | Qt.DropAction.CopyAction, Qt.DropAction.MoveAction)
            return
        super().mouseMoveEvent(e)

    def dragEnterEvent(self, e):
        if e.mimeData().hasFormat(_MIME):
            e.acceptProposedAction()

    def dropEvent(self, e):
        if e.mimeData().hasFormat(_MIME):
            src = int(bytes(e.mimeData().data(_MIME)).decode())
            copy = bool(e.modifiers() & Qt.KeyboardModifier.ControlModifier)
            self._dlg.drop_slot(src, self.slot, copy)
            e.acceptProposedAction()


class SetListEditorDialog(QDialog):
    """`describe(type, func33_bank, index)` names a target; `pick(current)` asks the user for a new target
    (returns (type, func33_bank, index) or None); `target_name(ref)` is that target's own name, used to name a slot."""

    def __init__(self, title: str, body: bytes, describe: Callable[[int, int, int], str],
                 pick: Optional[Callable[[Optional[Ref]], Optional[Ref]]] = None,
                 target_name: Optional[Callable[[Ref], str]] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(980, 560)
        self._body = body
        self._describe = describe
        self._pick = pick
        self._target_name = target_name
        self._loading = False
        self._name_edited = False         # the name box is written back only after the user typed in it
        self._page = 0
        self._sel = 0
        self.new_body: Optional[bytes] = None

        root = QVBoxLayout(self)
        main = QHBoxLayout()
        root.addLayout(main, 1)

        left = QVBoxLayout()
        grid = QGridLayout()
        self._cells = [_Cell(self, i) for i in range(PER_PAGE)]
        for i, c in enumerate(self._cells):
            grid.addWidget(c, i // COLS, i % COLS)
        left.addLayout(grid, 1)
        nav = QHBoxLayout()
        self._prev = QPushButton("< Page")
        self._next = QPushButton("Page >")
        self._page_lbl = QLabel()
        self._prev.clicked.connect(lambda: self._set_page(self._page - 1))
        self._next.clicked.connect(lambda: self._set_page(self._page + 1))
        nav.addWidget(self._prev)
        nav.addWidget(self._page_lbl, 1, Qt.AlignmentFlag.AlignCenter)
        nav.addWidget(self._next)
        left.addLayout(nav)
        main.addLayout(left, 3)

        right = QVBoxLayout()
        self._slot_box = QGroupBox("Slot")
        g = QGridLayout(self._slot_box)
        self._name = QLineEdit()
        self._name.setMaxLength(ob.NAME_LEN)
        self._target = QLabel()
        self._target.setWordWrap(True)
        self._change = QPushButton("Change...")
        self._change.setEnabled(pick is not None)
        self._color = QComboBox()
        for c in ALL_COLORS:
            self._color.addItem(_swatch(c.hex), c.display_name)
        self._transpose = QSpinBox()
        self._transpose.setRange(*se.TRANSPOSE_RANGE)
        self._transpose.setSuffix(" semitones")
        self._comments = QPlainTextEdit()
        g.addWidget(QLabel("Name"), 0, 0)
        g.addWidget(self._name, 0, 1, 1, 2)
        g.addWidget(QLabel("Plays"), 1, 0)
        g.addWidget(self._target, 1, 1)
        g.addWidget(self._change, 1, 2)
        g.addWidget(QLabel("Color"), 2, 0)
        g.addWidget(self._color, 2, 1, 1, 2)
        g.addWidget(QLabel("Pitch offset"), 3, 0)
        g.addWidget(self._transpose, 3, 1, 1, 2)
        g.addWidget(QLabel("Comments"), 4, 0, Qt.AlignmentFlag.AlignTop)
        g.addWidget(self._comments, 4, 1, 1, 2)
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
        main.addLayout(right, 2)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        for seq, fn in ((QKeySequence.StandardKey.Copy, lambda: self.copy_slot(self._sel)),
                        (QKeySequence.StandardKey.Cut, lambda: self.cut_slot(self._sel)),
                        (QKeySequence.StandardKey.Paste, lambda: self.paste_slot(self._sel)),
                        (QKeySequence(Qt.Key.Key_Delete), lambda: self.delete_slot(self._sel))):
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(fn)

        self._load_eq()
        self._refresh_grid()
        self._select(0)
        self._name.textEdited.connect(self._on_name_typed)
        self._color.currentIndexChanged.connect(self._commit_color)
        self._transpose.valueChanged.connect(self._commit_transpose)
        self._comments.textChanged.connect(self._commit_comments)
        self._change.clicked.connect(self.change_target)
        self._eq_bypass.toggled.connect(self._commit_eq)
        self._eq_trim.valueChanged.connect(self._commit_eq)
        for sp in self._bands:
            sp.valueChanged.connect(self._commit_eq)

    # -- grid --
    def _slot_text(self, i: int) -> str:
        s = ob.parse_setlist_slot(self._body, i)
        if self._blank(i):
            return f"{i:03d}\n(empty)"
        return f"{i:03d}  {s.name.strip() or '(unnamed)'}\n{self._target_text(i)}"

    def _blank(self, i: int) -> bool:
        """Empty only when nothing is set: a slot with no name but a comment, target or pitch offset still shows as itself."""
        s = ob.parse_setlist_slot(self._body, i)
        return (s is None or (s.is_empty and not s.comments.strip() and se.slot_ref(self._body, i) == (0, 0, 0)
                              and not se.slot_transpose(self._body, i)))

    def _target_text(self, i: int) -> str:
        try:
            return self._describe(*se.slot_ref(self._body, i))
        except Exception:
            return "?"

    def _style(self, i: int) -> str:
        s = ob.parse_setlist_slot(self._body, i)
        if self._blank(i):
            bg, fg = "#2b2b2b", "#808080"
        else:
            bg = get_by_index_or_default(s.color).hex
            c = QColor(bg)
            fg = "#000000" if c.lightness() > 140 else "#ffffff"
        border = "3px solid #ffd400" if i == self._sel else "1px solid #555"
        return f"QPushButton{{background:{bg};color:{fg};border:{border};text-align:left;padding:4px;}}"

    def _refresh_cell(self, i: int) -> None:
        if self._page * PER_PAGE <= i < (self._page + 1) * PER_PAGE:
            c = self._cells[i - self._page * PER_PAGE]
            c.slot = i
            c.setText(self._slot_text(i))
            c.setStyleSheet(self._style(i))

    def _refresh_grid(self) -> None:
        for i in range(self._page * PER_PAGE, (self._page + 1) * PER_PAGE):
            self._refresh_cell(i)
        self._page_lbl.setText(f"Page {self._page + 1} of {PAGES}  (slots {self._page * PER_PAGE}-{(self._page + 1) * PER_PAGE - 1})")
        self._prev.setEnabled(self._page > 0)
        self._next.setEnabled(self._page < PAGES - 1)

    def _set_page(self, p: int) -> None:
        self._page = max(0, min(PAGES - 1, p))
        self._refresh_grid()

    def _select(self, slot: int) -> None:
        self._commit_name()
        old, self._sel = self._sel, slot
        self._refresh_cell(old)
        self._refresh_cell(slot)
        self._load_slot(slot)

    def _load_slot(self, row: int) -> None:
        s = ob.parse_setlist_slot(self._body, row)
        self._slot_box.setEnabled(s is not None)
        if s is None:
            return
        self._loading = True
        self._name_edited = False
        self._name.setText(s.name.strip())
        self._target.setText(self._target_text(row))
        self._color.setCurrentIndex(get_by_index_or_default(s.color).index)
        self._transpose.setValue(se.slot_transpose(self._body, row))
        self._comments.setPlainText(s.comments.rstrip())
        self._loading = False

    # -- slot operations (grid drag/drop, menu, keys) --
    def _set_body(self, body: bytes, *slots: int) -> None:
        self._body = body
        for i in slots:
            self._refresh_cell(i)
        self._load_slot(self._sel)

    def drop_slot(self, src: int, dst: int, copy: bool) -> None:
        if src == dst:
            return
        self._commit_name()
        if copy:
            self._set_body(se.put_slot_bytes(self._body, dst, se.slot_bytes(self._body, src)), dst)
        else:
            self._set_body(se.swap_slots(self._body, src, dst), src, dst)
        self._select(dst)

    def copy_slot(self, slot: int) -> None:
        global _clip
        self._commit_name()
        _clip = se.slot_bytes(self._body, slot)

    def delete_slot(self, slot: int) -> None:
        self._set_body(se.clear_slot(self._body, slot), slot)

    def cut_slot(self, slot: int) -> None:
        self.copy_slot(slot)
        self.delete_slot(slot)

    def paste_slot(self, slot: int) -> None:
        if _clip is None:
            return
        try:
            self._set_body(se.put_slot_bytes(self._body, slot, _clip), slot)
        except ValueError:
            pass                                   # copied from the other instrument's Set List

    def _menu(self, slot: int, pos) -> None:
        self._select(slot)
        m = QMenu(self)
        m.addAction("Change target...", self.change_target).setEnabled(self._pick is not None)
        m.addSeparator()
        m.addAction("Copy", lambda: self.copy_slot(slot))
        m.addAction("Cut", lambda: self.cut_slot(slot))
        pa = m.addAction("Paste", lambda: self.paste_slot(slot))
        pa.setEnabled(_clip is not None and len(_clip) == len(se.slot_bytes(self._body, slot)))
        m.addAction("Delete", lambda: self.delete_slot(slot))
        m.exec(pos)

    def change_target(self) -> None:
        """Point the selected slot at another Combi / Program / Song. A slot still carrying its old target's name is
        renamed to the new target's."""
        if self._pick is None:
            return
        row = self._sel
        cur = se.slot_ref(self._body, row)
        new = self._pick(cur)
        if new is None or tuple(new) == tuple(cur):
            return
        self._commit_name()
        s = ob.parse_setlist_slot(self._body, row)
        old_name = self._target_name(cur) if self._target_name else ""
        body = se.set_slot_ref(self._body, row, *new)
        if self._target_name and s is not None and (s.is_empty or s.name.strip() == old_name.strip()):
            body = ob.write_setlist_slot_name(body, row, self._target_name(tuple(new))[:ob.NAME_LEN])
        self._set_body(body, row)

    # -- field edits (each writes into the working copy at once) --
    def _on_name_typed(self, *_) -> None:
        self._name_edited = True
        self._commit_name()

    def _commit_name(self, *_) -> None:
        row = self._sel
        if self._loading or not self._name_edited or ob.parse_setlist_slot(self._body, row) is None:
            return
        self._body = ob.write_setlist_slot_name(self._body, row, self._name.text())
        self._refresh_cell(row)

    def _commit_color(self, idx: int) -> None:
        if not self._loading:
            self._body = ob.write_setlist_slot_color(self._body, self._sel, ALL_COLORS[idx].index)
            self._refresh_cell(self._sel)

    def _commit_transpose(self, v: int) -> None:
        if not self._loading:
            self._body = se.write_slot_transpose(self._body, self._sel, v)

    def _commit_comments(self) -> None:
        if not self._loading:
            self._body = ob.write_setlist_slot_comments(self._body, self._sel, self._comments.toPlainText())

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
