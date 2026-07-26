r"""
Raw-body decoders for Program/Combi/Set-List objects — port of
Core/ObjectBody/ProgramBody.cs, CombiBody.cs, and SetListBody.cs from the
Windows client (read-only side only; this port has no need yet for the C#
Write*/mutator methods, which exist there to support in-place local edits).

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


# ── Self-test (run: python object_body.py) ───────────────────────────────────
# Ports ObjectBodySelfTests.cs's actual test vectors (the parts relevant to
# this module -- read-only; the C# self-test's Write*/EraseBody/registry
# checks aren't ported here since this module has no mutator/registry side).


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

    # 3. Combi: category round-trip, C#'s byte pattern (i*13+2 & 0xFF, 7810 bytes),
    #    category=7 sub=1.
    combi = bytearray((i * 13 + 2) & 0xFF for i in range(7810))
    combi[COMBI_CATEGORY_OFS] = (7 & 0x1F) | ((1 & 0x07) << 5)
    combi_info = parse_combi_body(bytes(combi))
    check("combi-category-read", combi_info.category == 7 and combi_info.sub_category == 1)

    combi_named = bytearray(combi)
    combi_named[0:24] = b"TESTCOMBI".ljust(24)   # PadAscii: full 24-byte field, space-padded
    check("combi-name-roundtrip", parse_combi_body(bytes(combi_named)).name == "TESTCOMBI")

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

    if fails:
        print("FAIL:", ", ".join(fails))
        sys.exit(1)
    print("object_body self-test: OK")


if __name__ == "__main__":
    _selftest()
