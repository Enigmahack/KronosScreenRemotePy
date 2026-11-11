"""Live, READ-ONLY: probe Song Timbre Set (obj 0x02) on a unit: dump slots, digest, sizes.
usage: live_song_probe.py <host> [--nautilus] [--save DIR]"""
import os, sys, tempfile, time
os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_song_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtCore import QCoreApplication
app = QCoreApplication([])
from Core.sysex_service import SysExService
host = sys.argv[1]; naut = "--nautilus" in sys.argv
save = sys.argv[sys.argv.index("--save") + 1] if "--save" in sys.argv else None
svc = SysExService(); svc.start(host)
t = time.time()
while time.time() - t < 12 and not svc.can_dump:
    app.processEvents(); time.sleep(0.05)
assert svc.can_dump
svc.set_device_family(naut)
for bank in (0, 1, 0x40):
    print("digest obj 2 bank", bank, svc.bank_digest(2, bank, timeout_s=4))
for slot in (0, 1, 2, 3, 99, 100, 127, 128, 199):
    d = svc.dump_object_parsed(2, 0, slot, no_response_ms=4000)
    if d is None: print("slot", slot, "no reply"); continue
    print("slot", slot, "size", len(d.body), "version", d.version, "name", bytes(d.body[:24]).split(b"\0")[0])
    if save:
        os.makedirs(save, exist_ok=True); open(os.path.join(save, f"song_{slot:03d}.bin"), "wb").write(bytes(d.body))
svc.stop()
