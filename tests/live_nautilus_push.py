"""Live, WRITES the Nautilus: convert the first N convertible HD-1 Programs of a Kronos PCG into consecutive slots.
usage: live_nautilus_push.py <host> <source.pcg> [--bank 0x4C] [--slot 0] [--count 8] [--combis N --combi-bank 0x46 --combi-slot 0]
Every target slot is backed up to librarian_backups/*.syx first (Conversion.push)."""
import argparse, os, sys, tempfile, time, pathlib
os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_livepush_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtCore import QCoreApplication
app = QCoreApplication([])
from Core.sysex_service import SysExService
from Conversion.combi import convert_combi
from Conversion.data import KarmaGeCatalog
from Conversion.program import convert_program
from Conversion.profiles import ProgramKind
from Conversion.push import ConvertItem, ConvertTables, push_converted
from Data.librarian_sysex import OBJ_COMBI, OBJ_PROGRAM
from Data.pcg_file import open_pcg, wire_body_from_pcg_entry

ap = argparse.ArgumentParser()
ap.add_argument("host"); ap.add_argument("pcg")
ap.add_argument("--bank", type=lambda x: int(x, 0), default=0x4C)
ap.add_argument("--slot", type=int, default=0)
ap.add_argument("--count", type=int, default=8)
ap.add_argument("--arp-start", type=int, default=None)
ap.add_argument("--combis", type=int, default=0)
ap.add_argument("--combi-bank", type=lambda x: int(x, 0), default=0x46)
ap.add_argument("--combi-slot", type=int, default=0)
a = ap.parse_args()

tables = ConvertTables.load()
pcg = open_pcg(open(a.pcg, "rb").read())
items, dest = [], {}
blank_hd1 = bytes(3700)
for o in pcg.objects:
    if o.obj_type == OBJ_PROGRAM and not o.is_exi and len([i for i in items if i.obj_type == OBJ_PROGRAM]) < a.count:
        body = wire_body_from_pcg_entry(o.obj_type, o)
        if convert_program(body, ProgramKind.HD1, tables.rom_map, tables.exs, tables.ge_catalog, blank_hd1).body:
            items.append(ConvertItem(OBJ_PROGRAM, o.name, body, False))
    elif o.obj_type == OBJ_COMBI and len([i for i in items if i.obj_type == OBJ_COMBI]) < a.combis:
        items.append(ConvertItem(OBJ_COMBI, o.name, o.body))
dest[OBJ_PROGRAM] = (a.bank, a.slot)
if a.combis:
    dest[OBJ_COMBI] = (a.combi_bank, a.combi_slot)

svc = SysExService(); svc.start(a.host)
t = time.time()
while time.time() - t < 12 and not svc.can_dump:
    app.processEvents(); time.sleep(0.05)
assert svc.can_dump, "bridge"
svc.set_device_family(True)
bk = pathlib.Path(__file__).resolve().parent.parent / "librarian_backups"
bk.mkdir(exist_ok=True)
path = str(bk / f"{time.strftime('%Y%m%d_%H%M%S')}_nautilus_convert_live.syx")
reports = push_converted(svc, items, dest, tables, path, progress=print, arp_start=a.arp_start)
for r in reports:
    print(f"[{r.status.upper()}] {r.name!r} -> {r.dest}")
    for m in r.messages:
        print("     ", m[:170])
print("backup:", path)
svc.stop()
