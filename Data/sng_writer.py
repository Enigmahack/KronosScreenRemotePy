"""Builds a .SNG file (layout: Data.sng_file). Verified by rebuilding all four reference files byte-for-byte from their parsed
parts (tests/test_sng_writer.py).

What is derived: the header (counts, sizes, per-song rows), SDK1 (per song: name, 5264 + event bytes, MIDI/master/audio/
pattern byte counts, absolute offset and size of its SDT1), every chunk size, and the BMT1 `dwX` (object version << 16 | 0x100).
What is carried unchanged: each song's BMT2 and TRK1, the RGN1 audio-region table and the header's tail list (one entry per
region). Songs that use audio tracks reference .WAV files the .SNG does not contain."""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from Data.sng_file import (BODY_PREFIX, MODEL_KRONOS, MODEL_NAUTILUS, ROWS_OFFSET, SNG1_GAP, Node, SngSong)
from Data.song_model import CONTROL_SIZE, SongData

HEADER_CONSTANT = bytes.fromhex(
    "4b4f524700010201" "0000000000000000" "0000ffff00000000" "0000000000000000" "0000000000000000"
    "0000000000000000" "0000007c00000000" "0000000400000008" "0010000100100000" "0808" "0a08" "0800" "0000")
OBJECT_VERSION = {MODEL_KRONOS: 3, MODEL_NAUTILUS: 4}      # of the Song Timbre Set (SysEx version byte)
SDK1_RECORD = 64


class SngBuildError(ValueError):
    pass


@dataclass(frozen=True)
class SongParts:
    control: bytes
    timbre_set: bytes
    bmt2: bytes
    trk1: Node


def parts_of(song: SongData) -> SongParts:
    """The pieces to write for a song that carries its events."""
    if song.events is None:
        raise SngBuildError(f"Song {song.name!r} has no events (it came over SysEx): it cannot be written to a .SNG.")
    return parts_from_sng(song.events)


def parts_from_sng(s: SngSong) -> SongParts:
    trk1 = s.node.find("TRK1")
    if trk1 is None:
        raise SngBuildError("The song has no TRK1 chunk.")
    return SongParts(s.control, s.timbre_set, s.bmt2, trk1)


def _prefixed(body: bytes, index: int) -> bytes:
    """[1][body length][song index in the file] + body."""
    return struct.pack(">III", 1, len(body), index) + body


def _sdt1(model: int, p: SongParts, index: int) -> Node:
    return Node("SDT1", 0x400, children=[
        Node("SPR1", 0x100, _prefixed(p.control, index)),
        Node("BMT1", (OBJECT_VERSION[model] << 16) | 0x100, _prefixed(p.timbre_set, index)),
        Node("BMT2", 0, p.bmt2),
        p.trk1,
    ])


def _area_bytes(node: Node, tag: str, size: int) -> int:
    """Event bytes of every `tag` leaf below `node` (their payloads minus the 12-byte count/size/id prefix)."""
    total = 0
    stack = [node]
    while stack:
        n = stack.pop()
        if n.children is None:
            if n.tag == tag:
                total += len(n.payload) - 12
        else:
            stack.extend(n.children)
    return total


def _record(name: bytes, trk1: Node, sdt_offset: int, sdt_size: int) -> bytes:
    mte, evm = _area_bytes(trk1, "MTE1", 8), _area_bytes(trk1, "EVM1", 8)
    ate, eta = _area_bytes(trk1, "ATE1", 10), _area_bytes(trk1, "ETA1", 8)
    words = (CONTROL_SIZE + mte + evm + ate + eta, mte, evm, ate, eta, 0, 0, sdt_offset, sdt_size, sdt_size)
    return name.ljust(24, b"\x00")[:24] + struct.pack("<10I", *words)


def build_sng(model: int, songs: Sequence[SongParts], regions: Optional[bytes] = None,
              tail: Sequence[int] = ()) -> bytes:
    """`regions` is an RGN1 payload (carried opaque); `tail` has one entry per region."""
    if model not in OBJECT_VERSION:
        raise SngBuildError(f"Unknown instrument type 0x{model:02X}.")
    if not songs:
        raise SngBuildError("A .SNG needs at least one song.")
    for p in songs:
        if len(p.control) != CONTROL_SIZE:
            raise SngBuildError(f"A Song Control is {CONTROL_SIZE} bytes, not {len(p.control)}.")
    sdts = [_sdt1(model, p, i) for i, p in enumerate(songs)]
    sizes = [len(n.serialize()) - 12 for n in sdts]
    header_len = ROWS_OFFSET + 8 * len(songs) + 4 * len(tail)
    sng1_at = header_len                                  # header_len field = this - 36, SNG1 follows the table
    rgn = Node("RGN1", 0x100, regions) if regions is not None else None
    sdk_size = 12 + SDK1_RECORD * len(songs)
    # file offset of the first SDT1 payload: SNG1 header + SDK1 chunk (+ RGN1 chunk) + SGS1 header + SDT1 header
    first = sng1_at + 12 + (12 + sdk_size) + ((12 + len(regions)) if rgn else 0) + 12 + 12
    offsets, off = [], first
    for size in sizes:
        offsets.append(off)
        off += size + 12
    sdk = struct.pack(">III", len(songs), SDK1_RECORD, 0) + b"".join(
        _record(p.timbre_set[:24].split(b"\x00")[0], p.trk1, o, s) for p, o, s in zip(songs, offsets, sizes))
    top = Node("SNG1", 0x400, children=[Node("SDK1", 0x100, sdk)] + ([rgn] if rgn else [])
               + [Node("SGS1", 0x400, children=sdts)])
    body = top.serialize()
    rows = b"".join(struct.pack(">II", i, _event_bytes(p.trk1)) for i, p in enumerate(songs))
    head = bytearray(HEADER_CONSTANT)
    head[4] = model
    head[0x18:0x1C] = struct.pack(">I", sng1_at + len(body))
    head[0x20:0x24] = struct.pack(">I", sng1_at - SNG1_GAP)
    head[0x28:0x30] = struct.pack(">II", len(songs), len(tail))
    return bytes(head) + rows + struct.pack(f">{len(tail)}I", *tail) + body


def _event_bytes(trk1: Node) -> int:
    return (_area_bytes(trk1, "MTE1", 8) + _area_bytes(trk1, "EVM1", 8) + _area_bytes(trk1, "ATE1", 10)
            + _area_bytes(trk1, "ETA1", 8))
