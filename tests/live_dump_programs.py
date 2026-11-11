"""Live, READ-ONLY: dump every Program of the given banks to <out dir>/bank_<hex>.bin (bodies concatenated in slot order,
4954 bytes each on a Nautilus EXi bank) and print slot names. Writes nothing to the unit.
usage: live_dump_programs.py <host> <out dir> <bank hex> [<bank hex> ...] [--slots N]"""
import os
import sys
import tempfile
import time

os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_livedump_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtCore import QCoreApplication

app = QCoreApplication([])
from Core.sysex_service import SysExService
from Data.librarian_sysex import OBJ_PROGRAM

args = [a for a in sys.argv[1:] if not a.startswith("--")]
host, out_dir, banks = args[0], args[1], [int(b, 16) for b in args[2:]]
slots = int(sys.argv[sys.argv.index("--slots") + 1]) if "--slots" in sys.argv else 128
if "--slots" in sys.argv:
    banks = [b for b in banks if b != slots]
os.makedirs(out_dir, exist_ok=True)
svc = SysExService()
svc.start(host)
t = time.time()
while time.time() - t < 12 and not svc.can_dump:
    app.processEvents(); time.sleep(0.05)
if not svc.can_dump:
    print("MIDI bridge not reachable on", host); sys.exit(2)
svc.set_device_family(True)
for bank in banks:
    chunks, names, missing = [], [], 0
    for slot in range(slots):
        d = svc.dump_object_parsed(OBJ_PROGRAM, bank, slot, no_response_ms=6000)
        if d is None:
            missing += 1; chunks.append(b""); names.append("<no reply>"); continue
        chunks.append(bytes(d.body)); names.append(bytes(d.body[:24]).split(b"\0")[0].decode("latin-1"))
    sizes = sorted({len(c) for c in chunks if c})
    with open(os.path.join(out_dir, f"bank_{bank:02X}.bin"), "wb") as f:
        for c in chunks:
            f.write(c if len(c) == 4954 else bytes(4954))
    print(f"bank 0x{bank:02X}: {slots - missing}/{slots} slots, body sizes {sizes}")
    for i in range(0, slots, 8):
        print("  %3d  " % i + " | ".join(n[:10].ljust(10) for n in names[i:i + 8]))
svc.stop()
