r"""
PCG (Program/Combi/Set-List bank) file reader — port of Core/Pcg/PcgFile.cs,
Core/Pcg/PcgObjectExtractor.cs, and Core/Pcg/ProgramFormatConverter.cs from the
Windows client.

Read-only, in-memory: given a .pcg file's raw bytes (however the caller got
them — local file, FTP download into a temp cache, etc), scans for the four
self-describing sub-chunk tags that carry real object data — MBK1/PBK1
(Program, EXi vs HD-1), CBK1 (Combi), SBK1 (Set List) — and extracts fixed-size
records. Does NOT model the outer .pcg container hierarchy (the doc's DIV1/
SLS1/SLD1/SDB1/STL1 directory passes) — deliberately, per the C# original:
those tags are ambiguous (the reference doc's own author leaves the SLS1-vs-
STL1 Set List relationship unresolved) and unnecessary once you scan for the
fixed-24-byte-header inner chunks directly, validating each candidate by its
own declared count/item-size fields rather than trusting tag or position alone.

Reuses this project's existing bank-identity plumbing instead of reinventing
it: kronos_sysex.BankId/program_label/combi_label for human-readable bank
labels, kronos_sysex._func33_to_obj_bank for the Combi on-disk index -> obj_bank
map (an exact match — both conventions agree Combi has 7 int + 7 user banks),
and librarian_sysex.OBJ_PROGRAM/OBJ_COMBI/OBJ_SET_LIST for the object-type
codes. This lines up because it's the same underlying hardware bank concept,
just addressed via a file container instead of the live SysEx wire protocol.

CRITICAL ASYMMETRY (ported byte-for-byte from PcgObjectExtractor.cs — do not
"clean up"): Program has only 6 internal banks on disk (I-A..I-F) with a
dedicated 0x8000 flag value for I-F; there is no on-disk Program "I-G". Combi
genuinely has 7 internal banks (I-A..I-G), no such split. An earlier version of
this exact decoding logic routed Program through the 7-int-bank indirection
Combi uses, which silently shifted every user bank's index down by one and
dropped the LAST bank (U-GG) out of the valid range entirely — caught only by
loading a real user file with confirmed U-GG content. See
decode_program_obj_bank()'s docstring and the self-test below, which pins that
exact regression.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

import kronos_sysex as ksx
from kronos_sysex import BankId, combi_label, program_label
from librarian_sysex import OBJ_COMBI, OBJ_PROGRAM, OBJ_SET_LIST

_HEADER_SIZE = 24

# Fixed on-disk record sizes (PcgObjectExtractor.cs class comment; verified
# against a real factory PRELOAD.PCG).
PROGRAM_ITEM_SIZE = 4960
COMBI_ITEM_SIZE = 7810
SETLIST_ITEM_SIZE = 69416

# Program wire-format sizes (ProgramFormatConverter.cs).
PCG_SLOT_SIZE = 4960
WIRE_SIZE_EXI = 4960
WIRE_SIZE_HD1 = 3706

_BANK_CHUNK_OBJ_TYPE = {
    "MBK1": OBJ_PROGRAM,
    "PBK1": OBJ_PROGRAM,
    "CBK1": OBJ_COMBI,
    "SBK1": OBJ_SET_LIST,
}

_BANK_CHUNK_TAGS = tuple(t.encode("ascii") for t in _BANK_CHUNK_OBJ_TYPE)


# ── Bank-id decoding ─────────────────────────────────────────────────────────
# Korg's .pcg on-disk bankId (+0x14 in the chunk header) is NOT a plain linear
# bank index; it's a different encoding from the live-SysEx obj_bank/func33
# conventions in kronos_sysex.py, resolved here to the SAME obj_bank values so
# BankId/program_label/combi_label can be reused unchanged.


def decode_program_obj_bank(bank_id_raw: int) -> int:
    """.pcg on-disk Program bankId -> obj_bank (kronos_sysex convention).

    Literal 0x00..0x04 for I-A..I-E. 0x8000 is a dedicated FLAG value for I-F,
    NOT a continuation of the literal sequence (there is no on-disk Program
    "I-G" at all). 0x20000+N (N=0..13) maps directly to U-A..U-GG. Ported
    byte-for-byte from PcgObjectExtractor.DecodeProgramObjBank — do not
    "clean up" the branch order or route this through the 7-int-bank
    Combi-style indirection (see module docstring for why that regressed).
    Returns -1 if bankIdRaw doesn't resolve to a real bank.
    """
    if bank_id_raw == 0x8000:
        return 0x05  # I-F
    if bank_id_raw < 0x8000:
        return bank_id_raw  # I-A..I-E, literal 0x00..0x04
    n = bank_id_raw - 0x20000
    return 0x40 + n if 0 <= n <= 13 else -1  # U-A..U-GG


def decode_combi_obj_bank(bank_id_raw: int) -> int:
    """.pcg on-disk Combi bankId -> obj_bank.

    Combi genuinely has 7 internal banks on disk (I-A..I-G, literal 0x00..0x06)
    -- no I-F-style split. 0x20000+N resumes the sequence at the first user
    bank (func33 index 7). The resulting 0..13 index is exactly what
    kronos_sysex._func33_to_obj_bank(0, idx) already maps for Combi (both
    conventions agree: 7 int + 7 user banks), so it's reused directly instead
    of re-deriving an "EditableBanks()"-equivalent table. Returns -1 if
    bankIdRaw doesn't resolve to a real bank.
    """
    idx = bank_id_raw if bank_id_raw < 0x20000 else bank_id_raw - 0x20000 + 7
    return ksx._func33_to_obj_bank(0, idx)


# ── Extracted records ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PcgObjectEntry:
    """One Program/Combi/Set-List record recovered from a .pcg file (port of
    the PcgObjectEntry record in PcgObjectExtractor.cs).

    `bank` is None for Set List (no per-object-type bank concept on disk --
    same convention the live SysEx side uses). `is_exi` is only meaningful for
    Program (which chunk tag -- MBK1 vs PBK1 -- it came from); ignored for
    Combi/Set List. See pcg_program_to_wire() for why that split matters.
    """
    obj_type: int          # librarian_sysex.OBJ_PROGRAM / OBJ_COMBI / OBJ_SET_LIST
    bank: Optional[BankId]  # obj_bank + display label; .number is the slot index
    index: int              # 0-based position within its bank chunk
    body: bytes              # raw on-disk record bytes, exactly item_size long
    name: str
    is_exi: bool = False


@dataclass(frozen=True)
class PcgRejectedBank:
    """A candidate bank chunk (one of the four known tags, found literally in
    the file) whose header didn't validate, or whose bankId didn't resolve to
    a real bank. Diagnostic only -- most of these are coincidental 4-byte tag
    matches inside unrelated binary parameter data, but a genuinely missing
    bank (an encoding case not yet understood) would show up here too, which a
    synthetic self-test alone never can."""
    tag: str
    offset: int
    count: int
    item_size: int
    bank_id_raw: int
    reason: str


@dataclass(frozen=True)
class PcgFile:
    """Port of PcgFile.cs. Read-only, in-memory -- this module never touches
    disk; the caller supplies bytes however it obtained them."""
    objects: List[PcgObjectEntry]
    rejected_banks: List[PcgRejectedBank]


def open_pcg(data: bytes) -> Optional[PcgFile]:
    """Returns None if `data` isn't a recognizable Kronos .pcg file (bad
    magic/product id/file type) rather than raising -- a malformed or
    unrelated file is expected input from a "Load PCG..." file picker, not a
    bug (mirrors PcgFile.Open)."""
    if len(data) < 16:
        return None
    if data[0:4] != b"KORG":
        return None
    if data[4] != 0x68:   # Product ID: Kronos (other Korg models out of scope)
        return None
    if data[5] != 0x00:   # File type: 00 = PCG (01 = SNG -- Songs out of scope)
        return None
    objects, rejected = extract_objects(data)
    return PcgFile(objects, rejected)


# ── Chunk scanning (PcgObjectExtractor.Extract / TryReadBank) ────────────────


def _read_be32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 4], "big")


def _read_record_name(body: bytes) -> str:
    """Every record type has a 24-byte ASCII name field at offset 0 (verified
    against a real factory PRELOAD.PCG -- see PcgObjectExtractor.cs class
    comment). Simplification vs the C# port: that code dispatches through
    ProgramBody.ReadName / CombiBody.ReadName / SetListBody.FromRawBody(...)
    ?.Name per object type; all three ultimately read the same 24-byte ASCII
    field for the purposes this module cares about (name + bank/number + raw
    body), so it's read directly here rather than porting three separate
    body-model classes this module has no other use for."""
    return ksx._ascii_trim(body, 0, 24)


def _candidate_offsets(data: bytes, limit: int) -> List[Tuple[int, bytes]]:
    """Every offset < `limit` where one of the four bank tags appears literally,
    in ascending order.

    bytes.find runs the search in C. Testing each offset in Python instead --
    slicing four bytes and decoding them to str at every position -- costs about
    11 s on a 20 MB .pcg versus 0.04 s here, and .pcg files are routinely larger
    than that.
    """
    hits: List[Tuple[int, bytes]] = []
    for tag in _BANK_CHUNK_TAGS:
        pos = data.find(tag)
        while 0 <= pos < limit:
            hits.append((pos, tag))
            pos = data.find(tag, pos + 1)
    hits.sort()
    return hits


def extract_objects(data: bytes) -> Tuple[List[PcgObjectEntry], List[PcgRejectedBank]]:
    """Scan `data` for MBK1/PBK1/CBK1/SBK1 chunks and extract their records."""
    results: List[PcgObjectEntry] = []
    rejected: List[PcgRejectedBank] = []
    # A validated chunk consumes its own records, so any tag found inside them is
    # part of that object's payload, not a chunk header. `consumed_to` reproduces
    # the old byte-walk's `pos += consumed` skip.
    consumed_to = 0
    for pos, tag_bytes in _candidate_offsets(data, len(data) - _HEADER_SIZE + 1):
        if pos < consumed_to:
            continue
        obj_type = _BANK_CHUNK_OBJ_TYPE[tag_bytes.decode("ascii")]
        consumed, reason = _try_read_bank(data, pos, obj_type, tag_bytes == b"MBK1", results)
        if consumed:
            consumed_to = pos + consumed
        elif reason is not None:
            rejected.append(reason)
    return results, rejected


def _try_read_bank(data: bytes, offset: int, obj_type: int, is_exi: bool,
                    results: List[PcgObjectEntry]) -> Tuple[int, Optional[PcgRejectedBank]]:
    tag = data[offset:offset + 4].decode("ascii", errors="replace")
    count = _read_be32(data, offset + 0x0C)
    item_size = _read_be32(data, offset + 0x10)
    bank_id_raw = _read_be32(data, offset + 0x14)

    if not (1 <= count <= 128):
        return 0, PcgRejectedBank(tag, offset, count, item_size, bank_id_raw,
                                   f"count {count} out of range 1..128")
    if not (64 <= item_size <= 200_000):   # sane range for a Kronos object body
        return 0, PcgRejectedBank(tag, offset, count, item_size, bank_id_raw,
                                   f"itemSize {item_size} out of range 64..200000")
    records_end = offset + _HEADER_SIZE + count * item_size
    if records_end > len(data):
        return 0, PcgRejectedBank(tag, offset, count, item_size, bank_id_raw,
                                   "records would run past end of file")

    bank: Optional[BankId]
    if obj_type == OBJ_SET_LIST:
        bank = None   # Set Lists have no per-object-type bank -- same convention as the live path
    elif obj_type == OBJ_PROGRAM:
        ob = decode_program_obj_bank(bank_id_raw)
        if ob < 0:
            return 0, PcgRejectedBank(tag, offset, count, item_size, bank_id_raw,
                f"bankId 0x{bank_id_raw:X} didn't decode to a valid Program bank")
        bank = BankId(1, program_label(ob), ob, 0)
    else:  # OBJ_COMBI
        ob = decode_combi_obj_bank(bank_id_raw)
        if ob < 0:
            return 0, PcgRejectedBank(tag, offset, count, item_size, bank_id_raw,
                f"bankId 0x{bank_id_raw:X} didn't decode to a valid Combi bank")
        bank = BankId(0, combi_label(ob), ob, 0)

    entries: List[PcgObjectEntry] = []
    for i in range(count):
        rec_off = offset + _HEADER_SIZE + i * item_size
        body = bytes(data[rec_off:rec_off + item_size])
        entry_bank = bank if bank is None else BankId(bank.type, bank.label, bank.obj_bank, i)
        entries.append(PcgObjectEntry(obj_type, entry_bank, i, body, _read_record_name(body), is_exi))

    results.extend(entries)
    return _HEADER_SIZE + count * item_size, None


# ── Program PCG<->wire conversion (ProgramFormatConverter.cs) ────────────────


def pcg_program_to_wire(pcg_body: bytes, is_exi: bool) -> bytes:
    """Program body: .pcg on-disk slot -> wire Object Dump format.

    EXi programs are byte-identical in both formats (4960 bytes). HD-1
    programs' wire dump (3706 bytes) is an exact truncation of the .pcg
    slot's first 3706 bytes. Combi and Set List need no conversion at all
    (their .pcg and wire sizes already match) -- this function is
    Program-only. See ProgramFormatConverter.cs class comment for the
    empirical evidence (~1000 real hardware-pulled bodies cross-referenced
    against a factory PRELOAD.PCG: EXi matched exactly across 397 pairs;
    HD-1 matched as an exact byte-offset truncation across 620/632 pairs,
    remainder differing only by ordinary patch-content edits).
    """
    if len(pcg_body) != PCG_SLOT_SIZE:
        raise ValueError(f"expected a {PCG_SLOT_SIZE}-byte .pcg Program record, got {len(pcg_body)}")
    if is_exi:
        return pcg_body
    return bytes(pcg_body[:WIRE_SIZE_HD1])


def wire_body_from_pcg_entry(obj_type: int, entry: PcgObjectEntry) -> Optional[bytes]:
    """Programs need pcg_program_to_wire(); Combi and Set List records already
    match the wire format exactly. Returns None (never raises) for a
    malformed .pcg Program slot -- every caller treats "can't place this" as
    a skip, not a crash (mirrors ProgramFormatConverter.WireBodyFromPcgEntry)."""
    if obj_type != OBJ_PROGRAM:
        return entry.body
    try:
        return pcg_program_to_wire(entry.body, entry.is_exi)
    except ValueError:
        return None


# ── Self-tests (run: python pcg_file.py) ─────────────────────────────────────
# Mirrors Core/Pcg/PcgFileSelfTests.cs as closely as a byte-for-byte port
# allows, including its BuildBankIdEncodedPcg() test vectors -- those pin the
# exact U-GG regression this module's docstring calls out.


def _write_be32(buf: bytearray, value: int) -> None:
    buf += value.to_bytes(4, "big")


def _make_bank_chunk(tag: bytes, count: int, item_size: int, bank_id_raw: int,
                      records: List[bytes]) -> bytes:
    assert len(records) == count
    buf = bytearray()
    buf += tag
    _write_be32(buf, 0)          # chunk length -- not read by the scanner
    _write_be32(buf, 0)          # reserved/meta
    _write_be32(buf, count)
    _write_be32(buf, item_size)
    _write_be32(buf, bank_id_raw)
    for rec in records:
        buf += rec
    return bytes(buf)


def _make_named_record(item_size: int, name: str) -> bytes:
    rec = bytearray(item_size)
    rec[0:len(name)] = name.encode("ascii")
    return bytes(rec)


_KORG_HEADER = b"KORG" + bytes([0x68, 0x00, 0x02, 0x01]) + bytes(8)   # 16 bytes


def _build_synthetic_pcg() -> Tuple[bytes, str, str, str, str]:
    """Port of PcgFileSelfTests.BuildSyntheticPcg -- one full-size real-shaped
    record per type (Program/Combi/Set List), plus a SECOND Program record
    from a PBK1 (HD-1) bank so both isExi branches of pcg_program_to_wire()
    get exercised (the C# original only needed one Program record since it
    doesn't test the PCG->wire conversion in this same synthetic file)."""
    program_name, program_hd1_name = "SYNTH PROGRAM", "SYNTH PROGRAM HD1"
    combi_name, setlist_name = "SYNTH COMBI", "SYNTH SETLIST"
    program_body = _make_named_record(PROGRAM_ITEM_SIZE, program_name)
    program_hd1_body = _make_named_record(PROGRAM_ITEM_SIZE, program_hd1_name)
    combi_body = _make_named_record(COMBI_ITEM_SIZE, combi_name)
    # NOTE: the C# original writes real slot comments via SetListBody here to
    # also prove a shared-decoder round-trip; this port doesn't carry that
    # deeper Set List body model (out of scope -- see module docstring), so a
    # plain name-at-offset-0 record stands in, and the round-trip proof below
    # instead checks the raw body survives the existing 8<->7 wire codec
    # (kronos_sysex.encode_7to8/decode_8to7) unchanged, which is the part of
    # "one decoder, two ingestion paths" this module is actually responsible for.
    setlist_body = _make_named_record(SETLIST_ITEM_SIZE, setlist_name)

    blob = bytearray(_KORG_HEADER)
    blob += _make_bank_chunk(b"MBK1", 1, PROGRAM_ITEM_SIZE, 0x00, [program_body])       # EXi, I-A
    blob += _make_bank_chunk(b"PBK1", 1, PROGRAM_ITEM_SIZE, 0x04, [program_hd1_body])   # HD-1, I-E
    blob += _make_bank_chunk(b"CBK1", 1, COMBI_ITEM_SIZE, 0, [combi_body])
    blob += _make_bank_chunk(b"SBK1", 1, SETLIST_ITEM_SIZE, 0, [setlist_body])
    return bytes(blob), program_name, program_hd1_name, combi_name, setlist_name


def _build_bank_id_encoded_pcg() -> bytes:
    """Port of PcgFileSelfTests.BuildBankIdEncodedPcg -- the exact test
    vectors that pin the Program-vs-Combi bank-id asymmetry (U-GG regression)."""
    item_size = 64
    blob = bytearray(_KORG_HEADER)
    for tag, bank_id, name in [
        (b"MBK1", 0x00000, "I-A PROG"),
        (b"MBK1", 0x00004, "I-E PROG"),
        (b"MBK1", 0x08000, "I-F PROG"),
        (b"PBK1", 0x20000, "U-A PROG"),
        (b"PBK1", 0x20006, "U-G PROG"),
        (b"PBK1", 0x20007, "U-AA PROG"),
        (b"PBK1", 0x2000D, "U-GG PROG"),
        (b"MBK1", 0x2000E, "OUT OF RANGE PROG"),   # N=14 -- must be rejected
        (b"CBK1", 0x00006, "I-G COMBI"),
        (b"CBK1", 0x20000, "U-A COMBI"),
        (b"CBK1", 0x20006, "U-G COMBI"),
    ]:
        blob += _make_bank_chunk(tag, 1, item_size, bank_id, [_make_named_record(item_size, name)])
    return bytes(blob)


def _find(entries: List[PcgObjectEntry], obj_type: int, obj_bank: int) -> Optional[PcgObjectEntry]:
    for e in entries:
        if e.obj_type == obj_type and e.bank is not None and e.bank.obj_bank == obj_bank:
            return e
    return None


def _selftest() -> None:
    import sys

    fails: List[str] = []

    def check(name: str, cond: bool) -> None:
        if not cond:
            fails.append(name)

    # 1. On-disk record sizes match the C# constants exactly.
    check("program-item-size", PROGRAM_ITEM_SIZE == 4960)
    check("combi-item-size", COMBI_ITEM_SIZE == 7810)
    check("setlist-item-size", SETLIST_ITEM_SIZE == 69416)
    check("wire-hd1-size", WIRE_SIZE_HD1 == 3706)
    check("wire-exi-size", WIRE_SIZE_EXI == 4960)

    # 2. Synthetic full-shape file: one real-sized record per type, plus a
    #    second Program record from a PBK1 (HD-1) bank.
    blob, program_name, program_hd1_name, combi_name, setlist_name = _build_synthetic_pcg()
    pcg = open_pcg(blob)
    check("opens-valid-file", pcg is not None)
    if pcg is not None:
        check("no-rejects", len(pcg.rejected_banks) == 0)
        check("object-count", len(pcg.objects) == 4)
        prog_exi = _find(pcg.objects, OBJ_PROGRAM, 0x00)      # MBK1 -> I-A
        prog_hd1 = _find(pcg.objects, OBJ_PROGRAM, 0x04)      # PBK1 -> I-E
        combi = _find(pcg.objects, OBJ_COMBI, 0x00)
        setlists = [e for e in pcg.objects if e.obj_type == OBJ_SET_LIST]
        check("program-exi-found", prog_exi is not None and prog_exi.name == program_name)
        check("program-hd1-found", prog_hd1 is not None and prog_hd1.name == program_hd1_name)
        check("combi-found", combi is not None and combi.name == combi_name)
        check("setlist-found", len(setlists) == 1 and setlists[0].name == setlist_name)
        check("setlist-bank-none", setlists[0].bank is None)
        check("program-exi-is-exi", prog_exi is not None and prog_exi.is_exi is True)    # from MBK1
        check("program-hd1-not-exi", prog_hd1 is not None and prog_hd1.is_exi is False)  # from PBK1
        check("combi-not-exi", combi is not None and combi.is_exi is False)              # from CBK1
        check("program-exi-label", prog_exi is not None and prog_exi.bank is not None and prog_exi.bank.label == "I-A")
        check("program-hd1-label", prog_hd1 is not None and prog_hd1.bank is not None and prog_hd1.bank.label == "I-E")
        check("combi-label", combi is not None and combi.bank is not None and combi.bank.label == "I-A")

        # Program PCG->wire conversion off real extracted bodies: EXi (MBK1)
        # passes through unchanged; HD-1 (PBK1) truncates to WIRE_SIZE_HD1.
        if prog_exi is not None:
            wire_exi = wire_body_from_pcg_entry(OBJ_PROGRAM, prog_exi)
            check("wire-exi-passthrough", wire_exi == prog_exi.body and len(wire_exi) == WIRE_SIZE_EXI)
        if prog_hd1 is not None:
            wire_hd1 = wire_body_from_pcg_entry(OBJ_PROGRAM, prog_hd1)
            check("wire-hd1-len", wire_hd1 is not None and len(wire_hd1) == WIRE_SIZE_HD1)
            check("wire-hd1-truncation", wire_hd1 == prog_hd1.body[:WIRE_SIZE_HD1])

        # "One decoder, two ingestion paths": a Set List body extracted from the
        # .pcg file must round-trip through the existing wire 8<->7 codec exactly
        # like a live dump would -- no PCG-specific transform hiding in there.
        if len(setlists) == 1:
            sl_body = setlists[0].body
            rt = ksx.decode_8to7(ksx.encode_7to8(sl_body), 0, None)
            check("setlist-body-wire-codec-roundtrip", rt == sl_body)

    # 3. A stray tag with no valid header following (all-zero -> count=0) must
    #    be skipped, not mis-extracted or crash the scan, and must show up in
    #    rejected_banks rather than silently vanishing.
    garbage = b"MBK1" + bytes(12)
    with_garbage = bytearray(blob)
    with_garbage[16:16] = garbage   # splice right after the KORG header
    pcg_g = open_pcg(bytes(with_garbage))
    check("garbage-does-not-crash", pcg_g is not None)
    if pcg_g is not None:
        prog_g = _find(pcg_g.objects, OBJ_PROGRAM, 0x00)
        check("garbage-does-not-corrupt-real-extraction", prog_g is not None and prog_g.name == program_name)
        check("garbage-tracked-as-rejected",
              any(r.tag == "MBK1" and "count" in r.reason for r in pcg_g.rejected_banks))

    # 4. Bad magic -> None, not an exception.
    check("rejects-non-pcg", open_pcg(bytes([1, 2, 3, 4, 5, 6, 7, 8])) is None)
    check("too-short", open_pcg(b"KORG") is None)
    bad_product = bytearray(_KORG_HEADER); bad_product[4] = 0x00
    check("bad-product-id", open_pcg(bytes(bad_product)) is None)
    bad_filetype = bytearray(_KORG_HEADER); bad_filetype[5] = 0x01
    check("bad-file-type", open_pcg(bytes(bad_filetype)) is None)

    # 5. Real-file bank-id encoding -- the exact PcgFileSelfTests.cs vectors,
    #    pinning the Program (6 int banks + 0x8000 I-F flag) vs Combi (7 int
    #    banks, no split) asymmetry, including the historical U-GG regression.
    bank_blob = _build_bank_id_encoded_pcg()
    bank_pcg = open_pcg(bank_blob)
    check("bankid-file-opens", bank_pcg is not None)
    if bank_pcg is not None:
        def name_at(obj_type: int, ob: int) -> Optional[str]:
            e = _find(bank_pcg.objects, obj_type, ob)
            return e.name if e is not None else None

        check("bankid-program-I-A", name_at(OBJ_PROGRAM, 0x00) == "I-A PROG")
        check("bankid-program-I-E", name_at(OBJ_PROGRAM, 0x04) == "I-E PROG")
        check("bankid-program-I-F-via-0x8000-flag", name_at(OBJ_PROGRAM, 0x05) == "I-F PROG")
        check("bankid-program-no-I-G-slot", name_at(OBJ_PROGRAM, 0x06) is None)
        check("bankid-program-U-A-via-0x20000", name_at(OBJ_PROGRAM, 0x40) == "U-A PROG")
        check("bankid-program-U-G-via-0x20006", name_at(OBJ_PROGRAM, 0x46) == "U-G PROG")
        check("bankid-program-U-AA-via-0x20007", name_at(OBJ_PROGRAM, 0x47) == "U-AA PROG")
        check("bankid-program-U-GG-via-0x2000D", name_at(OBJ_PROGRAM, 0x4D) == "U-GG PROG")
        # 0x2000E (N=14) is out of range -- must be rejected, not silently mapped.
        check("bankid-program-N14-rejected",
              any(r.bank_id_raw == 0x2000E for r in bank_pcg.rejected_banks))

        check("bankid-combi-I-G", name_at(OBJ_COMBI, 0x06) == "I-G COMBI")
        check("bankid-combi-U-A-via-0x20000", name_at(OBJ_COMBI, 0x40) == "U-A COMBI")
        check("bankid-combi-U-G-via-0x20006", name_at(OBJ_COMBI, 0x46) == "U-G COMBI")

    if fails:
        print("FAIL:", ", ".join(fails))
        sys.exit(1)
    print("pcg_file self-test: OK")


if __name__ == "__main__":
    _selftest()
