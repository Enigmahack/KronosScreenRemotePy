"""Conversion lookup tables. A same-named file in the app data dir overrides the bundled copy, so a
revised table can ship without a release; an override that won't parse never shadows the bundled one."""
from __future__ import annotations

import functools
import json
import logging
import pathlib
import struct
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

_BUNDLED = pathlib.Path(__file__).resolve().parent.parent / "Resources" / "Conversion"

ROM_MULTISAMPLES = "ROM_MULTISAMPLES"
ROM_DRUMSAMPLES = "ROM_DRUMSAMPLES"


def _read_json(*names: str) -> Optional[list]:
    import Models.storage as storage
    candidates = [storage.data_dir() / n for n in names] + [_BUNDLED / n for n in names]
    for path in candidates:
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as ex:
            log.warning("[conversion] %s unreadable: %s", path, ex)
    return None


@functools.lru_cache(maxsize=None)
def read_resource(name: str) -> bytes:
    """A bundled binary table (an app-data-dir copy wins, like the JSON ones); read once, the data dir is on a share."""
    import Models.storage as storage
    for path in (storage.data_dir() / name, _BUNDLED / name):
        try:
            if path.exists():
                return path.read_bytes()
        except OSError as ex:
            log.warning("[conversion] %s unreadable: %s", path, ex)
    raise FileNotFoundError(name)


def init_program_template(nautilus: bool, exi: bool) -> bytes:
    """An Init Program of the given instrument (EXi shape; HD-1 is its prefix, as on the wire)."""
    raw = read_resource("NautilusInitProgram.bin" if nautilus else "KronosInitProgram.bin")
    return raw if exi else raw[:3700 if nautilus else 3706]


def init_combi_template(nautilus: bool) -> bytes:
    return read_resource("NautilusInitSongTimbreSet.bin" if nautilus else "KronosInitCombination.bin")


class RomWaveformMap:
    target_name = "Nautilus"

    def __init__(self) -> None:
        self._map: Dict[Tuple[str, int], int] = {}
        self._audio_differs: set = set()

    def reversed(self) -> "ReverseRomWaveformMap":
        if getattr(self, "_reversed", None) is None:
            self._reversed = ReverseRomWaveformMap(self)
        return self._reversed

    def __len__(self) -> int:
        return len(self._map)

    def try_map(self, section: str, kronos_index: int) -> Optional[int]:
        return self._map.get((section, kronos_index))

    def is_audio_identical(self, section: str, kronos_index: int) -> bool:
        return (section, kronos_index) not in self._audio_differs

    @classmethod
    def from_entries(cls, entries: Optional[list]) -> "RomWaveformMap":
        m = cls()
        for e in entries or []:
            key = (e["section"], e["kronos_index"])
            m._map[key] = e["nautilus_index"]
            if e.get("audio") == "differs":
                m._audio_differs.add(key)
        return m

    @classmethod
    def from_catalog(cls) -> "RomWaveformMap":
        return cls.from_entries(_read_json("nautilus_rom_waveform_map.json", "NautilusRomWaveformMap.json"))


class ReverseRomWaveformMap:
    """Nautilus ROM sample -> Kronos ROM sample, for the same lookups RomWaveformMap offers. Where several Kronos
    samples map to one Nautilus sample, one with identical audio wins, then the lowest index."""
    target_name = "Kronos"

    def __init__(self, forward: RomWaveformMap) -> None:
        best: Dict[Tuple[str, int], Tuple[bool, int]] = {}
        for (section, kronos_index), nautilus_index in forward._map.items():
            same = (section, kronos_index) not in forward._audio_differs
            key = (section, nautilus_index)
            if key not in best or (same, -kronos_index) > (best[key][0], -best[key][1]):
                best[key] = (same, kronos_index)
        self._best = best

    def __len__(self) -> int:
        return len(self._best)

    def try_map(self, section: str, nautilus_index: int) -> Optional[int]:
        hit = self._best.get((section, nautilus_index))
        return None if hit is None else hit[1]

    def is_audio_identical(self, section: str, nautilus_index: int) -> bool:
        hit = self._best.get((section, nautilus_index))
        return hit is not None and hit[0]


@dataclass(frozen=True)
class ProgramRefEntry:
    kronos_bank: int
    kronos_index: int
    kronos_name: str
    nautilus_bank: int
    nautilus_index: int
    nautilus_name: str
    source: str


class ProgramRefMap:
    """Kronos factory Program (Combi Timbre bank byte, index) -> the Nautilus factory Program that replaces it."""

    def __init__(self) -> None:
        self._by_kronos: Dict[Tuple[int, int], ProgramRefEntry] = {}

    def __len__(self) -> int:
        return len(self._by_kronos)

    def get(self, bank: int, index: int) -> Optional[ProgramRefEntry]:
        return self._by_kronos.get((bank, index))

    def get_by_nautilus(self, bank: int, index: int) -> Optional[ProgramRefEntry]:
        """The Kronos factory Program a Nautilus factory Program replaced (first listed, when several share it)."""
        if not hasattr(self, "_by_nautilus"):
            self._by_nautilus: Dict[Tuple[int, int], ProgramRefEntry] = {}
            for r in self._by_kronos.values():
                self._by_nautilus.setdefault((r.nautilus_bank, r.nautilus_index), r)
        return self._by_nautilus.get((bank, index))

    @classmethod
    def from_entries(cls, entries: Optional[list]) -> "ProgramRefMap":
        m = cls()
        for e in entries or []:
            # The table counts Kronos user banks from 17 (U-A); the bank byte of a Kronos Combi Timbre counts from 18,
            # which is what every lookup here uses. (Checked: its Kronos names match 17-based addresses in 53 of 53
            # corpus PCGs, its Nautilus banks are already the Nautilus Combi's own byte.)
            kronos_bank = e["kronos_bank"] + 1 if 17 <= e["kronos_bank"] <= 30 else e["kronos_bank"]
            r = ProgramRefEntry(kronos_bank, e["kronos_index"], e["kronos_name"], e["nautilus_bank"],
                                e["nautilus_index"], e["nautilus_name"], e["source"])
            m._by_kronos[(r.kronos_bank, r.kronos_index)] = r
        return m

    @classmethod
    def from_catalog(cls) -> "ProgramRefMap":
        return cls.from_entries(_read_json("KronosToNautilusProgramRefs.json"))


# A Combi's Timbre Program bank byte counts user banks from 18 on the Kronos (U-A; 17 is the GM drum bank) and from 17
# on the Nautilus (G; checked against the Combis of a factory Nautilus PCG), so a user bank keeps its wire bank only
# if the byte moves by one.
KRONOS_USER_BANK_FIRST, KRONOS_USER_BANK_LAST = 18, 31
NAUTILUS_USER_BANK_FIRST, NAUTILUS_USER_BANK_LAST = 17, 30
_USER_WIRE_FIRST = 0x40


def kronos_timbre_bank_for_obj(obj_bank: int) -> Optional[int]:
    """The Timbre bank byte a Kronos Combi uses for Program bank `obj_bank` (SysEx bank), if it can name it."""
    if 0 <= obj_bank <= 5:
        return obj_bank
    if _USER_WIRE_FIRST <= obj_bank <= _USER_WIRE_FIRST + KRONOS_USER_BANK_LAST - KRONOS_USER_BANK_FIRST:
        return KRONOS_USER_BANK_FIRST + obj_bank - _USER_WIRE_FIRST
    return None


def nautilus_timbre_bank_for_wire(wire_bank: int) -> Optional[int]:
    if 0 <= wire_bank <= 5:
        return wire_bank
    if _USER_WIRE_FIRST <= wire_bank <= _USER_WIRE_FIRST + NAUTILUS_USER_BANK_LAST - NAUTILUS_USER_BANK_FIRST:
        return NAUTILUS_USER_BANK_FIRST + wire_bank - _USER_WIRE_FIRST
    return None


def kronos_user_to_nautilus_bank(bank: int) -> Optional[int]:
    if KRONOS_USER_BANK_FIRST <= bank <= KRONOS_USER_BANK_LAST:
        return bank - 1
    return None


def nautilus_user_to_kronos_bank(bank: int) -> Optional[int]:
    if NAUTILUS_USER_BANK_FIRST <= bank <= NAUTILUS_USER_BANK_LAST:
        return bank + 1
    return None


def kronos_program_label(bank: int, index: int) -> str:
    if bank <= 5:
        return f"I-{chr(65 + bank)}{index:03d}"
    if 18 <= bank <= 24:
        return f"U-{chr(65 + bank - 18)}{index:03d}"
    if 25 <= bank <= 31:
        return f"U-{chr(65 + bank - 25) * 2}{index:03d}"
    return f"bank {bank}:{index:03d}"


def nautilus_program_label(bank: int, index: int) -> str:
    if bank <= 5:
        return f"{chr(65 + bank)}{index:03d}"
    if 17 <= bank <= 30:
        return f"{chr(71 + bank - 17)}{index:03d}"
    return f"bank {bank}:{index:03d}"


class KarmaGeCatalog:
    def __init__(self) -> None:
        self._names: Dict[int, str] = {}

    def __len__(self) -> int:
        return len(self._names)

    def name(self, ge_number: int) -> Optional[str]:
        return self._names.get(ge_number)

    @classmethod
    def from_entries(cls, entries: Optional[list]) -> "KarmaGeCatalog":
        c = cls()
        for e in entries or []:
            c._names[e["ge_number"]] = e["name"]
        return c

    @classmethod
    def from_catalog(cls) -> "KarmaGeCatalog":
        return cls.from_entries(_read_json("kronos_karma_ge_names.json", "KronosKarmaGeNames.json"))


ARP_PATTERN_SIZE = 556
ARP_ENCODING_NORMAL, ARP_ENCODING_FIXED = 0, 1
_GE_LIB_ENTRY = 2 + 1 + 1 + 2 + ARP_PATTERN_SIZE


@dataclass(frozen=True)
class GePatternEntry:
    ge_number: int
    mode: int
    steps_per_quarter: int
    score: float
    pattern: bytes


class KarmaGePatternLibrary:
    """Transcribed Nautilus arp patterns per Kronos GE, packed offline. Format (LE): 'KGPL', u16 version=1,
    u16 count, then per entry u16 ge, u8 encoding (0 Normal/1 Fixed), u8 steps/quarter, u16 score*1000, 556-byte pattern."""

    def __init__(self) -> None:
        self._by_ge: Dict[int, GePatternEntry] = {}

    def __len__(self) -> int:
        return len(self._by_ge)

    def for_ge(self, ge_number: int) -> Optional[GePatternEntry]:
        return self._by_ge.get(ge_number)

    @staticmethod
    def pack(entries: List[GePatternEntry]) -> bytes:
        entries = sorted(entries, key=lambda e: e.ge_number)
        out = bytearray(b"KGPL" + struct.pack("<HH", 1, len(entries)))
        for e in entries:
            out += struct.pack("<HBBH", e.ge_number, e.mode, e.steps_per_quarter, int(round(e.score * 1000)))
            out += e.pattern[:ARP_PATTERN_SIZE]
        return bytes(out)

    @classmethod
    def parse(cls, data: bytes) -> Optional["KarmaGePatternLibrary"]:
        if len(data) < 8 or data[:4] != b"KGPL" or struct.unpack_from("<H", data, 4)[0] != 1:
            return None
        count = struct.unpack_from("<H", data, 6)[0]
        if len(data) < 8 + count * _GE_LIB_ENTRY:
            return None
        lib = cls()
        for i in range(count):
            o = 8 + i * _GE_LIB_ENTRY
            ge, mode, spq, score = struct.unpack_from("<HBBH", data, o)
            lib._by_ge[ge] = GePatternEntry(ge, mode, spq, score / 1000.0, bytes(data[o + 6:o + 6 + ARP_PATTERN_SIZE]))
        return lib

    @classmethod
    def from_catalog(cls) -> "KarmaGePatternLibrary":
        import Models.storage as storage
        for path in (storage.data_dir() / "KronosKarmaGePatterns.bin", _BUNDLED / "KronosKarmaGePatterns.bin"):
            try:
                if path.exists() and (lib := cls.parse(path.read_bytes())) is not None:
                    return lib
            except OSError as ex:
                log.warning("[conversion] %s unreadable: %s", path, ex)
        return cls()
