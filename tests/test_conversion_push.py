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
import Data.nautilus_sysex as nsx
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
        ops = list(ops)
        self.calls.append(("backup", len(ops)))
        with open(path, "wb") as f:   # a real dump per object: push_converted re-parses the file before writing
            for op in ops:
                f.write(nsx.object_dump_write(op.obj, op.bank, op.index, op.version, op.body))

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

# ---- Combi Timbre references follow the Programs written in the same run
def _timbre(body, t):
    o = 2632 + t * 188
    return body[o + 1], body[o]


def _combi_refs(*refs):
    body = bytearray(7810)
    for t, (bank, num) in enumerate(refs):
        o = 4802 + t * 188
        body[o], body[o + 1] = num, bank
        body[o + 2] = 0x20   # status On
    return bytes(body)


# Kronos U-A (byte 18) is wire 0x40; on the Nautilus that bank is G and its Timbre byte is 17.
p_a = ConvertItem(OBJ_PROGRAM, "A", bytes(3706), is_exi=False, src_ref=(18, 10))
p_b = ConvertItem(OBJ_PROGRAM, "B", bytes(3706), is_exi=False, src_ref=(19, 3))
cmb = ConvertItem(OBJ_COMBI, "C", _combi_refs((18, 10), (19, 3), (20, 7)))
svc = FakeNautilus({(OBJ_PROGRAM, 0x4C): 3700, (OBJ_COMBI, 0x40): 5640})
rep = push_converted(svc, [p_a, p_b, cmb], {OBJ_PROGRAM: (0x4C, 5), OBJ_COMBI: (0x40, 0)}, tables, bk)
out = svc.slots[(OBJ_COMBI, 0x40, 0)]
check("batch-repoint-written", [r.status for r in rep] == ["written"] * 3)
check("batch-repoint-first", _timbre(out, 0) == (17 + 0x4C - 0x40, 5))
check("batch-repoint-second", _timbre(out, 1) == (17 + 0x4C - 0x40, 6))
check("unplaced-user-bank-keeps-wire-bank", _timbre(out, 2) == (19, 7))
check("batch-repoint-reported", any("written in this run" in m for m in rep[2].messages))

calls = [c for c in svc.calls if c[0] in ("write", "store")]
check("programs-stored-before-combi-written",
      calls.index(("store", OBJ_PROGRAM, 0x4C)) < calls.index(("write", 0x40, 0)))


# ---- a Combi is written only when every Program of this run it points at was written AND Stored
class PickyNautilus(FakeNautilus):
    def __init__(self, sizes, write_codes=None, store_codes=None):
        super().__init__(sizes)
        self.write_codes = write_codes or {}   # (obj, bank) -> reply code
        self.store_codes = store_codes or {}

    def write_object(self, op):
        self.write_code = self.write_codes.get((op.obj, op.bank), 0)
        return super().write_object(op)

    def store_bank(self, obj, bank):
        super().store_bank(obj, bank)
        return self.store_codes.get((obj, bank), 0)


exi_ref = ConvertItem(OBJ_PROGRAM, "EXI-A", bytes(4960), is_exi=True, src_ref=(18, 10))   # refused: HD-1 bank
dep = ConvertItem(OBJ_COMBI, "DEP", _combi_refs((18, 10)))
indep = ConvertItem(OBJ_COMBI, "INDEP", _combi_refs((20, 7)))
svc = PickyNautilus({(OBJ_PROGRAM, 0x4C): 3700, (OBJ_COMBI, 0x40): 5640})
rep = push_converted(svc, [exi_ref, dep, indep], {OBJ_PROGRAM: (0x4C, 5), OBJ_COMBI: (0x40, 0)}, tables, bk)
check("dep-program-refused", rep[0].status == "refused")
check("dep-combi-skipped", rep[1].status == "failed" and (OBJ_COMBI, 0x40, 0) not in svc.slots
      and ("write", 0x40, 0) not in svc.calls)
check("dep-combi-warning-names-program", any('"EXI-A"' in m and "S005" in m and "refused" in m for m in rep[1].messages)
      and any(m.startswith("Not written") for m in rep[1].messages))
check("indep-combi-written", rep[2].status == "written" and (OBJ_COMBI, 0x40, 1) in svc.slots)
check("skipped-combi-still-backed-up", svc.calls[0] == ("backup", 2))

for label, kw in (("write-refused", {"write_codes": {(OBJ_PROGRAM, 0x4C): 5}}),
                  ("store-failed", {"store_codes": {(OBJ_PROGRAM, 0x4C): 3}}),
                  ("store-no-reply", {"store_codes": {(OBJ_PROGRAM, 0x4C): -1}})):
    svc = PickyNautilus({(OBJ_PROGRAM, 0x4C): 3700, (OBJ_COMBI, 0x40): 5640}, **kw)
    rep = push_converted(svc, [p_a, dep, indep], {OBJ_PROGRAM: (0x4C, 5), OBJ_COMBI: (0x40, 0)}, tables, bk)
    check(f"{label}-combi-skipped", rep[1].status == "failed" and ("write", 0x40, 0) not in svc.calls
          and any('"A"' in m and "S005" in m for m in rep[1].messages))
    check(f"{label}-indep-written", rep[2].status == "written")

svc = PickyNautilus({(OBJ_COMBI, 0x40): 5640})   # Program's target slot doesn't answer
rep = push_converted(svc, [p_a, dep], {OBJ_PROGRAM: (0x4C, 5), OBJ_COMBI: (0x40, 0)}, tables, bk)
check("no-read-reply-combi-not-written", (OBJ_COMBI, 0x40, 0) not in svc.slots and rep[1].status != "written")

lone = ConvertItem(OBJ_COMBI, "C", _combi_refs((18, 10)))
svc = FakeNautilus({(OBJ_COMBI, 0x40): 5640})
push_converted(svc, [lone], {OBJ_COMBI: (0x40, 0)}, tables, bk)
check("lone-combi-user-bank-shifted", _timbre(svc.slots[(OBJ_COMBI, 0x40, 0)], 0) == (17, 10))

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
