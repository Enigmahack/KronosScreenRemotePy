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


def _message_voices(msg: bytes) -> List[bytes]:
    """The 32 packed voices of one 4104-byte message, validated like the instrument's own `CDX7ProgramBankDump::
    ReadAndValidate` (EVA 3.2.2): F0 43 0n(any channel) 09 ..., F7 last, data bytes masked with 0x7F, then the sum of the
    4096 data bytes and the checksum byte must be 0 modulo 128. Bytes 4-5 (the size) are not checked."""
    if msg[0] != 0xF0 or msg[1] != 0x43 or msg[2] & 0xF0 or msg[3] != 0x09 or msg[-1] != 0xF7:
        raise dx7_voice.Dx7Error("Not a DX7 32-voice bulk dump.")
    body = bytes(b & 0x7F for b in msg[6:6 + 4096])
    if (sum(body) + msg[6 + 4096]) & 0x7F:
        raise dx7_voice.Dx7Error("The bank's checksum is wrong.")
    return [body[i * 128:(i + 1) * 128] for i in range(32)]


def read_dx7_syx(data: bytes) -> List[bytes]:
    """The packed 128-byte voices of a DX7 bulk dump file. Several back-to-back dumps are all read. Anything else is skipped:
    other makers' messages and the second message of a DX7II file (`F0 43 0n 06 ...`, 32 x 35 bytes of supplement data per
    voice, which the instrument's own importer ignores too - it reads only the first 4104 bytes). A 4104-byte candidate
    with a bad checksum is skipped, not guessed at; if nothing valid is found the checksum problem is reported."""
    voices: List[bytes] = []
    problem: Optional[Exception] = None
    i = 0
    while i + DX7_BANK_SIZE <= len(data):
        if data[i] == 0xF0 and data[i + 1] == 0x43 and data[i + 3] == 0x09 and data[i + DX7_BANK_SIZE - 1] == 0xF7:
            try:
                voices += _message_voices(data[i:i + DX7_BANK_SIZE])
            except dx7_voice.Dx7Error as e:
                problem = e
            i += DX7_BANK_SIZE
        else:
            i += 1
    if not voices:
        raise problem or dx7_voice.Dx7Error(
            "No DX7 32-voice bulk dump (4104 bytes, F0 43 0n 09 20 00 ...) found in the file.")
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
