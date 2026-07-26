r"""
Raw-body decoders + mutators for Program/Combi/Set-List objects — port of
Core/ObjectBody/ProgramBody.cs, CombiBody.cs, and SetListBody.cs from the
Windows client. Read side (`parse_*`) and write side (`write_*`) both
covered; the write side mirrors the C# Write*/PadAscii/BuildRenamedBody
mutators used to support in-place local edits.

Operates on the decoded binary body only, no wire-format/8-to-7 knowledge --
same discipline as the C# originals. A wire-format HD-1 Program body is an
exact prefix of the larger .pcg on-disk slot (see pcg_file.py's
pcg_program_to_wire()), and every offset read here (name at 0, category at
2568/4790) falls within that shared prefix, so this module works unchanged
against either a live-dump body or a pcg_file.PcgObjectEntry.body.

CATEGORY NAME TABLE -- DOES NOT EXIST, deliberately not fabricated here:
The task this module was built from asked for "the fixed category name table
Program/Combi categories index into (real hardware category names like
Keyboard, Organ, Bass, etc.)". That table does not exist anywhere in the C#
source or its docs -- verified by:
  * ProgramBody.cs/CombiBody.cs only ever expose the category as a raw
    (int Category, int SubCategory) pair; no name lookup anywhere near them.
  * Views/PropertiesDialog.xaml.cs's ForProgramOrCombi() comment says so
    explicitly: "Category/Sub-Category (numeric only; no name table exists
    anywhere in the documented format for these values)" -- and its actual UI
    is a plain ComboBox of the ints 0..0x11 / 0..7, not a named dropdown.
  * Documentation/MIDI implementation/Global.txt instead models "Program
    Category 00".."Program Category 17" as 18 independently user-editable
    24-byte name fields living in GLOBAL memory (P0 Basic Setup > Category
    Name) -- i.e. category names are per-instrument mutable data, not a
    fixed firmware table a Python module could port literally.
Porting a plausible-looking "Keyboard/Organ/Bass/..." list here would be
inventing ground truth the C# side explicitly says doesn't exist. Category/
sub-category are therefore surfaced as plain ints everywhere in this module,
matching PropertiesDialog's own numeric-only treatment.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import kronos_sysex as ksx

# ── Program (ProgramBody.cs) ─────────────────────────────────────────────────

# Category/Sub-Category: bits 4-0 = Category (00~0x11), bits 7-5 = Sub-Category
# (00~07). Offset confirmed byte-identical in both Prog_HD-1.txt and
# Prog_EXi_Common.txt (the shared "Common" parameter block) -- one offset
# covers both program bank types.
PROGRAM_CATEGORY_OFS = 2568

# Category/Sub-Category: same bit layout as Program, 12 bytes before
# LibRefs.Timbre0Num -- sits in the Combi Common block immediately before the
# timbre array (CombiAndSongTimbreSet.txt).
COMBI_CATEGORY_OFS = 4790


@dataclass(frozen=True)
class ProgramBodyInfo:
    """Port of the fields ProgramBody.cs exposes for a Program body (obj
    0x00): name and the raw (unnamed -- see module docstring) category pair."""
    name: str
    category: int
    sub_category: int


@dataclass(frozen=True)
class CombiBodyInfo:
    """Port of the fields CombiBody.cs exposes for a Combi body (obj 0x01)."""
    name: str
    category: int
    sub_category: int


def _read_category(body: bytes, offset: int) -> tuple[int, int]:
    """Shared bit layout for ProgramBody.ReadCategory / CombiBody.ReadCategory:
    packed byte at `offset`, bits 4-0 = category, bits 7-5 = sub-category.
    Returns (0, 0) if `offset` is out of range -- mirrors the C# guard
    exactly (a truncated/short body is a normal input here, not a bug)."""
    if offset >= len(body):
        return (0, 0)
    packed = body[offset]
    return (packed & 0x1F, (packed >> 5) & 0x07)


def parse_program_body(body: bytes) -> ProgramBodyInfo:
    """Port of ProgramBody.ReadName + ProgramBody.ReadCategory."""
    name = ksx._ascii_trim(body, 0, 24)
    category, sub_category = _read_category(body, PROGRAM_CATEGORY_OFS)
    return ProgramBodyInfo(name, category, sub_category)


def parse_combi_body(body: bytes) -> CombiBodyInfo:
    """Port of CombiBody.ReadName + CombiBody.ReadCategory."""
    name = ksx._ascii_trim(body, 0, 24)
    category, sub_category = _read_category(body, COMBI_CATEGORY_OFS)
    return CombiBodyInfo(name, category, sub_category)


# ── Mutators (Write* side of ProgramBody.cs / CombiBody.cs / SetListBody.cs) ─
#
# Pure functions over immutable byte input: every write_* helper below clones
# `body` into a bytearray, updates only the bytes the corresponding C# Write*
# method touches, and returns a brand-new bytes object -- the original is
# never mutated, matching `(byte[])body.Clone()` in every C# Write* method.
#
# Validation: the C# Write* methods for Category do NOT range-check or raise
# -- they just mask (`category & 0x1F`, `subCategory & 0x07`) before packing,
# so an out-of-range int silently wraps instead of throwing. Ported literally
# here (no ValueError) to match that exact (lack of) behavior -- inventing
# stricter validation than the C# side has would be a divergence, not a port.


def _pad_ascii(s: str, length: int) -> bytes:
    """Port of Librarian.PadAscii: space-padded (0x20) to exactly `length`
    bytes, ASCII-encoded, truncated if `s` is longer. Non-ASCII characters
    fall back to '?' (0x3F), matching .NET's Encoding.ASCII.GetBytes."""
    data = bytearray(b"\x20" * length)
    enc = s.encode("ascii", errors="replace")
    n = min(len(enc), length)
    data[0:n] = enc[0:n]
    return bytes(data)


def _build_renamed_body(body: bytes, name: str) -> bytes:
    """Port of Librarian.BuildRenamedBody: only the first 24 bytes (the name
    field) are replaced, every other byte of `body` preserved exactly."""
    b = bytearray(body)
    padded = _pad_ascii(name, 24)
    n = min(24, len(b))
    b[0:n] = padded[0:n]
    return bytes(b)


def write_program_name(body: bytes, name: str) -> bytes:
    """Port of ProgramBody.WriteName (delegates to Librarian.BuildRenamedBody
    in the C# source: name lives in the first 24 bytes)."""
    return _build_renamed_body(body, name)


def write_combi_name(body: bytes, name: str) -> bytes:
    """Port of CombiBody.WriteName (same Librarian.BuildRenamedBody
    delegation as ProgramBody.WriteName: name in the first 24 bytes)."""
    return _build_renamed_body(body, name)


def write_program_category(body: bytes, category: int, sub_category: int) -> bytes:
    """Port of ProgramBody.WriteCategory. Same bytes as `body`, only the
    packed Category/Sub-Category byte at PROGRAM_CATEGORY_OFS replaced --
    every other byte preserved exactly."""
    b = bytearray(body)
    if PROGRAM_CATEGORY_OFS < len(b):
        b[PROGRAM_CATEGORY_OFS] = (category & 0x1F) | ((sub_category & 0x07) << 5)
    return bytes(b)


def write_combi_category(body: bytes, category: int, sub_category: int) -> bytes:
    """Port of CombiBody.WriteCategory -- same bit-packing as
    write_program_category, at COMBI_CATEGORY_OFS."""
    b = bytearray(body)
    if COMBI_CATEGORY_OFS < len(b):
        b[COMBI_CATEGORY_OFS] = (category & 0x1F) | ((sub_category & 0x07) << 5)
    return bytes(b)


# ── Set List (SetListBody.cs) ────────────────────────────────────────────────

NAME_LEN = 24
SLOT_BASE = 24
SLOT_SIZE = 542
COMMENT_LEN = 512
SLOT_COUNT = 128   # SetListData.SlotCount -- slots per set list


@dataclass(frozen=True)
class SetListSlotInfo:
    """One Set List slot, decoded from a Set List object (obj 0x0D) body --
    port of the fields SetListBody.FromRawBody populates into SetListSlot.

    `type` is 0=Combi, 1=Program, 2=Song (SetListSlot.cs's own note: this is
    the func 0x33 ResolveBankLabel convention, NOT the "prog/combi/song"
    order the SetList.txt doc lists -- hardware-confirmed there). `bank` is
    the raw on-disk bank index for whichever `type` this slot points at, not
    a kronos_sysex.BankId -- resolving that is the caller's job, same
    division of responsibility as the C# side (SetListSlot.PerformanceLabel).
    """
    number: int
    name: str
    type: int
    bank: int
    index: int
    color: int
    hold_time: int
    volume: int
    comments: str

    @property
    def is_empty(self) -> bool:
        """An unused slot has a blank name (mirrors SetListSlot.IsEmpty)."""
        return self.name.strip() == ""


def parse_setlist_slot(body: bytes, slot_index: int) -> Optional[SetListSlotInfo]:
    """Port of the per-slot field walk inside SetListBody.FromRawBody's loop.

    Returns None for a truncated body that doesn't even reach this slot's
    fixed fields (b+30 > len(body)) -- mirrors the C# loop's own `break`
    (a partial/truncated dump keeps whatever slots DID decode, rather than
    raising). Comments are truncated to whatever bytes remain, exactly like
    the C# `comLen = Math.Min(CommentLen, bin.Length - (b + 30))`.
    """
    b = SLOT_BASE + slot_index * SLOT_SIZE
    if b + 30 > len(body):
        return None

    slot_name = ksx._ascii_trim(body, b, NAME_LEN)
    packed = body[b + 24]
    slot_type = packed & 0x03
    color = (packed >> 2) & 0x0F
    bank = body[b + 25] & 0x1F
    index = body[b + 26]
    hold_time = body[b + 27]
    volume = body[b + 28]
    com_len = min(COMMENT_LEN, len(body) - (b + 30))
    comments = ksx._ascii_trim(body, b + 30, com_len) if com_len > 0 else ""

    return SetListSlotInfo(slot_index, slot_name, slot_type, bank, index,
                            color, hold_time, volume, comments)


def write_setlist_name(body: bytes, name: str) -> bytes:
    """Port of SetListBody.WriteName (delegates to Librarian.BuildRenamedBody
    in the C# source: the Set List's own name lives in the first 24 bytes,
    distinct from any individual slot's name)."""
    return _build_renamed_body(body, name)


def write_setlist_slot_name(body: bytes, slot_index: int, name: str) -> bytes:
    """Port of SetListBody.WriteSlotName: slot name field (24 bytes at the
    slot's own +0), space-padded/truncated like Librarian.PadAscii."""
    b = bytearray(body)
    base = SLOT_BASE + slot_index * SLOT_SIZE
    padded = _pad_ascii(name, NAME_LEN)
    n = min(NAME_LEN, max(0, len(b) - base))
    if n > 0:
        b[base:base + n] = padded[0:n]
    return bytes(b)


def write_setlist_slot_color(body: bytes, slot_index: int, color: int) -> bytes:
    """Port of SetListBody.WriteSlotColor. Color shares its byte with Type
    (bits 1-0) and font-LSB (bits 7-6) -- mask, don't overwrite those bits,
    same discipline as the C# side's own comment about LibRefs.SetSetListSlotRef."""
    b = bytearray(body)
    ofs = SLOT_BASE + slot_index * SLOT_SIZE + 24
    if ofs < len(b):
        b[ofs] = (b[ofs] & ~0b0011_1100) | ((color & 0x0F) << 2)
    return bytes(b)


def write_setlist_slot_comments(body: bytes, slot_index: int, comments: str) -> bytes:
    """Port of SetListBody.WriteSlotComments. Truncated/padded to
    COMMENT_LEN=512 ASCII bytes -- same 512-byte-max the reader already
    respects via `com_len = min(COMMENT_LEN, len(body) - (b + 30))`."""
    b = bytearray(body)
    ofs = SLOT_BASE + slot_index * SLOT_SIZE + 30
    padded = _pad_ascii(comments, COMMENT_LEN)
    n = min(COMMENT_LEN, max(0, len(b) - ofs))
    if n > 0:
        b[ofs:ofs + n] = padded[0:n]
    return bytes(b)


# ── Self-test (run: python object_body.py) ───────────────────────────────────
# Ports ObjectBodySelfTests.cs's actual test vectors -- both the read-only
# checks and (now) the Write*/mutator round-trips. EraseBody/registry checks
# in the C# self-test aren't ported: they exercise LibObj/ObjectTypeRegistry/
# EraseBody, none of which this module owns.


def _selftest() -> None:
    import sys

    fails: list = []

    def check(name: str, cond: bool) -> None:
        if not cond:
            fails.append(name)

    # 1. Module constants match the C# constants exactly.
    check("program-category-ofs", PROGRAM_CATEGORY_OFS == 2568)
    check("combi-category-ofs", COMBI_CATEGORY_OFS == 4790)
    check("setlist-name-len", NAME_LEN == 24)
    check("setlist-slot-base", SLOT_BASE == 24)
    check("setlist-slot-size", SLOT_SIZE == 542)
    check("setlist-comment-len", COMMENT_LEN == 512)
    check("setlist-slot-count", SLOT_COUNT == 128)

    # 2. Program: category round-trip, ObjectBodySelfTests.cs's own byte pattern
    #    (i*11+1 & 0xFF over a 3706-byte HD-1-sized body), category=5 sub=3.
    prog = bytearray((i * 11 + 1) & 0xFF for i in range(3706))
    prog[PROGRAM_CATEGORY_OFS] = (5 & 0x1F) | ((3 & 0x07) << 5)
    prog_info = parse_program_body(bytes(prog))
    check("program-category-read", prog_info.category == 5 and prog_info.sub_category == 3)

    prog_named = bytearray(prog)
    prog_named[0:24] = b"TESTPROG".ljust(24)   # PadAscii: full 24-byte field, space-padded
    check("program-name-roundtrip", parse_program_body(bytes(prog_named)).name == "TESTPROG")

    # 2b. Program mutators: ObjectBodySelfTests.cs's own write-side checks,
    #     same "3706-byte, (i*11+1)&0xFF" body, WriteCategory(5, 3) then
    #     confirm every byte except the category byte is untouched.
    prog_cat_written = write_program_category(bytes(prog), 5, 3)
    wp_cat, wp_sub = parse_program_body(prog_cat_written).category, parse_program_body(prog_cat_written).sub_category
    check("program-category-write-read", wp_cat == 5 and wp_sub == 3)
    prog_expected_tail = bytearray(prog)
    prog_expected_tail[PROGRAM_CATEGORY_OFS] = prog_cat_written[PROGRAM_CATEGORY_OFS]
    check("program-category-preserves-tail", prog_cat_written == bytes(prog_expected_tail))

    prog_name_written = write_program_name(bytes(prog), "TESTPROG")
    check("program-name-write-roundtrip", parse_program_body(prog_name_written).name == "TESTPROG")
    check("program-name-write-preserves-tail", prog_name_written[24:] == bytes(prog)[24:])

    # 3. Combi: category round-trip, C#'s byte pattern (i*13+2 & 0xFF, 7810 bytes),
    #    category=7 sub=1.
    combi = bytearray((i * 13 + 2) & 0xFF for i in range(7810))
    combi[COMBI_CATEGORY_OFS] = (7 & 0x1F) | ((1 & 0x07) << 5)
    combi_info = parse_combi_body(bytes(combi))
    check("combi-category-read", combi_info.category == 7 and combi_info.sub_category == 1)

    combi_named = bytearray(combi)
    combi_named[0:24] = b"TESTCOMBI".ljust(24)   # PadAscii: full 24-byte field, space-padded
    check("combi-name-roundtrip", parse_combi_body(bytes(combi_named)).name == "TESTCOMBI")

    # 3b. Combi mutators: same write-then-read-back + tail-preservation shape
    #     as the Program checks above, C#'s (7, 1) / "TESTCOMBI" vectors.
    combi_cat_written = write_combi_category(bytes(combi), 7, 1)
    wc_cat, wc_sub = parse_combi_body(combi_cat_written).category, parse_combi_body(combi_cat_written).sub_category
    check("combi-category-write-read", wc_cat == 7 and wc_sub == 1)
    combi_expected_tail = bytearray(combi)
    combi_expected_tail[COMBI_CATEGORY_OFS] = combi_cat_written[COMBI_CATEGORY_OFS]
    check("combi-category-preserves-tail", combi_cat_written == bytes(combi_expected_tail))

    combi_name_written = write_combi_name(bytes(combi), "TESTCOMBI")
    check("combi-name-write-roundtrip", parse_combi_body(combi_name_written).name == "TESTCOMBI")
    check("combi-name-write-preserves-tail", combi_name_written[24:] == bytes(combi)[24:])

    # 4. Set List: ObjectBodySelfTests.cs's exact synthetic slBody vector --
    #    69416-byte, space-padded, name "TESTLIST", slot 0 = "SLOT0" with
    #    type=1 (program), color=5, bank=3, index=9.
    sl_body = bytearray(b" " * 69416)
    sl_body[0:8] = b"TESTLIST"
    b0 = SLOT_BASE
    sl_body[b0:b0 + 5] = b"SLOT0"
    sl_body[b0 + 24] = 1 | (5 << 2)   # type=1 (program), color=5
    sl_body[b0 + 25] = 3              # bank
    sl_body[b0 + 26] = 9              # index

    slot0 = parse_setlist_slot(bytes(sl_body), 0)
    check("setlistbody-not-null", slot0 is not None)
    if slot0 is not None:
        check("setlistbody-slot0-name", slot0.name == "SLOT0")
        check("setlistbody-slot0-type", slot0.type == 1)
        check("setlistbody-slot0-bank", slot0.bank == 3)
        check("setlistbody-slot0-index", slot0.index == 9)
        check("setlistbody-slot0-color", slot0.color == 5)
        check("setlistbody-slot0-comments-blank", slot0.comments == "")
        check("setlistbody-slot0-not-empty", slot0.is_empty is False)

    # An untouched slot (still all spaces) decodes as empty.
    slot1 = parse_setlist_slot(bytes(sl_body), 1)
    check("setlistbody-slot1-empty", slot1 is not None and slot1.is_empty)

    # A slot whose fixed fields run past the end of a truncated body -> None,
    # not an exception (mirrors FromRawBody's loop `break`).
    truncated = bytes(sl_body[:40])   # short body: doesn't even reach slot 0's +30
    check("setlistbody-truncated-none", parse_setlist_slot(truncated, 0) is None)

    # 4b. SetListBody mutators: ObjectBodySelfTests.cs's own bit-preserving
    #     color write (WriteSlotColor(9) must not disturb Type/bank/index,
    #     the C# check being "setlist-color-preserves-refs" via
    #     LibRefs.SetListSlotRef -- parse_setlist_slot gives the same
    #     bit-level view here) and comments write ("hello world").
    with_color = write_setlist_slot_color(bytes(sl_body), 0, 9)
    color_slot = parse_setlist_slot(with_color, 0)
    check("setlist-color-preserves-refs",
          color_slot is not None and color_slot.type == 1 and color_slot.bank == 3 and color_slot.index == 9)
    check("setlist-color-write", color_slot is not None and color_slot.color == 9)

    with_comments = write_setlist_slot_comments(bytes(sl_body), 0, "hello world")
    comments_slot = parse_setlist_slot(with_comments, 0)
    check("setlist-comments-write", comments_slot is not None and comments_slot.comments == "hello world")
    # Comments write must not disturb the slot name or the packed type/color byte.
    check("setlist-comments-preserves-name", comments_slot is not None and comments_slot.name == "SLOT0")
    check("setlist-comments-preserves-color", comments_slot is not None and comments_slot.color == 5)

    # write_setlist_name / write_setlist_slot_name: not in ObjectBodySelfTests.cs
    # as standalone checks, but both delegate to the same BuildRenamedBody /
    # PadAscii primitives as Program/Combi WriteName -- round-trip them too.
    sl_renamed = write_setlist_name(bytes(sl_body), "NEWLIST")
    check("setlist-name-write-roundtrip", ksx._ascii_trim(sl_renamed, 0, 24) == "NEWLIST")
    check("setlist-name-write-preserves-tail", sl_renamed[24:] == bytes(sl_body)[24:])

    slot_renamed = write_setlist_slot_name(bytes(sl_body), 0, "RENAMED0")
    renamed_slot0 = parse_setlist_slot(slot_renamed, 0)
    check("setlist-slot-name-write-roundtrip", renamed_slot0 is not None and renamed_slot0.name == "RENAMED0")
    check("setlist-slot-name-write-preserves-color", renamed_slot0 is not None and renamed_slot0.color == 5)

    if fails:
        print("FAIL:", ", ".join(fails))
        sys.exit(1)
    print("object_body self-test: OK")


if __name__ == "__main__":
    _selftest()
