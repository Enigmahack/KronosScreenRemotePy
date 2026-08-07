r"""
Global object (obj 0x03) category-name decode — port of
Core/ObjectBody/GlobalBody.cs.

The user-editable Category / Sub-Category NAMES a Program or Combi's numeric
category fields point at. Programs and Combis each have 18 categories of 8
sub-categories, named independently of each other; a body only ever stores the
two NUMBERS (object_body.parse_program_body/parse_combi_body's category pair),
so without this the Librarian's Properties dialog could only ever show
"Category 5 / Sub-Category 2" where the instrument itself shows
"Guitar / Acoustic".

Decodes the Global object (obj 0x03, bank 0, index 0 — "for all other types
bank must be 0", KRONOS_MIDI_SysEx.txt *2). Only the category-name block is
decoded; the rest of Global (~24 KB of tuning, MIDI, controller and scale
settings) is out of scope and deliberately untouched.

Offsets read straight off Documentation/MIDI implementation/SysExDumps/
Global.txt's own offset column, which lays the block out as four contiguous
runs: 18 Program category names, then their 18x8 sub-category names, then the
same two runs for Combi. Every name is a fixed 24-byte ASCII field, same
convention as an object's own name.

Shape rules (identical to the C# original):

  * Every array is exactly CategoryCount / SubCategoryCount long and never
    contains None — a name the source didn't provide comes back as the
    numeric fallback ("Category 05"), so every display path can use these
    directly with no null/short-array handling of its own.
  * TryCreate() is the ONLY way to build one from untrusted arrays (a JSON
    cache file that could be truncated, hand-edited, or written by an
    older/newer build). Returns None unless all four are exactly the right
    shape with no None entries — the caller then falls back to Numeric().
  * A blank/whitespace field means "this category was never named" — fall
    back to the numeric label rather than showing an empty row.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

CATEGORY_COUNT = 18       # 00~0x11, matching the body field's own range
SUB_CATEGORY_COUNT = 8    # 00~07

# Offsets into the Global body (SysExDumps/Global.txt's offset column).
PROGRAM_CATEGORY_OFS    = 12912   # 18 x 24
PROGRAM_SUBCATEGORY_OFS = 13344   # 18 x 8 x 24
COMBI_CATEGORY_OFS      = 16800   # 18 x 24
COMBI_SUBCATEGORY_OFS   = 17232   # 18 x 8 x 24
NAME_LENGTH = 24

# The last byte this decoder touches — anything shorter isn't a Global body
# we can read.
MINIMUM_BODY_LENGTH = (
    COMBI_SUBCATEGORY_OFS + CATEGORY_COUNT * SUB_CATEGORY_COUNT * NAME_LENGTH)


def _read_name(body: bytes, offset: int, fallback: str) -> str:
    """A blank/whitespace field means "this category was never named" — fall
    back to the numeric label rather than showing an empty row."""
    if offset + NAME_LENGTH > len(body):
        return fallback
    name = body[offset:offset + NAME_LENGTH].decode("ascii", errors="replace")
    name = name.rstrip("\x00 ").strip()
    return name if name else fallback


def _read_run(body: bytes, base_ofs: int, fallback_prefix: str) -> List[str]:
    return [_read_name(body, base_ofs + i * NAME_LENGTH, f"{fallback_prefix} {i:02d}")
            for i in range(CATEGORY_COUNT)]


def _read_sub_run(body: bytes, base_ofs: int) -> List[List[str]]:
    return [
        [_read_name(body, base_ofs + (c * SUB_CATEGORY_COUNT + s) * NAME_LENGTH,
                    f"Sub {s:02d}")
         for s in range(SUB_CATEGORY_COUNT)]
        for c in range(CATEGORY_COUNT)
    ]


def _valid_flat(names: Optional[List[str]]) -> bool:
    return names is not None and len(names) == CATEGORY_COUNT and all(
        n is not None for n in names)


def _valid_nested(names: Optional[List[List[str]]]) -> bool:
    return (names is not None and len(names) == CATEGORY_COUNT
            and all(len(subs) == SUB_CATEGORY_COUNT and all(s is not None for s in subs)
                    for subs in names))


@dataclass(frozen=True)
class CategoryNames:
    """Port of GlobalBody.cs's CategoryNames class — the neutral, always-
    available answer is plain numeric labels (Numeric()), identical in shape
    to a real decode. Used before anything has ever been synced, and whenever
    a Global dump isn't available at all (offline)."""

    program: List[str]
    program_sub: List[List[str]]
    combi: List[str]
    combi_sub: List[List[str]]

    @staticmethod
    def numeric() -> "CategoryNames":
        return CategoryNames(
            program=_build_numeric("Category"),
            program_sub=_build_numeric_sub(),
            combi=_build_numeric("Category"),
            combi_sub=_build_numeric_sub(),
        )

    @staticmethod
    def try_create(program: Optional[List[str]],
                   program_sub: Optional[List[List[str]]],
                   combi: Optional[List[str]],
                   combi_sub: Optional[List[List[str]]]) -> Optional["CategoryNames"]:
        if not (_valid_flat(program) and _valid_nested(program_sub)
                and _valid_flat(combi) and _valid_nested(combi_sub)):
            return None
        return CategoryNames(program=program, program_sub=program_sub,
                             combi=combi, combi_sub=combi_sub)

    def category_label(self, obj_type: int, category: int) -> str:
        table = self.combi if obj_type == 1 else self.program  # 1 = Combi obj type
        if 0 <= category < CATEGORY_COUNT:
            return table[category]
        return f"Category {category:02d}"

    def sub_category_label(self, obj_type: int, category: int, sub: int) -> str:
        table = self.combi_sub if obj_type == 1 else self.program_sub
        if 0 <= category < CATEGORY_COUNT and 0 <= sub < SUB_CATEGORY_COUNT:
            return table[category][sub]
        return f"Sub {sub:02d}"

    def to_dict(self) -> dict:
        return {"program": self.program, "program_sub": self.program_sub,
                "combi": self.combi, "combi_sub": self.combi_sub}

    @staticmethod
    def from_dict(d: Optional[dict]) -> Optional["CategoryNames"]:
        if not d:
            return None
        return CategoryNames.try_create(
            d.get("program"), d.get("program_sub"),
            d.get("combi"), d.get("combi_sub"))


def _build_numeric(prefix: str) -> List[str]:
    return [f"{prefix} {i:02d}" for i in range(CATEGORY_COUNT)]


def _build_numeric_sub() -> List[List[str]]:
    return [[f"Sub {s:02d}" for s in range(SUB_CATEGORY_COUNT)]
            for _ in range(CATEGORY_COUNT)]


def read_category_names(body: bytes) -> Optional[CategoryNames]:
    """Port of GlobalBody.ReadCategoryNames — None when `body` is too short to
    be a real Global dump (a truncated/rejected reply), so the caller keeps
    whatever it already had rather than replacing it with garbage."""
    if len(body) < MINIMUM_BODY_LENGTH:
        return None
    return CategoryNames(
        program=_read_run(body, PROGRAM_CATEGORY_OFS, "Category"),
        program_sub=_read_sub_run(body, PROGRAM_SUBCATEGORY_OFS),
        combi=_read_run(body, COMBI_CATEGORY_OFS, "Category"),
        combi_sub=_read_sub_run(body, COMBI_SUBCATEGORY_OFS),
    )


# ── Self-test (python global_body.py) ───────────────────────────────────────


def _selftest() -> None:
    import sys

    fails: List[str] = []

    def check(name: str, cond: bool) -> None:
        if not cond:
            fails.append(name)

    # A full-size body of spaces reads back as numeric fallbacks (blank fields).
    body = bytearray(b" " * MINIMUM_BODY_LENGTH)
    names = read_category_names(bytes(body))
    check("blank-body-not-none", names is not None)
    if names is not None:
        check("blank-program-cat0", names.category_label(0, 0) == "Category 00")
        check("blank-program-sub", names.sub_category_label(0, 5, 3) == "Sub 03")
        check("blank-combi-cat1", names.category_label(1, 1) == "Category 01")

    # Stamping a real name into the Program category-5 field round-trips.
    body2 = bytearray(b" " * MINIMUM_BODY_LENGTH)
    body2[PROGRAM_CATEGORY_OFS + 5 * NAME_LENGTH:
          PROGRAM_CATEGORY_OFS + 5 * NAME_LENGTH + 7] = b"Guitar\x00"
    names2 = read_category_names(bytes(body2))
    check("named-body-not-none", names2 is not None)
    if names2 is not None:
        check("program-cat5-name", names2.category_label(0, 5) == "Guitar")
        check("program-cat6-fallback", names2.category_label(0, 6) == "Category 06")

    # Combi sub-category 2/4 name round-trips.
    body3 = bytearray(b" " * MINIMUM_BODY_LENGTH)
    ofs = COMBI_SUBCATEGORY_OFS + (2 * SUB_CATEGORY_COUNT + 4) * NAME_LENGTH
    body3[ofs:ofs + 6] = b"Funk\x00\x00"
    names3 = read_category_names(bytes(body3))
    check("combi-sub-body-not-none", names3 is not None)
    if names3 is not None:
        check("combi-sub-2-4-name", names3.sub_category_label(1, 2, 4) == "Funk")
        check("combi-sub-2-5-fallback", names3.sub_category_label(1, 2, 5) == "Sub 05")

    # Truncated body -> None (caller keeps what it had).
    check("short-body-none", read_category_names(bytes(body[:1000])) is None)

    # Numeric() fallback is always valid-shaped.
    num = CategoryNames.numeric()
    check("numeric-shape", len(num.program) == 18 and len(num.program_sub) == 18
          and len(num.program_sub[0]) == 8 and len(num.combi) == 18)

    # try_create rejects malformed arrays (wrong length, or None entries),
    # accepts well-shaped ones — 18 non-null strings is valid regardless of
    # content (mirrors the C# ValidFlat's length+non-null check).
    check("try-create-wrong-length", CategoryNames.try_create(["a"] * 17, num.program_sub,
                                                              num.combi, num.combi_sub) is None)
    check("try-create-null-entry", CategoryNames.try_create([None] * 18, num.program_sub,
                                                            num.combi, num.combi_sub) is None)
    check("try-create-short-sub", CategoryNames.try_create(num.program,
                                                           [["x"] * 8] * 17,
                                                           num.combi, num.combi_sub) is None)
    good = CategoryNames.try_create(["a"] * 18, num.program_sub, num.combi, num.combi_sub)
    check("try-create-good", good is not None)

    # Serialization round-trip survives the to_dict/from_dict path (JSON cache).
    import json
    d = json.loads(json.dumps(num.to_dict()))
    restored = CategoryNames.from_dict(d)
    check("json-roundtrip", restored is not None
          and restored.category_label(0, 3) == num.category_label(0, 3))
    check("json-empty-none", CategoryNames.from_dict(None) is None)
    check("json-truncated-none", CategoryNames.from_dict({"program": ["x"] * 18}) is None)

    if fails:
        print("global_body self-test FAIL:", ", ".join(fails))
        sys.exit(1)
    print("global_body self-test: OK")


if __name__ == "__main__":
    _selftest()
