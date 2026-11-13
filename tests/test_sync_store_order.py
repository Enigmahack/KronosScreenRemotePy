"""Sync persists referenced objects before their referrers: Wave Sequences / Drum Kits, then Programs, then Combis,
then Set Lists. A failed Store stops the sweep, so a Combi bank is never Stored while a Program bank it depends on was
not (the Combi would point at whatever the Program slot still holds)."""
import os, sys, tempfile, pathlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("KRONOS_DATA_DIR", tempfile.mkdtemp(prefix="kr_data_"))

from Data.changeset_sync import ChangesetEntry, ChangesetPlan, execute_changeset, store_order
from Data.librarian_sysex import OBJ_COMBI, OBJ_DRUM_KIT, OBJ_PROGRAM, OBJ_SET_LIST, OBJ_WAVE_SEQ
from Data.local_library_store import LocalLibraryIndex

fails = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


check("store_order ranks dependencies first",
      store_order([(OBJ_SET_LIST, 0), (OBJ_COMBI, 0x40), (OBJ_PROGRAM, 0x4C), (OBJ_DRUM_KIT, 0), (OBJ_WAVE_SEQ, 0)])
      == [(OBJ_WAVE_SEQ, 0), (OBJ_DRUM_KIT, 0), (OBJ_PROGRAM, 0x4C), (OBJ_COMBI, 0x40), (OBJ_SET_LIST, 0)])


def plan_of():
    p = ChangesetPlan()
    # entries deliberately in referrer-first order
    for t, b, n in ((OBJ_COMBI, 0x40, 0), (OBJ_PROGRAM, 0x4C, 0), (OBJ_PROGRAM, 0x4C, 1)):
        p.entries.append(ChangesetEntry(f"{t}:{b}:{n}", t, b, n, b"x", 1))
    return p


def run(fail_store_of=None):
    idx = LocalLibraryIndex(pathlib.Path(tempfile.mkdtemp(prefix="kr_idx_")))
    log, recorded = [], []

    def write(t, b, n, body):
        log.append(("write", t, b, n))
        return True

    def store(t, b):
        log.append(("store", t, b))
        return (t, b) != fail_store_of

    res = execute_changeset(plan_of(), idx, write, record_success=lambda e: recorded.append(e.key), store_bank=store)
    return log, recorded, res


log, recorded, res = run()
stores = [e for e in log if e[0] == "store"]
check("stores go Program bank before Combi bank", stores == [("store", OBJ_PROGRAM, 0x4C), ("store", OBJ_COMBI, 0x40)])
check("all three recorded when every Store succeeds", len(recorded) == 3 and res.failed == 0)

log, recorded, res = run(fail_store_of=(OBJ_PROGRAM, 0x4C))
stores = [e for e in log if e[0] == "store"]
check("Program Store failed -> the Combi bank is never Stored", stores == [("store", OBJ_PROGRAM, 0x4C)])
check("nothing is recorded as committed", recorded == [] and res.failed > 0)

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
