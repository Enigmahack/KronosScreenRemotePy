"""
Korg Kronos SysEx decode/encode utilities — port of Networking/KronosSysEx.cs and
Core/KronosBanks.cs from the Windows client.

Contains only pure decode/encode logic (no I/O): the 8-to-7 SysEx codec, bank
number <-> display label tables, live Bank-Select+ProgramChange decode, and the
Object Dump Request / Dump Bank Request builders used by SysExDumpCollector.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# ── Hex utilities ────────────────────────────────────────────────────────────


def hex_to_bytes(hex_str: str) -> Optional[bytes]:
    clean = hex_str.replace(" ", "").replace("\t", "").replace("\n", "")
    if len(clean) % 2 != 0:
        return None
    try:
        return bytes.fromhex(clean)
    except ValueError:
        return None


def bytes_to_hex(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


# ── Korg 8-to-7-bit SysEx codec ──────────────────────────────────────────────


def decode_8to7(src: bytes, offset: int = 0, sysex_len: Optional[int] = None) -> bytes:
    """Every 8 SysEx bytes encode 7 binary bytes; first byte carries the MSBs."""
    if sysex_len is None:
        sysex_len = len(src) - offset
    binary_len = (sysex_len // 8) * 7 + (sysex_len % 8 - 1 if sysex_len % 8 > 0 else 0)
    dst = bytearray(binary_len)
    si, di = offset, 0
    end = offset + sysex_len
    while si < end and di < binary_len:
        msbs = src[si]
        si += 1
        bit = 0
        while bit < 7 and si < end and di < binary_len:
            dst[di] = src[si] | (((msbs >> bit) & 1) << 7)
            si += 1
            di += 1
            bit += 1
    return bytes(dst)


def encode_7to8(src: bytes, offset: int = 0, binary_len: Optional[int] = None) -> bytes:
    """Every 7 binary bytes become 8 SysEx bytes."""
    if binary_len is None:
        binary_len = len(src) - offset
    sysex_len = binary_len + (binary_len + 6) // 7
    dst = bytearray(sysex_len)
    si, di = offset, 0
    end = offset + binary_len
    while si < end:
        group_len = min(7, end - si)
        msbs = 0
        for bit in range(group_len):
            msbs |= ((src[si + bit] >> 7) & 1) << bit
        dst[di] = msbs
        di += 1
        for bit in range(group_len):
            dst[di] = src[si] & 0x7F
            si += 1
            di += 1
    return bytes(dst)


# ── Bank identity ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BankId:
    type: int          # 0 = combi, 1 = program (func 0x33 convention)
    label: str
    obj_bank: int       # object-dump bank number (KRONOS_MIDI_SysEx.txt *2)
    number: int         # raw program-change number (cache key)

    @property
    def display(self) -> str:
        one_based = self.label == "GM" or self.label.startswith("g(")
        n = self.number + 1 if one_based else self.number
        return f"{self.label}:{n:03d}"


@dataclass
class CachedName:
    type: int
    bank: int
    number: int
    name: str


# Combis have SEVEN internal banks (I-A..I-G) — one more than programs (I-A..I-F).
COMBI_BANKS: List[str] = [
    "I-A", "I-B", "I-C", "I-D", "I-E", "I-F", "I-G",
    "U-A", "U-B", "U-C", "U-D", "U-E", "U-F", "U-G",
]

PROGRAM_BANKS: List[str] = [
    "I-A", "I-B", "I-C", "I-D", "I-E", "I-F", "I-G",
    "GM", "g(1)", "g(2)", "g(3)", "g(4)", "g(5)", "g(6)", "g(7)",
    "g(8)", "g(9)", "g(d)",
    "U-A", "U-B", "U-C", "U-D", "U-E", "U-F", "U-G",
    "U-AA", "U-BB", "U-CC", "U-DD", "U-EE", "U-FF", "U-GG",
]


def resolve_bank_label(type_: int, bank: int) -> str:
    """type: 0=Combi, 1=Program, 2=Song."""
    if type_ == 0:
        return COMBI_BANKS[bank] if bank < len(COMBI_BANKS) else f"?{bank}"
    if type_ == 1:
        return PROGRAM_BANKS[bank] if bank < len(PROGRAM_BANKS) else f"?{bank}"
    if type_ == 2:
        return ""
    return f"?{bank}"


def _int_label(i: int) -> str:
    return f"I-{chr(ord('A') + i)}"


def _user_label(i: int) -> str:
    if i <= 6:
        return f"U-{chr(ord('A') + i)}"
    j = i - 7
    c = chr(ord('A') + j)
    return f"U-{c}{c}"


def program_label(ob: int) -> str:
    if 0x00 <= ob <= 0x06:
        return _int_label(ob)
    if ob == 0x10:
        return "GM"
    if 0x11 <= ob <= 0x19:
        return f"g({ob - 0x10})"
    if ob == 0x1A:
        return "g(d)"
    if 0x40 <= ob <= 0x4D:
        return _user_label(ob - 0x40)
    return f"?{ob:02X}"


def combi_label(ob: int) -> str:
    if 0x00 <= ob <= 0x06:
        return _int_label(ob)
    if 0x40 <= ob <= 0x46:
        return _user_label(ob - 0x40)
    return f"?{ob:02X}"


def program_obj_bank(msb: int, lsb: int) -> int:
    if msb in (0x00, 0x3F) and 0x00 <= lsb <= 0x06:
        return lsb
    if msb in (0x00, 0x3F) and 0x08 <= lsb <= 0x15:
        return 0x40 + (lsb - 8)
    if msb == 0x79 and lsb == 0x00:
        return 0x10
    if msb == 0x79 and 0x01 <= lsb <= 0x09:
        return 0x10 + lsb
    if msb == 0x78 and lsb == 0x00:
        return 0x1A
    return -1


def combi_obj_bank(msb: int, lsb: int) -> int:
    if msb in (0x00, 0x3F) and 0x00 <= lsb <= 0x06:
        return lsb
    if msb in (0x00, 0x3F) and 0x08 <= lsb <= 0x0E:
        return 0x40 + (lsb - 8)
    return -1


def name_object(type_: int) -> int:
    """Object-dump name-object type: program -> 0x13, combi -> 0x12."""
    return 0x12 if type_ == 0 else 0x13


def decode_bank(state_mode: int, msb: int, lsb: int, pc: int) -> Optional[BankId]:
    """state_mode: 3 = Program, 2 = Combi. Other modes -> None (use func 0x33)."""
    if state_mode == 3:
        ob = program_obj_bank(msb, lsb)
        return None if ob < 0 else BankId(1, program_label(ob), ob, pc)
    if state_mode == 2:
        ob = combi_obj_bank(msb, lsb)
        return None if ob < 0 else BankId(0, combi_label(ob), ob, pc)
    return None


def _func33_to_obj_bank(type_: int, idx: int) -> int:
    if type_ == 1:  # program: seven internal banks
        if 0 <= idx <= 6:
            return idx
        if idx == 7:
            return 0x10
        if 8 <= idx <= 17:
            return 0x10 + (idx - 7)
        if 18 <= idx <= 31:
            return 0x40 + (idx - 18)
        return -1
    if type_ == 0:  # combi: seven internal banks
        if 0 <= idx <= 6:
            return idx
        if 7 <= idx <= 13:
            return 0x40 + (idx - 7)
        return -1
    return -1


def from_func33(type_: int, func33_bank: int, number: int) -> Optional[BankId]:
    ob = _func33_to_obj_bank(type_, func33_bank)
    if ob < 0:
        return None
    label = program_label(ob) if type_ == 1 else combi_label(ob)
    return BankId(type_, label, ob, number)


def all_name_banks() -> List[Tuple[int, int]]:
    """(type, objBank) pairs to sweep for a full Sync Names."""
    banks: List[Tuple[int, int]] = []
    banks += [(1, b) for b in range(0x00, 0x07)]   # program INT   I-A..I-G
    banks += [(0, b) for b in range(0x00, 0x07)]   # combi INT     I-A..I-G
    banks += [(1, b) for b in range(0x10, 0x1B)]   # program GM/g
    banks += [(1, b) for b in range(0x40, 0x4E)]   # program USER
    banks += [(0, b) for b in range(0x40, 0x47)]   # combi USER
    return banks


# ── SysEx Mode Data (func 0x42) ──────────────────────────────────────────────

_MODE_NAMES: Dict[int, str] = {
    0: "Combi", 2: "Program", 4: "Sequencer", 6: "Sampling",
    7: "Global", 8: "Disk", 9: "Setlist",
}

_MODE_TO_STATE: Dict[int, int] = {
    0: 2, 2: 3, 4: 4, 6: 5, 7: 6, 8: 7, 9: 1,
}


@dataclass(frozen=True)
class SysExModeData:
    mode: int
    option: int
    setup1: int
    setup2: int

    @property
    def mode_name(self) -> str:
        return _MODE_NAMES.get(self.mode, f"Unknown ({self.mode})")

    def to_state_mode(self) -> int:
        return _MODE_TO_STATE.get(self.mode, 0)


def parse_mode_data(data: bytes) -> Optional[SysExModeData]:
    if len(data) < 10:
        return None
    for i in range(0, len(data) - 9):
        if (data[i] == 0xF0 and data[i + 1] == 0x42 and (data[i + 2] & 0xF0) == 0x30 and
                data[i + 3] == 0x68 and data[i + 4] == 0x42):
            return SysExModeData(
                data[i + 5] & 0x0F, data[i + 6] & 0x7F,
                data[i + 7] & 0x7F, data[i + 8] & 0x7F)
    return None


# ── Current Performance Id (func 0x33) ───────────────────────────────────────


@dataclass(frozen=True)
class PerformanceInfo:
    type: int
    bank: int
    number: int
    bank_label: str
    type_label: str
    name: str = ""

    def to_state_mode(self) -> int:
        return {0: 2, 1: 3, 2: 4}.get(self.type, 0)

    def to_display_string(self) -> str:
        id_str = f"Song {self.number:03d}" if self.type == 2 else f"{self.bank_label}:{self.number:03d}"
        return id_str if not self.name.strip() else f"{id_str} {self.name}"


def _is_valid_performance(type_: int, bank: int, number: int) -> bool:
    if type_ == 0:
        return bank < len(COMBI_BANKS) and number <= 127
    if type_ == 1:
        return bank < len(PROGRAM_BANKS) and number <= 127
    if type_ == 2:
        return number <= 199
    return False


def parse_performance_id(data: bytes) -> Optional[PerformanceInfo]:
    for i in range(0, len(data) - 4):
        if not (data[i] == 0xF0 and data[i + 1] == 0x42 and (data[i + 2] & 0xF0) == 0x30 and
                data[i + 3] == 0x68 and data[i + 4] == 0x33):
            continue
        end = data.find(0xF7, i + 5)
        if end < 0:
            end = len(data)
        if end - (i + 5) < 4:
            continue

        type_ = data[i + 5] & 0x7F
        bank = data[end - 3] & 0x7F
        number = ((data[end - 2] & 0x7F) << 7) | (data[end - 1] & 0x7F)

        if not _is_valid_performance(type_, bank, number):
            continue

        return PerformanceInfo(
            type_, bank, number, resolve_bank_label(type_, bank),
            {0: "Combi", 1: "Program", 2: "Song"}.get(type_, "Unknown"))
    return None


# ── Object Dump (func 0x73) name parsing ─────────────────────────────────────


def parse_name_dump(data: bytes, expected_obj: int) -> Optional[str]:
    """Parse Current Object Dump (func 0x75) name-only reply."""
    for i in range(0, len(data) - 7):
        if not (data[i] == 0xF0 and data[i + 1] == 0x42 and (data[i + 2] & 0xF0) == 0x30 and
                data[i + 3] == 0x68 and data[i + 4] == 0x75 and data[i + 5] == expected_obj):
            continue
        data_start = i + 7
        data_end = data.find(0xF7, data_start)
        if data_end < 0:
            data_end = len(data)
        sysex_len = data_end - data_start
        if sysex_len < 2:
            return None
        decoded = decode_8to7(data, data_start, sysex_len)
        if len(decoded) < 24:
            return None
        return _ascii_trim(decoded, 0, 24)
    return None


def parse_name_object_dump(msg: bytes) -> Tuple[int, str]:
    """Parse an Object Dump (func 0x73) for a name-only object into (index, name)."""
    if len(msg) < 12:
        return (-1, "")
    if not (msg[0] == 0xF0 and msg[1] == 0x42 and (msg[2] & 0xF0) == 0x30 and
            msg[3] == 0x68 and msg[4] == 0x73):
        return (-1, "")

    index = ((msg[7] & 0x7F) << 7) | (msg[8] & 0x7F)
    data_start = 10
    data_end = msg.find(0xF7, data_start)
    if data_end < 0:
        data_end = len(msg)
    if data_end - data_start < 2:
        return (index, "")

    decode_len = min(data_end - data_start, 32)
    decoded = decode_8to7(msg, data_start, decode_len)
    n = min(24, len(decoded))
    return (index, _ascii_trim(decoded, 0, n))


def _ascii_trim(data: bytes, offset: int, length: int) -> str:
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


# ── Request builders (Korg header F0 42 30 68) ───────────────────────────────


def object_dump_request(obj: int, bank: int, index: int) -> str:
    """Object Dump Request (func 0x72): one specific object."""
    return (f"F0 42 30 68 72 {obj:02X} {bank:02X} "
            f"{(index >> 7) & 0x7F:02X} {index & 0x7F:02X} F7")


def dump_bank_request(obj: int, bank: int) -> str:
    """Dump Bank Request (func 0x77): every object of a type in a bank (preset banks only)."""
    return f"F0 42 30 68 77 {obj:02X} {bank:02X} F7"


def mode_request_hex() -> str:
    return "F0 42 30 68 12 F7"


def perf_id_request_hex() -> str:
    return "F0 42 30 68 32 F7"


def current_name_request_hex(perf_type: int) -> Optional[str]:
    obj = {0: 0x12, 1: 0x13, 2: 0x14}.get(perf_type, -1)
    if obj < 0:
        return None
    return f"F0 42 30 68 74 {obj:02X} F7"


# ── Human-readable MIDI decode (for the SysEx traffic log) ──────────────────

_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def _note_name(midi: int) -> str:
    return f"{_NOTE_NAMES[midi % 12]}{midi // 12 - 1}"


def _pitch_bend(lsb: int, msb: int) -> int:
    return ((msb << 7) | lsb) - 8192


def classify_message(msg: bytes) -> str:
    """Bucket a complete message for UI filter badges."""
    if not msg:
        return "Other"
    status = msg[0]
    if status == 0xF0:
        return "SysEx"
    hi = status & 0xF0
    if status >= 0xF8 or status in (0xFA, 0xFB, 0xFC, 0xFF):
        return "Transport"
    if hi == 0x90 and len(msg) >= 3 and msg[2] > 0:
        return "Note"
    if hi in (0x80, 0x90):
        return "Note"
    if hi == 0xB0:
        return "CC"
    if hi == 0xC0:
        return "ProgramChange"
    if hi == 0xE0:
        return "PitchBend"
    if hi in (0xA0, 0xD0):
        return "AfterTouch"
    return "Other"


def decode_midi(msg: bytes, max_hex_bytes: Optional[int] = None) -> str:
    if not msg:
        return ""
    status = msg[0]

    if status == 0xF0:
        raw = bytes_to_hex(msg if max_hex_bytes is None else msg[:max_hex_bytes])
        suffix = "" if max_hex_bytes is None or len(msg) <= max_hex_bytes else \
            f" … (+{len(msg) - max_hex_bytes} bytes)"
        if len(msg) >= 5 and msg[1] == 0x42 and (msg[2] & 0xF0) == 0x30 and msg[3] == 0x68:
            return f"SysEx Korg func={msg[4]:02X} [{len(msg)}B]  [{raw}{suffix}]"
        return f"SysEx [{len(msg)}B]  [{raw}{suffix}]"

    hexs = f"[{bytes_to_hex(msg)}]"

    if status == 0xFA:
        return f"Start              {hexs}"
    if status == 0xFB:
        return f"Continue           {hexs}"
    if status == 0xFC:
        return f"Stop               {hexs}"
    if status == 0xFF:
        return f"Reset              {hexs}"
    if (status & 0x80) == 0:
        return hexs

    ch = (status & 0x0F) + 1
    hi = status & 0xF0
    if hi == 0x90 and len(msg) >= 3 and msg[2] > 0:
        return f"NoteOn  Ch{ch:<2} {_note_name(msg[1])} vel={msg[2]:<3}  {hexs}"
    if hi == 0x90 and len(msg) >= 3:
        return f"NoteOff Ch{ch:<2} {_note_name(msg[1])}          {hexs}"
    if hi == 0x80 and len(msg) >= 3:
        return f"NoteOff Ch{ch:<2} {_note_name(msg[1])}          {hexs}"
    if hi == 0xB0 and len(msg) >= 3:
        return f"CC#{msg[1]:<3} Ch{ch:<2} val={msg[2]:<3}    {hexs}"
    if hi == 0xC0 and len(msg) >= 2:
        return f"PC      Ch{ch:<2} #{msg[1]:<3}          {hexs}"
    if hi == 0xE0 and len(msg) >= 3:
        return f"Bend    Ch{ch:<2} {_pitch_bend(msg[1], msg[2]):+6}      {hexs}"
    if hi == 0xD0 and len(msg) >= 2:
        return f"ChPres  Ch{ch:<2} val={msg[1]:<3}    {hexs}"
    if hi == 0xA0 and len(msg) >= 3:
        return f"PolyPres Ch{ch:<2} {_note_name(msg[1])} val={msg[2]:<3}  {hexs}"
    return hexs


def decode_hex(hex_str: str) -> str:
    b = hex_to_bytes(hex_str)
    if b is None:
        return hex_str
    return decode_midi(b)
