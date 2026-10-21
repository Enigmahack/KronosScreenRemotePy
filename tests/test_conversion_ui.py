"""Librarian 'Convert to Nautilus...' against a duck-typed fake Nautilus (real window code).
Needs the Kronos PCG fixture; override with KRONOS_CONVERT_PCG. Run from the repo root."""
import os
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_convui_test_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtWidgets import QApplication, QDialog, QInputDialog, QMessageBox
app = QApplication([])

from Data.librarian_model import ObjLoc
from Data.librarian_sysex import OBJ_COMBI, OBJ_PROGRAM
from Data.pcg_file import open_pcg
from Models.app_settings import AppSettings
import Views.librarian_shell_window as L

PCG = os.environ.get("KRONOS_CONVERT_PCG", "F:/K2_62gb/HD/NARF-SOUNDS/SGC V8 K2.PCG")
if not os.path.exists(PCG):
    print("SKIP: fixture not found:", PCG)
    sys.exit(0)


def wait(cond, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


class FakeNautilus:
    can_dump = True
    is_nautilus = True

    def __init__(self):
        self.slots, self.calls = {}, []
        self.sizes = {(OBJ_PROGRAM, 0x4C): 3700, (OBJ_COMBI, 0x46): 5640, (0x19, 0x40): 556}

    def bank_digest(self, obj, bank):
        return b"d" * 20

    def dump_object_parsed(self, obj, bank, index, **kw):
        if (obj, bank) not in self.sizes:
            return None
        return SimpleNamespace(version=6, body=self.slots.get((obj, bank, index)) or b"I" * self.sizes[(obj, bank)])

    def backup_objects(self, ops, path):
        self.calls.append("backup"); open(path, "wb").write(b"x")

    def write_object(self, op):
        self.calls.append("write"); self.slots[(op.obj, op.bank, op.index)] = op.body; return 0

    def store_bank(self, obj, bank):
        self.calls.append("store"); return 0

    def cached_bank_names(self, *a, **k): return {}
    def sync_names(self, **k): return None
    def __getattr__(self, name): return MagicMock()


fake = FakeNautilus()
w = L.LibrarianShellWindow("1.2.3.4", fake, parent=None, settings=AppSettings())
pcg = open_pcg(open(PCG, "rb").read())
w._pcg_by_addr = {(e.obj_type, e.bank.obj_bank, e.index): e for e in pcg.objects if e.bank is not None}
hd1 = [a for a, e in w._pcg_by_addr.items() if e.obj_type == OBJ_PROGRAM and not e.is_exi][:3]
exi = [a for a, e in w._pcg_by_addr.items() if e.obj_type == OBJ_PROGRAM and e.is_exi][:1]
w._selected_pcg_object_locs = lambda: [ObjLoc(*a) for a in hd1 + exi]

shown = []
QDialog.exec = lambda self: QDialog.DialogCode.Accepted
QMessageBox.exec = lambda self: shown.append(self.detailedText()) or 0
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
QInputDialog.getInt = staticmethod(lambda *a, **k: (43, True))
QMessageBox.information = staticmethod(lambda *a, **k: shown.append(a[2]))

# default destination is Program bank S (0x4C) slot 0
dlg = L._DestinationDialog(OBJ_PROGRAM, "x", None, default=L.LibrarianShellWindow._CONVERT_DEFAULT_DEST[OBJ_PROGRAM])
assert dlg.selected() == (0x4C, 0), dlg.selected()

w._convert_to_nautilus("pcg")
assert wait(lambda: not w._converting and shown), "conversion finished"
written = sorted(k for k in fake.slots if k[0] == OBJ_PROGRAM)
print("written:", written)
assert len(written) >= 1 and all(k[1] == 0x4C for k in written)
assert fake.calls[0] == "backup" and 1 <= fake.calls.count("store") <= 2, fake.calls   # +1 when arp bank B is written
assert "EXI" not in shown[-1] or "HD-1 bank" in shown[-1]
assert any("[WRITTEN]" in shown[-1] for _ in [0]) and "HD-1 bank" in shown[-1], shown[-1]

# not connected to a Nautilus -> explains, writes nothing
fake.is_nautilus = False
n = len(fake.calls)
w._convert_to_nautilus("pcg")
assert len(fake.calls) == n and "Connect to a Nautilus" in shown[-1]
w._closed = True
print("ALL PASS")
