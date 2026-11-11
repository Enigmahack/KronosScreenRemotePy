"""DX7 .syx banks -> Nautilus EXi Programs (MOD-7), written with the same safe push as every other conversion.

`dx7_items` turns the voices of a DX7 32-voice bulk dump (4104 bytes, F0 43 0n 09 20 00 ...) into `ConvertItem`s whose body
is already a Nautilus EXi Program (`native=True`, so Conversion.push copies it instead of converting it). The destination is
a Nautilus EXi bank (default Q = wire 0x4A, see `Conversion.push.EXI_DEST`); push_converted reads every target slot, backs the
old content up to a .syx, writes, stores the bank once and re-reads to verify.
The conversion itself is `Conversion.dx7_to_mod7` (a port of the instrument's own import; not yet checked by ear on hardware)."""
from __future__ import annotations

import os
from typing import Callable, List, Optional, Tuple

from Conversion import dx7_to_mod7
from Conversion.push import EXI_DEST, ConvertItem
from Data import dx7_voice
from Data.librarian_sysex import OBJ_PROGRAM

DX7_BANK_SIZE = 4104
DEFAULT_EXI_BANK = 0x4A      # Q


def read_dx7_syx(data: bytes) -> List[bytes]:
    """The packed 128-byte voices of a DX7 bulk dump. A file may hold several back-to-back dumps; each 4104-byte
    F0 43 .. F7 message is parsed, anything else (DX7II 5232-byte messages, other makers) is skipped."""
    voices: List[bytes] = []
    i = 0
    while i + DX7_BANK_SIZE <= len(data):
        if data[i] == 0xF0 and data[i + 1] == 0x43 and data[i + 3] == 0x09 and data[i + DX7_BANK_SIZE - 1] == 0xF7:
            voices += dx7_voice.parse_bank(data[i:i + DX7_BANK_SIZE])
            i += DX7_BANK_SIZE
        else:
            i += 1
    if not voices:
        raise dx7_voice.Dx7Error("No DX7 32-voice bulk dump (4104 bytes, F0 43 0n 09 20 00 ...) found in the file.")
    return voices


def dx7_items(voices: List[bytes]) -> List[ConvertItem]:
    """One native EXi Program item per voice, named after the voice (padding spaces dropped)."""
    items = []
    for v in voices:
        name = dx7_voice.name_of(v)
        items.append(ConvertItem(OBJ_PROGRAM, name or "INIT VOICE", dx7_to_mod7.convert_voice(v), is_exi=True, native=True))
    return items


def dx7_dest(bank: int = DEFAULT_EXI_BANK, first_slot: int = 0) -> dict:
    """`dest` for Conversion.push.push_converted: every item is an EXi Program."""
    return {OBJ_PROGRAM: (bank, first_slot), EXI_DEST: (bank, first_slot)}


def load_dx7_file(path: str, limit: Optional[int] = None) -> List[ConvertItem]:
    with open(path, "rb") as f:
        voices = read_dx7_syx(f.read())
    return dx7_items(voices[:limit] if limit else voices)
