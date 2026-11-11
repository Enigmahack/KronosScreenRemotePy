"""Remaps ROM waveform references in Program / Drum Kit / Wave Sequence bodies from Kronos to Nautilus indices.

Anything that can't be positively resolved is reported as unmapped, never skipped silently:
  - a ROM index missing from the map;
  - an EXs bank reference with no audio-identical (or partly identical) multisample on the other instrument;
  - a raw UUID that resolves to a known EXs product via the catalog.
A resolved reference may move between banks (a Kronos EXs1 sample can live in the Nautilus ROM), so the 16-byte bank id is
rewritten along with the number. RAM (Sampling Mode), empty zones and raw UUIDs absent from the catalog (the user's own
.KSC content) pass through.
Program EXi PCM OSC references (MOD-7/STR-1) are a different, undecoded mechanism and are not handled."""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import List, Optional, Tuple

from Conversion.data import default_piano, ROM_DRUMSAMPLES, ROM_MULTISAMPLES, RomWaveformMap
from Data.librarian_sysex import (program_oscillator_mode, program_zone_reversed, set_program_zone_number,
                                  set_program_zone_reverse, set_program_zone_uuid)
from Objects.object_body import (iter_drum_kit_sample_refs, iter_wave_seq_sample_refs, set_drum_kit_sample_id,
                                 set_drum_kit_zone_off, set_drum_kit_zone_uuid, set_wave_seq_step_select,
                                 set_wave_seq_step_uuid)
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
    """(outcome, SampleTarget or None, reason)."""
    c = classify(uuid)
    target_name = getattr(rom_map, "target_name", "Nautilus")
    if c.kind in (RefClass.NOTHING, RefClass.RAM):
        return _Outcome.PASS, None, None
    if c.kind is RefClass.ROM:
        t = rom_map.lookup(section, 0, number)
        if t is not None:
            return _Outcome.REMAPPED, t, None
        return _Outcome.UNMAPPED, None, f"ROM waveform #{number} ({section}) has no {target_name} mapping"
    if c.kind is RefClass.EXS:
        t = rom_map.lookup(section, c.exs_number, number)
        if t is not None:
            return _Outcome.REMAPPED, t, None
        return _Outcome.UNMAPPED, None, (f"EXs{c.exs_number} waveform #{number} has no {target_name} equivalent "
                                         "(its audio is not on the instrument)")
    lookup_uuid = getattr(rom_map, "lookup_uuid", None)
    if lookup_uuid is not None:      # Nautilus -> Kronos: a Nautilus EXs bank is known by its bank id
        t = lookup_uuid(section, c.raw_uuid_hex, number)
        if t is not None:
            return _Outcome.REMAPPED, t, None
        if rom_map.known_uuid(c.raw_uuid_hex):
            return _Outcome.UNMAPPED, None, (f"Nautilus EXs bank {c.raw_uuid_hex[:12]}... waveform #{number} has no "
                                             f"{target_name} equivalent")
        return _Outcome.PASS, None, None
    # EXs127+ and every third-party pack are named by their raw bank id, not by the legacy KORG/MS number.
    # HW-VERIFY: Nautilus Programs are confirmed to store the Bank.info id big-endian (Bank301-313 in PRELOAD.PCG); that a
    # Kronos Program stores Bank206's id the same way is the documented convention but no Kronos Program in the corpus uses
    # a third-party pack. Pull a Program that uses EXs206 from the Kronos and compare its zone id with 8e7ab882-4abf-....
    number_of_bank = exs.number_for_uuid_hex(c.raw_uuid_hex)
    if number_of_bank is not None:
        t = rom_map.lookup(section, number_of_bank, number)
        if t is not None:
            return _Outcome.REMAPPED, t, None
    name = exs.name_for_uuid_hex(c.raw_uuid_hex)
    if name:
        return _Outcome.UNMAPPED, None, (f'EXs product "{name}" ({c.raw_uuid_hex[:12]}...) has no {target_name} '
                                         "equivalent (its audio is not on the instrument)")
    return _Outcome.PASS, None, None


def substitution_warning(target_name: str, substitutions: List[str]) -> str:
    shown = "; ".join(substitutions[:6]) + (f"; and {len(substitutions) - 6} more" if len(substitutions) > 6 else "")
    return (f"{len(substitutions)} sample zone(s) have no equivalent on the {target_name} and now play the default piano "
            f"(Acoustic Piano 1) so the rest of the Program is kept. Replace these sounds by hand: {shown}.")


def remap_program_hd1(body: bytes, rom_map: RomWaveformMap, exs: ExsOptionIndex, substitute: bool = False,
                      notes: Optional[List[str]] = None) -> Tuple[bytes, List[UnmappedRef]]:
    """With `substitute`, a zone whose sample has no equivalent plays the default piano instead (the rest of the Program
    is kept); it is still returned in the list so the caller can tell the user which sounds to replace. `notes` collects
    warnings: partial multisamples and oscillators that mix reversed and normal samples."""
    out, unmapped = bytearray(body), []
    osc2_unused = program_oscillator_mode(body) == 0   # Single: Oscillator 2 is silent, whatever its zones hold
    reversed_zones, normal_zones, partial = [[], []], [[], []], []
    for osc, zone, uuid, number in iter_program_sample_zone_refs(body):
        outcome, target, reason = _resolve(uuid, number, rom_map, exs, ROM_MULTISAMPLES)
        if outcome is _Outcome.REMAPPED:
            set_program_zone_uuid(out, osc, zone, target.ref_bytes(uuid))
            set_program_zone_number(out, osc, zone, target.index)
            if target.reverse:
                set_program_zone_reverse(out, osc, zone, True)
            if target.partial and not (osc == 1 and osc2_unused):
                partial.append(f"osc {osc + 1} zone {zone + 1}")
        elif outcome is _Outcome.UNMAPPED and not (osc == 1 and osc2_unused):
            unmapped.append(UnmappedRef(f"Oscillator {osc + 1} zone {zone + 1}", reason))
            if substitute:
                piano = default_piano(rom_map)
                set_program_zone_uuid(out, osc, zone, piano.ref_bytes(uuid))
                set_program_zone_number(out, osc, zone, piano.index)
                set_program_zone_reverse(out, osc, zone, False)
                continue
        if outcome is not _Outcome.UNMAPPED and not (osc == 1 and osc2_unused):
            (reversed_zones if program_zone_reversed(bytes(out), osc, zone) else normal_zones)[osc].append(zone + 1)
    if notes is not None:
        if program_oscillator_mode(body) in (4, 5):
            # Drums / Wave Sequence oscillator modes hold object numbers in their zones, not sample references, and
            # the two instruments' kit and sequence sets differ; they are carried over as they are.
            notes.append("This Program's oscillator uses a Drum Kit or Wave Sequence by number; the number was kept, "
                         "so check that the same-numbered object on the instrument is the one you want.")
        if partial:
            notes.append("Only part of these multisamples exists on the " + getattr(rom_map, "target_name", "Nautilus")
                         + ", so the sample zones may differ slightly: " + ", ".join(partial) + ".")
        for osc in range(2):
            if reversed_zones[osc] and normal_zones[osc]:
                notes.append(f"Oscillator {osc + 1} mixes reversed samples (zones {', '.join(map(str, reversed_zones[osc]))}) "
                             f"with normal ones (zones {', '.join(map(str, normal_zones[osc]))}). The reverse setting is "
                             "stored per zone, but check that the layers still sound right on the instrument.")
    return bytes(out), unmapped


def program_zones_not_audio_identical(body: bytes, rom_map: RomWaveformMap) -> List[Tuple[int, int, int]]:
    """ROM zones that are remapped, but to a re-sampled equivalent whose audio differs: (osc, zone, number)."""
    found = []
    for osc, zone, uuid, number in iter_program_sample_zone_refs(body):
        if (classify(uuid).kind is RefClass.ROM and rom_map.try_map(ROM_MULTISAMPLES, number) is not None
                and not rom_map.is_audio_identical(ROM_MULTISAMPLES, number)):
            found.append((osc, zone, number))
    return found


def remap_drum_kit(body: bytes, rom_map: RomWaveformMap, exs: ExsOptionIndex,
                   switch_off_unmapped: bool = False) -> Tuple[bytes, List[UnmappedRef]]:
    """With `switch_off_unmapped` a zone whose sample has no Nautilus equivalent is turned off (and still listed)."""
    out, unmapped = bytearray(body), []
    for note, zone, uuid, sample_id in iter_drum_kit_sample_refs(body):
        # ROM drum sample #0 is "no sample": the Nautilus's own factory kits switch zones on at #0 for unused
        # notes, and it has no entry in the ROM map because there is nothing to map.
        if sample_id == 0 and classify(uuid).kind is RefClass.ROM:
            continue
        outcome, new, reason = _resolve(uuid, sample_id, rom_map, exs, ROM_DRUMSAMPLES)
        if outcome is _Outcome.REMAPPED:
            set_drum_kit_zone_uuid(out, note, zone, new.ref_bytes(uuid))
            set_drum_kit_sample_id(out, note, zone, new.index)
        elif outcome is _Outcome.UNMAPPED:
            unmapped.append(UnmappedRef(f"Note {note} zone {zone + 1}", reason))
            if switch_off_unmapped:
                set_drum_kit_zone_off(out, note, zone)
    return bytes(out), unmapped


def remap_wave_seq(body: bytes, rom_map: RomWaveformMap, exs: ExsOptionIndex, substitute: bool = False,
                   notes: Optional[List[str]] = None) -> Tuple[bytes, List[UnmappedRef]]:
    """With `substitute`, a step whose sample has no equivalent plays the default piano (still returned in the list)."""
    out, unmapped, partial = bytearray(body), [], []
    for step, uuid, select in iter_wave_seq_sample_refs(body):
        # The Nautilus's own Wave Sequences leave their unused steps on ROM sample #0, which has no Kronos twin.
        if select == 0 and classify(uuid).kind is RefClass.ROM and getattr(rom_map, "target_name", "") == "Kronos":
            continue
        outcome, new, reason = _resolve(uuid, select, rom_map, exs, ROM_MULTISAMPLES)
        if outcome is _Outcome.REMAPPED:
            set_wave_seq_step_uuid(out, step, new.ref_bytes(uuid))
            set_wave_seq_step_select(out, step, new.index)
            if new.partial:
                partial.append(str(step + 1))
        elif outcome is _Outcome.UNMAPPED:
            unmapped.append(UnmappedRef(f"Step {step + 1}", reason))
            if substitute:
                piano = default_piano(rom_map)
                set_wave_seq_step_uuid(out, step, piano.ref_bytes(uuid))
                set_wave_seq_step_select(out, step, piano.index)
    if notes is not None and partial:
        notes.append("Only part of the multisample used by step(s) " + ", ".join(partial) + " exists on the "
                     + getattr(rom_map, "target_name", "Nautilus") + ", so those steps may sound slightly different.")
    return bytes(out), unmapped
