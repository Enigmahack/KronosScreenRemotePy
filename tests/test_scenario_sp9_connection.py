"""SP9: never write to the wrong unit. push_converted checks, before every phase and every write, that the service
is still the live Nautilus connection the user confirmed (SysExService.stop() resets the codec to Kronos; a
reconnect may be a different instrument). Once it is not, nothing more is sent: no write, no Store; slots already
written are reported as volatile-only with the backup named. Also: the dump gate is held for the whole push, the
bank-B arp slot used is reported (for the persisted next default), and that default survives a settings round trip."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_sp9_")
from pathlib import Path
from types import SimpleNamespace
import Data.nautilus_sysex as nsx
import Models.storage
from Conversion.data import (ARP_ENCODING_NORMAL, ARP_PATTERN_SIZE, GePatternEntry, KarmaGeCatalog,
                             KarmaGePatternLibrary, ProgramRefMap, RomWaveformMap)
from Conversion.push import ConvertItem, ConvertTables, plan_arp_slots, push_converted
from Core.sysex_service import DumpGate, SysExService
from Data.librarian_sysex import OBJ_PROGRAM
from Models.app_settings import AppSettings
from Tools.exs_option_index import ExsOptionIndex

fails = []


def check(name, cond):
    if not cond:
        print("FAIL " + name)
        fails.append(name)


class Svc:
    """flip_after: ('write', n) flips is_nautilus after the n-th write; ('store', 0) before the first Store, etc."""

    def __init__(self, flip=None, gate=False):
        self.is_nautilus, self.can_dump = True, True
        self.flip = flip
        self.calls, self.slots, self.log = [], {}, []
        self._codec = nsx
        if gate:
            self.hold_dump_gate = self._hold
        self.depth = 0

    import contextlib

    @contextlib.contextmanager
    def _hold(self):
        self.log.append("enter")
        self.depth += 1
        try:
            yield
        finally:
            self.depth -= 1
            self.log.append("exit")

    def _tick(self, kind):
        self.calls.append(kind)
        if self.depth == 0 and self.log is not None and "enter" in self.log:
            self.log.append("outside-gate:" + kind)
        if self.flip and self.flip == (kind, self.calls.count(kind)):
            self.is_nautilus = False

    def dump_object_parsed(self, obj, bank, index, **kw):
        self._tick("dump")
        return SimpleNamespace(version=6, body=self.slots.get((obj, bank, index), bytes([index + 1]) * 3700))

    def backup_objects(self, ops, path):
        self._tick("backup")
        with open(path, "wb") as f:
            for op in ops:
                f.write(nsx.object_dump_write(op.obj, op.bank, op.index, op.version, op.body))

    def write_object(self, op):
        self._tick("write")
        self.slots[(op.obj, op.bank, op.index)] = op.body
        return 0

    def store_bank(self, obj, bank):
        self._tick("store")
        return 0


tables = ConvertTables(RomWaveformMap(), ExsOptionIndex.from_catalog("{}"), KarmaGeCatalog(), ProgramRefMap())
items = [ConvertItem(OBJ_PROGRAM, n, bytes(3706)) for n in "abc"]
dest = {OBJ_PROGRAM: (0x4C, 0)}
bk = os.path.join(tempfile.mkdtemp(prefix="kr_sp9b_"), "b.syx")

# flipped after the 3 target reads, i.e. before the backup: nothing is written
s = Svc(flip=("dump", 3))
r = push_converted(s, items, dest, tables, bk)
check("flip before backup: no backup, write or Store", not {"backup", "write", "store"} & set(s.calls))
check("flip before backup: every item says why", all("no longer a Nautilus" in " ".join(x.messages) for x in r))

# flipped after the first write: the rest are not written, no Store, the written one is volatile-only + backup named
s = Svc(flip=("write", 1))
r = push_converted(s, items, dest, tables, bk)
check("flip after first write: exactly one write", s.calls.count("write") == 1)
check("flip after first write: no Store", "store" not in s.calls)
check("flip after first write: not reported written", all(x.status != "written" for x in r))
check("flip after first write: names the backup and volatile memory",
      "volatile" in " ".join(r[0].messages) and bk in " ".join(r[0].messages))
check("flip after first write: the others say why", all("no longer a Nautilus" in " ".join(x.messages) for x in r[1:]))

# the caller's own check (a different service object now owns the connection)
current = {"svc": None}
s = Svc()
current["svc"] = s
calls_before_flip = []
r = push_converted(s, items, dest, tables, bk, still_valid=lambda: len(s.calls) < 4)
check("still_valid=False stops further sends", s.calls.count("write") == 0 and "store" not in s.calls)
check("still_valid=False: reason is the connection change", any("connection changed" in " ".join(x.messages) for x in r))

# link down
s = Svc()
s.can_dump = False
r = push_converted(s, items, dest, tables, bk)
check("SysEx link down: nothing sent", not s.calls and all("link is down" in " ".join(x.messages) for x in r))

# a normal run is untouched by the guard
s = Svc()
r = push_converted(s, items, dest, tables, bk)
check("normal run: all written, one Store", [x.status for x in r] == ["written"] * 3 and s.calls.count("store") == 1)

# the dump gate is held across the whole push
s = Svc(gate=True)
r = push_converted(s, items, dest, tables, bk)
check("gate: entered once, exited once, no call outside it", s.log == ["enter", "exit"])
stub = SimpleNamespace(_dump_gate=DumpGate(), refresh_now=lambda: stub.refreshed.append(1), refreshed=[])
with SysExService.hold_dump_gate(stub):
    check("real gate: active inside", stub._dump_gate.active)
check("real gate: released and one refresh afterwards", not stub._dump_gate.active and stub.refreshed == [1])
try:
    with SysExService.hold_dump_gate(stub):
        raise RuntimeError("boom")
except RuntimeError:
    pass
check("real gate: released when the push raises", not stub._dump_gate.active)

# arp slots: plan matches what is written, and the used slots are reported
pat = bytes([47]) * ARP_PATTERN_SIZE
lib = KarmaGePatternLibrary.parse(KarmaGePatternLibrary.pack([GePatternEntry(47, ARP_ENCODING_NORMAL, 4, 0.95, pat)]))
arp_tables = ConvertTables(RomWaveformMap(), ExsOptionIndex.from_catalog("{}"), KarmaGeCatalog(), ProgramRefMap(), lib)


def prog_ge(ge):
    b = bytearray(3706)
    b[1814], b[1815] = ge & 0xFF, ge >> 8
    return ConvertItem(OBJ_PROGRAM, f"GE{ge}", bytes(b))


class ArpSvc(Svc):
    def dump_object_parsed(self, obj, bank, index, **kw):
        self._tick("dump")
        n = 556 if obj == 0x19 else 3700
        return SimpleNamespace(version=6, body=self.slots.get((obj, bank, index), bytes([index + 1]) * n))


aitems = [prog_ge(47), prog_ge(47)]
check("plan_arp_slots: one slot per distinct GE", plan_arp_slots(aitems, arp_tables, 60) == [60])
check("plan_arp_slots: none when arp is off", plan_arp_slots(aitems, arp_tables, None) == [])
check("plan_arp_slots: none when the bank is full", plan_arp_slots(aitems, arp_tables, 128) == [])
s = ArpSvc()
r = push_converted(s, aitems, dest, arp_tables, bk, arp_start=60)
check("arp_used reports the slot written", list(r.arp_used) == [60])

# the remembered next slot survives a settings round trip
st = AppSettings()
check("default next arp slot is B043", st.convert_arp_next_slot == 43)
st.convert_arp_next_slot = 61
Models.storage.save_settings(st)
check("next arp slot persists", Models.storage.load_settings().convert_arp_next_slot == 61)

print(f"{len(fails)} violations")
sys.exit(1 if fails else 0)
