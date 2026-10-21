"""SP3 (backup): no write happens on a backup that was not verified, and SysExService.backup_objects is atomic.
push_converted re-parses the backup it just asked for and aborts, with zero writes and a message, when the file is
missing, truncated, holds the wrong object count or wrong bodies. backup_objects writes a temp file, fsyncs and
moves it into place, so a failure part-way never leaves a truncated file under the real name or clobbers a
previous backup."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("KRONOS_DATA_DIR", tempfile.mkdtemp(prefix="kr_data_"))
from pathlib import Path
from types import SimpleNamespace
import Data.nautilus_sysex as nsx
from Conversion.data import KarmaGeCatalog, ProgramRefMap, RomWaveformMap
from Conversion.push import ConvertItem, ConvertTables, push_converted
from Core.sysex_service import SysExService
from Data.librarian_sysex import OBJ_PROGRAM
from Tools.exs_option_index import ExsOptionIndex

fails = []


def check(name, cond):
    if not cond:
        print("FAIL " + name)
        fails.append(name)


class Svc:
    def __init__(self, backup):
        self.backup = backup
        self.calls = []
        self.slots = {}
        self._codec = nsx

    def dump_object_parsed(self, obj, bank, index, **kw):
        return SimpleNamespace(version=6, body=self.slots.get((obj, bank, index), bytes([index + 1]) * 3700))

    def backup_objects(self, ops, path):
        self.calls.append("backup")
        self.backup(self, list(ops), path)

    def write_object(self, op):
        self.calls.append("write")
        self.slots[(op.obj, op.bank, op.index)] = op.body
        return 0

    def store_bank(self, obj, bank):
        self.calls.append("store")
        return 0


def good(self, ops, path):
    SysExService.backup_objects(self, ops, path)


def truncated(self, ops, path):
    SysExService.backup_objects(self, ops, path)
    Path(path).write_bytes(Path(path).read_bytes()[:-40])


def short(self, ops, path):
    SysExService.backup_objects(self, ops[:-1], path)


def wrong_body(self, ops, path):
    ops = [SimpleNamespace(obj=o.obj, bank=o.bank, index=o.index, version=o.version, body=bytes(len(o.body)))
           for o in ops]
    SysExService.backup_objects(self, ops, path)


def nothing(self, ops, path):
    pass


def garbage(self, ops, path):
    Path(path).write_bytes(b"x")


tables = ConvertTables(RomWaveformMap(), ExsOptionIndex.from_catalog("{}"), KarmaGeCatalog(), ProgramRefMap())
items = [ConvertItem(OBJ_PROGRAM, n, bytes(3706)) for n in "abc"]
dest = {OBJ_PROGRAM: (0x4C, 0)}
tmp = tempfile.mkdtemp(prefix="kr_sp3b_")

for name, fn, ok in (("good", good, True), ("truncated", truncated, False), ("missing a record", short, False),
                     ("wrong bodies", wrong_body, False), ("no file", nothing, False), ("garbage", garbage, False)):
    path = os.path.join(tmp, f"{name}.syx")
    s = Svc(fn)
    r = push_converted(s, items, dest, tables, path)
    if ok:
        check(f"{name}: converts", [x.status for x in r] == ["written"] * 3)
    else:
        check(f"{name}: no write or Store", "write" not in s.calls and "store" not in s.calls)
        check(f"{name}: every item failed with the backup named as the reason",
              all(x.status == "failed" and any("Backup could not be verified" in m for m in x.messages) for x in r))

# atomic backup_objects
ops = [SimpleNamespace(obj=OBJ_PROGRAM, bank=0x4C, index=i, version=6, body=bytes([i]) * 3700) for i in range(3)]
stub = SimpleNamespace(_codec=nsx)
path = os.path.join(tmp, "atomic.syx")
SysExService.backup_objects(stub, ops, path)
check("backup_objects: file written, no temp left", os.path.exists(path) and not os.path.exists(path + ".tmp"))
before = Path(path).read_bytes()
bad = ops + [SimpleNamespace(obj=OBJ_PROGRAM, bank=0x4C, index=3, version=6, body=None)]   # fails after 3 records
try:
    SysExService.backup_objects(stub, bad, path)
    check("backup_objects: a failing record raises", False)
except Exception:
    pass
check("backup_objects: failure leaves the previous backup intact", Path(path).read_bytes() == before)
check("backup_objects: failure leaves no temp file", not os.path.exists(path + ".tmp"))
fresh = os.path.join(tmp, "fresh.syx")
try:
    SysExService.backup_objects(stub, bad, fresh)
except Exception:
    pass
check("backup_objects: a failed first write leaves nothing under the real name", not os.path.exists(fresh))

print(f"{len(fails)} violations")
sys.exit(1 if fails else 0)
