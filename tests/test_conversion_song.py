"""Song Timbre Set conversion: .syx loader and push against a fake Nautilus. Run from the repo root."""
import os
import sys
import tempfile
from types import SimpleNamespace

os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_song_test_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Conversion.data import KarmaGeCatalog, ProgramRefMap, RomWaveformMap
from Conversion.push import SONG_SLOTS, ConvertItem, ConvertTables, dest_label, push_converted
from Conversion.song_source import read_song_timbre_sets
import Data.librarian_sysex as kx
import Data.nautilus_sysex as nsx
from Data.librarian_sysex import OBJ_COMBI, OBJ_SONG_TIMBRE
from Tools.exs_option_index import ExsOptionIndex

fails = []


def check(name, cond):
    if not cond:
        fails.append(name)


def kronos_song(name):
    b = bytearray(7810)
    b[:24] = name.ljust(24, "\0").encode()
    return bytes(b)


tmp = tempfile.mkdtemp()
syx = os.path.join(tmp, "songs.syx")
with open(syx, "wb") as f:
    f.write(kx.object_dump_write(OBJ_SONG_TIMBRE, 0, 3, 3, kronos_song("Alpha")))
    f.write(kx.object_dump_write(OBJ_SONG_TIMBRE, 0, 4, 3, kronos_song("Beta")))
    f.write(kx.object_dump_write(0x18, 0, 5, 3, kronos_song("Whole") + bytes(2) + bytes(5264)))   # whole Song
    f.write(kx.object_dump_write(OBJ_COMBI, 0, 0, 3, bytes(7810)))                                   # not a song
    f.write(kx.object_dump_write(OBJ_SONG_TIMBRE, 0, 6, 3, bytes(100)))                              # too short
    f.write(nsx.object_dump_write(OBJ_SONG_TIMBRE, 0, 7, 4, bytes(5640)))                            # Nautilus dump
items, notes = read_song_timbre_sets(syx)
check("loads-three-songs", [i.name for i in items] == ["Alpha (song 3)", "Beta (song 4)", "Whole (song 5)"])
check("bodies-are-timbre-sets", all(len(i.body) == 7810 and i.obj_type == OBJ_SONG_TIMBRE for i in items))
check("skips-reported", len(notes) == 3)


class FakeNautilus:
    def __init__(self, names):
        self.slots, self.calls, self.names = {}, [], names

    def dump_object_parsed(self, obj, bank, index, **kw):
        if (obj, bank) != (OBJ_SONG_TIMBRE, 0) or index >= SONG_SLOTS:
            return None
        body = self.slots.get(index) or self.names.get(index, f"NEW SONG {index:03d}").ljust(24, "\0").encode() + bytes(5616)
        return SimpleNamespace(version=4, body=body)

    def backup_objects(self, ops, path):
        self.calls.append(("backup", len(list(ops))))
        with open(path, "wb") as f:
            for op in ops:
                f.write(nsx.object_dump_write(op.obj, op.bank, op.index, op.version, op.body))

    def write_object(self, op):
        self.calls.append(("write", op.index))
        self.slots[op.index] = op.body
        return 0

    def store_bank(self, obj, bank):
        self.calls.append(("store", obj, bank))
        return 0


tables = ConvertTables(RomWaveformMap(), ExsOptionIndex.from_catalog("{}"), KarmaGeCatalog(), ProgramRefMap())
bk = os.path.join(tmp, "b.syx")
svc = FakeNautilus({1: "Lady Zadora"})
rep = push_converted(svc, items[:2], {OBJ_SONG_TIMBRE: (0, 1)}, tables, bk)
check("song-written", [r.status for r in rep] == ["written", "written"])
check("song-dest-labels", [r.dest for r in rep] == ["Song 001", "Song 002"])
check("song-body-is-nautilus-size", len(svc.slots[1]) == 5640)
check("song-overwrite-warned", any("Lady Zadora" in m for m in rep[0].messages)
      and not any("Replaces" in m for m in rep[1].messages))
check("song-one-store", [c for c in svc.calls if c[0] == "store"] == [("store", OBJ_SONG_TIMBRE, 0)])

rep = push_converted(FakeNautilus({}), [items[0]], {OBJ_SONG_TIMBRE: (0, SONG_SLOTS)}, tables, bk)
check("song-past-end", rep[0].status == "refused" and "last slot" in rep[0].messages[0])
check("song-label", dest_label(OBJ_SONG_TIMBRE, 0, 20) == "Song 020")

if fails:
    print("FAIL:", fails)
    sys.exit(1)
print("ALL PASS")
