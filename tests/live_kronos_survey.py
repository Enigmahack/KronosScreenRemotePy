"""Live, READ-ONLY: for each Program/Combi bank of the Kronos print the body size of slot 0 and the names of slots 0,1,126,127.
usage: live_kronos_survey.py <host> [--nautilus]"""
import os, sys, tempfile, time
os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_survey_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtCore import QCoreApplication
app = QCoreApplication([])
from Core.sysex_service import SysExService
from Data.librarian_sysex import OBJ_PROGRAM, OBJ_COMBI
host = sys.argv[1]
svc = SysExService(); svc.start(host)
t = time.time()
while time.time() - t < 12 and not svc.can_dump:
    app.processEvents(); time.sleep(0.05)
assert svc.can_dump, "bridge"
svc.set_device_family("--nautilus" in sys.argv)
def nm(b): return bytes(b[:24]).split(b"\0")[0].decode("latin-1")
for obj, banks in ((OBJ_PROGRAM, list(range(0, 7)) + list(range(0x40, 0x4E))), (OBJ_COMBI, list(range(0, 3)) + list(range(0x40, 0x4E)))):
    for b in banks:
        row = []
        for s in (0, 1, 127):
            d = svc.dump_object_parsed(obj, b, s, no_response_ms=3000)
            row.append("none" if d is None else f"{len(d.body)}:{nm(d.body)!r}")
        print(f"obj {obj:#x} bank {b:#04x}: " + " | ".join(row), flush=True)
svc.stop()
