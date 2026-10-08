"""Live, READ-ONLY: user-bank slot bounds + a real pull of every Drum Kit / Wave Sequence bank into a
THROWAWAY library (KRONOS_DATA_DIR temp dir). usage: live_drum_wave_pull.py <host> --nautilus"""
import os, sys, tempfile, time
os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_livedwp_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtCore import QCoreApplication
app = QCoreApplication([])
from Core.sysex_service import SysExService
from Data.librarian_sysex import OBJ_DRUM_KIT, OBJ_WAVE_SEQ
import Data.object_types as ot
import Data.library_pull_pipeline as lpp
from Data.local_library_store import BlobStore, LocalLibraryIndex
import pathlib

host = sys.argv[1]
svc = SysExService(); svc.start(host)
t = time.time()
while time.time() - t < 12 and not svc.can_dump:
    app.processEvents(); time.sleep(0.05)
assert svc.can_dump, "bridge"
if "--nautilus" in sys.argv:
    svc.set_device_family(True)

for obj_type in (OBJ_DRUM_KIT, OBJ_WAVE_SEQ):
    n = ot.slot_count(obj_type, 0x40)
    last = svc.dump_object_parsed(obj_type, 0x4D, n - 1, no_response_ms=8000)
    over = svc.dump_object_parsed(obj_type, 0x4D, n, no_response_ms=3000)
    print(f"{ot.get(obj_type).display_name} U-GG: slot {n-1} {'replies' if last else 'NO REPLY (count too big?)'}; "
          f"slot {n} {'REPLIES (count too small?)' if over else 'no reply (expected)'}")
gm = [svc.dump_object_parsed(OBJ_DRUM_KIT, 0x10, i, no_response_ms=3000) is not None for i in range(12)]
print("GM drum kit slots that reply (first 12):", [i for i, ok in enumerate(gm) if ok])

root = pathlib.Path(os.environ["KRONOS_DATA_DIR"]) / "lib"
blobs = BlobStore(root)
index = LocalLibraryIndex(root)
only = {OBJ_DRUM_KIT, OBJ_WAVE_SEQ}
orig = lpp.all_banks
lpp.all_banks = lambda: [b for b in orig() if b.obj_type in only]
def live_digest(key):
    d = svc.bank_digest(*(int(x) for x in key.split(":")))
    return d.hex() if d is not None else None
def get_objs(obj_type, bank):
    out = {}
    for n in range(ot.slot_count(obj_type, bank)):
        d = svc.dump_object_parsed(obj_type, bank, n, no_response_ms=8000 if obj_type == OBJ_DRUM_KIT else 4000)
        if d is not None:
            out[n] = (d.version, d.body)
    return out
t0 = time.time()
res = lpp.pull(index, blobs, live_digest, get_objs, full=True, progress=lambda m: None)
print(f"pull: banks={res.banks_checked} objects={res.objects_fetched} conflicts={res.conflicts} aborted={res.aborted} in {time.time()-t0:.0f}s")
by = {t: 0 for t in only}
for k in index.entries:
    by[int(k.split(":")[0])] += 1
print("indexed:", {ot.get(t).plural_name: n for t, n in by.items()}, "| expected", {ot.get(t).plural_name: sum(ot.slot_count(t, b) for b in ot.get(t).editable_banks) for t in only})
svc.stop()
