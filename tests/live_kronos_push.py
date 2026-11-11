"""Live, WRITES the Kronos: reads native Nautilus objects, converts them to Kronos (Conversion.auto.convert_object), backs
up every target slot to librarian_backups/*.syx, writes, Stores, re-reads and compares.
usage: live_kronos_push.py <kronos host> <nautilus host> [--prog-src BANK:SLOT:N] [--exi-src BANK:SLOT:N] [--combi-src BANK:SLOT:N]
          [--prog-dst BANK:SLOT] [--exi-dst BANK:SLOT] [--combi-dst BANK:SLOT]   (banks in hex, obj-dump encoding)
Default targets are the Init slots U-GG (0x4D), EXi 0x4B slot 127, Combi U-G (0x46)."""
import argparse, os, sys, tempfile, time, pathlib
from types import SimpleNamespace
os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_livek_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtCore import QCoreApplication
app = QCoreApplication([])
from Core.sysex_service import SysExService
from Conversion.auto import convert_object
from Conversion.push import ConvertTables, _store_checked
from Data.librarian_sysex import OBJ_COMBI, OBJ_PROGRAM, OBJ_DRUM_KIT, OBJ_WAVE_SEQ
from Data.pcg_file import FAMILY_KRONOS, FAMILY_NAUTILUS

ap = argparse.ArgumentParser()
ap.add_argument("kronos"); ap.add_argument("nautilus")
ap.add_argument("--prog-src", default=""); ap.add_argument("--exi-src", default=""); ap.add_argument("--combi-src", default="")
ap.add_argument("--kit-src", default=""); ap.add_argument("--seq-src", default=""); ap.add_argument("--kit-dst", default="4D:0"); ap.add_argument("--seq-dst", default="4D:0")
ap.add_argument("--prog-dst", default="4D:0"); ap.add_argument("--exi-dst", default="4B:127"); ap.add_argument("--combi-dst", default="46:0")
a = ap.parse_args()
def spec(s): return [int(x, 16 if i == 0 else 10) for i, x in enumerate(s.split(":"))] if s else None

def connect(host, naut):
    svc = SysExService(); svc.start(host)
    t = time.time()
    while time.time() - t < 12 and not svc.can_dump:
        app.processEvents(); time.sleep(0.05)
    assert svc.can_dump, f"bridge {host}"
    svc.set_device_family(naut)
    return svc
nsvc, ksvc = connect(a.nautilus, True), connect(a.kronos, False)
tables = ConvertTables.load()
names_cache = {}
def nautilus_prog_name(bank_byte, slot):
    # Combi Timbre bank byte on the Nautilus numbering (G=17..) -> obj bank; 0..: I-A..; ask the unit
    ob = bank_byte if bank_byte < 17 else 0x40 + (bank_byte - 17)
    k = (ob, slot)
    if k not in names_cache:
        d = nsvc.dump_object_parsed(OBJ_PROGRAM, ob, slot, no_response_ms=4000)
        names_cache[k] = None if d is None else bytes(d.body[:24]).split(b"\0")[0].decode("latin-1").rstrip()
    return names_cache[k]

jobs = []   # (label, obj, is_exi, src body, dst bank, dst slot)
for flag, obj, exi, dst in ((a.prog_src, OBJ_PROGRAM, False, a.prog_dst), (a.exi_src, OBJ_PROGRAM, True, a.exi_dst), (a.combi_src, OBJ_COMBI, False, a.combi_dst), (a.kit_src, OBJ_DRUM_KIT, False, a.kit_dst), (a.seq_src, OBJ_WAVE_SEQ, False, a.seq_dst)):
    s = spec(flag)
    if not s: continue
    sb, ss, n = s; db, ds = spec(dst)
    for i in range(n):
        d = nsvc.dump_object_parsed(obj, sb, ss + i, no_response_ms=6000)
        assert d is not None, f"no reply for source {obj} {sb:#x}:{ss+i}"
        jobs.append((f"{bytes(d.body[:24]).split(b'\0')[0].decode('latin-1').rstrip()!r} ({sb:#04x}:{ss+i})", obj, exi, bytes(d.body), db, ds + i))

ops, pres, labels = [], [], []
for label, obj, exi, body, db, ds in jobs:
    res = convert_object(obj, body, exi, FAMILY_NAUTILUS, FAMILY_KRONOS, tables, program_name=nautilus_prog_name)
    print(f"{label} -> Kronos {db:#04x}:{ds}: " + ("REFUSED " + "; ".join(res.refusals) if res.body is None else f"{len(res.body)} bytes"))
    for w in res.warnings: print("    warn:", w[:200])
    if res.body is None: continue
    cur = ksvc.dump_object_parsed(obj, db, ds, no_response_ms=6000)
    assert cur is not None, "no reply from Kronos target"
    if len(cur.body) != len(res.body):
        print(f"    SKIP: target holds {len(cur.body)} bytes, converted is {len(res.body)}"); continue
    print(f"    target currently {bytes(cur.body[:24]).split(b'\0')[0].decode('latin-1')!r}")
    ops.append(SimpleNamespace(obj=obj, bank=db, index=ds, version=cur.version, body=res.body))
    pres.append(SimpleNamespace(obj=obj, bank=db, index=ds, version=cur.version, body=cur.body)); labels.append(label)
if not ops: print("nothing to write"); sys.exit(1)
bk = pathlib.Path(__file__).resolve().parent.parent / "librarian_backups"; bk.mkdir(exist_ok=True)
path = str(bk / f"{time.strftime('%Y%m%d_%H%M%S')}_kronos_convert_live.syx")
ksvc.backup_objects(pres, path); print("backup:", path)
for op, lb in zip(ops, labels):
    print("write", lb, "->", ksvc.write_object(op))
for ob, bk_ in sorted({(o.obj, o.bank) for o in ops}):
    print("store", hex(ob), hex(bk_), _store_checked(ksvc, ob, bk_))
ok = 0
for op, lb in zip(ops, labels):
    back = ksvc.dump_object_parsed(op.obj, op.bank, op.index, no_response_ms=6000)
    same = back is not None and bytes(back.body) == bytes(op.body)
    ok += same
    print("readback", lb, "IDENTICAL" if same else ("NO REPLY" if back is None else f"DIFFERS ({sum(x!=y for x,y in zip(back.body, op.body))} bytes)"))
print(f"{ok}/{len(ops)} identical")
nsvc.stop(); ksvc.stop()
