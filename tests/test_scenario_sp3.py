"""SP3: back up, then restore after a failed push. Sweeps a link drop across every service call of
Conversion.push.push_converted against a simulated Nautilus (writes volatile until Store, Store commits a bank).
Invariants (show-preparer.md): the backup file is complete and byte-identical to the pre-images before the first
write; nothing is written if the backup itself fails; after ANY drop, replaying the backup file and re-Storing the
banks restores every touched slot to its original content."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("KRONOS_DATA_DIR", tempfile.mkdtemp(prefix="kr_data_"))
import random
from pathlib import Path
from types import SimpleNamespace
import Data.nautilus_sysex as nsx
from Conversion.data import KarmaGeCatalog, ProgramRefMap, RomWaveformMap
from Conversion.push import ConvertItem, ConvertTables, push_converted
from Core.sysex_service import SysExService
from Data.librarian_sysex import OBJ_COMBI, OBJ_PROGRAM
from Tools.exs_option_index import ExsOptionIndex

fails = []


def check(name, cond):
    if not cond:
        print("FAIL " + name)
        fails.append(name)


class Dropped(Exception):
    pass


rng = random.Random(3)
SIZES = {(OBJ_PROGRAM, 0x4C): 3700, (OBJ_COMBI, 0x46): 5640}
ORIG = {(o, b, i): bytes(rng.randrange(256) for _ in range(n)) for (o, b), n in SIZES.items() for i in range(3)}


class SimNautilus:
    def __init__(self, trip=None, dead=True):
        self.persistent = dict(ORIG)
        self.volatile = {}
        self.calls = []
        self.trip, self.dead, self.tripped = trip, dead, False
        self._codec = nsx

    def _op(self, name):
        i = len(self.calls)
        self.calls.append(name)
        if self.trip is not None and (i == self.trip or (self.dead and self.tripped)):
            self.tripped = True
            raise Dropped(f"link dropped at call {i} ({name})")

    def dump_object_parsed(self, obj, bank, index, **kw):
        self._op("dump")
        k = (obj, bank, index)
        return SimpleNamespace(version=6, body=self.volatile.get(k, self.persistent[k]))

    def backup_objects(self, ops, path):
        self._op("backup")
        SysExService.backup_objects(self, list(ops), path)

    def write_object(self, op):
        self._op("write")
        self.volatile[(op.obj, op.bank, op.index)] = op.body
        return 0

    def store_bank(self, obj, bank):
        self._op("store")
        for k in [k for k in self.volatile if k[:2] == (obj, bank)]:
            self.persistent[k] = self.volatile.pop(k)
        return 0


def restore_from(unit, path):
    unit.trip = None
    raw = Path(path).read_bytes()
    banks = set()
    for m in raw.split(b"\xf7"):
        if m:
            d = nsx.parse_object_dump(m + b"\xf7")
            unit.write_object(SimpleNamespace(obj=d.obj, bank=d.bank, index=d.index, version=d.version, body=d.body))
            banks.add((d.obj, d.bank))
    for obj, bank in sorted(banks):
        unit.store_bank(obj, bank)


tables = ConvertTables(RomWaveformMap(), ExsOptionIndex.from_catalog("{}"), KarmaGeCatalog(), ProgramRefMap())
items = [ConvertItem(OBJ_PROGRAM, "P0", bytes(3706)), ConvertItem(OBJ_PROGRAM, "P1", bytes(3706)),
         ConvertItem(OBJ_COMBI, "C0", bytes(7810))]
dest = {OBJ_PROGRAM: (0x4C, 0), OBJ_COMBI: (0x46, 0)}
TOUCHED = [(OBJ_PROGRAM, 0x4C, 0), (OBJ_PROGRAM, 0x4C, 1), (OBJ_COMBI, 0x46, 0)]

with tempfile.TemporaryDirectory(prefix="kr_sp3_") as d:
    bk = os.path.join(d, "b.syx")
    clean = SimNautilus()
    rep = push_converted(clean, items, dest, tables, bk)
    check("clean: all written", [r.status for r in rep] == ["written"] * 3)
    n_calls = len(clean.calls)
    check("clean: backup before first write", clean.calls.index("backup") < clean.calls.index("write"))
    got = {}
    for m in Path(bk).read_bytes().split(b"\xf7"):
        if m:
            o = nsx.parse_object_dump(m + b"\xf7")
            got[(o.obj, o.bank, o.index)] = o.body
    check("clean: backup == pre-images byte for byte", got == {k: ORIG[k] for k in TOUCHED})

    for dead in (True, False):
        for i in range(n_calls):
            tag = f"drop dead={dead} call={i}"
            if os.path.exists(bk):
                os.remove(bk)
            u = SimNautilus(trip=i, dead=dead)
            try:
                push_converted(u, items, dest, tables, bk)
                survived = True
            except Dropped:
                survived = False
            wrote = "write" in u.calls[:i + 1]
            backup_idx = u.calls.index("backup") if "backup" in u.calls else None
            if wrote:
                check(f"{tag}: backup done before first write", backup_idx is not None and backup_idx < u.calls.index("write"))
                check(f"{tag}: backup file complete",
                      os.path.exists(bk) and len([m for m in Path(bk).read_bytes().split(b"\xf7") if m]) == len(TOUCHED))
            if backup_idx is None or i <= (backup_idx or 0):
                check(f"{tag}: nothing written when backup did not complete", "write" not in u.calls and not u.volatile)
            if wrote and os.path.exists(bk):
                u.volatile.clear()   # a dropped link / power cycle discards volatile edits
                restore_from(u, bk)
                check(f"{tag}: restore from backup == original", all(u.persistent[k] == ORIG[k] for k in ORIG))

print(f"{n_calls} service calls x2 drop modes; {len(fails)} violations")
sys.exit(1 if fails else 0)
