"""Conversion.dx7_push: DX7 .syx -> items -> Conversion.push against a duck-typed fake Nautilus.
Run from the repo root: python tests/test_dx7_push.py"""
import os
import sys
import tempfile
from types import SimpleNamespace

os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_dx7push_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from Conversion import dx7_push, dx7_to_mod7
from Conversion.data import KarmaGeCatalog, ProgramRefMap, RomWaveformMap
from Conversion.push import ConvertTables, push_converted
from Data import dx7_voice
from Data.librarian_sysex import OBJ_PROGRAM
import Data.nautilus_sysex as nsx
from Tools.exs_option_index import ExsOptionIndex

fails = []


def check(name, cond):
    if not cond:
        fails.append(name)


class FakeNautilus:
    def __init__(self, size):
        self.size, self.slots, self.calls = size, {}, []

    def dump_object_parsed(self, obj, bank, index, **kw):
        return SimpleNamespace(version=6, body=self.slots.get((obj, bank, index)) or b"I" * self.size)

    def backup_objects(self, ops, path):
        ops = list(ops)
        self.calls.append(("backup", len(ops)))
        with open(path, "wb") as f:
            for op in ops:
                f.write(nsx.object_dump_write(op.obj, op.bank, op.index, op.version, op.body))

    def write_object(self, op):
        self.calls.append(("write", op.bank, op.index))
        self.slots[(op.obj, op.bank, op.index)] = op.body
        return 0

    def store_bank(self, obj, bank):
        self.calls.append(("store", obj, bank))
        return 0


voices = []
for i in range(32):
    p = dx7_voice.default_params()
    p["op0_eg_r1"] = 50 + i
    voices.append(dx7_voice.pack(p, f"VOICE {i:02d}"))
bank = dx7_voice.build_bank(voices)
check("read-one-dump", len(dx7_push.read_dx7_syx(bank)) == 32)
check("read-two-dumps-and-junk", len(dx7_push.read_dx7_syx(b"junk" + bank + bank)) == 64)
try:
    dx7_push.read_dx7_syx(b"\xf0\x43" + bytes(100))
    check("no-bank-raises", False)
except dx7_voice.Dx7Error:
    pass

# DX7II files: the voice dump followed by a 1128-byte supplement message (F0 43 0n 06 08 60 + 32 x 35 bytes + sum + F7)
supp = bytes([0xF0, 0x43, 0x00, 0x06, 0x08, 0x60]) + bytes(1120) + bytes([0, 0xF7])
check("dx7ii-second-message-ignored", dx7_push.read_dx7_syx(bank + supp) == dx7_push.read_dx7_syx(bank))
ch5 = bytearray(bank); ch5[2] = 0x05
check("any-channel", len(dx7_push.read_dx7_syx(bytes(ch5))) == 32)
hb = bytearray(bank); hb[10] |= 0x80      # high bit on a data byte: masked, checksum computed on the masked bytes
check("high-bit-masked", dx7_push.read_dx7_syx(bytes(hb)) == dx7_push.read_dx7_syx(bank))
bad = bytearray(bank); bad[4102] ^= 0x01
try:
    dx7_push.read_dx7_syx(bytes(bad))
    check("bad-checksum-raises", False)
except dx7_voice.Dx7Error as e:
    check("bad-checksum-message", "checksum" in str(e))

items = dx7_push.dx7_items(dx7_push.read_dx7_syx(bank))
check("32-items", len(items) == 32 and all(i.native and i.is_exi for i in items))
check("item-names", items[3].name == "VOICE 03")
check("item-body-size", all(len(i.body) == dx7_to_mod7.PROGRAM_SIZE for i in items))

tables = ConvertTables(RomWaveformMap(), ExsOptionIndex.from_catalog("{}"), KarmaGeCatalog(), ProgramRefMap())
svc = FakeNautilus(dx7_to_mod7.PROGRAM_SIZE)
bk = os.path.join(tempfile.mkdtemp(), "b.syx")
rep = push_converted(svc, items, dx7_push.dx7_dest(0x4B, 0), tables, bk)
check("all-written", [r.status for r in rep] == ["written"] * 32)
check("dest-labels", rep[0].dest.endswith("000") and rep[31].dest.endswith("031"))
kinds = [c[0] for c in svc.calls]
check("backup-first-one-store", kinds[0] == "backup" and kinds.count("store") == 1 and svc.calls[0] == ("backup", 32))
check("bodies-written-unchanged", svc.slots[(OBJ_PROGRAM, 0x4B, 7)] == items[7].body)

# an HD-1 bank is refused, nothing is written there
hd1 = FakeNautilus(3700)
rep = push_converted(hd1, items[:2], dx7_push.dx7_dest(0x4C, 0), tables, bk)
check("hd1-bank-refused", [r.status for r in rep] == ["refused", "refused"] and not any(c[0] == "write" for c in hd1.calls))

print("FAIL: " + ", ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
