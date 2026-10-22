"""Data.sng_writer rebuilds the real .SNG files byte-for-byte from their parsed parts (skipped when the fixtures are absent)."""
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from Data.sng_file import read_sng
from Data.sng_writer import SngBuildError, build_sng, parts_from_sng
from fixture_paths import SNG_EXAMPLES

fails = []


def check(name, cond):
    if not cond:
        fails.append(name)


files = sorted(glob.glob(os.path.join(SNG_EXAMPLES, "*", "*.SNG")))
if not files:
    print("SKIP real files: fixtures not found in", SNG_EXAMPLES)
for f in files:
    data = open(f, "rb").read()
    sng = read_sng(data)
    tag = os.path.basename(f)
    rgn = sng.root.find("RGN1")
    rebuilt = build_sng(sng.model, [parts_from_sng(s) for s in sng.songs], rgn.payload if rgn else None, sng.header.tail)
    check(tag + "-rebuilt-identical", rebuilt == data)
    if rebuilt != data:
        first = next((i for i in range(min(len(rebuilt), len(data))) if rebuilt[i] != data[i]), None)
        print("  first difference at", first, "sizes", len(rebuilt), len(data))
    # a subset: still a valid file whose songs are the ones chosen
    pick = [parts_from_sng(sng.songs[i]) for i in (0, 2) if i < len(sng.songs)]
    sub = read_sng(build_sng(sng.model, pick))
    check(tag + "-subset", [s.name for s in sub.songs] == [sng.songs[i].name for i in (0, 2)]
          and [s.timbre_set for s in sub.songs] == [sng.songs[i].timbre_set for i in (0, 2)]
          and all(r[1] == s.event_bytes for r, s in zip(sub.header.rows, sub.songs)))

try:
    build_sng(0x68, [])
    check("empty-refused", False)
except SngBuildError:
    pass

if fails:
    print("FAIL:", fails)
    sys.exit(1)
print("ALL PASS")
