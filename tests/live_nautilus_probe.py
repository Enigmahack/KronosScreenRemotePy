"""Live, READ-ONLY: dump a few Nautilus Program slots (size/name). usage: live_nautilus_probe.py <host> [bank slot ...]"""
import os, sys, tempfile, time
os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_livenp_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtCore import QCoreApplication
app = QCoreApplication([])
from Core.sysex_service import SysExService
from Data.librarian_sysex import OBJ_PROGRAM

host = sys.argv[1]
svc = SysExService(); svc.start(host)
t = time.time()
while time.time() - t < 12 and not svc.can_dump:
    app.processEvents(); time.sleep(0.05)
assert svc.can_dump, "bridge"
svc.set_device_family(True)
slots = [(int(sys.argv[4], 0) if len(sys.argv) > 4 else 0x40, i) for i in range(int(sys.argv[2]), int(sys.argv[3]))] if len(sys.argv) > 3 else [(0x00, 0), (0x40, 0)]
for bank, idx in slots:
    d = svc.dump_object_parsed(OBJ_PROGRAM, bank, idx, no_response_ms=4000)
    if d is None:
        print(f"bank 0x{bank:02X} slot {idx}: no reply")
    else:
        print(f"bank 0x{bank:02X} slot {idx}: v{d.version} {len(d.body)} bytes name={d.body[:24].split(bytes(1))[0].decode('ascii', 'replace')!r}")
svc.stop()
