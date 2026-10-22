"""SP3 (replies): a write or Store whose Reply never arrives is UNKNOWN, not 'refused'. Drives
Conversion.push.push_converted against a simulated Nautilus (writes volatile until Store; the bank digest reflects
the live state, as measured on a Nautilus) and checks: a lost write reply is resolved by re-reading the slot; a bank is never Stored solely for slots
whose state is unproven; a lost Store reply stays unknown (the digest can't decide it); a partial multi-bank Store names the
banks already stored and the backup; and restore_from_backup puts the previous contents back."""
import os, sys, tempfile, hashlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("KRONOS_DATA_DIR", tempfile.mkdtemp(prefix="kr_data_"))
import random
from pathlib import Path
from types import SimpleNamespace
import Data.nautilus_sysex as nsx
from Conversion.data import (ARP_ENCODING_NORMAL, ARP_PATTERN_SIZE, GePatternEntry, KarmaGeCatalog,
                             KarmaGePatternLibrary, ProgramRefMap, RomWaveformMap)
from Conversion.push import ConvertItem, ConvertTables, push_converted, restore_from_backup
from Core.sysex_service import SysExService
from Data.librarian_sysex import OBJ_COMBI, OBJ_PROGRAM
from Tools.exs_option_index import ExsOptionIndex

fails = []


def check(name, cond):
    if not cond:
        print("FAIL " + name)
        fails.append(name)


rng = random.Random(11)
SIZES = {(OBJ_PROGRAM, 0x4C): 3700, (OBJ_COMBI, 0x46): 5640, (0x19, 0x40): 556}
ORIG = {(o, b, i): bytes(rng.randrange(256) for _ in range(n)) for (o, b), n in SIZES.items() for i in range(130)}


class Sim:
    """write_mode[k] / store_mode[k]: 'applied' | 'dropped' (both reply -1), 'refuse' (reply 5)."""

    def __init__(self, write_mode=None, store_mode=None, dump_script=None, has_digest=True):
        self.persistent = dict(ORIG)
        self.volatile = {}
        self.calls = []
        self.write_mode, self.store_mode = write_mode or {}, store_mode or {}
        self.script = {k: list(v) for k, v in (dump_script or {}).items()}   # slot -> [reply?, ...] per read
        self.n_write = self.n_store = 0
        self._codec = nsx
        if not has_digest:
            self.bank_digest = None   # service without a digest probe

    def bank_digest(self, obj, bank):
        h = hashlib.sha1()
        for k in sorted(k for k in self.persistent if k[:2] == (obj, bank)):
            h.update(bytes(k) + self.volatile.get(k, self.persistent[k]))
        return h.digest()

    def dump_object_parsed(self, obj, bank, index, **kw):
        k = (obj, bank, index)
        self.calls.append("dump")
        if self.script.get(k) and not self.script[k].pop(0):
            return None
        return SimpleNamespace(version=6, body=self.volatile.get(k, self.persistent[k]))

    def backup_objects(self, ops, path):
        SysExService.backup_objects(self, list(ops), path)

    def write_object(self, op):
        k, self.n_write = self.n_write, self.n_write + 1
        self.calls.append("write")
        mode = self.write_mode.get(k)
        if mode in (None, "applied"):
            self.volatile[(op.obj, op.bank, op.index)] = op.body
        if mode == "refuse":
            return 5
        return -1 if mode in ("applied", "dropped") else 0

    def store_bank(self, obj, bank):
        k, self.n_store = self.n_store, self.n_store + 1
        self.calls.append("store")
        mode = self.store_mode.get(k)
        if mode == "refuse":
            return 5
        if mode != "dropped":
            for key in [key for key in self.volatile if key[:2] == (obj, bank)]:
                self.persistent[key] = self.volatile.pop(key)
        return -1 if mode in ("applied", "dropped") else 0


tables = ConvertTables(RomWaveformMap(), ExsOptionIndex.from_catalog("{}"), KarmaGeCatalog(), ProgramRefMap())
P = lambda n: ConvertItem(OBJ_PROGRAM, n, bytes(3706))
dest = {OBJ_PROGRAM: (0x4C, 0), OBJ_COMBI: (0x46, 0)}
tmp = tempfile.mkdtemp(prefix="kr_sp3r_")
bk = os.path.join(tmp, "b.syx")


def run(sim, items, **kw):
    return push_converted(sim, items, dest, tables, bk, **kw)


def msgs(rep):
    return " | ".join(rep.messages)


# S1a: write reply lost and the slot did NOT change -> failed, 'unchanged', the rest still written and stored
s = Sim(write_mode={1: "dropped"})
r = run(s, [P("a"), P("b"), P("c")])
check("lost write, not applied: statuses", [x.status for x in r] == ["written", "failed", "written"])
check("lost write, not applied: says unchanged, not refused", "unchanged" in msgs(r[1]) and "refused" not in msgs(r[1]))
check("lost write, not applied: slot untouched in storage", s.persistent[(OBJ_PROGRAM, 0x4C, 1)] == ORIG[(OBJ_PROGRAM, 0x4C, 1)])
check("lost write, not applied: others stored", s.calls.count("store") == 1)

# S1b: write reply lost but the slot WAS applied -> proven by the re-read, counts as written
s = Sim(write_mode={1: "applied"})
r = run(s, [P("a"), P("b"), P("c")])
check("lost write, applied: all written", [x.status for x in r] == ["written"] * 3)
check("lost write, applied: says matches", "matches what was sent" in msgs(r[1]))
check("lost write, applied: stored", s.persistent[(OBJ_PROGRAM, 0x4C, 1)] != ORIG[(OBJ_PROGRAM, 0x4C, 1)])

# S1c: lost reply and the slot cannot be re-read -> unknown, honest message with the backup path, never 'refused'
s = Sim(write_mode={1: "applied"}, dump_script={(OBJ_PROGRAM, 0x4C, 1): [True, False, False]})
r = run(s, [P("a"), P("b"), P("c")])
check("unknown slot: failed", r[1].status == "failed")
check("unknown slot: honest message + backup path", "may or may not" in msgs(r[1]) and bk in msgs(r[1]) and "refused" not in msgs(r[1]))
check("unknown slot: the others are still written", r[0].status == "written" and r[2].status == "written")

# S1d: only unknown slots -> the bank is NOT Stored on their account
s = Sim(write_mode={0: "applied", 1: "applied"},
        dump_script={(OBJ_PROGRAM, 0x4C, 0): [True, False, False], (OBJ_PROGRAM, 0x4C, 1): [True, False, False]})
r = run(s, [P("a"), P("b")])
check("only unknown slots: no Store issued", "store" not in s.calls)
check("only unknown slots: nothing persisted", all(s.persistent[k] == ORIG[k] for k in ORIG))

# S1e: unknown at write time, proven afterwards (re-read works the second time) and its bank was stored
s = Sim(write_mode={1: "applied"}, dump_script={(OBJ_PROGRAM, 0x4C, 1): [True, False, True]})
r = run(s, [P("a"), P("b"), P("c")])
check("unknown then proven after Store: written", r[1].status == "written" and "Verified by re-read" in msgs(r[1]))

# S2a/b: Store reply lost -> unknown whether or not it committed; the digest changes on a write, so it can't decide
for mode in ("applied", "dropped"):
    s = Sim(store_mode={0: mode})
    r = run(s, [P("a")])
    check(f"lost Store reply ({mode}): failed, 'may or may not'", r[0].status == "failed" and "may or may not be stored" in msgs(r[0])
          and "only in volatile" not in msgs(r[0]) and "digest" not in msgs(r[0]))
# S2d: two banks, the second Store is refused -> first stored, second names the stored bank and the backup
s = Sim(store_mode={1: "refuse"})
r = run(s, [P("a"), ConvertItem(OBJ_COMBI, "c", bytes(7810))])
check("partial Store: first bank written, second failed", [x.status for x in r] == ["written", "failed"])
check("partial Store: names stored bank and backup", "already stored" in msgs(r[1]) and bk in msgs(r[1]) and "volatile" in msgs(r[1]))

# restore: put the previous contents back after that partial Store
res = restore_from_backup(s, bk)
check("restore: ok", res.ok)
check("restore: storage equals the original again", all(s.persistent[k] == ORIG[k] for k in ORIG))
bad = Sim()
Path(os.path.join(tmp, "cut.syx")).write_bytes(Path(bk).read_bytes()[:-30])
res = restore_from_backup(bad, os.path.join(tmp, "cut.syx"))
check("restore of a truncated backup: refuses, writes nothing", not res.ok and "write" not in bad.calls)
check("restore of a missing file", not restore_from_backup(Sim(), os.path.join(tmp, "nope.syx")).ok)
refusing = Sim(write_mode={0: "refuse"})
res = restore_from_backup(refusing, bk)
check("restore: a refused write leaves that bank unstored", not res.ok and refusing.calls.count("store") <= 1 and any("not Stored" in m for m in res.messages))

# arp pattern write with a lost reply
pat = bytes([47]) * ARP_PATTERN_SIZE
lib = KarmaGePatternLibrary.parse(KarmaGePatternLibrary.pack([GePatternEntry(47, ARP_ENCODING_NORMAL, 4, 0.95, pat)]))
arp_tables = ConvertTables(RomWaveformMap(), ExsOptionIndex.from_catalog("{}"), KarmaGeCatalog(), ProgramRefMap(), lib)


def prog_ge(ge):
    b = bytearray(3706)
    b[1814], b[1815] = ge & 0xFF, ge >> 8
    return ConvertItem(OBJ_PROGRAM, f"GE{ge}", bytes(b))


s = Sim(write_mode={0: "dropped"})   # the first write is the arp pattern
r = push_converted(s, [prog_ge(47)], dest, arp_tables, bk, arp_start=43)
check("arp lost reply, unchanged: dependent Program not written", r[0].status == "failed" and "unchanged" in msgs(r[0]))
check("arp lost reply: no Program write attempted", s.calls.count("write") == 1)

print(f"{len(fails)} violations")
sys.exit(1 if fails else 0)
