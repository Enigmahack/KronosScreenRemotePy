"""Live, WRITES the Kronos: Nautilus .SNG songs -> Kronos Song Timbre Sets (obj 0x02) into songs START.. (backed up first, Stored, re-read).
usage: live_kronos_song_push.py <kronos host> <nautilus .SNG> <first dest song> [count]"""
import os, sys, tempfile, time, pathlib
from types import SimpleNamespace
os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_liveks_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtCore import QCoreApplication
app = QCoreApplication([])
from Core.sysex_service import SysExService
from Conversion.push import ConvertTables, _store_checked
from Conversion.song_convert import convert_timbre_set
from Data.sng_file import MODEL_KRONOS, read_sng
from Data.song_model import from_sng_song
host, path, first = sys.argv[1], sys.argv[2], int(sys.argv[3]); count = int(sys.argv[4]) if len(sys.argv) > 4 else 3
sng = read_sng(open(path, "rb").read()); tables = ConvertTables.load()
svc = SysExService(); svc.start(host)
t = time.time()
while time.time() - t < 12 and not svc.can_dump:
    app.processEvents(); time.sleep(0.05)
assert svc.can_dump; svc.set_device_family(False)
ops, pres = [], []
for i, s in enumerate(sng.songs[:count]):
    sd = from_sng_song(s, sng.model)
    body, warns, refs = convert_timbre_set(sd.timbre_set, sd.model, MODEL_KRONOS, tables)
    print(f"song {i} {sd.name!r}:", "REFUSED " + "; ".join(refs) if body is None else f"{len(body)} bytes", flush=True)
    for w in warns[:3]: print("    warn:", w[:160])
    if body is None: continue
    cur = svc.dump_object_parsed(2, 0, first + i, no_response_ms=6000); assert cur is not None
    print("    target", first + i, bytes(cur.body[:24]).split(b"\0")[0])
    assert len(cur.body) == len(body)
    ops.append(SimpleNamespace(obj=2, bank=0, index=first + i, version=cur.version, body=body))
    pres.append(SimpleNamespace(obj=2, bank=0, index=first + i, version=cur.version, body=cur.body))
bk = pathlib.Path(__file__).resolve().parent.parent / "librarian_backups"; bk.mkdir(exist_ok=True)
bp = str(bk / f"{time.strftime('%Y%m%d_%H%M%S')}_kronos_songs_live.syx"); svc.backup_objects(pres, bp); print("backup", bp)
for op in ops: print("write", op.index, svc.write_object(op))
print("store", _store_checked(svc, 2, 0))
ok = 0
for op in ops:
    b = svc.dump_object_parsed(2, 0, op.index, no_response_ms=6000)
    same = b is not None and bytes(b.body) == bytes(op.body); ok += same
    print("readback", op.index, "IDENTICAL" if same else "DIFFERS/none")
print(f"{ok}/{len(ops)}"); svc.stop()
