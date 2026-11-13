"""Data.sng_file / Data.song_events against the real Kronos and Nautilus .SNG files (skipped when the fixtures are absent)
and damaged input."""
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import Data.song_events as ev
from Data.sng_file import MODEL_KRONOS, MODEL_NAUTILUS, SngError, read_sng
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


def _nested(depth):
    """A header with no songs followed by `depth` nested SNG1 containers (a crafted, hostile file)."""
    import struct
    body = b""
    for _ in range(depth):
        body = b"SNG1" + struct.pack(">II", len(body), 0x400) + body
    head = bytearray(0x50)
    head[:6] = b"KORG\x68\x01"
    struct.pack_into(">I", head, 0x20, 0x50 - 36)
    struct.pack_into(">I", head, 0x18, len(head) + len(body))
    return bytes(head) + body


def raises_cleanly(data):
    """SngError, not RecursionError (callers catch only SngError)."""
    try:
        read_sng(data)
    except SngError as e:
        return "nested" in str(e)
    except RecursionError:
        return False
    return False


check("deep-nesting-5000", raises_cleanly(_nested(5000)))
check("deep-nesting-18", raises_cleanly(_nested(18)))

files = sorted(glob.glob(os.path.join(SNG_EXAMPLES, "*", "*.SNG")))
if not files:
    print("SKIP real files: fixtures not found in", SNG_EXAMPLES)
for f in files:
    data = open(f, "rb").read()
    sng = read_sng(data)
    tag = os.path.basename(f)
    sts = {MODEL_KRONOS: 7810, MODEL_NAUTILUS: 5640}[sng.model]
    check(tag + "-songs", len(sng.songs) >= 6 and all(s.name for s in sng.songs))
    check(tag + "-sizes", all(len(s.control) == 5264 and len(s.timbre_set) == sts for s in sng.songs))
    check(tag + "-parts", all(s.parts[:4] == ("SPR1", "BMT1", "BMT2", "TRK1") for s in sng.songs))
    check(tag + "-lossless", sng.serialize() == data)
    check(tag + "-header-rows", [r[0] for r in sng.header.rows] == list(range(len(sng.songs)))
          and all(r[1] == s.event_bytes for r, s in zip(sng.header.rows, sng.songs)))
    every = [e for s in sng.songs for t in (*s.tracks, *(p for _, p in s.patterns)) for e in t]
    check(tag + "-known-events", all(e.known for e in every) and all(e.known for s in sng.songs for e in s.master))
    check(tag + "-event-roundtrip", all(ev.encode(ev.decode(ev.encode(e))) == ev.encode(e) for e in every[:5000]))
    check(tag + "-tracks-end", all(t and t[-1].kind == ev.TRK_END for s in sng.songs for t in s.tracks))
    check(tag + "-truncated", raises(data[:len(data) - 1000]))
    check(tag + "-wrong-size-header", raises(data[:0x18] + b"\x00\x00\x00\x01" + data[0x1C:]))

if fails:
    print("FAIL:", fails)
    sys.exit(1)
print("ALL PASS")
