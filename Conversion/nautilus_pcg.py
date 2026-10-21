"""Nautilus .pcg reader and single-object writer.

The container/chunk/checksum layer is the Kronos one (only the model byte differs: Kronos 0x68, Nautilus 0xDD).
The reader scans for the known bank sub-chunk tags instead of walking the outer hierarchy, as Data/pcg_file does.

Writer, checked against real files: PCG1's `size` is file length - 0x1C (payload length after the 12-byte
top-level header), on Kronos and Nautilus alike. NOT verified: real Nautilus DIV1 chunks are 52 (factory) or 60
(current OS) bytes with an extra block, and real top-level chunks carry a non-zero dwX (e.g. 0x400); this writer
emits the 44-byte Kronos-shaped DIV1 and dwX = 0. Nothing here has been loaded by a Nautilus from a file, so
writing to an instrument should go through SysEx (Core/sysex_service), not a file."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from Data.librarian_sysex import OBJ_DRUM_KIT, OBJ_PROGRAM, OBJ_WAVE_SEQ
from Data.pcg_file import PcgRejectedBank

MODEL_NAUTILUS = 0xDD
_HEADER_SIZE = 24
_BANK_CHUNK_OBJ_TYPE = {b"MBK1": OBJ_PROGRAM, b"PBK1": OBJ_PROGRAM, b"DBK1": OBJ_DRUM_KIT, b"WBK1": OBJ_WAVE_SEQ}
_TAG_RE = re.compile(rb"MBK1|PBK1|DBK1|WBK1")


@dataclass(frozen=True)
class NautilusPcgObject:
    obj_type: int
    bank_id_raw: int
    index: int
    body: bytes
    name: str
    is_exi: bool = False


@dataclass(frozen=True)
class PcgChecksumWarning:
    tag: str
    offset: int
    expected: int
    actual: int


@dataclass
class NautilusPcgFile:
    objects: List[NautilusPcgObject]
    rejected_banks: List[PcgRejectedBank]
    checksum_warnings: List[PcgChecksumWarning]


def _be32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 4], "big")


def _checksum(data: bytes, start: int, end: int) -> int:
    return sum(data[start:end]) & 0xFF


def _record_name(body: bytes) -> str:
    return body[:24].split(b"\0")[0].decode("ascii", "replace").rstrip()


def open_nautilus_pcg(data: bytes) -> Optional[NautilusPcgFile]:
    """None when `data` isn't a Nautilus PCG file (a wrong-platform or malformed file is expected input)."""
    if len(data) < 16 or data[:4] != b"KORG" or data[4] != MODEL_NAUTILUS or data[5] != 0x00:
        return None
    objects: List[NautilusPcgObject] = []
    rejected: List[PcgRejectedBank] = []
    warnings: List[PcgChecksumWarning] = []
    pos = 0
    while True:
        m = _TAG_RE.search(data, pos)
        if m is None or m.start() + _HEADER_SIZE > len(data):
            break
        offset, tag = m.start(), m.group()
        count, item_size, bank_id = _be32(data, offset + 0x0C), _be32(data, offset + 0x10), _be32(data, offset + 0x14)
        reason = None
        if not 1 <= count <= 200:
            reason = f"count {count} out of range 1..200"
        elif not 64 <= item_size <= 200_000:
            reason = f"itemSize {item_size} out of range 64..200000"
        elif offset + _HEADER_SIZE + count * item_size > len(data):
            reason = "records would run past end of file"
        if reason:
            rejected.append(PcgRejectedBank(tag.decode(), offset, count, item_size, bank_id, reason))
            pos = offset + 1
            continue
        end = offset + _HEADER_SIZE + count * item_size
        expected = _checksum(data, offset + 12, end)
        if data[offset + 11] != expected:
            warnings.append(PcgChecksumWarning(tag.decode(), offset, expected, data[offset + 11]))
        obj_type = _BANK_CHUNK_OBJ_TYPE[tag]
        for i in range(count):
            body = bytes(data[offset + _HEADER_SIZE + i * item_size:offset + _HEADER_SIZE + (i + 1) * item_size])
            objects.append(NautilusPcgObject(obj_type, bank_id, i, body, _record_name(body), tag == b"MBK1"))
        pos = end
    return NautilusPcgFile(objects, rejected, warnings)


# ── writer ───────────────────────────────────────────────────────────────────

_PROGRAM_SLOTS, _COMBI_SLOTS, _DRUM_KIT_SLOTS, _WAVE_SEQ_SLOTS = 21, 14, 15, 15   # fixed DIV1 totals


def _build_sub_chunk(tag: bytes, item_size: int, bank_id_raw: int, record: bytes) -> bytes:
    b = bytearray(_HEADER_SIZE + item_size)
    b[0:4] = tag
    b[4:8] = (16 + item_size).to_bytes(4, "big")
    b[12:16] = (1).to_bytes(4, "big")
    b[16:20] = item_size.to_bytes(4, "big")
    b[20:24] = bank_id_raw.to_bytes(4, "big")
    b[_HEADER_SIZE:] = record
    b[11] = _checksum(b, 12, len(b))
    return bytes(b)


def _top_level_chunk(tag: bytes, payload: bytes) -> bytes:
    return tag + len(payload).to_bytes(4, "big") + bytes(4) + payload


def _div1_payload(block: str) -> bytes:
    def blk(word_a: int, count: int) -> bytes:
        return word_a.to_bytes(2, "big") + count.to_bytes(2, "big") + bytes(2) + count.to_bytes(2, "big")
    p = bytes([0, 1, 0, 0, 0, 0, 0, 0])
    p += blk(1 if block == "program" else 0, _PROGRAM_SLOTS) + blk(0, _COMBI_SLOTS)
    p += blk(1 if block == "drum" else 0, _DRUM_KIT_SLOTS) + blk(1 if block == "wave" else 0, _WAVE_SEQ_SLOTS)
    return p + bytes(4)


def _write_single(block: str, top_tag: bytes, sub_tag: bytes, body: bytes) -> bytes:
    sub = _build_sub_chunk(sub_tag, len(body), 0, body)
    pcg1 = _top_level_chunk(b"PCG1", _top_level_chunk(b"DIV1", _div1_payload(block)) + _top_level_chunk(top_tag, sub))
    # byte 6 is always 0x02 in real files; byte 7 = 4 matches the current Nautilus firmware's own export; byte 8
    # is the checksum-present flag.
    header = b"KORG" + bytes([MODEL_NAUTILUS, 0x00, 0x02, 0x04, 0x01]) + bytes(7)
    return header + pcg1


def write_single_program(nautilus_body: bytes, is_exi: bool) -> bytes:
    """Body is a Nautilus-size Program (EXi 4954, HD-1 3700) as produced by convert_program."""
    return _write_single("program", b"PRG1", b"MBK1" if is_exi else b"PBK1", nautilus_body)


def write_single_drum_kit(body: bytes) -> bytes:
    return _write_single("drum", b"DKT1", b"DBK1", body)


def write_single_wave_seq(body: bytes) -> bytes:
    return _write_single("wave", b"WSQ1", b"WBK1", body)
