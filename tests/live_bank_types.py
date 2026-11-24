"""Live, read-only: func 0x60 Program Bank Types vs each bank's slot-0 Object Dump length.
usage: live_bank_types.py <host> [--nautilus]   (the MIDI bridge needs no login)"""
import os, sys, tempfile, time
host = sys.argv[1]; naut = "--nautilus" in sys.argv
os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_livebt_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtCore import QCoreApplication
app = QCoreApplication([])
from Core.sysex_service import SysExService
from Data.pcg_file import program_body_kind
from Core.kronos_sysex import program_label
svc = SysExService(); svc.set_device_family(naut); svc.start(host)
end = time.time() + 10
while time.time() < end and not (svc.bridge and svc.bridge.is_connected):
    app.processEvents(); time.sleep(0.05)
assert svc.bridge and svc.bridge.is_connected, "bridge did not connect"
types = svc.program_bank_types(timeout_s=8.0)
print("0x61 map:", None if types is None else {f"{program_label(b)}(0x{b:02X})": ("EXi" if v else "HD-1") for b, v in sorted(types.items())})
bad = 0
for b in sorted(types or {}):
    d = svc.dump_object_parsed(0, b, 0, no_response_ms=4000)
    kind = None if d is None else program_body_kind(len(d.body))
    ok = kind is not None and kind == types[b]
    bad += not ok
    print(f"  {program_label(b):<5} 0x{b:02X}  0x61={'EXi' if types[b] else 'HD-1'}  slot0 len={None if d is None else len(d.body)} -> "
          f"{'EXi' if kind else 'HD-1' if kind is False else '?'}  {'OK' if ok else 'MISMATCH'}")
print("cached:", len(svc.cached_bank_types()), "banks;", "MISMATCHES:", bad)
svc.stop(); svc.flush()   # exit fence: let the retired bridge thread end before the interpreter does
sys.exit(1 if types is None or bad else 0)
