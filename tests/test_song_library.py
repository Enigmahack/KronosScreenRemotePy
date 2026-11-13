"""Data.song_library: import from .SNG and SysEx forms, dedupe, export with conversion (real .SNG fixtures)."""
import glob
import os
import sys
import tempfile
import pathlib

os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_songlib_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from Conversion.push import ConvertTables
from Conversion.song_source import read_songs
from Data.sng_file import MODEL_KRONOS, MODEL_NAUTILUS, read_sng
from Data.song_library import SongLibrary
from Data.song_model import SongData
from fixture_paths import SNG_EXAMPLES

fails = []


def check(name, cond):
    if not cond:
        fails.append(name)


kr = sorted(glob.glob(os.path.join(SNG_EXAMPLES, "Kronos", "*.SNG")))
na = sorted(glob.glob(os.path.join(SNG_EXAMPLES, "Nautilus", "*.SNG")))
if not kr or not na:
    print("SKIP real files: fixtures not found in", SNG_EXAMPLES)
else:
    root = pathlib.Path(tempfile.mkdtemp(prefix="kr_songlib_store_"))
    lib = SongLibrary(root)
    songs, _ = read_songs(kr[0])
    added, notes = lib.add(songs, "test.SNG")
    # the file holds "WELCOME CHRISTMAS" twice with different Timbre Sets: both are kept
    check("distinct-songs-kept", len(added) == 6 and len(lib.entries()) == 6 and not any("already" in n for n in notes))
    again, notes2 = lib.add(songs, "test.SNG")
    check("readd-adds-nothing", not again and len(lib.entries()) == 6 and len(notes2) == 6)
    e = next(x for x in lib.entries() if x.name == "CHRISTMAS CANON")
    check("entry-stats", e.model == MODEL_KRONOS and e.has_events and e.tracks > 0 and e.notes > 100 and e.bars > 0)
    got = lib.get(e.id)
    orig = next(s for s in songs if s.name == "CHRISTMAS CANON")
    check("get-roundtrip", got.timbre_set == orig.timbre_set and got.control == orig.control
          and got.events.tracks == orig.events.tracks and got.events.master == orig.events.master)
    check("stored-file-is-a-real-sng", read_sng(lib.path_of(e.id).read_bytes()).songs[0].name == "CHRISTMAS CANON")

    # persistence
    check("reopen", len(SongLibrary(root).entries()) == 6)

    # SysEx-form song: kept, but cannot be exported
    nsongs, _ = read_songs(na[0])
    sysex_only = SongData(MODEL_NAUTILUS, nsongs[0].timbre_set, nsongs[0].control)
    ids, _ = lib.add([sysex_only], "instrument")
    check("sysex-song-stored", len(ids) == 1 and not lib.entries()[-1].has_events)
    check("sysex-song-roundtrip", lib.get(ids[0]) == sysex_only)
    tables = ConvertTables.load()
    out, w, ref = lib.export_sng(ids, MODEL_KRONOS, tables)
    check("sysex-song-not-exportable", out is None and ref and "no MIDI data" in ref[0])

    # export with conversion
    ids = [x.id for x in lib.entries() if x.name in ("CHRISTMAS CANON", "WE RISE AGAIN")]
    data, w, ref = lib.export_sng(ids, MODEL_NAUTILUS, tables)
    out = read_sng(data)
    check("export-converted", not ref and out.model == MODEL_NAUTILUS and sorted(s.name for s in out.songs)
          == ["CHRISTMAS CANON", "WE RISE AGAIN"] and all(len(s.timbre_set) == 5640 for s in out.songs))
    data2, _, _ = lib.export_sng(ids, MODEL_KRONOS, tables)
    check("export-same-model", read_sng(data2).model == MODEL_KRONOS)

    n_before = len(lib.entries())
    gone = lib.path_of(e.id)
    check("remove", lib.remove([e.id]) == 1 and len(lib.entries()) == n_before - 1 and not gone.exists())

    # a damaged index is kept aside and rebuilt from the song files, never treated as empty
    n_now = len(lib.entries())
    names_now = sorted(x.name for x in lib.entries())
    (root / "index.json").write_text("{ this is not json", encoding="utf-8")
    (root / ".index.json.deadbeef.tmp").write_text("leftover", encoding="utf-8")   # atomic-writer debris
    lib2 = SongLibrary(root)
    check("corrupt-rebuilt", len(lib2.entries()) == n_now and sorted(x.name for x in lib2.entries()) == names_now)
    check("corrupt-kept-aside", len(list(root.glob("index.json.corrupt-*"))) == 1)
    check("corrupt-note", lib2.recovered_note is not None and "rebuilt" in lib2.recovered_note)
    check("corrupt-recovered-source", all(x.source == "recovered" for x in lib2.entries()))
    check("corrupt-rebuilt-has-stats", any(x.has_events and x.notes > 0 for x in lib2.entries()))
    check("corrupt-sysex-kind", any(not x.has_events for x in lib2.entries()))
    # the rebuilt index was saved: adding now keeps every earlier song
    more, _ = lib2.add(read_songs(kr[-1])[0], "more.SNG")
    check("add-after-rebuild-keeps-old", len(SongLibrary(root).entries()) == n_now + len(more))
    # a missing index with song files present is rebuilt too
    (root / "index.json").unlink()
    check("missing-index-rebuilt", len(SongLibrary(root).entries()) == n_now + len(more))
    # an unreadable song file is skipped, not deleted
    bad = root / "01-0000000000000000.SNG"
    bad.write_bytes(b"not a song")
    (root / "index.json").write_text("[]", encoding="utf-8")         # valid JSON, wrong shape
    lib3 = SongLibrary(root)
    check("bad-file-skipped", bad.exists() and len(lib3.entries()) == n_now + len(more)
          and "could not be read" in (lib3.recovered_note or ""))
    check("no-tmp-left", not [p for p in root.iterdir() if p.name.endswith(".tmp") and "deadbeef" not in p.name])

if fails:
    print("FAIL:", sorted(set(fails)))
    sys.exit(1)
print("ALL PASS")
