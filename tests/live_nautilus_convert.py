"""Live, WRITES one Nautilus Program slot, then restores it. usage:
live_nautilus_convert.py <host> <source.pcg> [--bank 0x40] [--slot 127] [--pick N]

Backs the slot up to .syx, converts the Nth convertible HD-1 Program from the Kronos PCG using the slot's own
current contents as the Nautilus template, writes + stores it, re-reads it, then writes the original back and
checks the bank digest matches the pre-test value."""
import argparse, os, sys, tempfile, time, pathlib
from types import SimpleNamespace
os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_livenc_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtCore import QCoreApplication
app = QCoreApplication([])
from Core.sysex_service import SysExService
from Data.librarian_sysex import OBJ_PROGRAM
from Data.pcg_file import open_pcg, wire_body_from_pcg_entry
from Conversion.data import KarmaGeCatalog, KarmaGePatternLibrary, RomWaveformMap
from Conversion.program import convert_program
from Conversion.profiles import ProgramKind
from Tools.exs_option_index import ExsOptionIndex

ap = argparse.ArgumentParser()
ap.add_argument("host"); ap.add_argument("pcg")
ap.add_argument("--bank", type=lambda x: int(x, 0), default=0x40)
ap.add_argument("--slot", type=int, default=127)
ap.add_argument("--pick", type=int, default=0)
a = ap.parse_args()

rom, exs, ge, pats = RomWaveformMap.from_catalog(), ExsOptionIndex.from_catalog(), KarmaGeCatalog.from_catalog(), KarmaGePatternLibrary.from_catalog()
pcg = open_pcg(open(a.pcg, "rb").read())
svc = SysExService(); svc.start(a.host)
t = time.time()
while time.time() - t < 12 and not svc.can_dump:
    app.processEvents(); time.sleep(0.05)
assert svc.can_dump, "bridge"
svc.set_device_family(True)

orig = svc.dump_object_parsed(OBJ_PROGRAM, a.bank, a.slot, no_response_ms=6000)
assert orig is not None, "cannot read target slot"
name = orig.body[:24].split(bytes(1))[0].decode("ascii", "replace")
print(f"target bank 0x{a.bank:02X} slot {a.slot}: {name!r}, {len(orig.body)} bytes, v{orig.version}")
digest_before = svc.bank_digest(OBJ_PROGRAM, a.bank)
bk = pathlib.Path(__file__).resolve().parent.parent / "librarian_backups"
bk.mkdir(exist_ok=True)
bpath = bk / f"nautilus_prog_{a.bank:02X}_{a.slot}_{int(time.time())}.syx"
svc.backup_objects([SimpleNamespace(obj=OBJ_PROGRAM, bank=a.bank, index=a.slot, version=orig.version, body=orig.body)], str(bpath))
print("backup:", bpath)

kind = ProgramKind.HD1 if len(orig.body) == 3700 else ProgramKind.EXI
seen = 0
chosen = None
for o in pcg.objects:
    if o.obj_type != OBJ_PROGRAM or o.is_exi != (kind is ProgramKind.EXI):
        continue
    r = convert_program(wire_body_from_pcg_entry(o.obj_type, o), kind, rom, exs, ge, orig.body, None, pats)
    if r.body is None:
        continue
    if seen == a.pick:
        chosen = (o, r)
        break
    seen += 1
assert chosen, "no convertible program"
o, r = chosen
body = bytearray(r.body)
print(f"converting {o.name!r} ({kind.value}); warnings: {len(r.warnings)}")
for w in r.warnings:
    print("  -", w[:160])
op = SimpleNamespace(obj=OBJ_PROGRAM, bank=a.bank, index=a.slot, version=orig.version, body=bytes(body))
try:
    print("write reply:", svc.write_object(op), " store reply:", svc.store_bank(OBJ_PROGRAM, a.bank))
    back = svc.dump_object_parsed(OBJ_PROGRAM, a.bank, a.slot, no_response_ms=6000)
    print("re-read byte-identical:", back is not None and back.body == bytes(body))
    if back is not None and back.body != bytes(body):
        diffs = [i for i in range(len(body)) if back.body[i] != body[i]]
        print("  differing offsets:", len(diffs), diffs[:20])
finally:
    restore = SimpleNamespace(obj=OBJ_PROGRAM, bank=a.bank, index=a.slot, version=orig.version, body=orig.body)
    print("restore write:", svc.write_object(restore), " store:", svc.store_bank(OBJ_PROGRAM, a.bank))
    again = svc.dump_object_parsed(OBJ_PROGRAM, a.bank, a.slot, no_response_ms=6000)
    print("restored byte-identical:", again is not None and again.body == orig.body)
    print("bank digest matches pre-test:", svc.bank_digest(OBJ_PROGRAM, a.bank) == digest_before)
    svc.stop()
