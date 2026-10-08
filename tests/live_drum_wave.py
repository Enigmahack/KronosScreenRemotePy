"""Live, READ-ONLY check of Drum Kit / Wave Sequence support against a real unit: bank digests, then
object dumps (func 0x72) of a few slots. Writes nothing.
usage: live_drum_wave.py <host> [--nautilus]"""
import os
import sys
import tempfile
import time

os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_livedw_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtCore import QCoreApplication

app = QCoreApplication([])
from Core.sysex_service import SysExService
from Data.librarian_sysex import OBJ_DRUM_KIT, OBJ_WAVE_SEQ
import Data.object_types as ot
import Objects.object_body as ob

host = sys.argv[1]
svc = SysExService()
svc.start(host)
t = time.time()
while time.time() - t < 12 and not svc.can_dump:
    app.processEvents(); time.sleep(0.05)
if not svc.can_dump:
    print("MIDI bridge not reachable on", host)
    sys.exit(2)
if "--nautilus" in sys.argv:
    svc.set_device_family(True)   # the Kronos-format probe never answers on a Nautilus
print("bridge up; nautilus framing:", "--nautilus" in sys.argv)

for obj_type, name in ((OBJ_DRUM_KIT, "Drum Kit"), (OBJ_WAVE_SEQ, "Wave Sequence")):
    d = ot.get(obj_type)
    answered = 0
    for bank in d.editable_banks:
        dig = svc.bank_digest(obj_type, bank)
        answered += dig is not None
    print(f"{name}: digest answered for {answered}/{len(d.editable_banks)} editable banks")
    for bank, slot in ((0, 0), (0, d.slot_count(0) - 1), (0x40, 0)):
        dump = svc.dump_object_parsed(obj_type, bank, slot, no_response_ms=8000)
        if dump is None:
            print(f"  {ot.label_for(obj_type, bank)}:{slot:03d} -> no reply")
            continue
        nm = ob._ascii_trim(dump.body, 0, 24) if hasattr(ob, "_ascii_trim") else dump.body[:24].decode("ascii", "replace")
        want = ob.DRUM_KIT_BODY_SIZE if obj_type == OBJ_DRUM_KIT else ob.WAVE_SEQ_BODY_SIZE
        print(f"  {ot.label_for(obj_type, bank)}:{slot:03d} v{dump.version} len={len(dump.body)} "
              f"(expect {want}: {'OK' if len(dump.body) == want else 'MISMATCH'}) name={nm!r}")
    # one past the last slot of the INT bank must not exist
    over = svc.dump_object_parsed(obj_type, 0, d.slot_count(0), no_response_ms=3000)
    print(f"  slot {d.slot_count(0)} (one past INT end) -> {'REPLIED (slot count too small?)' if over else 'no reply (expected)'}")
svc.stop()
