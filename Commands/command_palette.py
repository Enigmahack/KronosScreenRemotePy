"""
Command Palette — VS-Code-style fuzzy command launcher.
Port of Views/CommandPaletteWindow.xaml(.cs).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QDialog, QLineEdit, QListWidget, QVBoxLayout

import Utils.theme as T


@dataclass
class CommandEntry:
    id: str
    label: str
    key_hint: str
    execute: Callable[[], None]


class _SearchEdit(QLineEdit):
    def __init__(self, palette: "CommandPalette"):
        super().__init__()
        self._palette = palette

    def keyPressEvent(self, event: QKeyEvent):
        key = event.key()
        if key == Qt.Key_Escape:
            self._palette._dismiss()
            event.accept()
            return
        if key in (Qt.Key_Return, Qt.Key_Enter):
            self._palette._invoke()
            event.accept()
            return
        if key == Qt.Key_Down:
            self._palette._move_selection(1)
            event.accept()
            return
        if key == Qt.Key_Up:
            self._palette._move_selection(-1)
            event.accept()
            return
        super().keyPressEvent(event)


class CommandPalette(QDialog):
    def __init__(self, commands: List[CommandEntry], parent=None):
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self._all = commands
        self._filtered: List[CommandEntry] = []
        self._dismissed = False  # reentrancy guard — WindowDeactivate can fire again during close()

        self.setWindowTitle("Command Palette")
        self.setFixedWidth(480)
        self.setStyleSheet(f"""
            QDialog {{ background-color: {T.PANEL}; border: 1px solid {T.BORDER_STRONG}; }}
            QLineEdit {{
                background-color: {T.PANEL_ALT}; color: {T.TEXT}; border: none;
                border-bottom: 1px solid {T.BORDER}; padding: 8px 10px; font-size: {T.FS_H2}px;
            }}
            QListWidget {{
                background-color: {T.PANEL}; color: {T.TEXT}; border: none;
                font-size: {T.FS_BODY}px; outline: none;
            }}
            QListWidget::item {{ padding: 6px 10px; }}
            QListWidget::item:selected {{ background-color: {T.ACCENT_DEEP}; color: #FFFFFF; }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._search = _SearchEdit(self)
        self._search.setPlaceholderText("Search commands…")
        layout.addWidget(self._search)

        self._list = QListWidget()
        self._list.setMaximumHeight(380)
        self._list.itemDoubleClicked.connect(lambda _item: self._invoke())
        layout.addWidget(self._list)

        self._search.textChanged.connect(self._refresh)

        if parent is not None:
            geo = parent.geometry()
            self.move(geo.center().x() - self.width() // 2, geo.top() + 80)

        self._refresh("")

    def showEvent(self, event):
        super().showEvent(event)
        self._search.setFocus()

    def event(self, e: QEvent) -> bool:
        if e.type() == QEvent.Type.WindowDeactivate:
            self._dismiss()
        return super().event(e)

    def _dismiss(self):
        if self._dismissed:
            return
        self._dismissed = True
        self.close()

    def _refresh(self, query: str):
        query = query.strip()
        if not query:
            self._filtered = list(self._all)
        else:
            ql = query.lower()
            self._filtered = [c for c in self._all if ql in c.label.lower()]
            self._filtered.sort(key=lambda c: (0 if c.label.lower().startswith(ql) else 1,
                                               c.label.lower()))

        self._list.clear()
        for c in self._filtered:
            text = f"{c.label}     {c.key_hint}" if c.key_hint else c.label
            self._list.addItem(text)
        if self._filtered:
            self._list.setCurrentRow(0)

    def _move_selection(self, delta: int):
        row = self._list.currentRow()
        new_row = max(0, min(self._list.count() - 1, row + delta))
        self._list.setCurrentRow(new_row)

    def _invoke(self):
        row = self._list.currentRow()
        if 0 <= row < len(self._filtered):
            entry = self._filtered[row]
            self._dismiss()
            entry.execute()
