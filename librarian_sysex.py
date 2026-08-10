r"""
Librarian SysEx — WRITE-side codec and object-body reference accessors.

Companion to kronos_sysex.py (which is read/decode only). This module adds the
pieces a coherent live "move" needs and nothing else:

  * obj_bank_to_func33()      — inverse of kronos_sysex._func33_to_obj_bank
                                (object-dump bank  ->  internal linear ref bank)
  * write builders (bytes)    — 0x73 Object Dump (write), 0x76 Store Bank,
                                0x4E Mode Change, 0x43 Parameter Change,
                                0x37 Bank Digest Request, 0x39 Digest Collection Req
  * parse_object_dump()       — split a received 0x73 into header + decoded body
  * combi/setlist body accessors — read/patch the (bank, number) reference bytes
                                in a decoded object body, in place
  * parse_bank_digest()       — decode a 0x38 reply into (obj, bank, sha1)

Everything here is pure logic (no I/O) so it is unit-testable off-hardware; run
`python librarian_sysex.py` for the built-in round-trip self-tests.

Authoritative wire spec: Z:\SysexInfo\MIDI implementation\ .  Object-type codes
(*1), bank encodings (*2), and 8->7 packing (*3) live in KRONOS_MIDI_SysEx.txt;
combi timbre offsets in CombiAndSongTimbreSet.txt; set-list slot offsets are
reused from setlist_data.py (hardware-confirmed).

CAUTION — unresolved bank-count ambiguity (validate on hardware, see plan Step 2):
KRONOS_MIDI_SysEx.txt *2 lists Program INT banks as 0-5 (INT-A..F, six banks),
but kronos_sysex.PROGRAM_BANKS / _func33_to_obj_bank treat 0x00-0x06 as SEVEN
INT banks (I-A..I-G). We deliberately mirror the existing code's convention here
so there is a single source of truth; the inverse below is an exact inverse of
_func33_to_obj_bank. If hardware shows programs have only six INT banks, fix it
in ONE place (kronos_sysex) and this module follows.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

import kronos_sysex as ksx
from kronos_sysex import decode_8to7, encode_7to8

# ── Korg exclusive header (global MIDI channel 0, matching kronos_sysex) ──────
_HDR = bytes([0xF0, 0x42, 0x30, 0x68])
_EOX = 0xF7

# Object-type codes (KRONOS_MIDI_SysEx.txt *1) — the ones the librarian touches.
OBJ_PROGRAM = 0x00
OBJ_COMBI = 0x01
OBJ_SONG_TIMBRE = 0x02   # not moved in v1, but decoded like a combi if ever needed
OBJ_SET_LIST = 0x0D

# Object body versions (the version byte in a 0x73/0x75 dump). Used only as a
# fallback; prefer echoing the version byte from the object you actually dumped.
OBJ_VERSION = {
    OBJ_COMBI: 3,
    OBJ_SONG_TIMBRE: 3,
    OBJ_SET_LIST: 0,
    # Program version (5) depends on HD-1 vs EXi; both are 5 today, but always
    # echo the dumped version rather than trusting this table for programs.
    OBJ_PROGRAM: 5,
}

# Combi timbre reference layout (CombiAndSongTimbreSet.txt, obj 0x01/0x02).
_TIMBRE_COUNT = 16
_TIMBRE0_PROG_NUM_OFS = 4802     # timbre 0 program NUMBER byte
_TIMBRE0_PROG_BANK_OFS = 4803    # timbre 0 program BANK byte (internal linear)
_TIMBRE_STRIDE = 188

# Set-list slot reference layout — mirror setlist_data.py so there is one source
# of truth for these offsets.
try:  # setlist_data uses module-private constants; fall back to literals if renamed
    from setlist_data import _SLOT_BASE as _SL_BASE, _SLOT_SIZE as _SL_STRIDE
except Exception:  # pragma: no cover - defensive
    _SL_BASE, _SL_STRIDE = 24, 542
_SL_TYPE_OFS = 24    # +24: bits1-0 = type (0=Combi,1=Prog,2=Song)
_SL_BANK_OFS = 25    # +25: bits4-0 = internal linear bank
_SL_INDEX_OFS = 26   # +26: performance index (0-199)
_SL_SLOT_COUNT = 128


# ── Bank-encoding inverse ────────────────────────────────────────────────────


def obj_bank_to_func33(type_: int, obj_bank: int) -> int:
    """Object-dump bank byte  ->  internal linear ("func 0x33") ref bank.

    Exact inverse of kronos_sysex._func33_to_obj_bank. `type_`: 1=Program,
    0=Combi (matching the func-0x33 / set-list-slot convention). Returns -1 for
    a bank that has no internal-linear representation (should never happen for a
    real reference).
    """
    if type_ == 1:  # program
        if 0x00 <= obj_bank <= 0x06:
            return obj_bank                 # I-A..I-G  (0..6)
        if obj_bank == 0x10:
            return 7                        # GM
        if 0x11 <= obj_bank <= 0x1A:
            return obj_bank - 0x10 + 7      # g(1)..g(d)  (8..17)
        if 0x40 <= obj_bank <= 0x4D:
            return obj_bank - 0x40 + 18     # U-A..U-GG  (18..31)
        return -1
    if type_ == 0:  # combi
        if 0x00 <= obj_bank <= 0x06:
            return obj_bank                 # I-A..I-G  (0..6)
        if 0x40 <= obj_bank <= 0x46:
            return obj_bank - 0x40 + 7      # U-A..U-G  (7..13)
        return -1
    return -1


def func33_to_obj_bank(type_: int, func33_bank: int) -> int:
    """Public alias for the (private) forward map in kronos_sysex."""
    return ksx._func33_to_obj_bank(type_, func33_bank)


# ── Write builders (return raw bytes; send via bridge.send_bytes) ────────────


def object_dump_write(obj: int, bank: int, index: int, version: int,
                      body: bytes) -> bytes:
    """0x73 Object Dump used as a WRITE to a specific bank+index (volatile).

    F0 42 30 68 73 obj bank idH idL version <body 7->8> F7.  `body` is the raw
    (decoded) object body; it is 8->7 encoded here. `version` should be the
    version byte echoed from the object you dumped (do not guess for programs).
    """
    return bytes(_HDR) + bytes([
        0x73, obj & 0x7F, bank & 0x7F,
        (index >> 7) & 0x7F, index & 0x7F, version & 0x7F,
    ]) + encode_7to8(body) + bytes([_EOX])


def store_bank_request(obj: int, bank: int) -> bytes:
    """0x76 Store Bank Request — commit the WHOLE bank to non-volatile storage."""
    return bytes(_HDR) + bytes([0x76, obj & 0x7F, bank & 0x7F, _EOX])


def change_program_bank_type_request(bank: int, is_exi: bool) -> bytes:
    """0x7C Change Program Bank Type. Confirmed wire format from the C# source
    (KronosSysEx.cs's BuildChangeProgramBankType — grepped for 0x7C across this whole
    Python codebase before adding this: nothing built it yet): `F0 42 3g 68 7C bank type F7`,
    type=1 for EXi, 0 for HD-1. If the new type differs from the bank's CURRENT type, the
    instrument REFORMATS AND ERASES that whole bank before replying with a func-0x24 Reply
    (a no-op reply if it's already that type) — only ever sent as the first step of a
    changeset_sync.py `bank_type_changes` entry (see that module's own docstring), never
    standalone. See sysex_service.SysExService.change_program_bank_type for the
    send-and-await-reply half."""
    return bytes(_HDR) + bytes([0x7C, bank & 0x7F, 1 if is_exi else 0, _EOX])


def mode_change(mode: int) -> bytes:
    """0x4E Mode Change. mode: 0 Combi, 2 Program, 4 Seq, 6 Sampling, 7 Global,
    8 Disk, 9 Set List."""
    return bytes(_HDR) + bytes([0x4E, mode & 0x0F, _EOX])


def param_change(typ: int, soc: int, sub: int, pid: int, idx: int,
                 value: int) -> bytes:
    """0x43 Parameter Change (integer). Edits the CURRENT edit buffer only —
    audible immediately, never persisted. Value is 21-bit two's-complement.

    NOTE typ/soc/sub/pid/idx are DECIMAL ids sent verbatim (7-bit) — e.g. a set
    list slot is pid=18 (0x12), typ=37 (0x25); do NOT pre-convert to 0x18/0x37.
    """
    v = value & 0x1FFFFF  # 21-bit
    return bytes(_HDR) + bytes([
        0x43, typ & 0x7F, soc & 0x7F, sub & 0x7F, pid & 0x7F, idx & 0x7F,
        (v >> 14) & 0x7F, (v >> 7) & 0x7F, v & 0x7F, _EOX,
    ])


def object_dump_request(obj: int, bank: int, index: int) -> bytes:
    """0x72 Object Dump Request (bytes form of kronos_sysex.object_dump_request)."""
    return bytes(_HDR) + bytes([
        0x72, obj & 0x7F, bank & 0x7F, (index >> 7) & 0x7F, index & 0x7F, _EOX,
    ])


def bank_digest_request(obj: int, bank: int) -> bytes:
    """0x37 Bank Digest Request — instrument replies with a 0x38 for this bank."""
    return bytes(_HDR) + bytes([0x37, obj & 0x7F, bank & 0x7F, _EOX])


def digest_collection_request() -> bytes:
    """0x39 Bank Digest Collection Request — instrument replies with one 0x3A."""
    return bytes(_HDR) + bytes([0x39, _EOX])


# ── Param-change helpers for the two reference kinds ─────────────────────────
# (Live-preview path; persistence is still 0x73->0x76.)


def combi_timbre_bank_pc(timbre: int, func33_bank: int) -> bytes:
    """0x43 setting a combi timbre's program BANK (pid 8) in the edit buffer."""
    return param_change(typ=4, soc=timbre, sub=0, pid=8, idx=0, value=func33_bank)


def combi_timbre_number_pc(timbre: int, number: int) -> bytes:
    """0x43 setting a combi timbre's program NUMBER (pid 9) in the edit buffer."""
    return param_change(typ=4, soc=timbre, sub=0, pid=9, idx=0, value=number)


def setlist_slot_pc(slot: int, type_: int, func33_bank: int, index: int) -> bytes:
    """0x43 setting a set-list slot's whole reference (pid 18): value packs
    type<<16 | bank<<8 | index (KRONOS_MIDI_SysEx.txt SetList *note)."""
    value = ((type_ & 0x03) << 16) | ((func33_bank & 0xFF) << 8) | (index & 0xFF)
    return param_change(typ=37, soc=0, sub=0, pid=18, idx=slot, value=value)


# ── Object-dump parsing (received 0x73) ──────────────────────────────────────


@dataclass
class ObjectDump:
    obj: int
    bank: int
    index: int
    version: int
    body: bytes            # decoded (8->7) object body; mutable copy


def parse_object_dump(msg: bytes) -> Optional[ObjectDump]:
    """Split a received func-0x73 Object Dump into header fields + decoded body."""
    if len(msg) < 11:
        return None
    if not (msg[0] == 0xF0 and msg[1] == 0x42 and (msg[2] & 0xF0) == 0x30 and
            msg[3] == 0x68 and msg[4] == 0x73):
        return None
    obj = msg[5]
    bank = msg[6]
    index = ((msg[7] & 0x7F) << 7) | (msg[8] & 0x7F)
    version = msg[9]
    data_start = 10
    data_end = msg.find(_EOX, data_start)
    if data_end < 0:
        data_end = len(msg)
    body = bytearray(decode_8to7(msg, data_start, data_end - data_start))
    return ObjectDump(obj, bank, index, version, bytes(body))


# ── Combi timbre reference accessors (operate on a decoded body) ─────────────


def combi_timbre_ref(body: bytes, timbre: int) -> Tuple[int, int]:
    """Return (func33_bank, number) the given 0-based timbre points at."""
    base = _TIMBRE0_PROG_NUM_OFS + timbre * _TIMBRE_STRIDE
    number = body[base]
    bank = body[base + 1]
    return bank, number


def set_combi_timbre_ref(body: bytearray, timbre: int,
                         func33_bank: int, number: int) -> None:
    """Patch a 0-based timbre's program reference in place (bytearray body)."""
    base = _TIMBRE0_PROG_NUM_OFS + timbre * _TIMBRE_STRIDE
    body[base] = number & 0x7F
    body[base + 1] = func33_bank & 0x7F


def iter_combi_timbre_refs(body: bytes):
    """Yield (timbre_index, func33_bank, number) for all 16 timbres."""
    for t in range(_TIMBRE_COUNT):
        bank, number = combi_timbre_ref(body, t)
        yield t, bank, number


# ── Set-list slot reference accessors (operate on a decoded body) ────────────


def setlist_slot_ref(body: bytes, slot: int) -> Tuple[int, int, int]:
    """Return (type, func33_bank, index) for a 0-based set-list slot.
    type: 0=Combi, 1=Prog, 2=Song."""
    b = _SL_BASE + slot * _SL_STRIDE
    type_ = body[b + _SL_TYPE_OFS] & 0x03
    bank = body[b + _SL_BANK_OFS] & 0x1F
    index = body[b + _SL_INDEX_OFS]
    return type_, bank, index


def set_setlist_slot_ref(body: bytearray, slot: int,
                         func33_bank: int, index: int,
                         type_: Optional[int] = None) -> None:
    """Patch a set-list slot's reference in place. Preserves the high bits of the
    type/bank bytes (color, transpose) that share those bytes. If type_ is None
    the existing type bits are kept."""
    b = _SL_BASE + slot * _SL_STRIDE
    if type_ is not None:
        body[b + _SL_TYPE_OFS] = (body[b + _SL_TYPE_OFS] & ~0x03) | (type_ & 0x03)
    body[b + _SL_BANK_OFS] = (body[b + _SL_BANK_OFS] & ~0x1F) | (func33_bank & 0x1F)
    body[b + _SL_INDEX_OFS] = index & 0xFF


def iter_setlist_slot_refs(body: bytes):
    """Yield (slot_index, type, func33_bank, index) for all slots that fit."""
    for s in range(_SL_SLOT_COUNT):
        b = _SL_BASE + s * _SL_STRIDE
        if b + _SL_INDEX_OFS >= len(body):
            break
        type_, bank, index = setlist_slot_ref(body, s)
        yield s, type_, bank, index


# ── Bank digest (0x38) parsing ───────────────────────────────────────────────


@dataclass(frozen=True)
class BankDigest:
    obj: int
    bank: int
    sha1: bytes    # 20-byte digest


def parse_bank_digest(msg: bytes) -> Optional[BankDigest]:
    """Decode a func-0x38 Bank Digest reply.  F0 42 3g 68 38 obj bank <sha1 7->8> F7."""
    if len(msg) < 8:
        return None
    if not (msg[0] == 0xF0 and msg[1] == 0x42 and (msg[2] & 0xF0) == 0x30 and
            msg[3] == 0x68 and msg[4] == 0x38):
        return None
    obj = msg[5]
    bank = msg[6]
    data_start = 7
    data_end = msg.find(_EOX, data_start)
    if data_end < 0:
        data_end = len(msg)
    sha1 = decode_8to7(msg, data_start, data_end - data_start)[:20]
    return BankDigest(obj, bank, bytes(sha1))


# ── Self-tests (run: python librarian_sysex.py) ──────────────────────────────


def _selftest() -> None:
    import sys

    fails: List[str] = []

    def check(name: str, cond: bool) -> None:
        if not cond:
            fails.append(name)

    # 1. 8<->7 round-trip on a range of body sizes.
    for n in (0, 1, 6, 7, 8, 13, 14, 188, 3706, 7810, 69416):
        body = bytes((i * 37 + 5) & 0xFF for i in range(n))
        rt = decode_8to7(encode_7to8(body), 0, None)
        check(f"codec-roundtrip-{n}", rt == body)

    # 2. Bank-encoding inverse round-trips for EVERY func33 index the forward map
    #    accepts (programs 0..31, combis 0..13).
    for idx in range(0, 32):
        ob = ksx._func33_to_obj_bank(1, idx)
        if ob >= 0:
            check(f"prog-bank-inv-{idx}", obj_bank_to_func33(1, ob) == idx)
    for idx in range(0, 14):
        ob = ksx._func33_to_obj_bank(0, idx)
        if ob >= 0:
            check(f"combi-bank-inv-{idx}", obj_bank_to_func33(0, ob) == idx)

    # 3. Combi timbre reference patch round-trip inside a full-size body.
    combi = bytearray(7810)
    for t in range(_TIMBRE_COUNT):
        set_combi_timbre_ref(combi, t, func33_bank=(t % 30), number=(t * 3) & 0x7F)
    for t, bank, number in iter_combi_timbre_refs(combi):
        check(f"timbre-ref-{t}", bank == (t % 30) and number == ((t * 3) & 0x7F))
    # timbre 15 must land at 4802 + 15*188 = 7622/7623
    check("timbre15-offset", (_TIMBRE0_PROG_NUM_OFS + 15 * _TIMBRE_STRIDE) == 7622)

    # 4. Set-list slot reference patch preserves the shared color/transpose bits.
    sl = bytearray(69416)
    b0 = _SL_BASE + 0 * _SL_STRIDE
    sl[b0 + _SL_TYPE_OFS] = 0b0011_1100      # color bits set, type=0
    sl[b0 + _SL_BANK_OFS] = 0b1110_0000      # transpose bits set, bank=0
    set_setlist_slot_ref(sl, 0, func33_bank=19, index=42, type_=1)
    t, bank, index = setlist_slot_ref(sl, 0)
    check("sl-type", t == 1)
    check("sl-bank", bank == 19)
    check("sl-index", index == 42)
    check("sl-color-preserved", (sl[b0 + _SL_TYPE_OFS] & 0b0011_1100) == 0b0011_1100)
    check("sl-transpose-preserved", (sl[b0 + _SL_BANK_OFS] & 0b1110_0000) == 0b1110_0000)

    # 5. Write builders produce well-formed, F0..F7-delimited messages.
    w = object_dump_write(OBJ_COMBI, 0x40, 5, 3, bytes(7810))
    check("write-hdr", w[:6] == bytes([0xF0, 0x42, 0x30, 0x68, 0x73, 0x01]))
    check("write-bank", w[6] == 0x40)
    check("write-idx", w[7] == 0 and w[8] == 5)
    check("write-ver", w[9] == 3)
    check("write-eox", w[-1] == 0xF7)
    rt = parse_object_dump(w)
    check("write-parse", rt is not None and rt.obj == OBJ_COMBI and rt.bank == 0x40
          and rt.index == 5 and rt.version == 3 and len(rt.body) == 7810)

    sb = store_bank_request(OBJ_COMBI, 0x40)
    check("store", sb == bytes([0xF0, 0x42, 0x30, 0x68, 0x76, 0x01, 0x40, 0xF7]))

    bt_exi = change_program_bank_type_request(0x02, True)
    check("banktype-req-exi", bt_exi == bytes([0xF0, 0x42, 0x30, 0x68, 0x7C, 0x02, 0x01, 0xF7]))
    bt_hd1 = change_program_bank_type_request(0x02, False)
    check("banktype-req-hd1", bt_hd1 == bytes([0xF0, 0x42, 0x30, 0x68, 0x7C, 0x02, 0x00, 0xF7]))

    # param change: set list slot pid must be sent as 18/37 verbatim (decimal ids)
    pc = setlist_slot_pc(slot=3, type_=1, func33_bank=19, index=42)
    check("pc-pid", pc[8] == 18 and pc[5] == 37)   # pid field, typ field
    check("pc-idx", pc[9] == 3)

    dr = bank_digest_request(OBJ_PROGRAM, 0x00)
    check("digest-req", dr == bytes([0xF0, 0x42, 0x30, 0x68, 0x37, 0x00, 0x00, 0xF7]))

    if fails:
        print("FAIL:", ", ".join(fails))
        sys.exit(1)
    print("librarian_sysex self-test: OK")


if __name__ == "__main__":
    _selftest()
