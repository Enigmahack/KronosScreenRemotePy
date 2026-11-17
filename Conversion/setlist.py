"""Set List body conversion, Kronos <-> Nautilus (one object = a 24-byte name, 128 slots, a 16-byte footer).

  Kronos 69416 bytes: slot stride 542, comment at slot +30.   Nautilus 69544: stride 543, Dynamics byte at +30
  (value bits 6-0, On bit 7), comment at +31. Every other slot field (name, type/colour/font, bank/transpose, index,
  hold, volume, keyboard track) sits at the same offset. Footer: byte 0 EQ bypass (+ Nautilus input trim in bits 7-1),
  1-9 EQ bands, 10-11 Kronos Control Surface Mode / Assign From (no Nautilus counterpart), 12 slot style (16/8/4 slots
  on both - seen on real Kronos files), 13-15 reserved.

References: a slot's bank is read and written through Data.librarian_sysex (func-33 numbering for both instruments), so a
user bank is U-A <-> G with no shift here. A Program slot is re-pointed to the other instrument's factory Program under the
same rule as Combi Timbres (the table entry, and only while the source slot still holds the factory Program); anything
else keeps its bank and slot, as do Combi and Song slots, and each case is reported. Nothing here is verified on a Kronos
reading a converted body: HW-VERIFY with Conversion.push-style write, Store, read back and look at the front panel."""
from __future__ import annotations

from typing import Callable, Dict, List, Optional

from Conversion.data import (ProgramRefMap, UnknownProgramIdentity, assumed_warning, factory_repoint, kronos_program_label,
                             kronos_user_to_nautilus_bank, nautilus_program_label, nautilus_user_to_kronos_bank)
from Conversion.result import ConversionResult
from Data.librarian_sysex import (SETLIST_KRONOS_SIZE, SETLIST_NAUTILUS_SIZE, set_setlist_slot_ref, setlist_slot_base,
                                  setlist_slot_ref, setlist_stride)

# HW-VERIFY: write a converted Set List to the other instrument (Store, read back byte-identical) and check on the front
# panel: slot names/comments, the Program/Combi each slot calls up, hold/volume/colour; the Nautilus Dynamics default
# (Off, 0) and Input Trim default (0); slot style (footer byte 12) on a unit set to 8 or 4 slots.
_NAME_LEN = 24
_HEAD = 30                 # name + packed fields, identical on both instruments
_COMMENT = 512
_SLOTS = 128
_FOOTER_OFS = 24 + _SLOTS * 542      # Kronos 69400
_FOOTER_OFS_N = 24 + _SLOTS * 543    # Nautilus 69528
_FOOTER_LEN = 16
_HOLD_MAX_NAUTILUS = 0x16            # Kronos allows 0x17 (SetList.txt of each instrument)


def convert_setlist(body: bytes, to_nautilus: bool, ref_map: Optional[ProgramRefMap] = None,
                    program_name: Optional[Callable[[int, int], Optional[str]]] = None,
                    unknown_identity: UnknownProgramIdentity = UnknownProgramIdentity.ASSUME_FACTORY) -> ConversionResult:
    """`program_name(Timbre bank byte, slot)` names the Program at a slot in the source set, in the SOURCE instrument's
    Combi Timbre numbering (Kronos U-A = 18, Nautilus G = 17); None = unknown. See Conversion.auto.convert_object."""
    src_size, dst_size = (SETLIST_KRONOS_SIZE, SETLIST_NAUTILUS_SIZE) if to_nautilus else (SETLIST_NAUTILUS_SIZE,
                                                                                         SETLIST_KRONOS_SIZE)
    if len(body) != src_size:
        who = "Kronos" if to_nautilus else "Nautilus"
        return ConversionResult(None, [], [f"{who} Set List body is {len(body)} bytes, expected {src_size}."])
    warnings: List[str] = []
    out = bytearray(dst_size)
    out[:_NAME_LEN] = body[:_NAME_LEN]
    hold_clamped: List[int] = []
    for s in range(_SLOTS):
        sb, db = setlist_slot_base(body, s), setlist_slot_base(out, s)
        out[db:db + _HEAD] = body[sb:sb + _HEAD]
        if to_nautilus:
            if out[db + 27] > _HOLD_MAX_NAUTILUS:
                out[db + 27] = _HOLD_MAX_NAUTILUS
                hold_clamped.append(s + 1)
            out[db + _HEAD] = 0   # Dynamics: Off, value 0
            out[db + _HEAD + 1:db + _HEAD + 1 + _COMMENT] = body[sb + _HEAD:sb + _HEAD + _COMMENT]
        else:
            if body[sb + _HEAD] != 0:
                warnings.append(f"Slot {s + 1} Dynamics ({'on, ' if body[sb + _HEAD] & 0x80 else ''}value "
                                f"{body[sb + _HEAD] & 0x7F}) has no Kronos counterpart and was dropped.")
            out[db + _HEAD:db + _HEAD + _COMMENT] = body[sb + _HEAD + 1:sb + _HEAD + 1 + _COMMENT]
    if hold_clamped:
        warnings.append(f"Hold Time above the Nautilus maximum was lowered to it in slot(s) {_ranges(hold_clamped)}.")
    _convert_footer(body, out, to_nautilus, warnings)
    _repoint_slots(body, out, to_nautilus, ref_map, program_name, warnings, unknown_identity)
    return ConversionResult(bytes(out), warnings, [])


def _convert_footer(src: bytes, out: bytearray, to_nautilus: bool, warnings: List[str]) -> None:
    f_src = src[(_FOOTER_OFS if to_nautilus else _FOOTER_OFS_N):][:_FOOTER_LEN]
    f = bytearray(f_src)
    if to_nautilus:
        f[0] &= 0x01                       # Kronos keeps only the bypass bit; Nautilus input trim (bits 7-1) = 0
    elif f[0] >> 1:
        warnings.append(f"The Set List EQ Input Trim ({f[0] >> 1}) has no Kronos counterpart and was dropped.")
        f[0] &= 0x01
    f[10] = f[11] = 0                      # Kronos Control Surface Mode / Assign From: Kronos-only, left at their default
    o = _FOOTER_OFS_N if to_nautilus else _FOOTER_OFS
    out[o:o + _FOOTER_LEN] = f
    if to_nautilus and f_src[10:12] != b"\x00\x00":
        warnings.append("The Set List Control Surface Mode / Assign From settings are Kronos-only and were reset.")


def _repoint_slots(src: bytes, out: bytearray, to_nautilus: bool, ref_map: Optional[ProgramRefMap],
                   program_name, warnings: List[str],
                   unknown_identity: UnknownProgramIdentity = UnknownProgramIdentity.ASSUME_FACTORY) -> None:
    use_map = ref_map is not None and len(ref_map) > 0
    remapped: List[str] = []
    assumed: List[str] = []
    kept: Dict[str, List[int]] = {}
    combis: List[int] = []
    songs: List[int] = []
    gm: List[int] = []
    for s in range(_SLOTS):
        base = setlist_slot_base(src, s)
        if not bytes(src[base:base + _NAME_LEN]).strip(b"\x00 \t"):
            continue                       # an unused slot: its zero reference means "nothing", not a dependency
        type_, bank, index = setlist_slot_ref(src, s)
        if type_ == 2:
            songs.append(s + 1)
            continue
        if type_ == 0:
            combis.append(s + 1)
            continue
        if type_ != 1:
            continue
        if 6 < bank < 18:                  # GM (7) and g(1)..g(d) in func-33 numbering
            gm.append(s + 1)
            continue
        label = (kronos_program_label if to_nautilus else nautilus_program_label)(
            bank if to_nautilus else (kronos_user_to_nautilus_bank(bank) or bank), index)
        if to_nautilus:
            e = ref_map.get(bank, index) if use_map else None
            actual = program_name(bank, index) if program_name is not None and e is not None else None
            matches, guessed = factory_repoint(e.kronos_name if e is not None else None, actual, unknown_identity)
        else:
            nbank = kronos_user_to_nautilus_bank(bank) or bank
            e = ref_map.get_by_nautilus(nbank, index) if use_map else None
            actual = program_name(nbank, index) if program_name is not None and e is not None else None
            matches, guessed = factory_repoint(e.nautilus_name if e is not None else None, actual, unknown_identity)
        if not matches:
            if e is not None and actual is None:
                label += " (not in the source set)"
            kept.setdefault(label, []).append(s + 1)
            continue
        done = assumed if guessed else remapped
        if to_nautilus:
            nb = e.nautilus_bank
            new_bank, new_index = nautilus_user_to_kronos_bank(nb) or nb, e.nautilus_index
            done.append(f'{s + 1}: {label} "{e.kronos_name}" -> {nautilus_program_label(nb, new_index)} '
                            f'"{e.nautilus_name}"')
        else:
            new_bank, new_index = e.kronos_bank, e.kronos_index
            done.append(f'{s + 1}: {label} "{e.nautilus_name}" -> {kronos_program_label(new_bank, new_index)} '
                            f'"{e.kronos_name}"')
        set_setlist_slot_ref(out, s, new_bank, new_index)
    other = "Nautilus" if to_nautilus else "Kronos"
    if assumed:
        warnings.append(assumed_warning("Program slots", assumed, other))
    if remapped:
        warnings.append(f"Program slots re-pointed to {other} factory equivalents: {'; '.join(remapped)}.")
    if kept:
        text = "; ".join(f"slot(s) {_ranges(ss)} ({label})" for label, ss in kept.items())
        warnings.append(f"Program references kept their bank and slot (no {other} factory equivalent, or the source slot "
                        f"no longer holds the factory Program; a user bank keeps its position): {text} - those Programs "
                        f"must exist at the same {other} slots.")
    if combis:
        warnings.append(f"Combi slots {_ranges(combis)} keep their bank and slot - factory Combis differ between the "
                        f"instruments, and the Combis must exist at the same {other} slots.")
    if songs:
        warnings.append(f"Song slots {_ranges(songs)} keep their song numbers - the Songs must exist at the same "
                        f"{other} numbers.")
    if gm:
        warnings.append(f"Slots {_ranges(gm)} reference GM / g(n) Programs; they were copied unchanged and not "
                        "checked against the other instrument (not verified).")


def _ranges(nums: List[int]) -> str:
    out: List[str] = []
    i = 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else f"{nums[i]}-{nums[j]}")
        i = j + 1
    return ", ".join(out)
