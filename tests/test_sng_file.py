"""Data.sng_file against the real Nautilus .SNG files (skipped when the fixtures are absent) and damaged input."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from Data.sng_file import SngError, read_sng
from fixture_paths import SNG_EXAMPLES

fails = []


def check(name, cond):
    if not cond:
        fails.append(name)


def raises(data):
    try:
        read_sng(data)
    except SngError:
        return True
    return False


check("not-korg", raises(b"RIFF" + bytes(1000)))
check("pcg-is-not-sng", raises(b"KORG\xdd\x00\x02\x01" + bytes(1000)))

root = os.path.join(SNG_EXAMPLES, "Nautilus")
files = [os.path.join(root, n) for n in ("PRELOAD.SNG", "LTE-KeySolo.SNG")]
if not all(os.path.exists(f) for f in files):
    print("SKIP real files: fixtures not found in", root)
else:
    for f in files:
        data = open(f, "rb").read()
        sng = read_sng(data)
        tag = os.path.basename(f)
        check(tag + "-songs", len(sng.songs) >= 12 and sng.songs[0].name == "Lady Zadora")
        check(tag + "-sizes", all(len(s.control) == 5264 and len(s.timbre_set) == 5640 for s in sng.songs))
        check(tag + "-parts", all([p.tag for p in s.parts] == ["SPR1", "BMT1", "BMT2", "TRK1"] for s in sng.songs))
        check(tag + "-contiguous", all(a.chunk.end == b.chunk.offset for a, b in zip(sng.songs, sng.songs[1:])))
        check(tag + "-truncated", raises(data[:len(data) - 1000]))

if fails:
    print("FAIL:", fails)
    sys.exit(1)
print("ALL PASS")
