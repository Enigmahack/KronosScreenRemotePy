"""App-wide layout rule: a popup's controls never move because a list is short or empty, or because a sibling is hidden.

Installed once as an application event filter. When a dialog is first shown, every child widget keeps its slot in the
layout while hidden (`retainSizeWhenHidden`), and every list / tree / table gets a minimum height, so an empty or short
list leaves blank space instead of pulling its neighbours around. Code that swaps controls should hide them; the slot stays.

Opt out with a class attribute `STABLE_LAYOUT = False`: for big tool windows whose banners / panels appear and disappear
and are meant to reflow (Librarian, Settings, ...)."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QAbstractItemView, QApplication, QDialog, QSizePolicy, QWidget

MIN_LIST_HEIGHT = 140
_DONE = "_stable_layout_done"


def stabilize(root: QWidget) -> None:
    for w in root.findChildren(QWidget):
        sp = w.sizePolicy()
        sp.setRetainSizeWhenHidden(True)
        if isinstance(w, QAbstractItemView):
            sp.setVerticalPolicy(QSizePolicy.Policy.Expanding)
            w.setMinimumHeight(max(w.minimumHeight(), MIN_LIST_HEIGHT))
        w.setSizePolicy(sp)
    root.setProperty(_DONE, True)


class _Filter(QObject):
    def eventFilter(self, obj, ev):
        if (ev.type() == QEvent.Type.Show and isinstance(obj, QDialog) and not obj.property(_DONE)
                and getattr(obj, "STABLE_LAYOUT", True)):
            stabilize(obj)
        return False


_filter = None


def install(app: QApplication) -> None:
    global _filter
    if _filter is None:
        _filter = _Filter(app)
        app.installEventFilter(_filter)
