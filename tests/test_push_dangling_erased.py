"""A Set List that points at a Combi this same push erases: plain / accepted-dangling pushes still refuse, only the explicit overwrite retry
writes the Set List as laid out locally and reports a CHECK."""
import os, sys, tempfile, pathlib
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from Commands.session_dependency_clipboard import SessionDependencyClipboard
from Data.changeset_sync import build_changeset
from Data.librarian_sysex import OBJ_COMBI, OBJ_SET_LIST, set_setlist_slot_ref
from Objects.object_body import write_setlist_slot_name
from Data.local_library_store import BlobStore, LocalIndexEntry, LocalLibraryIndex

fails = []
def check(n, c):
    if not c: fails.append(n)

root = pathlib.Path(tempfile.mkdtemp())
blobs = BlobStore(root / "blobs")
now = lambda: datetime.now(timezone.utc).isoformat()
old_sl = bytearray(69416)
new_sl = bytearray(old_sl)
set_setlist_slot_ref(new_sl, 4, func33_bank=0, index=26, type_=0)       # slot 4 -> Combi 26
new_sl = bytearray(write_setlist_slot_name(bytes(new_sl), 4, 'Pad'))  # unnamed slots are not scanned
h_old, h_new = blobs.put(bytes(old_sl)), blobs.put(bytes(new_sl))
c_old, c_blank = blobs.put(b"COMBI-OLD"), blobs.put(b"COMBI-BLANK")

def make():
    idx = LocalLibraryIndex(root / f"i{len(fails)}{os.urandom(2).hex()}")
    idx.set_entry(OBJ_SET_LIST, 0, 2, LocalIndexEntry(version=1, baseline_hash=h_old, current_hash=h_new,
                  display_name="SL", created_utc=now(), modified_utc=now()))
    idx.set_entry(OBJ_COMBI, 0, 26, LocalIndexEntry(version=1, baseline_hash=c_old, current_hash=c_blank,
                  display_name="C26", created_utc=now(), modified_utc=now(), pending_delete=True))
    idx.set_bank_digest_baseline(OBJ_SET_LIST, 0, "d1")
    idx.set_bank_digest_baseline(OBJ_COMBI, 0, "d1")
    return idx

def plan(**kw):
    return build_changeset(make(), blobs, SessionDependencyClipboard(), get_live_digest=lambda bk: "d1",
                           resolver=lambda t, b, n: True, **kw)

check("plain-refuses", plan().is_refusable)
check("accepted-dangling-still-refuses-erased-target", plan(allow_dangling=True).is_refusable)
check("force-setting-alone-still-refuses", plan(force_destructive_write=True, allow_dangling=True).is_refusable)
p = plan(force_destructive_write=True, allow_dangling=True, allow_erased_dangling=True)
check("overwrite-retry-writes-the-set-list", not p.is_refusable and any(e.obj_type == OBJ_SET_LIST for e in p.entries))
check("overwrite-retry-reports-check", any(w.startswith("CHECK:") and "does not exist locally" in w for w in p.warnings))
print("FAILED:" if fails else "ALL PASS", fails or "")
sys.exit(1 if fails else 0)
