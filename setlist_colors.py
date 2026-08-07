r"""
Set List slot color palette — port of Tools/SetListColors.cs.

The Kronos Set List editor's 16-slot color palette (authentic RGB values off
the device), each with a display name (Default, Charcoal, Brick, ...). A Set
List slot only stores the numeric index (0-15); without this table the
Properties dialog can only show "Color 5" where the instrument itself shows
"Olive" next to the actual swatch.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class SetListColor:
    index: int
    r: int
    g: int
    b: int
    display_name: str

    @property
    def hex(self) -> str:
        return f"#{self.r:02X}{self.g:02X}{self.b:02X}"


# Kronos Set List 16-slot color palette (authentic values from device) —
# index order matches SetListColors.cs's AllColors exactly.
ALL_COLORS: List[SetListColor] = [
    SetListColor(0,  0x4D, 0x4D, 0x4D, "Default"),
    SetListColor(1,  0x2F, 0x2F, 0x2F, "Charcoal"),
    SetListColor(2,  0xB2, 0x3F, 0x3F, "Brick"),
    SetListColor(3,  0x69, 0x1B, 0x1B, "Burgundy"),
    SetListColor(4,  0x91, 0xA7, 0x30, "Ivy"),
    SetListColor(5,  0x37, 0x45, 0x20, "Olive"),
    SetListColor(6,  0xAA, 0x84, 0x2A, "Gold"),
    SetListColor(7,  0x7F, 0x42, 0x36, "Cacao"),
    SetListColor(8,  0x53, 0x60, 0xA5, "Indigo"),
    SetListColor(9,  0x1A, 0x2B, 0x88, "Navy"),
    SetListColor(10, 0xAB, 0x81, 0xA2, "Rose"),
    SetListColor(11, 0x92, 0x67, 0xBA, "Lavender"),
    SetListColor(12, 0x88, 0xA4, 0xC5, "Azure"),
    SetListColor(13, 0x6A, 0x7F, 0x96, "Denim"),
    SetListColor(14, 0x80, 0x80, 0x80, "Silver"),
    SetListColor(15, 0x62, 0x62, 0x62, "Slate"),
]

DEFAULT_COLOR = ALL_COLORS[0]


def get_by_index_or_default(index: int) -> SetListColor:
    """Port of SetListColors.GetByIndexOrDefault — out-of-range falls back to
    Default rather than raising, matching every other malformed-data path in
    this codebase (a stray/legacy slot.color value must not crash the dialog)."""
    if 0 <= index < len(ALL_COLORS):
        return ALL_COLORS[index]
    return DEFAULT_COLOR


def _selftest() -> None:
    fails: List[str] = []

    def check(name: str, cond: bool) -> None:
        if not cond:
            fails.append(name)

    check("count", len(ALL_COLORS) == 16)
    check("index-order", all(c.index == i for i, c in enumerate(ALL_COLORS)))
    check("default-name", get_by_index_or_default(0).display_name == "Default")
    check("olive-name", get_by_index_or_default(5).display_name == "Olive")
    check("olive-hex", get_by_index_or_default(5).hex == "#374520")
    check("out-of-range-high-falls-back", get_by_index_or_default(99).display_name == "Default")
    check("out-of-range-negative-falls-back", get_by_index_or_default(-1).display_name == "Default")

    if fails:
        print("setlist_colors self-test: FAIL:", ", ".join(fails))
        raise SystemExit(1)
    print("setlist_colors self-test: OK")


if __name__ == "__main__":
    _selftest()
