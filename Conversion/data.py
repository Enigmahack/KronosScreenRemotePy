"""Conversion lookup tables. A same-named file in the app data dir overrides the bundled copy, so a
revised table can ship without a release; an override that won't parse never shadows the bundled one."""
from __future__ import annotations

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


class RomWaveformMap:
    def __init__(self) -> None:
        self._map: Dict[Tuple[str, int], int] = {}
        self._audio_differs: set = set()

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
    """Kronos factory Program (timbre bank code, index) -> the Nautilus factory Program that replaces it."""

    def __init__(self) -> None:
        self._by_kronos: Dict[Tuple[int, int], ProgramRefEntry] = {}

    def __len__(self) -> int:
        return len(self._by_kronos)

    def get(self, bank: int, index: int) -> Optional[ProgramRefEntry]:
        return self._by_kronos.get((bank, index))

    @classmethod
    def from_entries(cls, entries: Optional[list]) -> "ProgramRefMap":
        m = cls()
        for e in entries or []:
            r = ProgramRefEntry(e["kronos_bank"], e["kronos_index"], e["kronos_name"], e["nautilus_bank"],
                                e["nautilus_index"], e["nautilus_name"], e["source"])
            m._by_kronos[(r.kronos_bank, r.kronos_index)] = r
        return m

    @classmethod
    def from_catalog(cls) -> "ProgramRefMap":
        return cls.from_entries(_read_json("KronosToNautilusProgramRefs.json"))


def kronos_program_label(bank: int, index: int) -> str:
    if bank <= 5:
        return f"I-{chr(65 + bank)}{index:03d}"
    if 17 <= bank <= 23:
        return f"U-{chr(65 + bank - 17)}{index:03d}"
    if 24 <= bank <= 30:
        return f"U-{chr(65 + bank - 24) * 2}{index:03d}"
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
