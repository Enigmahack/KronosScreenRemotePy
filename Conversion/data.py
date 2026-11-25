"""Conversion lookup tables. A same-named file in the app data dir overrides the bundled copy, so a
revised table can ship without a release; an override that won't parse never shadows the bundled one."""
from __future__ import annotations

import enum
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

# "KORG", eight zero bytes, "MS", zero: the first 15 bytes of a ROM / RAM / numbered-EXs bank reference; byte 15 is bank << 1 | mono-stereo flag.
KORG_MS_PREFIX = bytes([0x4B, 0x4F, 0x52, 0x47, 0, 0, 0, 0, 0, 0, 0, 0, 0x4D, 0x53, 0x00])


@dataclass(frozen=True)
class SampleTarget:
    """Where a sample lives on the other instrument. `uuid` is None for its ROM, else the 16-byte bank id of one of its
    EXs banks. `partial`: some zones of the multisample have no counterpart there. `reverse`: the source multisample is
    a reversed one and the target is the forward sample, so the Program has to play it reversed."""
    index: int
    uuid: Optional[bytes] = None
    partial: bool = False
    reverse: bool = False
    stereo: Optional[bool] = None   # drum samples: force the mono/stereo flag (a Nautilus stereo pair is entered as its "-L" sample, flagged stereo)

    def ref_bytes(self, source_uuid: bytes) -> bytes:
        """The 16-byte bank reference to write; the source's mono/stereo flag (byte 15 bit 0) is kept unless `stereo` is set."""
        flag = source_uuid[15] & 1 if self.stereo is None else int(self.stereo)
        if self.uuid is None:
            return KORG_MS_PREFIX + bytes([flag])
        return self.uuid[:15] + bytes([(self.uuid[15] & 0xFE) | flag])


# "Acoustic Piano 1 mf-L". The two ROM numberings are offset (NautilusSampleEvidence.json: Kronos #0 = Nautilus #2), and
# Nautilus #0 is the placeholder its own Wave Sequences leave on unused steps, not a piano.
DEFAULT_PIANO = SampleTarget(0)             # on the Kronos
DEFAULT_PIANO_NAUTILUS = SampleTarget(2)    # on the Nautilus


def default_piano(rom_map) -> SampleTarget:
    """The fallback sample for a zone with no equivalent on the instrument `rom_map` converts to."""
    return DEFAULT_PIANO if getattr(rom_map, "target_name", "Nautilus") == "Kronos" else DEFAULT_PIANO_NAUTILUS


@functools.lru_cache(maxsize=None)
def nautilus_stereo_pairs() -> Tuple[int, ...]:
    """Raw ROM drum-sample index of each Nautilus stereo pair's "-L" half, in order. A drum zone whose bank id has the
    stereo flag (byte 15 bit 0) names its sample by position in THIS list ("ROM s": 0..1089, last "Applause-amb1"), not
    by raw index; a mono zone (flag 0, "ROM m") uses the raw index. Built from the ROM metadata (consecutive "X-L"/"X-R")
    and confirmed on the unit: pair 1064 is "Wood Block 5b" (raw 3383), the list ends at 1089."""
    return tuple(_read_json("nautilus_stereo_pairs.json", "NautilusStereoPairs.json") or ())


@functools.lru_cache(maxsize=None)
def nautilus_pair_ordinals() -> Dict[int, int]:
    return {raw: i for i, raw in enumerate(nautilus_stereo_pairs())}


def _section_kind(section: str) -> str:
    return ROM_DRUMSAMPLES if "DRUMSAMPLES" in section else ROM_MULTISAMPLES


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
        self._ext: Dict[Tuple[str, int, int], SampleTarget] = {}   # (kind, Kronos EXs number, index) -> target
        self._partial: set = set()                                  # ROM keys mapped to a partial multisample
        self._reverse: set = set()                                  # ROM keys whose Kronos sample is a reversed one
        self._source: Dict[Tuple[str, int], Optional[str]] = {}
        self._manual: set = set()
        self._nautilus_banks: set = set()   # bank ids (upper-case hex, flag bit cleared) of every Nautilus EXs bank
        self._similar: Dict[str, Dict[int, tuple]] = {}   # DrumSampleSimilarity.json, both directions

    def lookup(self, section: str, bank: int, kronos_index: int) -> Optional[SampleTarget]:
        """`bank` 0 is the Kronos ROM, N >= 1 is its EXs<N>."""
        key = (section, kronos_index)
        if bank == 0:
            idx = self._map.get(key)
            return None if idx is None else SampleTarget(idx, None, key in self._partial, key in self._reverse)
        return self._ext.get((section, bank, kronos_index))

    def similar_drumsample(self, kronos_index: int) -> Optional[Tuple[SampleTarget, float, str, bool]]:
        """Closest-sounding Nautilus ROM drum sample for a Kronos one with no exact twin: (target, distance, tier)."""
        hit = self._similar.get("kronos_to_nautilus", {}).get(kronos_index)
        return None if hit is None else (SampleTarget(hit[0], stereo=True if hit[3] else None), hit[1], hit[2], False)

    def similar_multisample(self, kronos_index: int) -> Optional[Tuple[SampleTarget, float, str]]:
        """Closest-sounding Nautilus ROM multisample for a Kronos one with no twin (Wave Sequence steps): (target, distance, tier)."""
        hit = self._similar.get("kronos_to_nautilus_ms", {}).get(kronos_index)
        return None if hit is None else (SampleTarget(hit[0]), hit[1], hit[2])

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
            m._source[key] = e.get("source")
            if e.get("source") == "manual":
                m._manual.add(key)
            if e.get("audio") == "differs":
                m._audio_differs.add(key)
        return m

    def merge_evidence(self, entries: Optional[list]) -> None:
        """Layer the audio-comparison results (build_conversion_evidence.py) over the hand-made ROM map. An entry marked
        "manual" is never touched; an approximate or name-based one is replaced by a better audio match."""
        manual = {k for k in self._manual}
        for e in entries or []:
            kind = _section_kind(e["section"])
            n = int(e["kronos_bank"][4:])
            uuid = bytes.fromhex(e["nautilus_uuid"].replace("-", "")) if e.get("nautilus_uuid") else None
            partial = e["match"] == "partial"
            if n:
                self._ext[(kind, n, e["kronos_index"])] = SampleTarget(e["nautilus_index"], uuid, partial,
                                                                       bool(e.get("reverse")))
                continue
            key = (kind, e["kronos_index"])
            if key in manual:
                continue
            old = self._map.get(key)
            weak = key in self._audio_differs or self._source.get(key) == "name"
            if old is None or (weak and not partial):
                self._map[key] = e["nautilus_index"]
                self._audio_differs.discard(key)
                (self._partial.add if partial else self._partial.discard)(key)
            if e.get("reverse") and self._map.get(key) == e["nautilus_index"]:
                self._reverse.add(key)

    @classmethod
    def from_catalog(cls) -> "RomWaveformMap":
        m = cls.from_entries(_read_json("nautilus_rom_waveform_map.json", "NautilusRomWaveformMap.json"))
        m.merge_evidence(_read_json("nautilus_sample_evidence.json", "NautilusSampleEvidence.json"))
        sim = _read_json("drum_sample_similarity.json", "DrumSampleSimilarity.json")
        if isinstance(sim, dict):
            m._similar = {d: {int(k): tuple(v) for k, v in sim.get(d, {}).items()}
                          for d in ("kronos_to_nautilus", "nautilus_to_kronos", "kronos_to_nautilus_ms", "nautilus_to_kronos_ms")}
        for b in _read_json("nautilus_exs_banks.json", "NautilusExsBanks.json") or []:
            raw = bytes.fromhex(b["uuid"].replace("-", ""))
            m._nautilus_banks.add(bytes([*raw[:15], raw[15] & 0xFE]).hex().upper())
        return m


def _kronos_bank_ref(n: int) -> Optional[bytes]:
    """The 16-byte bank id a Kronos Program stores for EXs<n>: the legacy KORG/MS form for EXs1-126 (byte 15 holds
    (n + 1) << 1, which only fits a byte up to 126), else the pack's own raw id from the EXs catalog (EXs127+ and every
    third-party pack). None when the catalog does not know the bank."""
    if n <= 126:
        return KORG_MS_PREFIX + bytes([(n + 1) << 1])
    from Tools.exs_option_index import ExsOptionIndex
    raw = ExsOptionIndex.from_catalog().uuid_for_exs_number(n)
    return None if raw is None else bytes(raw)


class ReverseRomWaveformMap:
    """Nautilus sample -> Kronos sample, for the same lookups RomWaveformMap offers. A Nautilus ROM sample can come back
    as a Kronos ROM sample or, when only an EXs bank holds it (much of EXs1 is in the Nautilus ROM), as that EXs sample.
    Where several Kronos samples map to one Nautilus sample: the Kronos ROM first (an EXs bank may not be installed, so
    EXs only fills gaps), then identical audio, then one that needs no reversal, then the lowest index. `reverse` is carried back: a Kronos "...REV" sample is the Nautilus forward
    sample played reversed, so reaching it from the forward sample needs the zone's reverse setting flipped."""
    target_name = "Kronos"

    def __init__(self, forward: RomWaveformMap) -> None:
        bank_ref: Dict[int, Optional[bytes]] = {}

        def kronos_ref(n: int) -> Optional[bytes]:
            if n not in bank_ref:
                bank_ref[n] = _kronos_bank_ref(n)
            return bank_ref[n]

        ranked: Dict[Tuple[str, int], Tuple[tuple, SampleTarget]] = {}

        def offer(key: Tuple[str, int], rank: tuple, target: SampleTarget) -> None:
            if key not in ranked or rank > ranked[key][0]:
                ranked[key] = (rank, target)

        for (section, kronos_index), nautilus_index in forward._map.items():
            k = (section, kronos_index)
            rev = k in forward._reverse
            offer((section, nautilus_index), (True, k not in forward._audio_differs, not rev, -kronos_index),
                  SampleTarget(kronos_index, None, k in forward._partial, rev))
        self._by_uuid: Dict[Tuple[str, str, int], SampleTarget] = {}
        for (kind, n, kronos_index), t in forward._ext.items():
            ref = kronos_ref(n)
            if ref is None:
                continue                      # a bank with no reference form we can write: never guess one
            target = SampleTarget(kronos_index, ref, t.partial, t.reverse)
            if t.uuid is None:                # a Kronos EXs sample whose audio is in the Nautilus ROM
                offer((kind, t.index), (False, not t.partial, not t.reverse, -kronos_index), target)
                continue
            key = (kind, bytes([*t.uuid[:15], t.uuid[15] & 0xFE]).hex().upper(), t.index)
            old = self._by_uuid.get(key)
            if old is None or (old.reverse and not t.reverse):
                self._by_uuid[key] = target
        self._best = {key: target for key, (_, target) in ranked.items()}
        self._identical = {key for key, (rank, _) in ranked.items() if rank[1]}
        self._fwd = forward

    def lookup(self, section: str, bank: int, nautilus_index: int) -> Optional[SampleTarget]:
        return self._best.get((section, nautilus_index)) if bank == 0 else None

    def similar_drumsample(self, nautilus_index: int) -> Optional[Tuple[SampleTarget, float, str]]:
        hit = self._fwd._similar.get("nautilus_to_kronos", {}).get(nautilus_index)
        # a Nautilus pair half becomes a mono Kronos sample; `right`: this is the pair's right half (see remap_drum_kit)
        return None if hit is None else (SampleTarget(hit[0], stereo=False), hit[1], hit[2], bool(hit[3]))

    def similar_multisample(self, nautilus_index: int) -> Optional[Tuple[SampleTarget, float, str]]:
        hit = self._fwd._similar.get("nautilus_to_kronos_ms", {}).get(nautilus_index)
        return None if hit is None else (SampleTarget(hit[0]), hit[1], hit[2])

    def lookup_uuid(self, section: str, uuid_hex: str, nautilus_index: int) -> Optional[SampleTarget]:
        """A Nautilus EXs bank (by its upper-case bank id with the flag bit cleared) -> the Kronos EXs bank holding the same audio."""
        return self._by_uuid.get((section, uuid_hex, nautilus_index))

    def known_uuid(self, uuid_hex: str) -> bool:
        """True for any Nautilus EXs bank, with or without a Kronos twin; anything else is the user's own content."""
        return uuid_hex in self._fwd._nautilus_banks

    def __len__(self) -> int:
        return len(self._best)

    def try_map(self, section: str, nautilus_index: int) -> Optional[int]:
        """The Kronos ROM index, or None (also when the best match lives in an EXs bank)."""
        hit = self._best.get((section, nautilus_index))
        return None if hit is None or hit.uuid is not None else hit.index

    def is_audio_identical(self, section: str, nautilus_index: int) -> bool:
        return (section, nautilus_index) in self._identical


@dataclass(frozen=True)
class ProgramRefEntry:
    kronos_bank: int
    kronos_index: int
    kronos_name: str
    nautilus_bank: int
    nautilus_index: int
    nautilus_name: str
    source: str


class UnknownProgramIdentity(enum.Enum):
    """What a Program reference becomes when the factory map has an equivalent for its slot but the source set does not
    show what that slot holds (a Combi-only PCG, a Song, a Program the Local library has not pulled)."""
    ASSUME_FACTORY = "assume"   # re-point to the equivalent, and say it was assumed
    KEEP_SLOT = "keep"          # keep the slot number, as for a slot known to hold the user's own Program

    @classmethod
    def of(cls, value) -> "UnknownProgramIdentity":
        try:
            return cls(value)
        except ValueError:
            return cls.ASSUME_FACTORY


def factory_repoint(entry_name: Optional[str], actual: Optional[str],
                    policy: "UnknownProgramIdentity") -> Tuple[bool, bool]:
    """(re-point, assumed) for a reference whose factory equivalent was made from a Program named `entry_name` (None: no
    equivalent) and whose source slot holds `actual` (None: unknown)."""
    if entry_name is None:
        return False, False
    if actual is None:
        assume = policy is UnknownProgramIdentity.ASSUME_FACTORY
        return assume, assume
    return actual.strip() == entry_name.strip(), False


def assumed_warning(what: str, items: List[str], other: str) -> str:
    return (f"{what} re-pointed to {other} factory equivalents ASSUMING their source slot holds the factory Program - the "
            f"source set does not show what it holds: {'; '.join(items)}. Check them, or choose 'Keep the slot numbers' "
            "in Settings > Librarian.")


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
