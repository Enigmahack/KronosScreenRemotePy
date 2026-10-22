"""Song Timbre Set conversion: .syx loader and push against a fake Nautilus. Run from the repo root."""
import os
import sys
import tempfile
from types import SimpleNamespace

os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_song_test_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Conversion.data import KarmaGeCatalog, ProgramRefMap, RomWaveformMap
from Conversion.push import SONG_SLOTS, ConvertItem, ConvertTables, dest_label, push_converted
from Conversion.song_source import convert_items, read_song_timbre_sets, read_songs
from Data.sng_file import MODEL_KRONOS, MODEL_NAUTILUS
from Data.song_model import SongData, from_whole_body
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
check("loads-songs", [i.name for i in items] == ["Alpha", "Beta", "Whole", "(unnamed)"])
check("bodies-are-timbre-sets", [len(i.body) for i in items] == [7810, 7810, 7810, 5640]
      and all(i.obj_type == OBJ_SONG_TIMBRE for i in items))
check("nautilus-dump-is-native", [i.native for i in items] == [False, False, False, True])
check("skips-reported", len(notes) == 2)
dx7 = os.path.join(tmp, "dx7.syx")
with open(dx7, "wb") as f:
    f.write(bytes([0xF0, 0x43, 0x00, 0x09, 0x20, 0x00]) + bytes(4096) + bytes([0x00, 0xF7]))   # DX7 32-voice bulk dump
dsongs, dnotes = read_songs(dx7)
check("dx7-is-recognised-not-a-song", not dsongs and len(dnotes) == 1 and "Yamaha" in dnotes[0])


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
rep = push_converted(svc, [items[0], items[3]], {OBJ_SONG_TIMBRE: (0, 1)}, tables, bk)
check("song-written", [r.status for r in rep] == ["written", "written"])
check("song-dest-labels", [r.dest for r in rep] == ["Song 001", "Song 002"])
check("song-body-is-nautilus-size", len(svc.slots[1]) == 5640)
check("native-body-copied-as-is", svc.slots[2] == bytes(5640))
check("song-overwrite-warned", any("Lady Zadora" in m for m in rep[0].messages)
      and not any("Replaces" in m for m in rep[1].messages))
check("song-one-store", [c for c in svc.calls if c[0] == "store"] == [("store", OBJ_SONG_TIMBRE, 0)])

rep = push_converted(FakeNautilus({}), [items[0]], {OBJ_SONG_TIMBRE: (0, SONG_SLOTS)}, tables, bk)
check("song-past-end", rep[0].status == "refused" and "last slot" in rep[0].messages[0])
check("song-label", dest_label(OBJ_SONG_TIMBRE, 0, 20) == "Song 020")


# ---- .SNG sources (real files; skipped when the fixtures are absent)
from fixture_paths import SNG_EXAMPLES

kr = os.path.join(SNG_EXAMPLES, "Kronos", "CYNTHIA-MAIN-9_9_17.SNG")
na = os.path.join(SNG_EXAMPLES, "Nautilus", "PRELOAD.SNG")
if os.path.exists(kr) and os.path.exists(na):
    songs, snotes = read_songs(kr)
    check("sng-kronos-songs", len(songs) == 6 and not snotes and all(s.has_events for s in songs))
    check("sng-kronos-layout", all(s.model == MODEL_KRONOS and len(s.timbre_set) == 7810 for s in songs))
    kitems = convert_items(songs)
    svc = FakeNautilus({})
    rep = push_converted(svc, kitems, {OBJ_SONG_TIMBRE: (0, 30)}, tables, bk)
    check("sng-kronos-converted", [r.status for r in rep] == ["written"] * 6 and all(len(b) == 5640 for b in svc.slots.values()))
    check("sng-kronos-names-carry-over", all(svc.slots[30 + i][:len(s.name)] == s.name.encode() for i, s in enumerate(songs)))
    nsongs, _ = read_songs(na)
    check("sng-nautilus-native", all(s.model == MODEL_NAUTILUS for s in nsongs) and all(i.native for i in convert_items(nsongs)))
    check("sng-whole-roundtrip", all(from_whole_body(s.whole_body()) == SongData(s.model, s.timbre_set, s.control)
                                      for s in songs + nsongs))

if fails:
    print("FAIL:", fails)
    sys.exit(1)
print("ALL PASS")
