"""
Set List decode — port of Core/SetListData.cs.

A Set List Object Dump (obj 0x0D, func 0x73) decodes to a name + 128 slots.
Layout per SysExInfo/MIDI implementation/SysExParams/SetList.txt:
  slot N base = 24 + N*542, fields relative to base:
    +0    name (24 ASCII)
    +24   packed: type(bits1-0) color(bits5-2) fontLSB(bits7-6)
    +25   bank(bits4-0) + transpose-MSB(bits7-5)
    +26   performance index (0-199)
    +27   hold time (0-22 -> 0-60 s)
    +28   volume (0-127)
    +29   keyboard track(bits3-0) fontMSB(bit4) transpose-LSB(bits7-5)
    +30   comments (512 ASCII)

NOTE: the Set List slot Type field is 0=COMBI, 1=PROGRAM, 2=song — the SAME
convention as func 0x33, NOT the "prog/combi/song" order some docs list.
Hardware-confirmed (see the C# source comment this was ported from).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from Core.kronos_sysex import decode_8to7, resolve_bank_label

_NAME_LEN = 24
_SLOT_BASE = 24
_SLOT_SIZE = 542
_COMMENT_LEN = 512

SLOT_COUNT = 128   # slots per set list
MAX_COUNT = 128    # number of set lists on the Kronos (0..127)


@dataclass(frozen=True)
class SetListSlot:
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
    def type_label(self) -> str:
        return {0: "Combi", 1: "Prog", 2: "Song"}.get(self.type, "?")

    @property
    def performance_label(self) -> str:
        if self.type == 2:
            return f"Song {self.index:03d}"
        return f"{resolve_bank_label(self.type, self.bank)}:{self.index:03d}"

    @property
    def is_empty(self) -> bool:
        return not self.name.strip()


@dataclass(frozen=True)
class SetListData:
    number: int
    name: str
    slots: List[SetListSlot]

    @property
    def is_empty(self) -> bool:
        return len(self.slots) == 0 or all(s.is_empty for s in self.slots)

    @staticmethod
    def from_object_dump(msg: bytes) -> Optional["SetListData"]:
        """Decode a func 0x73 Object Dump for obj 0x0D.
        Message: F0 42 3g 68 73 0D bank idH idL version <data 8->7> F7.
        """
        if len(msg) < 12:
            return None
        if not (msg[0] == 0xF0 and msg[1] == 0x42 and (msg[2] & 0xF0) == 0x30 and
                msg[3] == 0x68 and msg[4] == 0x73 and msg[5] == 0x0D):
            return None

        number = ((msg[7] & 0x7F) << 7) | (msg[8] & 0x7F)
        data_start = 10
        data_end = msg.find(0xF7, data_start)
        if data_end < 0:
            data_end = len(msg)

        binary = decode_8to7(msg, data_start, data_end - data_start)
        if len(binary) < _SLOT_BASE:
            return None

        name = _ascii(binary, 0, _NAME_LEN)
        slots: List[SetListSlot] = []
        for n in range(SLOT_COUNT):
            b = _SLOT_BASE + n * _SLOT_SIZE
            if b + 30 > len(binary):
                break   # truncated dump — keep what decoded

            slot_name = _ascii(binary, b, _NAME_LEN)
            packed = binary[b + 24]
            type_ = packed & 0x03
            color = (packed >> 2) & 0x0F
            bank = binary[b + 25] & 0x1F
            index = binary[b + 26]
            hold = binary[b + 27]
            volume = binary[b + 28]
            com_len = min(_COMMENT_LEN, len(binary) - (b + 30))
            comments = _ascii(binary, b + 30, com_len) if com_len > 0 else ""

            slots.append(SetListSlot(n, slot_name, type_, bank, index, color, hold, volume, comments))

        return SetListData(number, name, slots)


@dataclass(frozen=True)
class SetListSyncResult:
    """Result of a full Set List sweep ("Sync All")."""
    found: dict            # {number: SetListData} — has content, cache it
    confirmed_empty: list  # numbers that dumped blank — drop stale cache entry
    attempted: int
    cancelled: bool


def _ascii(data: bytes, offset: int, length: int) -> str:
    end = min(offset + length, len(data))
    if end <= offset:
        return ""
    chars = []
    for i in range(offset, end):
        c = data[i]
        if 0x20 <= c < 0x7F:
            chars.append(chr(c))
        elif c == 0:
            chars.append("\0")
        else:
            chars.append(" ")
    return "".join(chars).rstrip("\0 ")
