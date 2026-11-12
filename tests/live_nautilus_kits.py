"""Live, WRITES the Nautilus: Kronos Drum Kits / Wave Sequences -> Nautilus user bank through Conversion.push (backup, Store, re-read).
usage: live_nautilus_kits.py <kronos host> <nautilus host> <kit|seq> <src bank hex> <src slot> <count> <dst bank hex> <dst slot>"""
import os, sys, tempfile, time, pathlib
os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_livenk_"); os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtCore import QCoreApplication
app = QCoreApplication([])
from Core.sysex_service import SysExService
from Conversion.push import ConvertItem, ConvertTables, push_converted
from Data.librarian_sysex import OBJ_DRUM_KIT, OBJ_WAVE_SEQ
kh, nh, what, sb, ss, cnt, db, ds = sys.argv[1:9]
obj = OBJ_DRUM_KIT if what == "kit" else OBJ_WAVE_SEQ
def connect(h, naut):
    s = SysExService(); s.start(h); t = time.time()
    while time.time() - t < 12 and not s.can_dump: app.processEvents(); time.sleep(0.05)
    assert s.can_dump; s.set_device_family(naut); return s
k, n = connect(kh, False), connect(nh, True)
items = []
for i in range(int(cnt)):
    d = k.dump_object_parsed(obj, int(sb, 16), int(ss) + i, no_response_ms=6000); assert d is not None
    items.append(ConvertItem(obj, bytes(d.body[:24]).split(b"\0")[0].decode("latin-1").strip(), bytes(d.body)))
bk = pathlib.Path(__file__).resolve().parent.parent / "librarian_backups"; bk.mkdir(exist_ok=True)
path = str(bk / f"{time.strftime('%Y%m%d_%H%M%S')}_nautilus_{what}_live.syx")
reports = push_converted(n, items, {obj: (int(db, 16), int(ds))}, ConvertTables.load(), path, progress=lambda m: None)
for r in reports:
    print(f"[{r.status.upper()}] {r.name!r} -> {r.dest}")
    for m in r.messages: print("     ", m[:200])
print("backup:", path); k.stop(); n.stop()
