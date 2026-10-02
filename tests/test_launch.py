import os, sys, time, tempfile, hashlib
tmp = tempfile.mkdtemp(prefix="kr_lib_test_")
os.environ["KRONOS_DATA_DIR"] = tmp
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unittest.mock import MagicMock
from PySide6.QtWidgets import QApplication, QMessageBox
app = QApplication([])

from Core.sysex_service import SysExService
from Models.app_settings import AppSettings
from Data.librarian_sysex import OBJ_PROGRAM
from Data.local_library_store import LocalIndexEntry, LocalLibraryIndex, BlobStore
import Views.librarian_shell_window as L


def pump(ms=300):
    end = time.time() + ms / 1000
    while time.time() < end:
        app.processEvents(); time.sleep(0.01)


def wait(cond, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


class FakeKronos:
    """Duck-typed SysExService: every registry bank answers a digest; INT A holds one program."""
    def __init__(self):
        self.can_dump = True
        self.digest_ok = True
        self.digest_ver = 1
        self.writes = []
        self.stores = []
        self.digest_calls = 0
        self.objs = {(OBJ_PROGRAM, 0x00): {0: (1, (b"HWPROG" + bytes(4954))[:4960])}}

    def bank_digest(self, obj, bank):
        self.digest_calls += 1
        if not self.digest_ok:
            return None
        return hashlib.sha1(f"{obj}:{bank}:{self.digest_ver}".encode()).digest()

    def dump_object_parsed(self, obj, bank, number, no_response_ms=3000):
        o = self.objs.get((obj, bank), {}).get(number)
        if o is None:
            return None
        d = MagicMock(); d.version, d.body = o
        return d

    def write_object(self, op):
        self.writes.append((op.obj, op.bank, op.index, op.body)); return 0

    def store_bank(self, obj, bank):
        self.stores.append((obj, bank)); return 0

    def backup_objects(self, ops, path): return None
    def sync_names(self, **k): return None
    def cached_bank_names(self, *a, **k): return {}
    def change_program_bank_type(self, *a): return 0
    def __getattr__(self, name):
        return MagicMock()


def make(settings=None, fake=None):
    fake = fake or FakeKronos()
    s = settings or AppSettings()
    w = L.LibrarianShellWindow("1.2.3.4", fake, parent=None, settings=s)
    return w, fake, s


def mark_dirty(w, number=5):
    h_old = w._blobs.put(b"OLD" + bytes([number]))
    h_new = w._blobs.put((b"NEW" + bytes([number]) + bytes(4960))[:4960])
    w._index.set_entry(OBJ_PROGRAM, 0x00, number, LocalIndexEntry(
        version=1, baseline_hash=h_old, current_hash=h_new, display_name=f"P{number}",
        created_utc="t", modified_utc="t"))
    w._index.set_bank_digest_baseline(OBJ_PROGRAM, 0x00, hashlib.sha1(b"0:0:1").digest().hex())



s=AppSettings(); s.librarian_full_sync_on_launch=True
w,fake,s=make(s)
assert wait(lambda: "Full sync on launch" in w._status_label.text(), 60), w._status_label.text()
assert not w._busy
print("launch pull ok:", w._status_label.text())
# not armed when the setting is off, and never pushes
s2=AppSettings(); w2,fake2,_=make(s2); assert wait(lambda: not w2._probe_running)
pump(500); assert not w2._busy and "Full sync" not in w2._status_label.text()
assert fake2.writes==[]
print("launch pull off ok")
w._closed=True; w2._closed=True
