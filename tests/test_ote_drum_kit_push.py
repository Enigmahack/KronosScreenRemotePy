"""Real MAIN-REPERTOIRE.PCG: a Program in Drums mode converted with its Drum Kit points at the slot the kit was written to
(skips if the file is absent)."""
import os, sys, tempfile
from types import SimpleNamespace
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
F = os.environ.get("KRONOS_MAIN_REP_PCG", "E:/MAIN-REPERTOIRE.PCG")
if not os.path.exists(F):
    print("SKIP: fixture not present"); sys.exit(0)
import Data.pcg_file as pf
import Data.nautilus_sysex as nsx
import Data.object_types as ot
from Conversion.push import ConvertItem, ConvertTables, push_converted
from Data.librarian_sysex import OBJ_PROGRAM, OBJ_DRUM_KIT, iter_program_zone_refs

class Fake:
    def __init__(self, sizes): self.sizes, self.slots, self.calls = sizes, {}, []
    def dump_object_parsed(self, obj, bank, index, **kw):
        if (obj, bank) not in self.sizes: return None
        return SimpleNamespace(version=6, body=self.slots.get((obj, bank, index)) or b"I" * self.sizes[(obj, bank)])
    def backup_objects(self, ops, path):
        with open(path, "wb") as f:
            for op in ops: f.write(nsx.object_dump_write(op.obj, op.bank, op.index, op.version, op.body))
    def write_object(self, op): self.slots[(op.obj, op.bank, op.index)] = op.body; return 0
    def store_bank(self, obj, bank): return 0

pcg = pf.open_pcg(open(F, "rb").read())
T = ConvertTables.load()
prog = {e.name.strip(): e for e in pcg.objects if e.obj_type == OBJ_PROGRAM}
kits = {(e.bank.obj_bank, e.index): e for e in pcg.objects if e.obj_type == OBJ_DRUM_KIT}
hold, rel = prog["OTE-PERC HOLD"], prog["OTE-PERC RELEASE"]
items = [ConvertItem(OBJ_DRUM_KIT, kits[(0x42, 9)].name, kits[(0x42, 9)].body, src_ref=(0x42, 9)),
         ConvertItem(OBJ_DRUM_KIT, kits[(0x42, 10)].name, kits[(0x42, 10)].body, src_ref=(0x42, 10)),
         ConvertItem(OBJ_PROGRAM, hold.name, pf.wire_body_from_pcg_entry(OBJ_PROGRAM, hold), False),
         ConvertItem(OBJ_PROGRAM, rel.name, pf.wire_body_from_pcg_entry(OBJ_PROGRAM, rel), False)]
svc = Fake({(OBJ_DRUM_KIT, 0x40): 38424, (OBJ_PROGRAM, 0x4C): 3700})
rep = push_converted(svc, items, {OBJ_DRUM_KIT: (0x40, 3), OBJ_PROGRAM: (0x4C, 0)}, T, os.path.join(tempfile.mkdtemp(), "b.syx"))
fails = []
if [r.status for r in rep] != ["written"] * 4: fails.append(("status", [(r.status, r.messages[:2]) for r in rep]))
if not all(k in svc.slots for k in ((OBJ_DRUM_KIT, 0x40, 3), (OBJ_DRUM_KIT, 0x40, 4))): fails.append("kits-not-written")
want = ot.drum_kit_loc_to_linear(0x40, 3), ot.drum_kit_loc_to_linear(0x40, 4)   # where the kits now are
got = []
for slot in (0, 1):
    body = svc.slots[(OBJ_PROGRAM, 0x4C, slot)]
    got.append([n for o, z, t, n in iter_program_zone_refs(body) if (o, z) == (0, 0)][0])
if tuple(got) != want: fails.append(("repoint", got, want))
if fails: print("FAIL:", fails); sys.exit(1)
print("ote drum kit push ok: Programs point at kits", got)
