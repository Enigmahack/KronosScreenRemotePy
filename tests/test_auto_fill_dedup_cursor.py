"""Auto-Fill: a Program reused from existing content (dedup) must not consume a free slot.
Regression: a pulled library has an Init entry in every slot, so the cursor stepped past the slot of each
deduped Program, ran off the last bank and reported "full" with room left. Run: python tests/test_auto_fill_dedup_cursor.py"""
import os
import sys
import tempfile
import time

os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_afdc_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unittest.mock import MagicMock
from PySide6.QtWidgets import QApplication

app = QApplication([])

from Models.app_settings import AppSettings
from Data import pcg_file
from Data.librarian_sysex import OBJ_PROGRAM
from Data.local_library_store import LocalIndexEntry
from Data.merge_cache import MergeEntry
import Views.librarian_shell_window as L


def wait(cond, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


def prog(name):
    b = bytearray(pcg_file.WIRE_SIZE_EXI)
    b[0:24] = name.encode().ljust(24)
    return bytes(b)


class Fake:
    live = {0x00: True, 0x01: True}

    def cached_bank_types(self):
        return dict(self.live)

    def __getattr__(self, name):
        return MagicMock()


w = L.LibrarianShellWindow("1.2.3.4", Fake(), parent=None, settings=AppSettings())
assert wait(lambda: not w._probe_running)


def seed(bank, slot, name):
    h = w._blobs.put(prog(name))
    w._index.set_entry(OBJ_PROGRAM, bank, slot, LocalIndexEntry(
        version=5, baseline_hash=h, current_hash=h, display_name=name,
        created_utc="", modified_utc="", conflicted=False, has_resolved_dependencies=True,
        is_exi=True, pending_delete=False))


def stage(name):
    body = prog(name)
    h = L.BlobStore.compute_hash(body)
    w._merge._by_hash[h] = MergeEntry(content_hash=h, obj_type=OBJ_PROGRAM, body=body, version=5,
                                      display_name=name, is_top_level_pull=True)
    return h


for b in (0x00, 0x01):
    for s in range(128):
        seed(b, s, "Init Program")
for s in range(5):
    seed(0x01, s, f"Real{s}")             # real content already in the library
for s in range(5):
    stage(f"Real{s}")                      # staged duplicates -> reused, nothing written
new = [stage(f"New{i}") for i in range(3)]
w._auto_fill_to_library()
assert wait(lambda: not w._auto_fill_queue and not w._auto_fill_timer.isActive(), 60)
locs = [w._index.find_by_content_hash(OBJ_PROGRAM, h) for h in new]
assert locs == [(0x00, 0), (0x00, 1), (0x00, 2)], f"deduped Programs ate free slots: {locs}"
print("auto-fill dedup cursor ok")
