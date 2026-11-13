"""Live, READ-ONLY check of Set List dumps and the Set List converter against a real unit. Writes nothing.
usage: live_setlist.py <host> [--nautilus] [--number N]
Reports the body size, the slot layout, whether each referenced Program exists on the unit (resolved through the
func-33 accessors), and converts the Set List to the other family and back."""
import os
import sys
import tempfile
import time

os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_livesl_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtCore import QCoreApplication

app = QCoreApplication([])
from Conversion.setlist import convert_setlist
from Core.sysex_service import SysExService
from Data.librarian_sysex import (OBJ_PROGRAM, OBJ_SET_LIST, func33_to_obj_bank, setlist_slot_base, setlist_slot_ref,
                                  setlist_stride)
import Core.kronos_sysex as ksx

host = sys.argv[1]
nautilus = "--nautilus" in sys.argv
number = int(sys.argv[sys.argv.index("--number") + 1]) if "--number" in sys.argv else 0
svc = SysExService()
svc.start(host)
t = time.time()
while time.time() - t < 12 and not svc.can_dump:
    app.processEvents(); time.sleep(0.05)
if not svc.can_dump:
    print("MIDI bridge not reachable on", host)
    sys.exit(2)
if nautilus:
    svc.set_device_family(True)
d = svc.dump_object_parsed(OBJ_SET_LIST, 0, number, no_response_ms=15000)
if d is None:
    print("no Set List reply")
    sys.exit(2)
b = d.body
print(f"Set List {number}: version={d.version} len={len(b)} (Kronos 69416 / Nautilus 69544) stride={setlist_stride(b)} name={ksx._ascii_trim(b, 0, 24)!r}")
shown = ok = bad = 0
for s in range(128):
    base = setlist_slot_base(b, s)
    nm = ksx._ascii_trim(b, base, 24)
    if not nm.strip():
        continue
    typ, bank, idx = setlist_slot_ref(b, s)
    line = f"  slot {s + 1:3d} {nm!r:28} type={typ} func33 bank={bank} idx={idx}"
    if typ == 1 and shown < 12:
        ob_bank = func33_to_obj_bank(1, bank)
        p = svc.dump_object_parsed(OBJ_PROGRAM, ob_bank, idx, no_response_ms=8000) if ob_bank >= 0 else None
        pn = ksx._ascii_trim(p.body, 0, 24) if p is not None else None
        line += f"  -> Program bank 0x{ob_bank:02X} #{idx}: {pn!r}"
        shown += 1
    print(line)
    if shown >= 12 and typ == 1:
        pass
other = convert_setlist(b, not nautilus)
print("convert:", "refused " + "; ".join(other.refusals) if other.body is None else f"{len(other.body)} bytes")
for w in other.warnings[:6]:
    print("  warn:", w[:200])
if other.body is not None:
    back = convert_setlist(other.body, nautilus)
    print("round trip identical:", back.body == b)
svc.stop()
