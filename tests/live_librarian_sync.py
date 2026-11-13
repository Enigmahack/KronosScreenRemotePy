"""Live: the real LibrarianShellWindow (headless, throwaway KRONOS_DATA_DIR) against a real instrument.
The pull is restricted (library_pull_pipeline.all_banks) to two Program banks: one populated READ bank and the
designated SCRATCH bank.
  (a) Pull Only (full): completes, no unread slots (index.incomplete_banks == {}), baselines + digests recorded.
  (b) a second Pull Only (lazy): ~0 objects fetched.
  (c) WRITES ONE slot: rename the Program in SCRATCH:slot locally, 2-Way Sync -> exactly that object written + its
      bank Stored, re-read identical; then restore_from_backup of the Sync's own pre-image backup, re-read equal to
      the original. An extra backup of the slot is taken (and verified) by this script before (c).
usage: live_librarian_sync.py <host> [--nautilus] [--read-bank 0x00] [--scratch-bank 0x4D] [--slot 0]"""
import argparse, dataclasses, os, sys, tempfile, time, pathlib
os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_livelib_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtWidgets import QApplication, QMessageBox
app = QApplication([])
ap = argparse.ArgumentParser()
ap.add_argument("host"); ap.add_argument("--nautilus", action="store_true")
ap.add_argument("--read-bank", type=lambda x: int(x, 0), default=0x00)
ap.add_argument("--scratch-bank", type=lambda x: int(x, 0), default=0x4D)
ap.add_argument("--slot", type=int, default=0)
a = ap.parse_args()

import Data.library_pull_pipeline as lpp
import Data.librarian_sysex as lsx
import Data.nautilus_sysex as nsx
from Core.sysex_service import SysExService
from Conversion.push import _verify_backup, restore_from_backup
from Data.librarian_sysex import OBJ_PROGRAM
from Data.local_library_store import LocalLibraryIndex
from Models.app_settings import AppSettings
import Views.librarian_shell_window as L

BANKS = [lpp.BankRef(OBJ_PROGRAM, a.read_bank), lpp.BankRef(OBJ_PROGRAM, a.scratch_bank)]
lpp.all_banks = lambda: list(BANKS)

def pump_until(cond, timeout):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False

svc = SysExService(); svc.start(a.host)
assert pump_until(lambda: svc.can_dump, 12), "bridge"
svc.set_device_family(a.nautilus)
calls, backups = [], []
for name in ("write_object", "store_bank", "dump_object_parsed", "bank_digest", "backup_objects"):
    orig = getattr(svc, name)
    def mk(orig, name):
        def f(*args, **kw):
            r = orig(*args, **kw)
            if name == "write_object":
                calls.append((name, args[0].obj, args[0].bank, args[0].index, r))
            elif name == "backup_objects":
                backups.append(args[1]); calls.append((name, len(args[0]), args[1]))
            else:
                calls.append((name,) + tuple(args[:3]) + ((None if r is None else "ok") if name != "store_bank" else r,))
            return r
        return f
    setattr(svc, name, mk(orig, name))

prompts = []
def auto(*args, **kw):
    prompts.append(args[1] if len(args) > 1 else "?")
    return QMessageBox.StandardButton.Yes
for n in ("warning", "question", "information", "critical"):
    setattr(QMessageBox, n, staticmethod(auto))

s = AppSettings()
s.librarian_full_sync_on_launch = False
w = L.LibrarianShellWindow(a.host, svc, parent=None, settings=s)
assert pump_until(lambda: not w._probe_running, 30), "probe"
print("probe: sysex_unavailable =", w._sysex_unavailable, "| family", w._device_family())
fetched = []
orig_gbo = w._get_bank_objects
def gbo(obj_type, bank, slots=None):
    r = orig_gbo(obj_type, bank, slots); fetched.append((obj_type, bank, slots, r)); return r
w._get_bank_objects = gbo
res = {}

def run(mode, label, timeout=900):
    w._set_sync_mode(mode)
    n0 = len(calls); f0 = len(fetched); t0 = time.time()
    w._start_sync()
    ok = pump_until(lambda: not w._busy, timeout)
    dt = time.time() - t0
    objs = sum(len(getattr(r, "objects", r) if not isinstance(r, tuple) else r[0]) for *_, r in fetched[f0:])
    print(f"[{label}] done={ok} in {dt:.1f}s; status: {w._status_label.text()!r}; banks fetched: "
          f"{[(hex(b), 'all' if sl is None else sl) for _, b, sl, _ in fetched[f0:]]}; objects fetched: {objs}; "
          f"dump requests: {sum(1 for c in calls[n0:] if c[0] == 'dump_object_parsed')}")
    print(f"    incomplete_banks={w._index.incomplete_banks} baselines="
          f"{ {k: v[:12] for k, v in w._index.bank_digest_baseline.items()} } warning={w._lbl_warning.text()[:300]!r}")
    return ok, dt, objs, calls[n0:]

# (a) full Pull Only
w._chk_force_full.setChecked(True)
ok, dt, objs, c = run(L.SYNC_PULL_ONLY, "a: full pull")
keys = [LocalLibraryIndex.bank_key(OBJ_PROGRAM, b.bank) for b in BANKS]
n_entries = {k: sum(1 for e in w._index.entries if e.startswith(k + ":")) for k in keys}
print("    entries per bank:", n_entries)
res["a_pull"] = (ok and w._index.incomplete_banks == {} and all(k in w._index.bank_digest_baseline for k in keys)
                 and all(v == 128 for v in n_entries.values()) and not any(x[0] == "write_object" for x in c))
# (b) lazy Pull Only
w._chk_force_full.setChecked(False)
ok, dt, objs, c = run(L.SYNC_PULL_ONLY, "b: lazy pull")
res["b_lazy"] = ok and objs == 0 and not any(x[0] == "write_object" for x in c)

# (c) edit ONE Program in the scratch slot, 2-Way Sync
codec = nsx if a.nautilus else lsx
entry = w._index.get(OBJ_PROGRAM, a.scratch_bank, a.slot)
orig_body = w._blobs.get(entry.current_hash)
live = svc.dump_object_parsed(OBJ_PROGRAM, a.scratch_bank, a.slot, no_response_ms=6000)
assert live is not None and bytes(live.body) == orig_body, "library baseline != live slot"
print("scratch slot holds", bytes(orig_body[:24]).split(b"\0")[0])
mine = str(pathlib.Path(__file__).resolve().parent.parent / "librarian_backups" /
           f"{time.strftime('%Y%m%d_%H%M%S')}_librarian_sync_live_own.syx")
pre = dataclasses.make_dataclass("Op", ["obj", "bank", "index", "version", "body"])(
    OBJ_PROGRAM, a.scratch_bank, a.slot, live.version, bytes(live.body))
svc.backup_objects([pre], mine)
assert _verify_backup(mine, [pre], codec=codec) is None, "own backup not verified"
print("own backup verified:", mine)
new_body = bytearray(orig_body); new_body[:24] = b"CLAUDE SYNC TEST".ljust(24, b" ")
new_body = bytes(new_body)
nh = w._blobs.put(new_body)
w._index.set_entry(OBJ_PROGRAM, a.scratch_bank, a.slot, dataclasses.replace(entry, current_hash=nh, display_name="CLAUDE SYNC TEST"))
w._index.save()
nb = len(backups)
try:
    ok, dt, objs, c = run(L.SYNC_TWO_WAY, "c: 2-way sync with one edit")
    writes = [x for x in c if x[0] == "write_object"]
    stores = [x for x in c if x[0] == "store_bank"]
    print("    writes:", writes, "stores:", stores, "sync backup:", backups[nb:])
    back = svc.dump_object_parsed(OBJ_PROGRAM, a.scratch_bank, a.slot, no_response_ms=6000)
    same = back is not None and bytes(back.body) == new_body
    print("    re-read", "IDENTICAL to the edit" if same else "DIFFERS")
    res["c_push_one"] = (ok and writes == [("write_object", OBJ_PROGRAM, a.scratch_bank, a.slot, 0)]
                         and [(x[1], x[2]) for x in stores] == [(OBJ_PROGRAM, a.scratch_bank)] and stores[0][-1] == 0
                         and same and not w._index.get(OBJ_PROGRAM, a.scratch_bank, a.slot).is_dirty)
finally:
    path = backups[nb] if len(backups) > nb else mine
    with svc.hold_dump_gate():
        rr = restore_from_backup(svc, path)
    back = svc.dump_object_parsed(OBJ_PROGRAM, a.scratch_bank, a.slot, no_response_ms=6000)
    restored = rr.ok and back is not None and bytes(back.body) == orig_body
    print("RESTORE from", path, rr.ok, rr.messages, "| slot == original:", restored)
    if not restored and path != mine:
        with svc.hold_dump_gate():
            rr = restore_from_backup(svc, mine)
        back = svc.dump_object_parsed(OBJ_PROGRAM, a.scratch_bank, a.slot, no_response_ms=6000)
        restored = rr.ok and back is not None and bytes(back.body) == orig_body
        print("RESTORE (own backup)", rr.ok, rr.messages, "| slot == original:", restored)
    res["restored"] = restored
    print("prompts answered Yes:", prompts)
    print("RESULT", res)
    w._closed = True
    try:
        w.close()
    except Exception as ex:
        print("close:", ex)
    svc.stop()
