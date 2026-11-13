"""Live, WRITES the Kronos: Nautilus -> Kronos write order, instrumented. Reads one native Nautilus Combi and two
HD-1 Programs its Timbres use, converts them (Conversion.auto.convert_object, as the conversion-on-entry path
does), backs up the Kronos targets (re-parsed and compared before any write), writes and Stores them in the same
order tests/live_kronos_push.py uses, logs every transport call, re-reads, and ALWAYS restores the backup.
This path has no placement map: the Combi's Timbres keep the same wire bank/slot (or go to a factory equivalent),
they are NOT re-pointed at the Programs written here; the script reports what they point at.
usage: live_kronos_push_order.py <kronos host> <nautilus host> [--combi-src 0:0] [--prog-dst 0x4D:0] [--combi-dst 0x46:0]"""
import argparse, os, sys, tempfile, time, pathlib
from types import SimpleNamespace
os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_livekorder_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtCore import QCoreApplication
app = QCoreApplication([])
import Data.librarian_sysex as lsx
from Core.sysex_service import SysExService
from Conversion.auto import convert_object
from Conversion.push import ConvertTables, _store_checked, _verify_backup, restore_from_backup
from Data.librarian_sysex import OBJ_COMBI, OBJ_PROGRAM
from Data.pcg_file import FAMILY_KRONOS, FAMILY_NAUTILUS

ap = argparse.ArgumentParser()
ap.add_argument("kronos"); ap.add_argument("nautilus")
ap.add_argument("--combi-src", default="0:0"); ap.add_argument("--prog-dst", default="0x4D:0"); ap.add_argument("--combi-dst", default="0x46:0")
a = ap.parse_args()
def spec(s):
    b, n = s.split(":"); return int(b, 0), int(n)
csrc, pdst, cdst = spec(a.combi_src), spec(a.prog_dst), spec(a.combi_dst)

def connect(host, naut):
    svc = SysExService(); svc.start(host)
    t = time.time()
    while time.time() - t < 12 and not svc.can_dump:
        app.processEvents(); time.sleep(0.05)
    assert svc.can_dump, f"bridge {host}"
    svc.set_device_family(naut)
    return svc
nsvc, ksvc = connect(a.nautilus, True), connect(a.kronos, False)
log = []
for name, key in (("write_object", lambda a_, r: (a_[0].obj, a_[0].bank, a_[0].index, r)),
                  ("store_bank", lambda a_, r: (a_[0], a_[1], None, r)),
                  ("backup_objects", lambda a_, r: (len(a_[0]), None, None, None))):
    def mk(orig, key, name):
        def f(*args, **kw):
            r = orig(*args, **kw); log.append((name,) + key(args, r)); return r
        return f
    setattr(ksvc, name, mk(getattr(ksvc, name), key, name))

tables = ConvertTables.load()
def nobj_bank(byte): return byte if byte < 17 else 0x40 + (byte - 17)
names = {}
def nautilus_prog_name(bank_byte, slot):
    k = (nobj_bank(bank_byte), slot)
    if k not in names:
        d = nsvc.dump_object_parsed(OBJ_PROGRAM, k[0], slot, no_response_ms=4000)
        names[k] = None if d is None else bytes(d.body[:24]).split(b"\0")[0].decode("latin-1").rstrip()
    return names[k]
def nm(body): return bytes(body[:24]).split(b"\0")[0].decode("latin-1").rstrip()

cmb = nsvc.dump_object_parsed(OBJ_COMBI, csrc[0], csrc[1], no_response_ms=6000)
assert cmb is not None and len(cmb.body) == 5640
refs = []
for t in range(16):
    o = 2632 + t * 188
    if cmb.body[o + 2] >> 5:
        refs.append((t, (cmb.body[o + 1], cmb.body[o])))
print("Nautilus Combi", repr(nm(cmb.body)), "Timbres (Nautilus bank byte, slot):", refs)
progs = []
for ref in dict.fromkeys(r for _, r in refs):
    d = nsvc.dump_object_parsed(OBJ_PROGRAM, nobj_bank(ref[0]), ref[1], no_response_ms=6000)
    if d is not None and len(d.body) == 3700:
        progs.append((ref, bytes(d.body)))
    if len(progs) == 2: break
assert len(progs) == 2, progs
jobs = [(f"{nm(b)!r} {r}", OBJ_PROGRAM, b, pdst[0], pdst[1] + i) for i, (r, b) in enumerate(progs)]
jobs.append((f"{nm(cmb.body)!r} combi", OBJ_COMBI, bytes(cmb.body), cdst[0], cdst[1]))
ops, pres, labels = [], [], []
for label, obj, body, db, ds in jobs:
    res = convert_object(obj, body, False, FAMILY_NAUTILUS, FAMILY_KRONOS, tables, program_name=nautilus_prog_name)
    print(label, "->", f"{db:#04x}:{ds}", "REFUSED " + "; ".join(res.refusals) if res.body is None else f"{len(res.body)} bytes")
    for w in res.warnings: print("    warn:", w[:220])
    assert res.body is not None
    cur = ksvc.dump_object_parsed(obj, db, ds, no_response_ms=6000)
    assert cur is not None and len(cur.body) == len(res.body), (label, None if cur is None else len(cur.body))
    print("    target currently", repr(nm(cur.body)))
    ops.append(SimpleNamespace(obj=obj, bank=db, index=ds, version=cur.version, body=res.body))
    pres.append(SimpleNamespace(obj=obj, bank=db, index=ds, version=cur.version, body=cur.body)); labels.append(label)

bk = pathlib.Path(__file__).resolve().parent.parent / "librarian_backups"; bk.mkdir(exist_ok=True)
path = str(bk / f"{time.strftime('%Y%m%d_%H%M%S')}_kronos_push_order_live.syx")
result = {"backup_verified": False, "replies": False, "reread": False, "restore": False}
try:
    with ksvc.hold_dump_gate():
        ksvc.backup_objects(pres, path)
        prob = _verify_backup(path, pres, codec=lsx)
        print("backup", path, "verified" if prob is None else f"NOT VERIFIED: {prob}")
        assert prob is None
        result["backup_verified"] = True
        codes = []
        for op, lb in zip(ops, labels):
            codes.append(ksvc.write_object(op)); print("write", lb, "->", codes[-1])
        for ob, b in sorted({(o.obj, o.bank) for o in ops}):
            c = _store_checked(ksvc, ob, b); codes.append(0 if c[0] else c); print("store", hex(ob), hex(b), c)
        result["replies"] = all(c == 0 for c in codes)
        same = []
        for op, lb in zip(ops, labels):
            back = ksvc.dump_object_parsed(op.obj, op.bank, op.index, no_response_ms=6000)
            same.append(back is not None and bytes(back.body) == bytes(op.body))
            print("readback", lb, "IDENTICAL" if same[-1] else "DIFFERS/NO REPLY")
            if op.obj == OBJ_COMBI and back is not None:
                print("    Kronos Combi Timbres (t, Kronos bank byte, slot):",
                      [(t, back.body[4802 + t * 188 + 1], back.body[4802 + t * 188]) for t, _ in refs])
        result["reread"] = all(same)
finally:
    print("TRANSPORT SEQUENCE:")
    for e in log: print("   ", e)
    if os.path.exists(path) and result["backup_verified"]:
        with ksvc.hold_dump_gate():
            rr = restore_from_backup(ksvc, path, progress=print)
        print("RESTORE", rr.ok, rr.messages); result["restore"] = rr.ok
    print("RESULT", result)
    nsvc.stop(); ksvc.stop()
