"""The instrument's own initialised object, used to reset a deleted Program / Combi / Set List / Drum Kit / Wave
Sequence. The protocol has no delete, so a delete is a write of what the firmware itself holds for an untouched slot.

Sources (Resources/Conversion): the Init Program and Init Combination templates for the Kronos, the Nautilus Init
Program, and the Nautilus Combi / Drum Kit / Wave Sequence taken from a real Nautilus .PCG
(research/extract_factory_blanks.py). A Set List is rebuilt slot by slot from the pattern every factory slot not in use
carries (type Program, bank 0, number 0, colour 0, hold 6, volume 127, no name, no comment).
HW-VERIFY: the Kronos has no captured Drum Kit / Wave Sequence blank here, so it gets the Nautilus one (the two
instruments share those body sizes); push one to a Kronos slot and check it reads back as an empty kit / sequence."""
from __future__ import annotations

from typing import Optional

import Objects.object_body as ob
from Conversion.data import init_combi_template, init_program_template, read_resource
from Data import librarian_sysex as lsx

_NAUTILUS_SETLIST_SIZE = 69544
_BLANK_SLOT_BYTES = {24: 1, 27: 6, 28: 127}     # packed type=Program/colour 0, hold time, volume; everything else 0


def blank_body(obj_type: int, existing: bytes, nautilus: bool, exi: Optional[bool] = None,
               number: int = 0) -> Optional[bytes]:
    """None when this object type has no blank (the caller falls back to the derived erase body)."""
    if obj_type == lsx.OBJ_PROGRAM:
        exi = True if exi is None else exi
        body = init_program_template(nautilus, exi)
        return body if exi else ob.write_program_name(body, "Init Program")
    if obj_type == lsx.OBJ_COMBI:
        return read_resource("NautilusInitCombination.bin") if nautilus else init_combi_template(False)
    if obj_type == lsx.OBJ_DRUM_KIT:
        return read_resource("NautilusInitDrumKit.bin")
    if obj_type == lsx.OBJ_WAVE_SEQ:
        return read_resource("NautilusInitWaveSequence.bin")
    if obj_type == lsx.OBJ_SET_LIST:
        return _blank_setlist(existing, number) if existing else None
    return None


def _blank_setlist(existing: bytes, number: int) -> bytes:
    body = bytearray(existing)
    body[:ob.NAME_LEN] = f"Set List {number:03d}".encode("ascii").ljust(ob.NAME_LEN, bytes(1))
    for slot in range(ob.SLOT_COUNT):
        start = ob._slot_base(body, slot)
        size = ob.SLOT_SIZE + (1 if len(body) == _NAUTILUS_SETLIST_SIZE else 0)
        body[start:start + size] = bytes(size)
        for ofs, value in _BLANK_SLOT_BYTES.items():
            body[start + ofs] = value
    return bytes(body)
