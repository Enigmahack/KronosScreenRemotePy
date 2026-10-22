"""Reader for Korg .SNG files. Layout verified on two Nautilus and two Kronos files (see
kronosology/docs/interfaces/sng_file_format.md).

File = header, then SNG1 { SDK1, [RGN1], SGS1 { SDT1 x songs } }. Every chunk is [4-byte tag][u32 BE size][u32 BE dwX]
[payload]. One SDT1 is one song: SPR1 (Song Control), BMT1 (Song Timbre Set), BMT2, TRK1 { MDT1 { MTK1 { MTE1 per
track }, TMA1 { EVM1 } } }. SPR1 and BMT1 payloads open with a 12-byte [u32 1][u32 body length][u32 0] prefix and then
hold the same bytes as the SysEx Song Control (0x08) / Song Timbre Set (0x02) objects. MTE1/EVM1 payloads open with
[u32 event count][u32 8][u32 0] and then hold 8-byte events (Data.song_events).

Header: 0x00 'KORG', model (0x68 Kronos, 0xDD Nautilus), file type 1; 0x18 file size; 0x20 header length (SNG1 starts at
that + 36); from 0x24: [0][song count][tail count][124] and a constant block up to 0x50, then one (index, event bytes)
row per song, then `tail count` u32s whose meaning is not decoded. `serialize` returns the file's bytes."""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import List, Optional, Tuple

import Data.song_events as ev

HEADER_MAGIC = b"KORG"
FILE_TYPE_SNG = 0x01
BODY_PREFIX = 12
ROWS_OFFSET = 0x50
SNG1_GAP = 36                       # SNG1 starts this far past the header-length field
CONTAINERS = {"SNG1", "SGS1", "SDT1", "TRK1", "MDT1", "MTK1", "TMA1", "ADT1", "ATK1", "KTA1"}
MODEL_KRONOS, MODEL_NAUTILUS = 0x68, 0xDD
AUDIO_RECORD_SIZE = 10


class SngError(ValueError):
    pass


@dataclass
class Node:
    tag: str
    dwx: int
    payload: bytes = b""                         # leaves only
    children: Optional[List["Node"]] = None      # containers only

    def find(self, tag: str) -> Optional["Node"]:
        return next((c for c in self.children or () if c.tag == tag), None)

    def serialize(self) -> bytes:
        body = self.payload if self.children is None else b"".join(c.serialize() for c in self.children)
        return self.tag.encode() + struct.pack(">II", len(body), self.dwx) + body


def _parse_nodes(data: bytes, off: int, end: int) -> List[Node]:
    out: List[Node] = []
    while off < end:
        if off + 12 > end:
            raise SngError(f"Truncated chunk header at {off}.")
        tag = data[off:off + 4]
        if not all(65 <= c <= 90 or 48 <= c <= 57 for c in tag):
            raise SngError(f"No chunk tag at {off}.")
        size, dwx = struct.unpack(">II", data[off + 4:off + 12])
        if off + 12 + size > end:
            raise SngError(f"Chunk {tag.decode()} at {off} runs past its container.")
        name = tag.decode()
        if name in CONTAINERS:
            out.append(Node(name, dwx, children=_parse_nodes(data, off + 12, off + 12 + size)))
        else:
            out.append(Node(name, dwx, payload=data[off + 12:off + 12 + size]))
        off += 12 + size
    return out


def _body(node: Node, what: str) -> bytes:
    if len(node.payload) < BODY_PREFIX:
        raise SngError(f"{what} chunk is shorter than its prefix.")
    version, length, zero = struct.unpack(">III", node.payload[:BODY_PREFIX])
    if length != len(node.payload) - BODY_PREFIX:
        raise SngError(f"{what}: body length {length} does not match its chunk ({len(node.payload)}).")
    return node.payload[BODY_PREFIX:]


def _events(node: Node, master: bool, what: str) -> List[ev.Event]:
    if len(node.payload) < 12:
        raise SngError(f"{what} chunk is shorter than its prefix.")
    count, size, zero = struct.unpack(">III", node.payload[:12])
    area = node.payload[12:]
    if size != ev.EVENT_SIZE or count * size != len(area):
        raise SngError(f"{what}: {count} events of {size} bytes do not fill {len(area)} bytes.")
    return ev.decode_all(area, master)


def _audio(node: Node, what: str) -> Tuple[int, Tuple[bytes, ...]]:
    if len(node.payload) < 12:
        raise SngError(f"{what} chunk is shorter than its prefix.")
    count, size, track = struct.unpack(">III", node.payload[:12])
    area = node.payload[12:]
    if size != AUDIO_RECORD_SIZE or count * size != len(area):
        raise SngError(f"{what}: {count} records of {size} bytes do not fill {len(area)} bytes.")
    return track, tuple(area[i:i + size] for i in range(0, len(area), size))


@dataclass(frozen=True)
class SngSong:
    node: Node                          # the SDT1
    control: bytes                      # SysEx-compatible Song Control body
    timbre_set: bytes                   # SysEx-compatible Song Timbre Set body
    bmt2: bytes                         # 36 bytes, not decoded
    tracks: Tuple[Tuple[ev.Event, ...], ...]     # one tuple of events per MTE1, in file order
    master: Tuple[ev.Event, ...]        # tempo/bar events (EVM1)
    patterns: Tuple[Tuple[int, Tuple[ev.Event, ...]], ...] = ()   # (pattern id, events) per ETA1 in ADT1/KTA1
    audio: Tuple[Tuple[int, Tuple[bytes, ...]], ...] = ()         # (track, 10-byte records) per ATE1; not decoded

    @property
    def name(self) -> str:
        return self.timbre_set[:24].split(b"\x00")[0].decode("ascii", "replace").rstrip()

    @property
    def event_bytes(self) -> int:
        """What the header's per-song row counts: every event area of the song."""
        return (ev.EVENT_SIZE * (len(self.master) + sum(len(t) for t in self.tracks)
                                 + sum(len(e) for _, e in self.patterns))
                + AUDIO_RECORD_SIZE * sum(len(r) for _, r in self.audio))

    @property
    def parts(self) -> Tuple[str, ...]:
        return tuple(c.tag for c in self.node.children or ())


@dataclass(frozen=True)
class SngHeader:
    raw: bytes
    model: int
    file_size: int
    rows: Tuple[Tuple[int, int], ...]   # (song index, event bytes)
    tail: Tuple[int, ...]               # not decoded


@dataclass(frozen=True)
class SngFile:
    header: SngHeader
    root: Node                          # SNG1
    songs: Tuple[SngSong, ...]

    @property
    def model(self) -> int:
        return self.header.model

    def serialize(self) -> bytes:
        return self.header.raw + self.root.serialize()


def read_sng(data: bytes) -> SngFile:
    if data[:4] != HEADER_MAGIC or len(data) < ROWS_OFFSET:
        raise SngError("Not a Korg file.")
    if data[5] != FILE_TYPE_SNG:
        raise SngError("This is not a song (.SNG) file.")
    total, = struct.unpack(">I", data[0x18:0x1C])
    header_len, = struct.unpack(">I", data[0x20:0x24])
    count, tail_count = struct.unpack(">II", data[0x28:0x30])
    if total != len(data):
        raise SngError(f"The header says {total} bytes but the file is {len(data)}.")
    sng1_at = header_len + SNG1_GAP
    tail_at = ROWS_OFFSET + 8 * count
    if tail_at + 4 * tail_count != sng1_at or sng1_at > len(data):
        raise SngError("The header's song table does not fit its length.")
    rows = tuple(struct.unpack(">II", data[ROWS_OFFSET + 8 * i:ROWS_OFFSET + 8 * i + 8]) for i in range(count))
    tail = struct.unpack(f">{tail_count}I", data[tail_at:sng1_at]) if tail_count else ()
    header = SngHeader(data[:sng1_at], data[4], total, rows, tuple(tail))
    top = _parse_nodes(data, sng1_at, len(data))
    if len(top) != 1 or top[0].tag != "SNG1":
        raise SngError("No single SNG1 chunk.")
    sgs = top[0].find("SGS1")
    if sgs is None:
        raise SngError("No SGS1 (song set) chunk.")
    songs: List[SngSong] = []
    for i, sdt in enumerate(sgs.children or ()):
        if sdt.tag != "SDT1":
            raise SngError(f"Unexpected {sdt.tag} chunk in the song set.")
        spr, bmt1, bmt2, trk = sdt.find("SPR1"), sdt.find("BMT1"), sdt.find("BMT2"), sdt.find("TRK1")
        if spr is None or bmt1 is None or trk is None:
            raise SngError(f"Song {i} has no SPR1/BMT1/TRK1 chunk.")
        mdt = trk.find("MDT1")
        mtk, tma = (mdt.find("MTK1"), mdt.find("TMA1")) if mdt is not None else (None, None)
        tracks = tuple(tuple(_events(m, False, f"song {i} track {j}"))
                       for j, m in enumerate(mtk.children or ())) if mtk is not None else ()
        evm = tma.find("EVM1") if tma is not None else None
        master = tuple(_events(evm, True, f"song {i} master track")) if evm is not None else ()
        adt = trk.find("ADT1")
        atk, kta = (adt.find("ATK1"), adt.find("KTA1")) if adt is not None else (None, None)
        audio = tuple(_audio(n, f"song {i} audio track {j}") for j, n in enumerate(atk.children or ())) if atk else ()
        patterns = tuple((struct.unpack(">I", n.payload[8:12])[0], tuple(_events(n, False, f"song {i} pattern {j}")))
                         for j, n in enumerate(kta.children or ())) if kta else ()
        songs.append(SngSong(sdt, _body(spr, "Song Control"), _body(bmt1, "Song Timbre Set"),
                             bmt2.payload if bmt2 is not None else b"", tracks, master, patterns, audio))
    return SngFile(header, top[0], tuple(songs))


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        f = read_sng(open(p, "rb").read())
        print(p, f"model 0x{f.model:02X}", len(f.songs), "songs;",
              ", ".join(f"{s.name}({len(s.tracks)} trk, {s.event_bytes} B)" for s in f.songs[:4]))
