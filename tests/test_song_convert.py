"""Kronos <-> Nautilus Song/.SNG conversion (real .SNG fixtures; skipped when absent)."""
import glob
import os
import sys
import tempfile

os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_songconv_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from Conversion.combi_reverse import convert_combi_to_kronos
from Conversion.push import ConvertTables
from Conversion.song_convert import convert_sng, convert_song, convert_timbre_set
from Data.sng_file import MODEL_KRONOS, MODEL_NAUTILUS, read_sng
from Data.song_model import from_sng_song
from fixture_paths import SNG_EXAMPLES

fails = []


def check(name, cond):
    if not cond:
        fails.append(name)


tables = ConvertTables.load()
check("size-refused", convert_combi_to_kronos(b"x").body is None)
check("same-model-is-identity", convert_timbre_set(bytes(5640), MODEL_NAUTILUS, MODEL_NAUTILUS, tables)[0] == bytes(5640))

kr = sorted(glob.glob(os.path.join(SNG_EXAMPLES, "Kronos", "*.SNG")))
na = sorted(glob.glob(os.path.join(SNG_EXAMPLES, "Nautilus", "*.SNG")))
if not kr or not na:
    print("SKIP real files: fixtures not found in", SNG_EXAMPLES)
else:
    sng = read_sng(open(kr[0], "rb").read())
    for s in sng.songs:
        ts = s.timbre_set
        n, _, ref = convert_timbre_set(ts, MODEL_KRONOS, MODEL_NAUTILUS, tables)
        check("k2n-ok", n is not None and len(n) == 5640 and not ref and n[:24] == ts[:24])
        back, _, ref = convert_timbre_set(n, MODEL_NAUTILUS, MODEL_KRONOS, tables, template=ts)
        check("n2k-ok", back is not None and len(back) == 7810 and not ref)
        # shared head and every timbre field except the documented ones survive the round trip
        check("roundtrip-shared-head", back[:1268] == ts[:1268])
        base = 4790 + (2632 - 2620)
        same_timbres = 0
        for t in range(16):
            o = 2632 + 2170 + t * 188
            a, b = ts[o:o + 188], back[o:o + 188]
            differing = [i for i in range(188) if a[i] != b[i]]
            # only the Kronos-only bits (+30, +31, +35) and the Program reference (+0, +1) may differ
            same_timbres += all(i in (0, 1, 30, 31, 35) for i in differing)
        check("roundtrip-timbres-intact", same_timbres == 16)
        check("template-restores-karma-block", back[1268:4790] == ts[1268:4790])

    # whole files, both directions
    k2n, w, ref = convert_sng(open(kr[0], "rb").read(), MODEL_NAUTILUS, tables)
    check("file-k2n", k2n is not None and not ref)
    out = read_sng(k2n)
    orig = read_sng(open(kr[0], "rb").read())
    check("file-k2n-model", out.model == MODEL_NAUTILUS and all(len(s.timbre_set) == 5640 for s in out.songs))
    check("file-k2n-events-untouched", [s.tracks for s in out.songs] == [s.tracks for s in orig.songs]
          and [s.master for s in out.songs] == [s.master for s in orig.songs])
    check("file-k2n-control-untouched", [s.control for s in out.songs] == [s.control for s in orig.songs])
    check("file-k2n-names", [s.name for s in out.songs] == [s.name for s in orig.songs])
    check("file-k2n-rows", all(r[1] == s.event_bytes for r, s in zip(out.header.rows, out.songs)))
    back, w2, ref = convert_sng(k2n, MODEL_KRONOS, tables)
    check("file-n2k", back is not None and read_sng(back).model == MODEL_KRONOS and not ref)

    n2k, w3, ref = convert_sng(open(na[1], "rb").read(), MODEL_KRONOS, tables, only=[0, 2])
    check("file-n2k-subset", n2k is not None and [s.name for s in read_sng(n2k).songs]
          == [read_sng(open(na[1], "rb").read()).songs[i].name for i in (0, 2)])
    check("subset-drops-regions-with-warning", any("RGN1" in m for m in w3))
    check("garbage-refused", convert_sng(b"not a file", MODEL_KRONOS, tables)[0] is None)

if fails:
    print("FAIL:", sorted(set(fails)))
    sys.exit(1)
print("ALL PASS")
