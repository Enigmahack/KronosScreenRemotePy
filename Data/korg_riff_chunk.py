"""Shared chunk framing for the .KMP/.KSF family — port of C#'s
Core/Sample/KorgRiffChunk.cs.

Framing: [4-byte ASCII tag][4-byte big-endian length][payload], no outer file
header (CKorgRiff). See kronosology/docs/interfaces/ksc_kmp_ksf_file_format.md
for the format this implements and its hardware-confirmation history — every
field/behavior here is a direct read of Korg's own decompiled Eva source
(CKorgKsf/CKorgKmp/CKorgRiff), independently verified against real Kronos
hardware.
"""
from __future__ import annotations

from typing import List, Tuple


def read_chunks(data: bytes) -> List[Tuple[str, bytes]]:
    """Scan `data` for [tag][BE u32 length][payload] chunks until it runs out.

    Advances by the CLAMPED payload length, never the raw declared one — a
    corrupt/oversized length must not step backwards (infinite loop) or past
    the end of the buffer (out-of-range slice). Well-formed files are
    unaffected (there payloadLen == length); a garbage length simply consumes
    the rest of the buffer as one chunk's payload rather than throwing, so
    callers (KsfSample.Open/KmpMultisample.Open) get an empty-ish result
    instead of an exception on truncated/corrupt input.
    """
    chunks: List[Tuple[str, bytes]] = []
    pos = 0
    while pos + 8 <= len(data):
        tag = data[pos:pos + 4].decode("ascii", errors="replace")
        length = read_u32be(data, pos + 4)
        payload_len = max(0, min(length, len(data) - pos - 8))
        payload = bytes(data[pos + 8: pos + 8 + payload_len])
        chunks.append((tag, payload))
        pos += 8 + payload_len
    return chunks


def build_chunk(tag: str, payload: bytes) -> bytes:
    if len(tag) != 4:
        raise ValueError(f"chunk tag must be 4 chars: '{tag}'")
    out = bytearray(8 + len(payload))
    out[0:4] = tag.encode("ascii")
    write_u32be(out, 4, len(payload))
    out[8:] = payload
    return bytes(out)


def pad_bytes(s: str, n: int) -> bytes:
    """Space-pad (or truncate) to exactly n bytes — the convention every real
    Korg name field in this family uses."""
    b = s.encode("ascii", errors="replace")[:n]
    return b + b" " * (n - len(b))


def split_name_suffix(text: str) -> Tuple[str, str]:
    """Split a decoded, possibly stereo-suffixed name into (base, suffix),
    suffix being "", "-L", or "-R". Every real name field in this family
    (SMP1/MSP1's short name, NAME's 24-byte field) carries the same logical
    name+suffix, just independently padded per field width."""
    stripped = text.rstrip()
    if stripped.endswith("-L") or stripped.endswith("-R"):
        return stripped[:-2].rstrip(), stripped[-2:]
    return stripped, ""


def encode_name_field(base_name: str, suffix: str, width: int) -> bytes:
    """Encode base+suffix into a `width`-byte space-padded field with the
    suffix RIGHT-ALIGNED at the very end — confirmed independently for both
    the 16/18-byte short name and the 24-byte NAME chunk, each at its own
    width."""
    base_name = base_name[:max(0, width - len(suffix))]
    pad = width - len(base_name) - len(suffix)
    text = base_name + (" " * max(0, pad)) + suffix
    b = text.encode("ascii", errors="replace")[:width]
    return b + b" " * (width - len(b))


def read_u32be(data: bytes, offset: int) -> int:
    return (data[offset] << 24) | (data[offset + 1] << 16) | (data[offset + 2] << 8) | data[offset + 3]


def write_u32be(data: bytearray, offset: int, value: int) -> None:
    data[offset] = (value >> 24) & 0xFF
    data[offset + 1] = (value >> 16) & 0xFF
    data[offset + 2] = (value >> 8) & 0xFF
    data[offset + 3] = value & 0xFF


def u32be_bytes(value: int) -> bytes:
    out = bytearray(4)
    write_u32be(out, 0, value)
    return bytes(out)
