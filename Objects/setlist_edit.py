"""Field-level edits of a Set List body (obj 0x0D) for the Set List editor: slot transpose, slot order, and the
Set List's EQ block. Pure bytes in, bytes out. Layout: SysexInfo/MIDI implementation/SetList.txt.

A slot is 542 bytes (543 on the Nautilus, a Dynamics byte at +30); its transpose is a signed 6-bit value split over
+25 bits 7-5 (high 3) and +29 bits 7-5 (low 3). The EQ block follows the 128 slots: byte 0 bit 0 = bypass and bits
7-1 = input trim (0-99), then nine signed band levels in 0.5 dB steps (-18.0 .. +18.0).
HW-VERIFY: the EQ block offset and 0.5 dB step are from the Kronos table; on the Nautilus the block is assumed to
follow the 128 wider slots the same way (read a Nautilus set list's EQ on the unit and compare)."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import List, Tuple

from Objects import object_body as ob

BAND_COUNT = 9
TRANSPOSE_RANGE = (-24, 24)
_EQ_LEN = 1 + BAND_COUNT


def _stride(body: bytes) -> int:
    return ob.SLOT_SIZE + (1 if len(body) == 69544 else 0)


def _slot_base(body: bytes, slot: int) -> int:
    return ob.SLOT_BASE + slot * _stride(body)


def slot_transpose(body: bytes, slot: int) -> int:
    b = _slot_base(body, slot)
    raw = ((body[b + 25] >> 5) << 3) | (body[b + 29] >> 5)
    return raw - 64 if raw >= 32 else raw


def write_slot_transpose(body: bytes, slot: int, semitones: int) -> bytes:
    lo, hi = TRANSPOSE_RANGE
    raw = max(lo, min(hi, int(semitones))) & 0x3F
    out, b = bytearray(body), _slot_base(body, slot)
    out[b + 25] = (out[b + 25] & 0x1F) | ((raw >> 3) << 5)
    out[b + 29] = (out[b + 29] & 0x1F) | ((raw & 7) << 5)
    return bytes(out)


def move_slot(body: bytes, src: int, dst: int) -> bytes:
    """Take slot `src` out and insert it so it ends up at position `dst`; the slots between shift by one."""
    n, stride = ob.SLOT_COUNT, _stride(body)
    start = ob.SLOT_BASE
    chunks: List[bytes] = [body[start + i * stride:start + (i + 1) * stride] for i in range(n)]
    chunks.insert(dst, chunks.pop(src))
    return body[:start] + b"".join(chunks) + body[start + n * stride:]


def slot_bytes(body: bytes, slot: int) -> bytes:
    """The whole slot (name, reference, colour, transpose, comments); same-length bodies only exchange these."""
    s = _stride(body)
    return body[ob.SLOT_BASE + slot * s:ob.SLOT_BASE + (slot + 1) * s]


def put_slot_bytes(body: bytes, slot: int, chunk: bytes) -> bytes:
    """Overwrite a slot with `chunk`; a chunk from the other family (different length) is refused."""
    s = _stride(body)
    if len(chunk) != s:
        raise ValueError("slot is from the other instrument family")
    o = ob.SLOT_BASE + slot * s
    return body[:o] + chunk + body[o + s:]


def swap_slots(body: bytes, a: int, b: int) -> bytes:
    if a == b:
        return body
    ca, cb = slot_bytes(body, a), slot_bytes(body, b)
    return put_slot_bytes(put_slot_bytes(body, a, cb), b, ca)


def clear_slot(body: bytes, slot: int) -> bytes:
    """Empty a slot: blank name and zeroed fields (an unnamed slot is how both instruments show an empty one)."""
    return put_slot_bytes(body, slot, b" " * ob.NAME_LEN + bytes(_stride(body) - ob.NAME_LEN))


def slot_ref(body: bytes, slot: int):
    """(type 0 Combi / 1 Program / 2 Song, func-33 bank, index): one numbering for both instruments."""
    from Data.librarian_sysex import setlist_slot_ref
    return setlist_slot_ref(body, slot)


def set_slot_ref(body: bytes, slot: int, type_: int, func33_bank: int, index: int) -> bytes:
    from Data.librarian_sysex import set_setlist_slot_ref
    out = bytearray(body)
    set_setlist_slot_ref(out, slot, func33_bank=func33_bank, index=index, type_=type_)
    return bytes(out)


@dataclass(frozen=True)
class SetListEq:
    bypass: bool
    input_trim: int                    # 0..99
    bands: Tuple[float, ...]           # nine levels, dB


def _eq_base(body: bytes) -> int:
    return ob.SLOT_BASE + ob.SLOT_COUNT * _stride(body)


def has_eq(body: bytes) -> bool:
    return len(body) >= _eq_base(body) + _EQ_LEN


def read_eq(body: bytes) -> SetListEq:
    o = _eq_base(body)
    bands = tuple((v - 256 if v > 127 else v) / 2 for v in body[o + 1:o + 1 + BAND_COUNT])
    return SetListEq(bool(body[o] & 1), body[o] >> 1, bands)


def write_eq(body: bytes, eq: SetListEq) -> bytes:
    out, o = bytearray(body), _eq_base(body)
    out[o] = (max(0, min(99, eq.input_trim)) << 1) | int(eq.bypass)
    for i, db in enumerate(eq.bands[:BAND_COUNT]):
        out[o + 1 + i] = int(round(max(-18.0, min(18.0, db)) * 2)) & 0xFF
    return bytes(out)


if __name__ == "__main__":
    b = bytearray(69416)
    for i in range(ob.SLOT_COUNT):
        b[24 + i * 542] = 65 + i % 26
    body = bytes(b)
    for t in (-24, -1, 0, 7, 24):
        assert slot_transpose(write_slot_transpose(body, 3, t), 3) == t, t
    assert write_slot_transpose(body, 3, 5)[24 + 3 * 542] == body[24 + 3 * 542]       # name byte untouched
    moved = move_slot(body, 0, 5)
    assert moved[24 + 5 * 542] == body[24] and moved[24] == body[24 + 542] and len(moved) == len(body)
    assert move_slot(moved, 5, 0) == body
    sw = swap_slots(body, 1, 9)
    assert slot_bytes(sw, 9) == slot_bytes(body, 1) and swap_slots(sw, 1, 9) == body
    assert slot_bytes(clear_slot(body, 2), 2)[:24] == b" " * 24 and clear_slot(body, 2)[:24 + 2 * 542] == body[:24 + 2 * 542]
    r = set_slot_ref(body, 4, 1, 3, 77)
    assert slot_ref(r, 4) == (1, 3, 77) and slot_bytes(r, 5) == slot_bytes(body, 5)
    try:
        put_slot_bytes(body, 0, bytes(543)); raise SystemExit("cross-family chunk accepted")
    except ValueError:
        pass
    eq = SetListEq(True, 42, (-18, -0.5, 0, 0.5, 3, 6, 9, 12, 18))
    assert read_eq(write_eq(body, eq)) == eq and has_eq(body)
    print("setlist_edit self-test ok")
