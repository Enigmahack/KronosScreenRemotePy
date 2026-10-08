r"""
Per-object-type registry — port of Core/LocalLibrary/ObjectTypeRegistry.cs plus the Drum Kit /
Wave Sequence parts of Core/KronosBanks.cs.

One place that answers "what banks does this type have, how many slots in each, what are they
called, which are read-only" so the pull planner, local library, placement logic and the
Librarian tree iterate this table instead of hardcoding Program/Combi/Set List.

Bank bytes are object-dump (header) encoding. Drum Kit (obj 0x04) and Wave Sequence (obj 0x05)
use bank 0 = INT, 0x10 = GM (Drum Kit only, read-only), 0x40-0x4D = USER-A..GG (14)
(KRONOS_MIDI_SysEx.txt *2). Slot counts differ per bank (Drum Kit Int=40/User=16, Wave
Sequence Int=150/User=32) — taken from the .pcg corpus' DBK1/WBK1 chunk counts, not stated in the
MIDI doc, so they are per-bank here and never a single type-wide number.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

import Core.kronos_sysex as ksx
from Data.librarian_sysex import (
    OBJ_COMBI, OBJ_DRUM_KIT, OBJ_PROGRAM, OBJ_SET_LIST, OBJ_WAVE_SEQ,
)

SET_LIST_SLOTS = 128   # Tools.setlist_data.MAX_COUNT — duplicated to keep this module import-light


# ── Labels ───────────────────────────────────────────────────────────────────

def drum_kit_label(ob: int) -> str:
    if ob == 0:
        return "INT"
    if ob == 0x10:
        return "GM"
    if 0x40 <= ob <= 0x4D:
        return ksx._user_label(ob - 0x40)
    return f"?{ob:02X}"


def wave_seq_label(ob: int) -> str:
    if ob == 0:
        return "INT"
    if 0x40 <= ob <= 0x4D:
        return ksx._user_label(ob - 0x40)
    return f"?{ob:02X}"


def is_read_only_drum_kit_bank(ob: int) -> bool:
    return ob == 0x10


# ── Linear ("MS number") addressing ──────────────────────────────────────────
# Wherever an HD-1 Program references a Drum Kit or Wave Sequence (the oscillator zone's own
# "MS Number") it uses LINEAR addressing, not bank+index. GM sits between User-G and User-AA in the
# Drum Kit table (9 slots); Wave Seq has no GM gap.

def drum_kit_linear_to_loc(linear: int) -> Optional[Tuple[int, int]]:
    if 0 <= linear <= 39:
        return (0, linear)
    if 40 <= linear <= 151:
        return (0x40 + (linear - 40) // 16, (linear - 40) % 16)
    if 152 <= linear <= 160:
        return (0x10, linear - 152)
    if 161 <= linear <= 272:
        return (0x47 + (linear - 161) // 16, (linear - 161) % 16)
    return None


def drum_kit_loc_to_linear(bank: int, slot: int) -> Optional[int]:
    if bank == 0 and 0 <= slot <= 39:
        return slot
    if bank == 0x10 and 0 <= slot <= 8:
        return 152 + slot
    if 0x40 <= bank <= 0x46 and 0 <= slot <= 15:
        return 40 + (bank - 0x40) * 16 + slot
    if 0x47 <= bank <= 0x4D and 0 <= slot <= 15:
        return 161 + (bank - 0x47) * 16 + slot
    return None


def wave_seq_linear_to_loc(linear: int) -> Optional[Tuple[int, int]]:
    if 0 <= linear <= 149:
        return (0, linear)
    if 150 <= linear <= 373:
        return (0x40 + (linear - 150) // 32, (linear - 150) % 32)
    if 374 <= linear <= 597:
        return (0x47 + (linear - 374) // 32, (linear - 374) % 32)
    return None


def wave_seq_loc_to_linear(bank: int, slot: int) -> Optional[int]:
    if bank == 0 and 0 <= slot <= 149:
        return slot
    if 0x40 <= bank <= 0x46 and 0 <= slot <= 31:
        return 150 + (bank - 0x40) * 32 + slot
    if 0x47 <= bank <= 0x4D and 0 <= slot <= 31:
        return 374 + (bank - 0x47) * 32 + slot
    return None


# ── Descriptors ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ObjectTypeDescriptor:
    obj_type: int
    display_name: str                       # singular ("Drum Kit")
    plural_name: str                        # tree root label ("Drum Kits")
    is_referrer: bool                       # can its bodies reference other objects?
    is_referencable: bool                   # can other objects reference this type?
    func33_ref_type: Optional[int]          # func-33 reference "type" selector, or None
    bank_label: Callable[[int], str]
    editable_banks: Tuple[int, ...]         # WRITABLE banks — pull scope AND write scope
    read_only_banks: Tuple[int, ...]        # browse-only factory banks
    slot_count: Callable[[int], int]
    version: int                            # func-0x73 object version byte for the current OS

    def is_read_only_bank(self, bank: int) -> bool:
        return bank in self.read_only_banks

    def browsable_banks(self) -> Tuple[int, ...]:
        return self.editable_banks + self.read_only_banks


_USER_14 = tuple(range(0x40, 0x4E))

REGISTRY: Dict[int, ObjectTypeDescriptor] = {
    OBJ_PROGRAM: ObjectTypeDescriptor(
        OBJ_PROGRAM, "Program", "Programs", True, True, 1, ksx.program_label,
        tuple(range(0x00, 0x06)) + _USER_14, tuple(range(0x10, 0x1B)), lambda b: 128, 5),
    OBJ_COMBI: ObjectTypeDescriptor(
        OBJ_COMBI, "Combi", "Combis", True, True, 0, ksx.combi_label,
        tuple(range(0x00, 0x07)) + tuple(range(0x40, 0x47)), (), lambda b: 128, 3),
    OBJ_SET_LIST: ObjectTypeDescriptor(
        OBJ_SET_LIST, "Set List", "Set Lists", True, False, None, lambda b: "Set Lists",
        (0,), (), lambda b: SET_LIST_SLOTS, 0),
    OBJ_DRUM_KIT: ObjectTypeDescriptor(
        OBJ_DRUM_KIT, "Drum Kit", "Drum Kits", False, True, None, drum_kit_label,
        (0,) + _USER_14, (0x10,), lambda b: 40 if b == 0 else 16, 3),
    OBJ_WAVE_SEQ: ObjectTypeDescriptor(
        OBJ_WAVE_SEQ, "Wave Sequence", "Wave Sequences", False, True, None, wave_seq_label,
        (0,) + _USER_14, (), lambda b: 150 if b == 0 else 32, 1),
}

#: Registry order == pull order. Program stays first: the pull sweep's "instrument is silent" give-up
#: keys off the first bank it asks about, which must be one that always answers.
TYPE_ORDER: Tuple[int, ...] = (OBJ_PROGRAM, OBJ_COMBI, OBJ_SET_LIST, OBJ_DRUM_KIT, OBJ_WAVE_SEQ)


def get(obj_type: int) -> ObjectTypeDescriptor:
    return REGISTRY[obj_type]


def try_get(obj_type: int) -> Optional[ObjectTypeDescriptor]:
    return REGISTRY.get(obj_type)


def label_for(obj_type: int, bank: int) -> str:
    d = REGISTRY.get(obj_type)
    return d.bank_label(bank) if d else f"?{bank:02X}"


def is_read_only(obj_type: int, bank: int) -> bool:
    d = REGISTRY.get(obj_type)
    return bool(d and d.is_read_only_bank(bank))


def slot_count(obj_type: int, bank: int) -> int:
    return REGISTRY[obj_type].slot_count(bank)


def obj_type_for_func33_ref_type(ref_type: int) -> Optional[int]:
    for d in REGISTRY.values():
        if d.func33_ref_type == ref_type:
            return d.obj_type
    return None


def _selftest() -> None:
    fails: List[str] = []

    def check(name, cond):
        if not cond:
            fails.append(name)

    # linear <-> loc round trips over the full documented ranges
    for lin in range(0, 273):
        loc = drum_kit_linear_to_loc(lin)
        check(f"dk-{lin}", loc is not None and drum_kit_loc_to_linear(*loc) == lin)
    check("dk-out-of-range", drum_kit_linear_to_loc(273) is None)
    for lin in range(0, 598):
        loc = wave_seq_linear_to_loc(lin)
        check(f"ws-{lin}", loc is not None and wave_seq_loc_to_linear(*loc) == lin)
    check("ws-out-of-range", wave_seq_linear_to_loc(598) is None)
    # hardware-verified anchors from the C# source (PcgDrumWaveRefDump)
    check("dk-44-is-UA-004", drum_kit_linear_to_loc(44) == (0x40, 4))
    check("ws-33-is-INT-033", wave_seq_linear_to_loc(33) == (0, 33))
    check("labels", drum_kit_label(0x10) == "GM" and drum_kit_label(0x47) == "U-AA"
          and wave_seq_label(0x4D) == "U-GG" and wave_seq_label(0x10).startswith("?"))
    check("counts", slot_count(OBJ_DRUM_KIT, 0) == 40 and slot_count(OBJ_DRUM_KIT, 0x40) == 16
          and slot_count(OBJ_WAVE_SEQ, 0) == 150 and slot_count(OBJ_WAVE_SEQ, 0x4D) == 32)
    check("read-only", is_read_only(OBJ_DRUM_KIT, 0x10) and not is_read_only(OBJ_WAVE_SEQ, 0x10))
    check("editable-14-user", len(REGISTRY[OBJ_DRUM_KIT].editable_banks) == 15)
    if fails:
        print("FAIL:", fails)
        raise SystemExit(1)
    print("object_types self-test OK")


if __name__ == "__main__":
    _selftest()
