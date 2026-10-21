"""Conversion.push against a duck-typed fake Nautilus. Run from the repo root: python tests/test_conversion_push.py"""
import os
import sys
import tempfile
from types import SimpleNamespace

os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_push_test_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Conversion.data import (ARP_ENCODING_NORMAL, ARP_PATTERN_SIZE, GePatternEntry, KarmaGeCatalog,
                             KarmaGePatternLibrary, ProgramRefMap, RomWaveformMap)
from Conversion.push import ConvertItem, ConvertTables, push_converted
from Data.librarian_sysex import OBJ_COMBI, OBJ_PROGRAM
from Tools.exs_option_index import ExsOptionIndex

fails = []


def check(name, cond):
    if not cond:
        fails.append(name)


class FakeNautilus:
    def __init__(self, sizes):
        self.sizes = sizes            # (obj, bank) -> body size of an Init slot
        self.slots = {}
        self.calls = []
        self.write_code = 0

    def dump_object_parsed(self, obj, bank, index, **kw):
        if (obj, bank) not in self.sizes:
            return None
        body = self.slots.get((obj, bank, index)) or b"I" * self.sizes[(obj, bank)]
        return SimpleNamespace(version=6, body=body)

    def backup_objects(self, ops, path):
        self.calls.append(("backup", len(list(ops))))
        open(path, "wb").write(b"x")

    def write_object(self, op):
        self.calls.append(("write", op.bank, op.index))
        if self.write_code == 0:
            self.slots[(op.obj, op.bank, op.index)] = op.body
        return self.write_code

    def store_bank(self, obj, bank):
        self.calls.append(("store", obj, bank))
        return 0


tables = ConvertTables(RomWaveformMap(), ExsOptionIndex.from_catalog("{}"), KarmaGeCatalog(), ProgramRefMap())
bk = os.path.join(tempfile.mkdtemp(), "b.syx")
hd1 = ConvertItem(OBJ_PROGRAM, "HD", bytes(3706), is_exi=False)
exi = ConvertItem(OBJ_PROGRAM, "EXI", bytes(4960), is_exi=True)

svc = FakeNautilus({(OBJ_PROGRAM, 0x4C): 3700})
rep = push_converted(svc, [hd1, hd1, exi], {OBJ_PROGRAM: (0x4C, 0)}, tables, bk)
check("two-written", [r.status for r in rep] == ["written", "written", "refused"])
check("consecutive-slots", [r.dest for r in rep][:2] == ["S000", "S001"])
check("exi-refused-names-bank-type", any("HD-1 bank" in m for m in rep[2].messages))
kinds = [c[0] for c in svc.calls]
check("backup-before-writes", kinds[0] == "backup" and kinds.index("write") > 0)
check("one-store-per-bank", kinds.count("store") == 1 and svc.calls[0] == ("backup", 2))
check("written-length", len(svc.slots[(OBJ_PROGRAM, 0x4C, 0)]) == 3700)

svc = FakeNautilus({(OBJ_PROGRAM, 0x4C): 3700})
svc.write_code = 5
rep = push_converted(svc, [hd1], {OBJ_PROGRAM: (0x4C, 0)}, tables, bk)
check("write-refused-reported", rep[0].status == "failed" and any("refused" in m for m in rep[0].messages)
      and not any(c[0] == "store" for c in svc.calls))

svc = FakeNautilus({(OBJ_PROGRAM, 0x4C): 3700})
rep = push_converted(svc, [hd1, hd1], {OBJ_PROGRAM: (0x4C, 127)}, tables, bk)
check("past-end-of-bank", [r.status for r in rep] == ["written", "refused"] and "last slot" in rep[1].messages[0])

svc = FakeNautilus({})
rep = push_converted(svc, [hd1], {OBJ_PROGRAM: (0x4C, 0)}, tables, bk)
check("no-reply-failed-and-nothing-written", rep[0].status == "failed" and not svc.calls)

svc = FakeNautilus({(OBJ_COMBI, 0x46): 5640})
combi = ConvertItem(OBJ_COMBI, "C", bytes(7810))
rep = push_converted(svc, [combi], {OBJ_COMBI: (0x46, 0)}, tables, bk)
check("combi-written", rep[0].status == "written" and len(svc.slots[(OBJ_COMBI, 0x46, 0)]) == 5640)

# ---- arp patterns
pat47, pat48 = bytes([47]) * ARP_PATTERN_SIZE, bytes([48]) * ARP_PATTERN_SIZE
lib = KarmaGePatternLibrary.parse(KarmaGePatternLibrary.pack([
    GePatternEntry(47, ARP_ENCODING_NORMAL, 4, 0.95, pat47), GePatternEntry(48, ARP_ENCODING_NORMAL, 4, 0.50, pat48),
    GePatternEntry(26, ARP_ENCODING_NORMAL, 4, 1.0, pat47)]))
arp_tables = ConvertTables(RomWaveformMap(), ExsOptionIndex.from_catalog("{}"), KarmaGeCatalog(), ProgramRefMap(), lib)


def prog_with_ge(ge):
    b = bytearray(3706)
    b[1814], b[1815] = ge & 0xFF, ge >> 8
    return ConvertItem(OBJ_PROGRAM, f"GE{ge}", bytes(b))


def scene1_pattern(body):
    return body[2440] | body[2441] << 8


sizes = {(OBJ_PROGRAM, 0x4C): 3700, (0x19, 0x40): 556}
svc = FakeNautilus(sizes)
rep = push_converted(svc, [prog_with_ge(47), prog_with_ge(47), prog_with_ge(48), prog_with_ge(26), prog_with_ge(0)],
                     {OBJ_PROGRAM: (0x4C, 0)}, arp_tables, bk, arp_start=43)
check("arp-all-written", all(r.status == "written" for r in rep))
check("arp-one-slot-per-ge", svc.slots.get((0x19, 0x40, 43)) == pat47 and (0x19, 0x40, 44) not in svc.slots)
check("arp-programs-point-at-slot", scene1_pattern(svc.slots[(OBJ_PROGRAM, 0x4C, 0)]) == 2053 + 43
      and scene1_pattern(svc.slots[(OBJ_PROGRAM, 0x4C, 1)]) == 2053 + 43)
check("arp-low-score-not-used", scene1_pattern(svc.slots[(OBJ_PROGRAM, 0x4C, 2)]) == 0x4949)   # template value untouched
check("arp-preset-ge-uses-preset", scene1_pattern(svc.slots[(OBJ_PROGRAM, 0x4C, 3)]) == 0
      and (0x19, 0x40, 44) not in svc.slots)
order = [c for c in svc.calls if c[0] in ("write", "store")]
check("arp-written-before-programs", order[0] == ("write", 0x40, 43) and ("store", 0x19, 0x40) in order
      and order.index(("store", 0x19, 0x40)) < order.index(("write", 0x4C, 0)))

svc = FakeNautilus(sizes)
rep = push_converted(svc, [prog_with_ge(47)], {OBJ_PROGRAM: (0x4C, 0)}, arp_tables, bk)
check("arp-skipped-without-start", (0x19, 0x40, 43) not in svc.slots and scene1_pattern(svc.slots[(OBJ_PROGRAM, 0x4C, 0)]) == 0x4949)

svc = FakeNautilus({(OBJ_PROGRAM, 0x4C): 3700})   # arp bank doesn't answer
rep = push_converted(svc, [prog_with_ge(47)], {OBJ_PROGRAM: (0x4C, 0)}, arp_tables, bk, arp_start=43)
check("arp-unreadable-blocks-program", rep[0].status == "failed" and (OBJ_PROGRAM, 0x4C, 0) not in svc.slots
      and any("arp pattern" in m.lower() for m in rep[0].messages))

svc = FakeNautilus(sizes)
rep = push_converted(svc, [prog_with_ge(47)], {OBJ_PROGRAM: (0x4C, 0)}, arp_tables, bk, arp_start=127)
rep2 = push_converted(svc, [prog_with_ge(47), prog_with_ge(26 + 21)], {OBJ_PROGRAM: (0x4C, 1)}, arp_tables, bk, arp_start=128)
check("arp-start-past-bank", all(r.status == "written" for r in rep2) and (0x19, 0x40, 128) not in svc.slots)

if fails:
    print("FAILED:", fails)
    sys.exit(1)
print("ALL PASS")
