"""Type-to-filter search for the Librarian's trees (Local, Merge, PCG).

Matching mirrors the PCG pane's search: case-insensitive substring on a row's text. A leaf shows when its
own text matches or an ancestor header matches (typing "I-A" shows the whole I-A bank); a header shows when
anything under it does. Groups that gain a match are expanded and never force-collapsed, so clearing the
search does not fight a manual expand/collapse.

Cost: one casefold + `in` per row, repaint suppressed for the pass, typing debounced. A real library
(thousands of rows) filters in a few ms.
"""
from __future__ import annotations

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QLineEdit, QTreeWidget

DEBOUNCE_MS = 90


class TreeSearchBox(QLineEdit):
    """Line edit that emits `queryChanged` shortly after typing stops (a burst of keys filters once)."""

    queryChanged = Signal(str)

    def __init__(self, placeholder: str = "Search by name or bank...", parent=None):
        super().__init__(parent)
        self.setPlaceholderText(placeholder)
        self.setClearButtonEnabled(True)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(DEBOUNCE_MS)
        self._timer.timeout.connect(lambda: self.queryChanged.emit(self.text()))
        self.textChanged.connect(self._on_text)

    def _on_text(self, text: str) -> None:
        # Clearing is applied at once (restoring the full tree should feel instant).
        if not text:
            self._timer.stop()
            self.queryChanged.emit("")
        else:
            self._timer.start()


def apply_tree_filter(tree: QTreeWidget, query: str) -> int:
    """Filter `tree` to rows matching `query`; returns the number of matching leaves (all leaves when the
    query is blank). Selected rows that end up hidden are deselected so no action runs on an invisible row."""
    q = query.strip().casefold()
    tree.setUpdatesEnabled(False)
    try:
        count = 0
        # Iterative DFS: (item, ancestor_matched). Post-order visibility via a second pass over `order`.
        order = []
        stack = [(tree.topLevelItem(i), False) for i in range(tree.topLevelItemCount())]
        while stack:
            item, inherited = stack.pop()
            matched = inherited or (not q) or q in item.text(0).casefold()
            order.append((item, matched))
            for i in range(item.childCount()):
                stack.append((item.child(i), matched))
        visible = {}
        for item, matched in reversed(order):  # children are visited before their parent
            n = item.childCount()
            if n == 0:
                vis = matched
                count += vis
            else:
                vis = any(visible[id(item.child(i))] for i in range(n))
                if vis and q:
                    item.setExpanded(True)
            visible[id(item)] = vis
            if item.isHidden() == vis:
                item.setHidden(not vis)
        if q:
            for item in tree.selectedItems():
                if item.isHidden():
                    item.setSelected(False)
        return count
    finally:
        tree.setUpdatesEnabled(True)
