"""The gear button at the top right of the Librarian, Sample Editor and File Manager: opens Settings on that tool's tab.
MainWindow registers its `_open_settings` once (`bind`), so the dialog, its OK/Cancel handling and the saving are the
ones the main window already uses."""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtWidgets import QToolButton

_opener: Optional[Callable[..., None]] = None


def bind(opener: Callable[..., None]) -> None:
    global _opener
    _opener = opener


def open_settings(tab: str) -> None:
    if _opener is not None:
        _opener(initial_tab=tab)


def make_gear_button(tab: str, parent=None) -> QToolButton:
    btn = QToolButton(parent)
    btn.setText("⚙")
    btn.setToolTip(f"Open {tab} settings")
    btn.setAutoRaise(True)
    btn.setStyleSheet("QToolButton { font-size: 16px; padding: 0 6px; }")
    btn.clicked.connect(lambda: open_settings(tab))
    return btn
