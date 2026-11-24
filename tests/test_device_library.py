"""Keyboard Library is per instrument (PublicID-named folder) and labels/sizes follow the family. Exit code = result."""
import os, sys, tempfile
os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_devlib_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unittest.mock import MagicMock
from PySide6.QtWidgets import QApplication
app = QApplication([])

from Core import device_identity
import Views.librarian_shell_window as L
from Data.librarian_sysex import OBJ_PROGRAM, OBJ_COMBI
from Data.local_library_store import LocalIndexEntry
from Core.sysex_service import _BODY_SIZES_BY_FAMILY

device_identity._selftest()
fails = []
def check(n, c):
    if not c: fails.append(n)

def svc(nautilus):
    s = MagicMock(); s.is_nautilus = nautilus; s.can_dump = False
    s.cached_bank_names.return_value = {}
    return s

MAC_A, MAC_B = "AABBCCDDEE0F", "112233445566"
device_identity._cache.update({"10.0.0.1": MAC_A, "10.0.0.2": MAC_A})
a = L.LibrarianShellWindow("10.0.0.1", svc(False), 21, "kronos", "kronos")
a._index.set_entry(OBJ_PROGRAM, 0x40, 3, LocalIndexEntry(baseline_hash="", current_hash="x" * 40))
a._index.save()
device_identity._cache["10.0.0.1"] = MAC_B          # same address, a different unit now answers
b = L.LibrarianShellWindow("10.0.0.1", svc(True), 21, "kronos", "kronos")
a2 = L.LibrarianShellWindow("10.0.0.2", svc(False), 21, "kronos", "kronos")  # same unit, new address
a3 = L.LibrarianShellWindow("10.9.9.9", svc(False), 21, "kronos", "kronos")  # unresolvable -> host fallback

check("key-a", a.device_key == "MAC_" + MAC_A and a3.device_key == "host_10.9.9.9")
check("other-unit-empty", len(b._index.entries) == 0)
check("same-unit-follows", len(a2._index.entries) == 1)
check("files-named-by-id", (a._index.root / "index.json").parent.name == a.device_key)
check("oplog-per-unit", a._oplog.root == a._index.root and b._oplog.root != a._oplog.root)

n = lambda t, bk: L._bank_label(t, bk, True)
check("naut-int", n(OBJ_PROGRAM, 0) == "A" and n(OBJ_PROGRAM, 5) == "F")
check("naut-user", n(OBJ_PROGRAM, 0x40) == "G" and n(OBJ_PROGRAM, 0x4D) == "T")
check("naut-combi", [n(OBJ_COMBI, x) for x in (0, 5, 6, 0x40, 0x41, 0x46)] == ["A", "F", "G", "H", "I", "N"]
      and L._bank_label(OBJ_COMBI, 6) == "I-G" and L._bank_label(OBJ_COMBI, 0x40) == "U-A")
check("kronos-unchanged", L._bank_label(OBJ_PROGRAM, 0x40) == "U-A" and L._bank_label(OBJ_PROGRAM, 0) == "I-A")
from Data.librarian_model import ObjLoc
check("loc-label-family", ObjLoc(OBJ_PROGRAM, 0x40, 3).label() == "U-A:003" and ObjLoc(OBJ_PROGRAM, 0x40, 3).label(True) == "G:003"
      and a._loc_text(ObjLoc(OBJ_PROGRAM, 0x40, 3)) == "U-A:003" and b._loc_text(ObjLoc(OBJ_PROGRAM, 0x40, 3)) == "G:003")
check("sizes", 4960 not in _BODY_SIZES_BY_FAMILY[True][0] and 4954 not in _BODY_SIZES_BY_FAMILY[False][0]
      and _BODY_SIZES_BY_FAMILY[True][1] == {5640} and _BODY_SIZES_BY_FAMILY[False][1] == {7810})
for w in (a, b, a2, a3):
    w._closed = True
if fails:
    print("FAIL:", fails); sys.exit(1)
print("device library tests passed")
