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
    """Duck-typed SysExService. A bank this fake populates (INT A) answers a digest and EVERY slot
    (unwritten slots hold an Init Program, as on the instrument, where a slot that does not answer is a
    timeout); every other bank is one the instrument does not serve (no digest, no dump), which the pull
    pins with its NoDigest sentinel. `timeouts` = {(obj, bank, slot)} that never answer."""
    def __init__(self):
        self.can_dump = True
        self.digest_ok = True
        self.digest_ver = 1
        self.writes = []
        self.stores = []
        self.digest_calls = 0
        self.objs = {(OBJ_PROGRAM, 0x00): {0: (1, (b"HWPROG" + bytes(4954))[:4960])}}
        self.timeouts = set()
        self.dumps = []          # every (obj, bank, slot) asked for, in order

    def served_dumps(self):
        """The dump requests made to the bank this fake serves (others are never-answering banks)."""
        return [d for d in self.dumps if (d[0], d[1]) in self.objs]

    def bank_digest(self, obj, bank):
        self.digest_calls += 1
        if not self.digest_ok or (obj, bank) not in self.objs:
            return None
        return hashlib.sha1(f"{obj}:{bank}:{self.digest_ver}".encode()).digest()

    def dump_object_parsed(self, obj, bank, number, no_response_ms=3000):
        self.dumps.append((obj, bank, number))
        if (obj, bank, number) in self.timeouts or (obj, bank) not in self.objs:
            return None
        o = self.objs[(obj, bank)].get(number)
        if o is None:
            o = (1, (b"Init Program" + bytes(4960))[:4960])
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
    old = (b"OLD" + bytes([number]) + bytes(4960))[:4960]
    w._service.objs.setdefault((OBJ_PROGRAM, 0x00), {})[number] = (1, old)
    h_old = w._blobs.put(old)
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
assert answers and answers[-1] == "Overwrite the instrument?" and not fake.writes
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
assert w._busy, "the digest reads run on a worker"
assert wait(lambda: not w._busy, 30)
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

# ---- 7b. a slot that times out: what answered is applied and the bank's baseline ADVANCES (it
#          reflects the slots that answered); the slot is recorded per slot, the user is told, and
#          the next lazy Pull asks for that slot ONLY (the record, not the digest, drives it)
fake.digest_ver = 9
fake.timeouts = {(OBJ_PROGRAM, 0x00, 3)}
w._start_sync(); assert wait(lambda: not w._busy, 60)
new_digest = hashlib.sha1(b"0:0:9").digest().hex()
assert w._index.bank_digest_baseline.get("0:0") == new_digest, "baseline reflects the answered slots"
assert w._index.incomplete_banks == {"0:0": {3: 1}}, w._index.incomplete_banks
assert "did not answer" in w._lbl_warning.text(), w._lbl_warning.text()
assert "did not answer" in w._status_label.text(), w._status_label.text()
assert w._index.get(OBJ_PROGRAM, 0x00, 4) is not None, "the slots that did answer are applied"
assert not w._brd_incomplete.isHidden() and "Program I-A slot 3" in w._lbl_incomplete.text()
assert w._btn_recheck_unread.isHidden(), "Re-check is shown only for unreadable slots"
fake.timeouts = set()
fake.dumps.clear()
w._start_sync(); assert wait(lambda: not w._busy, 60)
assert fake.served_dumps() == [(OBJ_PROGRAM, 0x00, 3)], fake.served_dumps()
assert w._index.incomplete_banks == {} and w._index.get(OBJ_PROGRAM, 0x00, 3) is not None
assert "did not answer" not in w._lbl_warning.text()
assert w._brd_incomplete.isHidden()
print("incomplete pull ok (targeted re-fetch of the one slot)")

# ---- 7c. a dead slot in a bank with pending edits (2-Way): the edit to ANOTHER slot of the bank
#          pushes normally; the edit to the dead slot is held back (NOT Conflicted - a timeout is not a
#          front-panel change); the record survives a restart (old and new index formats); Resolve
#          resolves the readable slots only; force-destructive needs its own confirm naming the slot.
w._set_sync_mode(L.SYNC_TWO_WAY)
answers = []
QMessageBox.warning = staticmethod(lambda *a, **k: (answers.append(a[1]) or QMessageBox.StandardButton.No))
mark_dirty(w, 10)
mark_dirty(w, 3)
fake.digest_ver = 10                       # the bank moved since the baseline mark_dirty set
fake.timeouts = {(OBJ_PROGRAM, 0x00, 3)}
fake.writes.clear()
w._start_sync(); assert wait(lambda: not w._busy, 60)
e3 = w._index.get(OBJ_PROGRAM, 0x00, 3)
assert not e3.conflicted, "a timeout is not a conflict"
assert e3.is_dirty and not any(wr[2] == 3 for wr in fake.writes), "held back, not pushed"
assert any(wr[2] == 10 for wr in fake.writes), "a dead slot never blocks another slot's edit"
assert not w._index.get(OBJ_PROGRAM, 0x00, 10).is_dirty
assert w._index.incomplete_banks == {"0:0": {3: 1}}, w._index.incomplete_banks
assert "unread slot" in w._lbl_warning.text() and "slot 3" in w._lbl_warning.text(), w._lbl_warning.text()
assert "NOT pushed" in w._status_label.text(), w._status_label.text()
assert not w._brd_incomplete.isHidden() and "Program I-A slot 3" in w._lbl_incomplete.text()
assert w._brd_conflict.isHidden(), "no conflict banner for a timeout"
assert not answers, "no prompt without force destructive write"
print("dead slot held back, other slot pushed ok:", w._status_label.text())

# restart, OLD index format ({bank: [slots]}): loads as one failed attempt, banner shown straight away
import json
idx_file = w._index.root / "index.json"
doc = json.loads(idx_file.read_text(encoding="utf-8"))
assert doc["incomplete_banks"] == {"0:0": {"3": 1}}, doc["incomplete_banks"]   # new format on disk
doc["incomplete_banks"] = {"0:0": [3]}
idx_file.write_text(json.dumps(doc), encoding="utf-8")
w2, _, _ = make(fake=fake)
w2._refresh_local_tree()
assert w2._index.incomplete_banks == {"0:0": {3: 1}} and not w2._brd_incomplete.isHidden()
assert wait(lambda: not w2._probe_running)
w2._closed = True; w2.close()
w._index.save()
assert LocalLibraryIndex(w._index.root).load().incomplete_banks == {"0:0": {3: 1}}
print("record survives restart ok (old and new formats)")

# Resolve Conflicts: the readable slot (11) is resolved and the bank re-baselined; the unread slot (3)
# keeps its conflict, and its record is untouched
mark_dirty(w, 11)                           # baseline back to v1: the bank looks moved (v10)
w._index.mark_conflicted(OBJ_PROGRAM, 0x00, 11, True)
w._index.mark_conflicted(OBJ_PROGRAM, 0x00, 3, True)
answers.clear()
QMessageBox.warning = staticmethod(lambda *a, **k: (answers.append(a[1]) or QMessageBox.StandardButton.Yes))
w._resolve_conflicts_keep_mine(); assert wait(lambda: not w._busy, 30)
assert w._index.bank_digest_baseline.get("0:0") == hashlib.sha1(b"0:0:10").digest().hex()
assert not w._index.get(OBJ_PROGRAM, 0x00, 11).conflicted
assert w._index.get(OBJ_PROGRAM, 0x00, 3).conflicted, "an unread slot's conflict stays"
assert "not answered" in w._lbl_warning.text(), w._lbl_warning.text()
assert "Resolve Conflicts" in answers
assert w._index.incomplete_banks == {"0:0": {3: 1}}
w._index.mark_conflicted(OBJ_PROGRAM, 0x00, 3, False)
print("resolve: readable slots only ok")

# force destructive write: the readable slot (11) goes out; the dead slot needs its own confirm. Yes tries, and the
# push is refused because a dead slot cannot be backed up first. The next sync is the 3rd consecutive failure: the slot
# becomes unreadable, and from then on it is NOT asked about again (the write could never succeed).
s.librarian_force_destructive_write = True
answers.clear(); fake.writes.clear()
QMessageBox.warning = staticmethod(lambda *a, **k: (answers.append(a[1]) or QMessageBox.StandardButton.Yes))
w._start_sync(); assert wait(lambda: not w._busy, 60)
pump(300); assert wait(lambda: not w._busy, 60)
assert answers == ["Write into slots that could not be read?"], answers
assert any(wr[2] == 11 for wr in fake.writes) and not any(wr[2] == 3 for wr in fake.writes)
assert w._index.incomplete_banks == {"0:0": {3: 2}}, w._index.incomplete_banks
assert w._index.get(OBJ_PROGRAM, 0x00, 3).is_dirty
assert "Cannot back up" in w._lbl_warning.text(), w._lbl_warning.text()
answers.clear(); fake.writes.clear()
w._start_sync(); assert wait(lambda: not w._busy, 60)
pump(300); assert wait(lambda: not w._busy, 60)
assert answers == [], answers                      # already unreadable: no confirm on every sync
assert not any(wr[2] == 3 for wr in fake.writes) and w._index.get(OBJ_PROGRAM, 0x00, 3).is_dirty
assert w._index.incomplete_banks == {"0:0": {3: 3}} and w._index.unreadable_slots("0:0") == [3]
s.librarian_force_destructive_write = False
print("force destructive asks, and a dead slot cannot be backed up ok")

# unreadable: quiet - not asked for on a lazy Sync, no 'did not answer' warning, a standing line + Re-check
answers.clear(); fake.dumps.clear()
QMessageBox.warning = staticmethod(lambda *a, **k: (answers.append(a[1]) or QMessageBox.StandardButton.No))
w._start_sync(); assert wait(lambda: not w._busy, 60)
assert fake.served_dumps() == [], fake.served_dumps()
assert "did not answer" not in w._lbl_warning.text() and "did not answer" not in w._status_label.text()
assert "unreadable slot" in w._lbl_warning.text(), "the held edit still explains itself"
assert "Unreadable: Program I-A slot 3" in w._lbl_incomplete.text(), w._lbl_incomplete.text()
assert not w._btn_recheck_unread.isHidden() and w._btn_recheck_unread.isEnabled()
assert w._act_recheck_unread.isEnabled()
assert not answers
print("unreadable is quiet ok")

# the bank's digest changes: the whole bank is read, which retries the unreadable slot (still dead: quiet)
fake.digest_ver = 11
fake.dumps.clear()
w._start_sync(); assert wait(lambda: not w._busy, 60)
assert (OBJ_PROGRAM, 0x00, 3) in fake.served_dumps() and len(fake.served_dumps()) > 100
assert "did not answer" not in w._lbl_warning.text()
assert w._index.incomplete_banks == {"0:0": {3: 4}}
print("digest change retries the unreadable slot ok")

# Re-check unreadable slots: asks for that slot only; it answers now; the held edit then goes out
fake.timeouts = set(); fake.dumps.clear()
w._start_recheck_unread(); assert wait(lambda: not w._busy, 60)
assert fake.served_dumps() == [(OBJ_PROGRAM, 0x00, 3)], fake.served_dumps()
assert w._index.incomplete_banks == {}
assert "Re-checked 1 unread slot(s): 1 answered." in w._status_label.text(), w._status_label.text()
assert w._brd_incomplete.isHidden() and w._index.get(OBJ_PROGRAM, 0x00, 3).is_dirty
fake.writes.clear()
w._start_sync(); assert wait(lambda: not w._busy, 60)
assert any(wr[2] == 3 for wr in fake.writes) and not w._index.get(OBJ_PROGRAM, 0x00, 3).conflicted
assert not answers
print("re-check recovers the slot, edit pushed ok:", w._status_label.text())

# _get_bank_objects: a cancel part-way reports the never-asked slots as skipped (not failed); a subset
# fetch asks only those slots, with one retry for a dead one
real_dump = fake.dump_object_parsed


def dump_then_cancel(obj, bank, number, no_response_ms=3000):
    if number == 5:
        w._sync_cancel.set()
    return real_dump(obj, bank, number, no_response_ms)


fake.dump_object_parsed = dump_then_cancel
r = w._get_bank_objects(OBJ_PROGRAM, 0x00)
assert sorted(r) == list(range(6)) and r.missing == () and r.skipped == tuple(range(6, 128)), (r.missing, r.skipped)
w._sync_cancel.clear()
del fake.dump_object_parsed
fake.timeouts = {(OBJ_PROGRAM, 0x00, 7)}; fake.dumps.clear()
r = w._get_bank_objects(OBJ_PROGRAM, 0x00, slots=[2, 7])
assert sorted(r) == [2] and r.missing == (7,) and fake.dumps == [(0, 0, 2), (0, 0, 7), (0, 0, 7)], fake.dumps
fake.timeouts = set()
print("bank fetch cancel/subset ok")

# ---- 8. silent instrument mid-session: pull aborts, banner raised, nothing marked complete
fake.digest_ok = False
w._set_sync_mode(L.SYNC_TWO_WAY)
w._sysex_unavailable = False; w._refresh_enable()
w._start_sync(); assert wait(lambda: not w._busy, 90)
assert w._sysex_unavailable and not w._brd_sysex.isHidden()
print("silent abort ok")
w._closed = True
print("ALL PASS")
