"""Live, WRITES the Nautilus: Program-before-Combi ordering of Conversion.push.push_converted on real hardware.
Converts one Kronos Combi from a PCG plus two of the HD-1 Programs its Timbres use (built with src_ref, as the
Librarian's "Convert to Nautilus..." does), pushes them, logs every transport call in order, checks the order
(Programs written -> Program bank Stored -> Combi written -> Combi bank Stored), the re-reads and the Combi's
Timbre bytes, then ALWAYS restores the backup (restore_from_backup) and checks the restore.
usage: live_push_order.py <nautilus host> <kronos.pcg> <combi obj bank> <combi slot> [--prog-dst 0x4C:0] [--combi-dst 0x40:0]
       [--log out.txt]"""
import argparse, os, sys, tempfile, time, pathlib
os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_liveorder_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtCore import QCoreApplication
app = QCoreApplication([])
from Core.sysex_service import SysExService
from Conversion.data import kronos_timbre_bank_for_obj, nautilus_timbre_bank_for_wire
from Conversion.push import ConvertItem, ConvertTables, push_converted, restore_from_backup
from Data.librarian_sysex import OBJ_COMBI, OBJ_PROGRAM
from Data.pcg_file import open_pcg, wire_body_from_pcg_entry

ap = argparse.ArgumentParser()
ap.add_argument("host"); ap.add_argument("pcg")
ap.add_argument("combi_bank", type=lambda x: int(x, 0)); ap.add_argument("combi_slot", type=int)
ap.add_argument("--prog-dst", default="0x4C:0"); ap.add_argument("--combi-dst", default="0x40:0")
ap.add_argument("--log", default="")
a = ap.parse_args()
def spec(s):
    b, n = s.split(":"); return int(b, 0), int(n)
pdst, cdst = spec(a.prog_dst), spec(a.combi_dst)

pcg = open_pcg(open(a.pcg, "rb").read())
progs = {}
for o in pcg.objects:
    if o.obj_type == OBJ_PROGRAM and not o.is_exi:
        tb = kronos_timbre_bank_for_obj(o.bank.obj_bank)
        if tb is not None:
            progs[(tb, o.index)] = o
combi = next(o for o in pcg.objects if o.obj_type == OBJ_COMBI and o.bank.obj_bank == a.combi_bank and o.index == a.combi_slot)
refs = []
for t in range(16):
    k = 4802 + t * 188
    if combi.body[k + 2] >> 5:
        refs.append((t, (combi.body[k + 1], combi.body[k])))
picked = [r for r in dict.fromkeys(r for _, r in refs) if r in progs and r[0] >= 18][:2]
assert len(picked) == 2, picked
namer = lambda bank, slot: progs[(bank, slot)].name if (bank, slot) in progs else None
items = [ConvertItem(OBJ_PROGRAM, progs[r].name, wire_body_from_pcg_entry(OBJ_PROGRAM, progs[r]), False, src_ref=r) for r in picked]
items.append(ConvertItem(OBJ_COMBI, combi.name, combi.body, False, program_name=namer))
print("Combi", repr(combi.name), "Timbres:", refs)
print("Programs placed:", [(r, progs[r].name) for r in picked])

svc = SysExService(); svc.start(a.host)
t = time.time()
while time.time() - t < 12 and not svc.can_dump:
    app.processEvents(); time.sleep(0.05)
assert svc.can_dump, "bridge"
svc.set_device_family(True)

log = []
def wrap(name, key):
    orig = getattr(svc, name)
    def f(*args, **kw):
        r = orig(*args, **kw)
        log.append((time.time(), name) + key(args, r))
        return r
    setattr(svc, name, f)
wrap("write_object", lambda a_, r: (a_[0].obj, a_[0].bank, a_[0].index, r))
wrap("store_bank", lambda a_, r: (a_[0], a_[1], None, r))
wrap("dump_object_parsed", lambda a_, r: (a_[0], a_[1], a_[2], None if r is None else len(r.body)))
wrap("backup_objects", lambda a_, r: (len(a_[0]), a_[1], None, None))

bk = pathlib.Path(__file__).resolve().parent.parent / "librarian_backups"; bk.mkdir(exist_ok=True)
path = str(bk / f"{time.strftime('%Y%m%d_%H%M%S')}_nautilus_push_order_live.syx")
tables = ConvertTables.load()
result = {"order": False, "reread": False, "timbres": False, "restore": False}
try:
    reports = push_converted(svc, items, {OBJ_PROGRAM: pdst, OBJ_COMBI: cdst}, tables, path, progress=print)
    for r in reports:
        print(f"[{r.status.upper()}] {r.name!r} -> {r.dest}")
        for m in r.messages:
            print("     ", m[:200])
    # (name, obj, bank, slot, reply code)
    seq = [(e[1], e[2], e[3], e[4], e[5]) for e in log if e[1] in ("write_object", "store_bank", "backup_objects")]
    print("TRANSPORT SEQUENCE (writes/stores/backup):")
    for s in seq: print("   ", s)
    w = [i for i, e in enumerate(seq) if e[0] == "write_object"]
    pw = [i for i in w if seq[i][1] == OBJ_PROGRAM]
    cw = [i for i in w if seq[i][1] == OBJ_COMBI]
    ps = [i for i, e in enumerate(seq) if e[0] == "store_bank" and e[1] == OBJ_PROGRAM]
    cs = [i for i, e in enumerate(seq) if e[0] == "store_bank" and e[1] == OBJ_COMBI]
    bkp = [i for i, e in enumerate(seq) if e[0] == "backup_objects"]
    result["order"] = (len(pw) == 2 and len(cw) == 1 and len(ps) == 1 and len(cs) == 1 and bkp and bkp[0] < pw[0]
                       and max(pw) < ps[0] < cw[0] < cs[0] and all(seq[i][4] == 0 for i in w + ps + cs))
    result["reread"] = [r.status for r in reports] == ["written"] * 3
    back = svc.dump_object_parsed(OBJ_COMBI, cdst[0], cdst[1], no_response_ms=6000)
    want_bank = nautilus_timbre_bank_for_wire(pdst[0])
    tim = []
    for t, ref in refs:
        o = 2632 + t * 188
        tim.append((t, ref, (back.body[o + 1], back.body[o])))
    print("Combi Timbres on the unit (t, kronos ref, nautilus (bank byte, slot)):", tim)
    expect = {r: (want_bank, pdst[1] + i) for i, r in enumerate(picked)}
    result["timbres"] = all(nb == expect[ref] for t, ref, nb in tim if ref in expect)
finally:
    if os.path.exists(path):
        with svc.hold_dump_gate():
            rr = restore_from_backup(svc, path, progress=print)
        print("RESTORE", rr.ok, rr.messages)
        result["restore"] = rr.ok
    else:
        print("no backup written -> nothing to restore")
    print("backup:", path)
    print("RESULT", result)
    if a.log:
        with open(a.log, "w") as f:
            for e in log:
                f.write(repr(e) + "\n")
    svc.stop()
