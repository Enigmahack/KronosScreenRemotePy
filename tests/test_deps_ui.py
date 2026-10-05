import os, sys, time, tempfile, glob
tmp = tempfile.mkdtemp(prefix="kr_deps_test_")
os.environ["KRONOS_DATA_DIR"] = tmp
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unittest.mock import MagicMock
from PySide6.QtWidgets import QApplication, QFileDialog
from PySide6.QtCore import Qt
app = QApplication([])
from Models.app_settings import AppSettings
from Data.librarian_sysex import OBJ_PROGRAM, OBJ_COMBI, func33_to_obj_bank, set_combi_timbre_ref
from Data.local_library_store import LocalIndexEntry
from Data.pcg_file import open_pcg, wire_body_from_pcg_entry, WIRE_SIZE_HD1
from Data.librarian_model import ObjLoc
from Objects.object_body import write_program_name
import Views.librarian_shell_window as L


class Fake:
    can_dump = True
    def __getattr__(self, n): return MagicMock()
    def cached_bank_names(self, *a, **k): return {}


def pump(ms=200):
    end = time.time() + ms / 1000
    while time.time() < end:
        app.processEvents(); time.sleep(0.01)


w = L.LibrarianShellWindow("1.2.3.4", Fake(), parent=None, settings=AppSettings())
now = "t"


def add(obj_type, bank, number, body, name):
    h = w._blobs.put(body)
    w._index.set_entry(obj_type, bank, number, LocalIndexEntry(
        version=1, baseline_hash=h, current_hash=h, display_name=name, created_utc=now, modified_utc=now))


def prog_bank(f33):
    return func33_to_obj_bank(1, f33)


# ---- local selection: Combi -> A (present, has EXs10 sample + drum track -> B), B missing, ROM
combi = bytearray(7810)
for t in range(16):
    set_combi_timbre_ref(combi, t, 0, 7)
set_combi_timbre_ref(combi, 1, 0, 8)
set_combi_timbre_ref(combi, 2, 16, 5)
pa = bytearray(write_program_name(bytes(WIRE_SIZE_HD1), "Warm Pad"))
pa[2774] = 1; pa[2775:2790] = bytes([0x4B, 0x4F, 0x52, 0x47, 0, 0, 0, 0, 0, 0, 0, 0, 0x4D, 0x53, 0x00]); pa[2790] = 11 << 1
pa[1295] |= 0x10; pa[2688], pa[2689] = 8, 0
add(OBJ_PROGRAM, prog_bank(0), 7, bytes(pa), "Warm Pad")
add(OBJ_COMBI, 0, 1, bytes(combi), "My Combi")
w._refresh_local_tree()


def find_leaf(tree, tag):
    def walk(it):
        d = it.data(0, Qt.ItemDataRole.UserRole)
        if d is not None and tuple(d) == tag:
            return it
        for i in range(it.childCount()):
            r = walk(it.child(i))
            if r is not None: return r
    for i in range(tree.topLevelItemCount()):
        r = walk(tree.topLevelItem(i))
        if r is not None: return r


leaf = find_leaf(w._tree_local, ("local", OBJ_COMBI, 0, 1))
assert leaf is not None
leaf.parent().setExpanded(True); w._tree_local.setCurrentItem(leaf); pump()
rows = [w._deps_list.item(i) for i in range(w._deps_list.count())]
texts = [r.text() for r in rows]
print("local rows:"); [print("   ", t) for t in texts]
assert any("Warm Pad" in t for t in texts) and any("not found locally" in t for t in texts)
assert any("ROM bank" in t for t in texts)
assert any("EXs10" in t and "West Coast" in t for t in texts), "EXs name must resolve"
red = [r for r in rows if r.foreground().color().name().lower() == "#e05a5a"]
assert len(red) == 1 and red[0].font().bold(), "exactly one red/bold missing row"
sample = [r for r in rows if "EXs10" in r.text()][0]
assert sample.foreground().color().name().lower() == "#d9c23a"
print("local panel OK (rows=%d, red=%d)" % (len(rows), len(red)))

# a Program selection works too (drum track + sample rows) — previously Programs showed nothing
pleaf = find_leaf(w._tree_local, ("local", OBJ_PROGRAM, prog_bank(0), 7))
pleaf.parent().setExpanded(True); w._tree_local.setCurrentItem(pleaf); pump()
ptexts = [w._deps_list.item(i).text() for i in range(w._deps_list.count())]
print("program rows:", ptexts)
assert any("EXs10" in t for t in ptexts) and any("not found locally" in t for t in ptexts)

# More Info content is lazy + structured
info_calls = []
L._ObjectInfoDialog.exec = lambda self_: info_calls.append((self_.windowTitle()))
w._show_dependency_info(w._deps_list.item(0)); assert info_calls == ["Object Info"]

# ---- Merge gap rows (always first) + Search a PCG
from pathlib import Path
from fixture_paths import PCG_EXAMPLES
pcg_files = [str(f) for f in sorted(Path(PCG_EXAMPLES).rglob("*"))
             if f.suffix.lower() == ".pcg" and "BBPB" in str(f).upper()][:1]
assert pcg_files, "need a real PCG"
pcg_path = pcg_files[0]
pcg = open_pcg(open(pcg_path, "rb").read())
combi_entries = [e for e in pcg.objects if e.obj_type == OBJ_COMBI]
ce = None
for e in combi_entries:
    body = wire_body_from_pcg_entry(OBJ_COMBI, e)
    if body and w._resolve_refs(OBJ_COMBI, body):
        ce = e; break
assert ce is not None
addr = (OBJ_COMBI, ce.bank.obj_bank, ce.index)
only_combi = wire_body_from_pcg_entry(OBJ_COMBI, ce)
added, gaps = w._merge.pull_recursive(addr, lambda t, b, n: only_combi if t == OBJ_COMBI else None,
                                      w._resolve_refs, source="synthetic")
w._refresh_merge_tree(); pump()
gap_texts = [w._deps_list.item(i).text() for i in range(w._deps_list.count())]
print("gap rows:", len(gaps), "| panel rows:", len(gap_texts), "| first:", gap_texts[:1])
assert gaps and len(gap_texts) >= 1
first = w._deps_list.item(0)
assert w._dep_row(first).is_missing and first.foreground().color().name().lower() == "#e05a5a"
assert "needed by" in first.text()
assert not any(w._dep_row(w._deps_list.item(i)) is None for i in range(w._deps_list.count()))
# gap rows survive an unrelated selection change and a Local tree rebuild
w._tree_local.clearSelection(); pump(); w._refresh_local_tree(); pump()
assert w._deps_list.count() >= 1 and w._dep_row(w._deps_list.item(0)).is_missing, "gap rows must persist"
n_before = w._deps_list.count()

QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (pcg_path, ""))
w._deps_list.setCurrentRow(0)
w._search_pcg_for_row(w._deps_list.item(0)); pump()
status = w._merge_status_label.text()
print("search status:", status)
assert status.startswith("Found"), status
n_after = sum(1 for i in range(w._deps_list.count()) if w._dep_row(w._deps_list.item(i)).is_missing)
print("missing rows before/after search:", n_before, "->", n_after)
assert n_after < n_before, "staging found dependencies must shrink the red section"

# a PCG that has none of them -> the 'try another' message and nothing staged
other = w._merge.entries.__len__()
QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: ("", ""))
w._search_pcg_for_row(w._deps_list.item(0))   # cancelled dialog: no change, no crash
assert len(w._merge.entries) == other
print("ALL PASS")
w._closed = True
