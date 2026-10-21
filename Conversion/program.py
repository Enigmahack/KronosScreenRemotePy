"""Kronos -> Nautilus Program body conversion.

A Nautilus Program is 6 bytes shorter (EXi 4960 -> 4954, HD-1 3706 -> 3700): the layouts agree on [0, 1280) and
again from "Common | Oscillator Mode" (Kronos 2558, Nautilus 2552) to the end. Between them the Kronos
S/PDIF + Drum Track + KARMA block (1278 B) is replaced by an unrelated RT Controller / Scene / Step Seq block
(1272 B) that has no Kronos source, so it comes from `template` (normally the target slot's current contents)
or is zero-filled with a warning."""
from __future__ import annotations

from typing import Optional

from Conversion import karma
from Conversion.data import KarmaGeCatalog, KarmaGePatternLibrary, RomWaveformMap
from Conversion.profiles import (KRONOS, NAUTILUS, PROGRAM_SHARED_HEAD_END, PROGRAM_SIZE_DELTA, ProgramKind)
from Conversion.result import ConversionResult
from Conversion.zones import program_zones_not_audio_identical, remap_program_hd1
from Tools.exs_option_index import ExsOptionIndex


def pcg_slot_to_wire(slot_body: bytes, kind: ProgramKind) -> bytes:
    """A Kronos PCG Program slot is always 4960 bytes; an HD-1 wire dump is its first 3706."""
    if len(slot_body) != KRONOS.slot_size:
        raise ValueError(f"expected a {KRONOS.slot_size}-byte .pcg Program record, got {len(slot_body)}")
    return bytes(slot_body[:KRONOS.wire_size(kind)])


def _to_nautilus_layout(kronos_body: bytes, template: Optional[bytes]) -> bytearray:
    out = bytearray(len(kronos_body) - PROGRAM_SIZE_DELTA)
    out[:PROGRAM_SHARED_HEAD_END] = kronos_body[:PROGRAM_SHARED_HEAD_END]
    if template is not None:
        out[PROGRAM_SHARED_HEAD_END:NAUTILUS.tail_start] = template[PROGRAM_SHARED_HEAD_END:NAUTILUS.tail_start]
    out[NAUTILUS.tail_start:] = kronos_body[KRONOS.tail_start:]
    return out


def convert_program(body: bytes, kind: ProgramKind, rom_map: RomWaveformMap, exs: ExsOptionIndex,
                    ge_catalog: KarmaGeCatalog, template: Optional[bytes] = None,
                    options: Optional[karma.ArpOptions] = None,
                    ge_patterns: Optional[KarmaGePatternLibrary] = None) -> ConversionResult:
    """`body` is a Kronos wire body for `kind`, or a 4960-byte PCG slot. The result is a Nautilus wire body."""
    options = options or karma.ArpOptions()
    warnings, refusals = [], []
    wire_size = KRONOS.wire_size(kind)
    if len(body) == KRONOS.slot_size and wire_size != KRONOS.slot_size:
        body = pcg_slot_to_wire(body, kind)
    if len(body) != wire_size:
        return ConversionResult(None, warnings, [f"Kronos {kind.value.upper()} Program body is {len(body)} bytes, "
                                                 f"expected {wire_size}."])
    original = kronos_body = bytes(body)

    # The GE must be read before the KARMA bytes are stripped.
    ge = karma.read_ge_number(kronos_body) or 0
    arp = None
    if ge:
        arp = karma.resolve_ge(ge, ge_catalog, ge_patterns, options.transcribed_pattern_slot,
                               options.min_transcription_score, karma.scene_list(options.program_scenes))
        if options.program_scenes == 0 and arp.pattern_number is not None:
            arp = karma.ArpResolution(None, None, None, f"KARMA GE #{ge} was not converted: no Scene was chosen to receive it.")
        if arp.warning:
            warnings.append(arp.warning)

    if kind is ProgramKind.HD1:
        approx = program_zones_not_audio_identical(kronos_body, rom_map)
        if approx:
            warnings.append("ROM waveforms without an identical Nautilus sample, mapped to the nearest equivalent "
                            "(audio differs): " + ", ".join(f"osc {o + 1} zone {z + 1} (#{n})" for o, z, n in approx) + ".")
        kronos_body, unmapped = remap_program_hd1(kronos_body, rom_map, exs)
        refusals += [f"{u.where}: {u.reason}" for u in unmapped]

    stripped_body, was_stripped = karma.strip_karma_fields(kronos_body)
    if was_stripped:
        warnings.append("KARMA performance data (Realtime Controls, Dynamic MIDI, per-Scene switch states, "
                        "GE/Scene assignments) was present and has been removed - not supported on Nautilus.")

    nautilus_size = wire_size - PROGRAM_SIZE_DELTA
    if template is None:
        warnings.append("No Nautilus template Program supplied - the Nautilus-only RT Controller/Scene/Step Seq "
                        "settings were zero-filled.")
    elif len(template) != nautilus_size:
        refusals.append(f"Nautilus template Program is {len(template)} bytes, expected {nautilus_size} "
                        "(an EXi template for an EXi Program, HD-1 for HD-1).")
    if refusals:
        return ConversionResult(None, warnings, refusals)

    out = _to_nautilus_layout(stripped_body, template)
    assigned = arp is not None and arp.pattern_number is not None
    if assigned:
        karma.apply_to_scenes(out, options.program_scenes, karma.SCENE_STRIDE, 0, arp)
    # Tempo/latch/switch come from the original, not the stripped, Kronos body.
    karma.carry_karma_common(original, out, assigned)
    patterns = [arp.to_store] if arp is not None and arp.to_store else []
    return ConversionResult(bytes(out), warnings, refusals, patterns)
