"""Auto-Fill to Library must place a Program in a bank of its own format (HD-1 / EXi).
Regression: it took the first free Program slot anywhere, so an HD-1 Program (e.g. a Nautilus one
carrying Wave Sequences) landed in an EXi bank and the push was refused ("U-EE is currently formatted
as EXi ... edit in the other format"). Run from the repo root: python tests/test_auto_fill_bank_type.py"""
import os
import sys
import tempfile
import time

os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_afbt_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unittest.mock import MagicMock
from PySide6.QtWidgets import QApplication

app = QApplication([])

from Models.app_settings import AppSettings
from Data import pcg_file
from Data.librarian_sysex import OBJ_PROGRAM, PROGRAM_HD1_WIRE_SIZE
from Data.local_library_store import LocalIndexEntry, LocalLibraryIndex
import Views.librarian_shell_window as L


def wait(cond, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


def prog(size, name):
    b = bytearray(size)
    b[0:24] = name.encode().ljust(24)
    return bytes(b)


class Fake:
    live = {}

    def cached_bank_types(self):
        return dict(self.live)

    def __getattr__(self, name):
        return MagicMock()


fake = Fake()
w = L.LibrarianShellWindow("1.2.3.4", fake, parent=None, settings=AppSettings())
assert wait(lambda: not w._probe_running)


def seed(bank, slot, body, name):
    h = w._blobs.put(body)
    w._index.set_entry(OBJ_PROGRAM, bank, slot, LocalIndexEntry(
        version=5, baseline_hash=h, current_hash=h, display_name=name,
        created_utc="", modified_utc="", conflicted=False, has_resolved_dependencies=True,
        is_exi=True, pending_delete=False))   # is_exi=True everywhere, as the app really stores it


EXI, HD1 = pcg_file.WIRE_SIZE_EXI, PROGRAM_HD1_WIRE_SIZE
seed(0x00, 0, prog(EXI, "ExiA"), "ExiA")      # I-A: EXi (first in scan order - the old landing spot)
seed(0x01, 0, prog(HD1, "HdA"), "HdA")        # I-B: HD-1
assert w._local_bank_type_of(0x00) is True and w._local_bank_type_of(0x01) is False
assert w._local_bank_type_of(0x02) is None


def stage_and_fill(name, kind_size):
    from Data.merge_cache import MergeEntry
    body = prog(kind_size, name)
    h = L.BlobStore.compute_hash(body)
    w._merge._by_hash[h] = MergeEntry(content_hash=h, obj_type=OBJ_PROGRAM, body=body, version=5,
                                      display_name=name, is_top_level_pull=True)
    w._auto_fill_to_library()
    assert wait(lambda: not w._auto_fill_queue and not w._auto_fill_timer.isActive(), 60)
    return w._index.find_by_content_hash(OBJ_PROGRAM, h)


loc = stage_and_fill("NewHD1", HD1)
assert loc == (0x01, 1), f"HD-1 Program must follow the HD-1 bank, got {loc}"
loc = stage_and_fill("NewEXi", EXI)
assert loc == (0x00, 1), f"EXi Program must follow the EXi bank, got {loc}"
loc = stage_and_fill("NewHD1b", HD1)
assert loc == (0x01, 2), loc
print("auto-fill bank type ok")

# The instrument's own func-0x61 map wins over inference and makes an empty bank known.
fake.live = {0x02: True, 0x03: False, 0x01: True}      # 0x01 contradicts the stored bodies: live wins
assert w._local_bank_type_of(0x02) is True and w._local_bank_type_of(0x03) is False
assert w._local_bank_type_of(0x01) is True
w._auto_fill_bank_types = {}
loc = stage_and_fill("LiveHD1", HD1)
assert loc == (0x03, 0), f"HD-1 must go to the live-known HD-1 bank, got {loc}"
print("live bank-type map ok")
