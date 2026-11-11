"""Live, READ-ONLY: scan every Program of the unit for a hex byte pattern; print hits (bank, slot, name, offsets).
usage: live_find_bytes.py <host> [--nautilus] <hexpattern>"""
import os, sys, tempfile, time
os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_find_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtCore import QCoreApplication
app = QCoreApplication([])
from Core.sysex_service import SysExService
from Data.librarian_sysex import OBJ_PROGRAM
args = [a for a in sys.argv[1:] if not a.startswith("--")]
host, pat = args[0], bytes.fromhex(args[1])
svc = SysExService(); svc.start(host)
t = time.time()
while time.time() - t < 12 and not svc.can_dump:
    app.processEvents(); time.sleep(0.05)
assert svc.can_dump; svc.set_device_family("--nautilus" in sys.argv)
n = 0
for bank in list(range(0, 6)) + list(range(0x40, 0x4E)):
    for slot in range(128):
        d = svc.dump_object_parsed(OBJ_PROGRAM, bank, slot, no_response_ms=3000)
        if d is None: continue
        n += 1
        b = bytes(d.body); i = b.find(pat)
        while i >= 0:
            print(f"HIT bank {bank:#04x} slot {slot} {b[:24].split(bytes(1))[0]!r} @ {i}: {b[i-2:i+20].hex()}", flush=True)
            i = b.find(pat, i + 1)
    print("bank", hex(bank), "done", n, flush=True)
svc.stop()
