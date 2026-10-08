"""Live WRITE round trip on a real unit, on ONE Init slot per type, with automatic restore.
usage: live_drum_wave_roundtrip.py <host> --nautilus --yes
Backs up the slot to a .syx first; always restores in a finally and verifies byte-for-byte."""
import os, sys, tempfile, time, pathlib
os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_liverw_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtCore import QCoreApplication
app = QCoreApplication([])
from Core.sysex_service import SysExService
from Data.librarian_model import WriteOp
from Data.librarian_sysex import OBJ_DRUM_KIT, OBJ_WAVE_SEQ
import Data.object_types as ot
import Objects.object_body as ob
import Core.kronos_sysex as ksx
assert "--yes" in sys.argv
svc = SysExService(); svc.start(sys.argv[1])
t = time.time()
while time.time() - t < 12 and not svc.can_dump:
    app.processEvents(); time.sleep(0.05)
assert svc.can_dump
if "--nautilus" in sys.argv:
    svc.set_device_family(True)
backup_dir = pathlib.Path(__file__).resolve().parent.parent / "librarian_backups"
backup_dir.mkdir(exist_ok=True)

def find_init(obj_type):
    for bank in reversed(ot.get(obj_type).editable_banks):
        if bank == 0:
            continue
        for slot in range(ot.slot_count(obj_type, bank) - 1, -1, -1):
            d = svc.dump_object_parsed(obj_type, bank, slot, no_response_ms=8000)
            if d is not None and ob.is_init(obj_type, d.body):
                return bank, slot, d
    return None

for obj_type in (OBJ_WAVE_SEQ, OBJ_DRUM_KIT):
    name = ot.get(obj_type).display_name
    found = find_init(obj_type)
    if not found:
        print(f"{name}: no Init slot in a user bank - skipped"); continue
    bank, slot, orig = found
    label = f"{ot.label_for(obj_type, bank)}:{slot:03d}"
    pre = WriteOp(obj_type, bank, slot, orig.version, orig.body)
    bpath = backup_dir / f"roundtrip_{name.replace(' ','')}_{label.replace(':','_')}.syx"
    svc.backup_objects([pre], str(bpath))
    print(f"{name}: using Init slot {label} (v{orig.version}, {len(orig.body)} B); backup {bpath.name}")
    marker = ob.write_wave_seq_name(orig.body, "ZZ RoundTrip Test") if obj_type == OBJ_WAVE_SEQ \
        else ob.write_drum_kit_name(orig.body, "ZZ RoundTrip Test")
    try:
        r = svc.write_object(WriteOp(obj_type, bank, slot, orig.version, marker))
        s = svc.store_bank(obj_type, bank)
        print(f"  write reply={r} store reply={s}")
        assert r == 0 and s == 0, "write/store refused"
        back = svc.dump_object_parsed(obj_type, bank, slot, no_response_ms=8000)
        ok = back is not None and back.body == marker
        print(f"  re-read after write: {'IDENTICAL to what was written' if ok else 'MISMATCH'} "
              f"(name {ksx._ascii_trim(back.body,0,24)!r})")
        assert ok
    finally:
        r = svc.write_object(pre); s = svc.store_bank(obj_type, bank)
        back = svc.dump_object_parsed(obj_type, bank, slot, no_response_ms=8000)
        same = back is not None and back.body == orig.body and back.version == orig.version
        print(f"  RESTORE write={r} store={s}; slot byte-identical to original: {same}")
        assert same, "RESTORE FAILED - use the backup .syx"
svc.stop()
