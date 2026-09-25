r"""
Nautilus SysEx wire framing — port of Networking/NautilusSysEx.cs.

Wraps the ONE confirmed difference between Kronos's and Nautilus's live SysEx
wire protocol: the Exclusive Header length (Kronos 4 bytes — F0 42 3g 68;
Nautilus 6 bytes — F0 42 3g 00 01 5D), hardware-confirmed 2026-09-16 via a
passive USB SysEx capture of a Program Object Dump from a real Nautilus unit:

    F0 42 30 00 01 5D 73 00 00 00 00 06  <data...>  F7
    \_______header, 6B_______/ fn obj bk idH idL ver

Function codes, semantics, and the 8-to-7 encoding are identical to
Kronos's — reuses Core.kronos_sysex.decode_8to7/encode_7to8 directly. Kept as
a separate module rather than parameterizing Data/librarian_sysex.py's header
in place, mirroring C#'s own decision to avoid touching that module's many
existing hardcoded call sites; only the header bytes differ.

Deliberately narrow, matching Data/librarian_sysex.py's Kronos-side codec
surface exactly (the SAME function names/signatures, so
Core/sysex_service.py and Tools/sysex_dump_collector.py can select between
this module and librarian_sysex as a plain module reference): only the
primitives that are hardware round-trip-verified on Nautilus (2026-09-16:
passive capture, active 0x72/0x73 request/reply, and a full write+0x76
Store+re-read byte-diff, all against a real Nautilus) are here — func
0x72/0x73/0x76/0x77/0x37/0x38/0x24. Everything else (live-stream mode-
change/performance-id/name-dump decode, func 0x7C Change Program Bank Type,
func 0x61) stays Kronos-only, guarded by device family where it matters —
see Core/sysex_service.py's own comments at each call site.
"""
from __future__ import annotations

from typing import Optional

from Core.kronos_sysex import decode_8to7, encode_7to8
from Data.librarian_sysex import BankDigest, ObjectDump

_HDR = bytes([0xF0, 0x42, 0x30, 0x00, 0x01, 0x5D])
_EOX = 0xF7


def has_header_at(b: bytes, i: int, func: Optional[int] = None) -> bool:
    """True if a Nautilus Excl Header — F0 42 3g 00 01 5D — begins at index i
    (6 header bytes); if `func` is given, also requires the function byte at
    i+6 to match it."""
    if i + 5 >= len(b):
        return False
    if not (b[i] == 0xF0 and b[i + 1] == 0x42 and (b[i + 2] & 0xF0) == 0x30 and
            b[i + 3] == 0x00 and b[i + 4] == 0x01 and b[i + 5] == 0x5D):
        return False
    if func is None:
        return True
    return i + 6 < len(b) and b[i + 6] == func


def korg_message(func: int, payload: bytes = b"") -> bytes:
    """Assemble a Nautilus SysEx message — F0 42 30 00 01 5D <func> <payload...> F7."""
    return bytes(_HDR) + bytes([func & 0x7F]) + bytes(payload) + bytes([_EOX])


def object_dump_request(obj: int, bank: int, index: int) -> bytes:
    """0x72 Object Dump Request: F0 42 3g 00 01 5D 72 obj bank idH idL F7."""
    return korg_message(0x72, bytes([
        obj & 0x7F, bank & 0x7F, (index >> 7) & 0x7F, index & 0x7F,
    ]))


def dump_bank_request(obj: int, bank: int) -> bytes:
    """0x77 Dump Bank Request — every object of a type in a bank (preset banks
    only): F0 42 3g 00 01 5D 77 obj bank F7."""
    return korg_message(0x77, bytes([obj & 0x7F, bank & 0x7F]))


def store_bank_request(obj: int, bank: int) -> bytes:
    """0x76 Store Bank Request — commit a previously-sent Object Dump (func
    0x73) for the given object type/bank to non-volatile storage. DESTRUCTIVE
    on real hardware: F0 42 3g 00 01 5D 76 obj bank F7."""
    return korg_message(0x76, bytes([obj & 0x7F, bank & 0x7F]))


def object_dump_write(obj: int, bank: int, index: int, version: int, body: bytes) -> bytes:
    """0x73 Object Dump used as a WRITE to a specific bank+index (volatile) —
    same wire shape as Kronos's, just the 6-byte Nautilus header instead of
    Kronos's 4-byte one:
      F0 42 3g 00 01 5D 73 obj bank idH idL version <body 7->8> F7."""
    payload = bytes([
        obj & 0x7F, bank & 0x7F, (index >> 7) & 0x7F, index & 0x7F, version & 0x7F,
    ]) + encode_7to8(body)
    return korg_message(0x73, payload)


def parse_object_dump(msg: bytes) -> Optional[ObjectDump]:
    """Split a received func-0x73 Object Dump into header fields + decoded
    body: F0 42 3g 00 01 5D 73 obj bank idH idL version <body 7->8> F7."""
    if len(msg) < 13:
        return None
    if not has_header_at(msg, 0, 0x73):
        return None
    obj = msg[7]
    bank = msg[8]
    index = ((msg[9] & 0x7F) << 7) | (msg[10] & 0x7F)
    version = msg[11]
    data_start = 12
    data_end = msg.find(_EOX, data_start)
    if data_end < 0:
        data_end = len(msg)
    body = decode_8to7(msg, data_start, data_end - data_start)
    return ObjectDump(obj, bank, index, version, body)


def parse_reply(msg: bytes) -> Optional[int]:
    """Decode a func-0x24 Reply: F0 42 3g 00 01 5D 24 cc F7. Returns the
    Reply Code, or None if msg isn't one."""
    if len(msg) < 8:
        return None
    if not has_header_at(msg, 0, 0x24):
        return None
    return msg[7] & 0x7F


def bank_digest_request(obj: int, bank: int) -> bytes:
    """0x37 Bank Digest Request — instrument replies with a 0x38 Bank Digest
    for the given object type/bank: F0 42 3g 00 01 5D 37 obj bank F7."""
    return korg_message(0x37, bytes([obj & 0x7F, bank & 0x7F]))


def parse_bank_digest(msg: bytes) -> Optional[BankDigest]:
    """Decode a received func-0x38 Bank Digest — a 20-byte SHA-1:
      F0 42 3g 00 01 5D 38 obj bank <digest 8->7> F7."""
    if len(msg) < 10:
        return None
    if not has_header_at(msg, 0, 0x38):
        return None
    obj = msg[7]
    bank = msg[8]
    data_start = 9
    data_end = msg.find(_EOX, data_start)
    if data_end < 0:
        data_end = len(msg)
    sha1 = decode_8to7(msg, data_start, data_end - data_start)[:20]
    return BankDigest(obj, bank, bytes(sha1))


# ── Self-tests (run: python nautilus_sysex.py) ────────────────────────────────


def _selftest() -> None:
    import sys

    fails = []

    def check(name: str, cond: bool) -> None:
        if not cond:
            fails.append(name)

    # Wire shape, against the hardware-captured example in this module's docstring.
    odr = object_dump_request(0x00, 0x00, 5)
    check("odr", odr == bytes([0xF0, 0x42, 0x30, 0x00, 0x01, 0x5D, 0x72,
                                0x00, 0x00, 0x00, 0x05, 0xF7]))

    dbr = dump_bank_request(0x13, 0x00)
    check("dbr", dbr == bytes([0xF0, 0x42, 0x30, 0x00, 0x01, 0x5D, 0x77,
                                0x13, 0x00, 0xF7]))

    sbr = store_bank_request(0x01, 0x40)
    check("sbr", sbr == bytes([0xF0, 0x42, 0x30, 0x00, 0x01, 0x5D, 0x76,
                                0x01, 0x40, 0xF7]))

    # Write/parse round trip, mirroring librarian_sysex's own self-test.
    w = object_dump_write(0x01, 0x40, 5, 3, bytes(7810))
    check("write-hdr", w[:7] == bytes([0xF0, 0x42, 0x30, 0x00, 0x01, 0x5D, 0x73]))
    check("write-obj", w[7] == 0x01)
    check("write-bank", w[8] == 0x40)
    check("write-idx", w[9] == 0 and w[10] == 5)
    check("write-ver", w[11] == 3)
    check("write-eox", w[-1] == 0xF7)
    rt = parse_object_dump(w)
    check("write-parse", rt is not None and rt.obj == 0x01 and rt.bank == 0x40
          and rt.index == 5 and rt.version == 3 and len(rt.body) == 7810)

    # A Kronos-framed message must NOT parse as a Nautilus one (the whole point
    # of a 2-byte-longer, byte-for-byte different header).
    from Data.librarian_sysex import object_dump_write as kronos_write
    kw = kronos_write(0x01, 0x40, 5, 3, bytes(7810))
    check("kronos-not-nautilus", parse_object_dump(kw) is None)

    dr = bank_digest_request(0x00, 0x00)
    check("digest-req", dr == bytes([0xF0, 0x42, 0x30, 0x00, 0x01, 0x5D, 0x37,
                                      0x00, 0x00, 0xF7]))

    check("reply-ok", parse_reply(bytes([0xF0, 0x42, 0x30, 0x00, 0x01, 0x5D, 0x24, 0x00, 0xF7])) == 0)
    check("reply-rejected", parse_reply(bytes([0xF0, 0x42, 0x30, 0x00, 0x01, 0x5D, 0x24, 0x04, 0xF7])) == 4)
    check("reply-not-a-reply", parse_reply(w) is None)

    if fails:
        print("FAIL:", ", ".join(fails))
        sys.exit(1)
    print("nautilus_sysex self-test: OK")


if __name__ == "__main__":
    _selftest()
