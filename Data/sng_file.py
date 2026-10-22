"""Reader for Korg .SNG files (Nautilus layout, verified on two real files; the Kronos layout is unverified).

File = 0x2A4-byte header, then SNG1 { SDK1, RGN1, SGS1 { SDT1 x songs } }. Every chunk is [4-byte tag][u32 BE size]
[u32 BE dwX][size bytes]. One SDT1 is one song: SPR1 (Song Control), BMT1 (Song Timbre Set), BMT2, TRK1 (the
MIDI/event tracks). SPR1 and BMT1 payloads open with a 12-byte [u32 1][u32 body length][u32 0] prefix and then hold
the same bytes as the SysEx Song Control (0x08) / Song Timbre Set (0x02) objects."""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import List, Optional, Tuple

HEADER_MAGIC = b"KORG"
FILE_TYPE_SNG = 0x01
BODY_PREFIX = 12


class SngError(ValueError):
    pass


@dataclass(frozen=True)
class Chunk:
    tag: str
    offset: int          # of the 12-byte chunk header
    size: int            # payload size
    dwx: int

    @property
    def payload(self) -> int:
        return self.offset + 12

    @property
    def end(self) -> int:
        return self.offset + 12 + self.size


@dataclass(frozen=True)
class SngSong:
    chunk: Chunk                  # the SDT1
    control: bytes                # SysEx-compatible Song Control body
    timbre_set: bytes             # SysEx-compatible Song Timbre Set body
    parts: Tuple[Chunk, ...]      # the SDT1's children, in order

    @property
    def name(self) -> str:
        return self.timbre_set[:24].split(b"\x00")[0].decode("ascii", "replace").rstrip()


@dataclass(frozen=True)
class SngFile:
    model: int
    header_size: int
    songs: Tuple[SngSong, ...]


def _chunk_at(data: bytes, off: int, end: int) -> Chunk:
    if off + 12 > end:
        raise SngError(f"Truncated chunk header at {off}.")
    tag = data[off:off + 4]
    if not all(65 <= c <= 90 or 48 <= c <= 57 for c in tag):
        raise SngError(f"No chunk tag at {off}.")
    size, dwx = struct.unpack(">II", data[off + 4:off + 12])
    if off + 12 + size > end:
        raise SngError(f"Chunk {tag.decode()} at {off} runs past its container.")
    return Chunk(tag.decode(), off, size, dwx)


def _children(data: bytes, start: int, end: int) -> List[Chunk]:
    out, off = [], start
    while off < end:
        c = _chunk_at(data, off, end)
        out.append(c)
        off = c.end
    return out


def _body(data: bytes, c: Chunk, what: str) -> bytes:
    version, length, zero = struct.unpack(">III", data[c.payload:c.payload + BODY_PREFIX])
    if length != c.size - BODY_PREFIX:
        raise SngError(f"{what} at {c.offset}: body length {length} does not match its chunk ({c.size}).")
    return data[c.payload + BODY_PREFIX:c.end]


def read_sng(data: bytes) -> SngFile:
    if data[:4] != HEADER_MAGIC or len(data) < 0x2A4:
        raise SngError("Not a Korg file.")
    if data[5] != FILE_TYPE_SNG:
        raise SngError("This is not a song (.SNG) file.")
    total, = struct.unpack(">I", data[0x18:0x1C])
    header_size, = struct.unpack(">I", data[0x20:0x24])
    if total != len(data):
        raise SngError(f"The header says {total} bytes but the file is {len(data)}.")
    sng1 = _chunk_at(data, _find_sng1(data, header_size), len(data))
    if sng1.tag != "SNG1":
        raise SngError("No SNG1 chunk.")
    top = {c.tag: c for c in _children(data, sng1.payload, sng1.end)}
    if "SGS1" not in top:
        raise SngError("No SGS1 (song set) chunk.")
    songs: List[SngSong] = []
    for sdt in _children(data, top["SGS1"].payload, top["SGS1"].end):
        if sdt.tag != "SDT1":
            raise SngError(f"Unexpected {sdt.tag} chunk in the song set at {sdt.offset}.")
        parts = _children(data, sdt.payload, sdt.end)
        by_tag = {p.tag: p for p in parts}
        if "SPR1" not in by_tag or "BMT1" not in by_tag:
            raise SngError(f"Song at {sdt.offset} has no SPR1/BMT1 chunk.")
        songs.append(SngSong(sdt, _body(data, by_tag["SPR1"], "Song Control"),
                             _body(data, by_tag["BMT1"], "Song Timbre Set"), tuple(parts)))
    return SngFile(data[4], header_size, tuple(songs))


def _find_sng1(data: bytes, header_size: int) -> int:
    i = data.find(b"SNG1", header_size - 64, header_size + 128)
    if i < 0:
        raise SngError("No SNG1 chunk near the end of the header.")
    return i


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        f = read_sng(open(p, "rb").read())
        print(p, f"model 0x{f.model:02X}", len(f.songs), "songs;",
              ", ".join(f"{s.name}({len(s.control)}/{len(s.timbre_set)})" for s in f.songs[:4]))
