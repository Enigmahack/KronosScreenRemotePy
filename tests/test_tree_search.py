"""Librarian tree search: matching rules, bank-header match, expansion, selection, speed."""
import os, sys, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtWidgets import QApplication, QTreeWidget, QTreeWidgetItem
from Views.tree_search import apply_tree_filter

app = QApplication.instance() or QApplication([])
t = QTreeWidget()
roots = {}
for b in ("I-A", "I-B"):
    r = QTreeWidgetItem([b]); t.addTopLevelItem(r)
    for i, n in enumerate(("Alpha Lead", "Bassline", "alpha pad")):
        r.addChild(QTreeWidgetItem([f"{n}  {i:03d}"]))
    roots[b] = r

def vis(): return [(r.text(0), [r.child(i).text(0) for i in range(r.childCount()) if not r.child(i).isHidden()])
                   for r in map(t.topLevelItem, range(t.topLevelItemCount())) if not r.isHidden()]
fails = []
def check(name, ok):
    if not ok: fails.append(name)

check("count", apply_tree_filter(t, "ALPHA") == 4)
check("case+hide", all(len(c) == 2 for _, c in vis()) and len(vis()) == 2)
check("expanded", roots["I-A"].isExpanded())
check("bank match shows all", apply_tree_filter(t, "i-b") == 3 and [r for r, _ in vis()] == ["I-B"])
check("no match hides all", apply_tree_filter(t, "zzz") == 0 and vis() == [])
roots["I-A"].child(0).setSelected(True); roots["I-A"].setHidden(False)
apply_tree_filter(t, "bass")
check("hidden deselected", not t.selectedItems())
check("clear restores", apply_tree_filter(t, "  ") == 6 and len(vis()) == 2 and all(len(c) == 3 for _, c in vis()))

big = QTreeWidget()
for b in range(30):
    r = QTreeWidgetItem([f"Bank {b}"]); big.addTopLevelItem(r)
    r.addChild  # noqa
    for i in range(400):
        r.addChild(QTreeWidgetItem([f"Program {b}-{i}  {i:03d}"]))
t0 = time.perf_counter(); apply_tree_filter(big, "program 7-3"); dt = time.perf_counter() - t0
print(f"12000 rows filtered in {dt*1000:.1f} ms")
check("speed", dt < 0.5)
print("FAIL: " + ", ".join(fails) if fails else "OK"); sys.exit(1 if fails else 0)
