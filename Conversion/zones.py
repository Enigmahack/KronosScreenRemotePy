"""Remaps ROM waveform references in Program / Drum Kit / Wave Sequence bodies from Kronos to Nautilus indices.

Anything that can't be positively resolved as cross-platform-safe is reported as unmapped (a hard refusal for
the caller), never skipped silently:
  - a ROM index missing from the map;
  - any EXs bank reference. EXs bank identity is a Korg-assigned global product id, but nothing shows the
    zone-reference encoding resolves identically on a Nautilus, so availability is unverified;
  - a raw UUID that resolves to a known EXs product via the catalog.
RAM (Sampling Mode), empty zones and raw UUIDs absent from the catalog (the user's own .KSC content) pass through.
Program EXi PCM OSC references (MOD-7/STR-1) are a different, undecoded mechanism and are not handled."""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Callable, List, Tuple

from Conversion.data import ROM_DRUMSAMPLES, ROM_MULTISAMPLES, RomWaveformMap
from Data.librarian_sysex import set_program_zone_number
from Objects.object_body import (iter_drum_kit_sample_refs, iter_wave_seq_sample_refs, set_drum_kit_sample_id,
                                 set_wave_seq_step_select)
from Tools.exs_option_index import ExsOptionIndex
from Tools.sample_reference_walker import RefClass, classify, iter_program_sample_zone_refs


class _Outcome(enum.Enum):
    PASS = 0
    REMAPPED = 1
    UNMAPPED = 2


@dataclass(frozen=True)
class UnmappedRef:
    where: str      # "Oscillator 1 zone 2", "Note 36 zone 1", "Step 3"
    reason: str


def _resolve(uuid: bytes, number: int, rom_map: RomWaveformMap, exs: ExsOptionIndex, section: str):
    c = classify(uuid)
    if c.kind in (RefClass.NOTHING, RefClass.RAM):
        return _Outcome.PASS, 0, None
    if c.kind is RefClass.ROM:
        mapped = rom_map.try_map(section, number)
        if mapped is not None:
            return _Outcome.REMAPPED, mapped, None
        return _Outcome.UNMAPPED, 0, f"ROM waveform #{number} ({section}) has no Nautilus mapping"
    if c.kind is RefClass.EXS:
        return _Outcome.UNMAPPED, 0, (f"EXs{c.exs_number} waveform #{number} - EXs bank availability is not "
                                      "verified cross-platform (no mapping table for EXs content)")
    name = exs.name_for_uuid_hex(c.raw_uuid_hex)
    if name:
        return _Outcome.UNMAPPED, 0, (f'EXs product "{name}" ({c.raw_uuid_hex[:12]}...) - EXs bank availability '
                                      "is not verified cross-platform")
    return _Outcome.PASS, 0, None


def remap_program_hd1(body: bytes, rom_map: RomWaveformMap, exs: ExsOptionIndex) -> Tuple[bytes, List[UnmappedRef]]:
    out, unmapped = bytearray(body), []
    for osc, zone, uuid, number in iter_program_sample_zone_refs(body):
        outcome, new, reason = _resolve(uuid, number, rom_map, exs, ROM_MULTISAMPLES)
        if outcome is _Outcome.REMAPPED:
            set_program_zone_number(out, osc, zone, new)
        elif outcome is _Outcome.UNMAPPED:
            unmapped.append(UnmappedRef(f"Oscillator {osc + 1} zone {zone + 1}", reason))
    return bytes(out), unmapped


def program_zones_not_audio_identical(body: bytes, rom_map: RomWaveformMap) -> List[Tuple[int, int, int]]:
    """ROM zones that are remapped, but to a re-sampled equivalent whose audio differs: (osc, zone, number)."""
    found = []
    for osc, zone, uuid, number in iter_program_sample_zone_refs(body):
        if (classify(uuid).kind is RefClass.ROM and rom_map.try_map(ROM_MULTISAMPLES, number) is not None
                and not rom_map.is_audio_identical(ROM_MULTISAMPLES, number)):
            found.append((osc, zone, number))
    return found


def remap_drum_kit(body: bytes, rom_map: RomWaveformMap, exs: ExsOptionIndex) -> Tuple[bytes, List[UnmappedRef]]:
    out, unmapped = bytearray(body), []
    for note, zone, uuid, sample_id in iter_drum_kit_sample_refs(body):
        outcome, new, reason = _resolve(uuid, sample_id, rom_map, exs, ROM_DRUMSAMPLES)
        if outcome is _Outcome.REMAPPED:
            set_drum_kit_sample_id(out, note, zone, new)
        elif outcome is _Outcome.UNMAPPED:
            unmapped.append(UnmappedRef(f"Note {note} zone {zone + 1}", reason))
    return bytes(out), unmapped


def remap_wave_seq(body: bytes, rom_map: RomWaveformMap, exs: ExsOptionIndex) -> Tuple[bytes, List[UnmappedRef]]:
    out, unmapped = bytearray(body), []
    for step, uuid, select in iter_wave_seq_sample_refs(body):
        outcome, new, reason = _resolve(uuid, select, rom_map, exs, ROM_MULTISAMPLES)
        if outcome is _Outcome.REMAPPED:
            set_wave_seq_step_select(out, step, new)
        elif outcome is _Outcome.UNMAPPED:
            unmapped.append(UnmappedRef(f"Step {step + 1}", reason))
    return bytes(out), unmapped
