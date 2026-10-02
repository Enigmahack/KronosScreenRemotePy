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


# ---- 1. probe: banner hidden + sync enabled when Kronos answers; shown when silent
w, fake, s = make()
assert w._sysex_unavailable and not w._btn_sync.isEnabled(), "disabled until probed"
assert wait(lambda: not w._probe_running)
assert not w._sysex_unavailable and w._btn_sync.isEnabled() and w._brd_sysex.isHidden()
assert w._btn_sync.text() == "2-Way Sync"
print("probe ok (answering)")

fake.digest_ok = False
w._recheck_sysex(); assert wait(lambda: not w._probe_running, 20)
assert w._sysex_unavailable and not w._brd_sysex.isHidden() and not w._btn_sync.isEnabled()
fake.digest_ok = True
w._recheck_sysex(); assert wait(lambda: not w._probe_running)
assert not w._sysex_unavailable and w._brd_sysex.isHidden()
print("probe ok (silent -> banner -> recovered)")

# ---- 2. sync-mode persistence + labels
w._set_sync_mode(L.SYNC_PUSH_ONLY)
assert s.librarian_sync_mode == "PushOnly" and w._btn_sync.text() == "Push Only"
w._set_sync_mode(L.SYNC_PULL_ONLY); assert w._btn_sync.text() == "Pull Only"
w._set_sync_mode(L.SYNC_TWO_WAY); assert w._btn_sync.text() == "2-Way Sync"
print("modes ok")

# ---- 3. two-way sync pulls THEN pushes (the bug: plain click used to be push-only)
mark_dirty(w, 5)
fake.writes.clear()
digest_before = fake.digest_calls
w._start_sync(); assert wait(lambda: not w._busy, 60)
assert fake.digest_calls - digest_before >= 30, "pull sweep must query every bank's digest (plain click is NOT push-only)"
assert any(wr[2] == 5 for wr in fake.writes), "dirty slot 5 was pushed"
print("two-way ok; status:", w._status_label.text())
assert "Pulled" in w._status_label.text() and "Pushed" in w._status_label.text()

# ---- 4. push-only: conflict -> overwrite prompt -> No keeps; Yes forces
mark_dirty(w, 6)
fake.digest_ver = 2                      # bank moved on the Kronos since baseline
fake.writes.clear()
w._set_sync_mode(L.SYNC_PUSH_ONLY)
answers = []
QMessageBox.warning = staticmethod(lambda *a, **k: (answers.append(a[1]) or QMessageBox.StandardButton.No))
w._start_sync(); assert wait(lambda: not w._busy, 30)
assert answers and answers[-1] == "Overwrite the Kronos?" and not fake.writes
assert w._index.get(OBJ_PROGRAM, 0x00, 6).conflicted
assert not w._brd_conflict.isHidden(), "conflict banner shown"
assert "NOT pushed" in w._lbl_warning.text()
print("push-only conflict ok (declined -> nothing written, banner shown)")

QMessageBox.warning = staticmethod(lambda *a, **k: (answers.append(a[1]) or QMessageBox.StandardButton.Yes))
w._start_sync(); assert wait(lambda: not w._busy, 30), "retry"
pump(300); assert wait(lambda: not w._busy, 30)
assert any(wr[2] == 6 for wr in fake.writes), "force retry wrote the stale-bank object"
assert not w._index.get(OBJ_PROGRAM, 0x00, 6).is_dirty
print("push-only overwrite ok")

# ---- 5. force destructive setting: no prompt, writes straight through a moved bank
mark_dirty(w, 7); fake.digest_ver = 3; fake.writes.clear(); answers.clear()
s.librarian_force_destructive_write = True
w._refresh_destructive_banner(); assert not w._brd_destructive.isHidden()
w._start_sync(); assert wait(lambda: not w._busy, 30)
assert not answers and any(wr[2] == 7 for wr in fake.writes), (answers, fake.writes)
s.librarian_force_destructive_write = False; w._refresh_destructive_banner()
assert w._brd_destructive.isHidden()
print("force destructive ok")

# ---- 6. resolve conflicts keep-mine
mark_dirty(w, 8); fake.digest_ver = 4
w._set_sync_mode(L.SYNC_PUSH_ONLY)
QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)
w._start_sync(); wait(lambda: not w._busy, 30)
assert w._index.get(OBJ_PROGRAM, 0x00, 8).conflicted
QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
w._resolve_conflicts_keep_mine()
assert not w._index.get(OBJ_PROGRAM, 0x00, 8).conflicted
assert w._index.bank_digest_baseline["0:0"] == hashlib.sha1(b"0:0:4").digest().hex()
assert w._brd_conflict.isHidden()
print("resolve ok:", w._status_label.text())

# ---- 7. pull-only discards after confirm; cancel keeps
mark_dirty(w, 9)
w._set_sync_mode(L.SYNC_PULL_ONLY)
QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)
w._start_sync()
assert w._index.get(OBJ_PROGRAM, 0x00, 9).is_dirty and "cancelled" in w._status_label.text()
QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
w._start_sync(); assert wait(lambda: not w._busy, 30)
assert not w._index.get(OBJ_PROGRAM, 0x00, 9).is_dirty
assert "discarded" in w._status_label.text(), w._status_label.text()
print("pull-only ok:", w._status_label.text())

# ---- 8. silent instrument mid-session: pull aborts, banner raised, nothing marked complete
fake.digest_ok = False
w._set_sync_mode(L.SYNC_TWO_WAY)
w._sysex_unavailable = False; w._refresh_enable()
w._start_sync(); assert wait(lambda: not w._busy, 90)
assert w._sysex_unavailable and not w._brd_sysex.isHidden()
print("silent abort ok")
w._closed = True
print("ALL PASS")
