"""Rebuilding the Merge / PCG tree after a move keeps collapsed and expanded headers (and scroll) as the user left them."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QTreeWidget, QTreeWidgetItem
from Views.librarian_shell_window import LibrarianShellWindow as W
app = QApplication.instance() or QApplication([])

def build(t, banks):
    t.clear()
    for root, kids in banks:
        r = QTreeWidgetItem([root]); t.addTopLevelItem(r)
        for k in kids: r.addChild(QTreeWidgetItem([k]))

t = QTreeWidget()
build(t, [("Programs", ["a", "b"]), ("Combis", ["c"])])
W._restore_tree_expansion(t, {}, True)
t.topLevelItem(0).setExpanded(False)                      # user minimises Programs
st = W._tree_expansion(t)
build(t, [("Programs", ["a"]), ("Combis", ["c", "d"]), ("Set Lists", ["s"])])   # rebuild after a move; new root appears
W._restore_tree_expansion(t, st, True)
ok = (not t.topLevelItem(0).isExpanded() and t.topLevelItem(1).isExpanded() and t.topLevelItem(2).isExpanded())
print("ALL PASS" if ok else "FAILED"); sys.exit(0 if ok else 1)
