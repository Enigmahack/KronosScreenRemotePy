"""Live Nautilus: push a DX7 .syx as MOD-7 EXi Programs, and/or compare them with Programs the instrument built itself.
usage: live_dx7_push.py <host> <file.syx> [--bank 0x4B] [--slot 0] [--count 32] [--compare-bank 0x4A] [--compare-slot 0] [--write]
Default is READ-ONLY: it reads the target slots (names) and, with --compare-bank, diffs the converted bodies against the
Programs in that bank (e.g. the native import of the same file). --write backs every target slot up to librarian_backups/*.syx,
writes, stores the bank once and re-reads to verify (Conversion.push). Banks: P=0x49 Q=0x4A R=0x4B (EXi)."""
import argparse, collections, os, pathlib, sys, tempfile, time
os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_livedx7_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtCore import QCoreApplication
app = QCoreApplication([])
from Core.sysex_service import SysExService
from Conversion import dx7_push
from Conversion.push import ConvertTables, push_converted
from Data.librarian_sysex import OBJ_PROGRAM

ap = argparse.ArgumentParser()
ap.add_argument("host"); ap.add_argument("syx")
ap.add_argument("--bank", type=lambda x: int(x, 0), default=0x4B)
ap.add_argument("--slot", type=int, default=0)
ap.add_argument("--count", type=int, default=32)
ap.add_argument("--compare-bank", type=lambda x: int(x, 0), default=None)
ap.add_argument("--compare-slot", type=int, default=0)
ap.add_argument("--write", action="store_true")
a = ap.parse_args()

items = dx7_push.load_dx7_file(a.syx, a.count)
svc = SysExService(); svc.start(a.host)
t = time.time()
while time.time() - t < 12 and not svc.can_dump:
    app.processEvents(); time.sleep(0.05)
assert svc.can_dump, "bridge"
svc.set_device_family(True)


def read(bank, slot):
    d = svc.dump_object_parsed(OBJ_PROGRAM, bank, slot, no_response_ms=6000)
    return d.body if d else None


def name_of(body):
    return body[:24].split(b"\x00")[0].decode("latin-1").rstrip()


print(f"{len(items)} voices -> bank {a.bank:#x} slots {a.slot}..{a.slot + len(items) - 1}")
for i, it in enumerate(items):
    cur = read(a.bank, a.slot + i)
    print(f"  slot {a.slot + i:3d}: now {name_of(cur) if cur else '(no answer)'!r:28} <- {it.name!r}")

if a.write:
    bk = pathlib.Path(__file__).resolve().parent.parent / "librarian_backups"
    bk.mkdir(exist_ok=True)
    path = str(bk / f"{time.strftime('%Y%m%d_%H%M%S')}_dx7_push_live.syx")
    for r in push_converted(svc, items, dx7_push.dx7_dest(a.bank, a.slot), ConvertTables.load(), path, progress=print):
        print(f"[{r.status.upper()}] {r.name!r} -> {r.dest}", *[m[:150] for m in r.messages])
    print("backup:", path)

if a.compare_bank is not None:
    print(f"\ncompare converted bodies with bank {a.compare_bank:#x} from slot {a.compare_slot}:")
    off = collections.Counter(); exact = 0
    for i, it in enumerate(items):
        native = read(a.compare_bank, a.compare_slot + i)
        if native is None or len(native) != len(it.body):
            print(f"  {i:2d} {it.name!r}: no comparable Program"); continue
        diff = [k for k in range(len(native)) if native[k] != it.body[k]]
        same_name = name_of(native) == it.name
        exact += not diff
        off.update(k for k in diff if k >= 2902)
        print(f"  {i:2d} {it.name!r:16} native {name_of(native)!r:16} {'name=' if same_name else 'NAME?'} "
              f"{len(diff)} bytes differ ({sum(1 for k in diff if k >= 2902)} in the MOD-7 section)")
    print(f"{exact}/{len(items)} byte-identical; MOD-7 offsets that differ most often: {off.most_common(8)}")
svc.stop()
