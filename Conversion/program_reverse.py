"""Nautilus -> Kronos Program body conversion: the inverse of Conversion.program. NOT verified on a Kronos.

The Nautilus RT Controller / Scene / Step Seq block [1280, 2552) has no Kronos counterpart and is replaced by the
Kronos S/PDIF + Drum Track + KARMA block [1280, 2558) of `template` (normally an Init Program), so KARMA and the Drum
Track stay off. Everything else is shared, and HD-1 ROM sample references are mapped back through the inverted ROM table."""
from __future__ import annotations

from typing import List, Optional

from Conversion.data import RomWaveformMap
from Conversion.profiles import KRONOS, NAUTILUS, PROGRAM_SHARED_HEAD_END, PROGRAM_SIZE_DELTA, ProgramKind
from Conversion.result import ConversionResult
from Conversion.zones import program_zones_not_audio_identical, remap_program_hd1, substitution_warning
from Tools.exs_option_index import ExsOptionIndex


def pcg_slot_to_wire(slot_body: bytes, kind: ProgramKind) -> bytes:
    """A Nautilus PCG Program slot is always 4954 bytes; an HD-1 wire dump is its first 3700."""
    if len(slot_body) != NAUTILUS.slot_size:
        raise ValueError(f"expected a {NAUTILUS.slot_size}-byte Nautilus .pcg Program record, got {len(slot_body)}")
    return bytes(slot_body[:NAUTILUS.wire_size(kind)])


def _to_kronos_layout(nautilus_body: bytes, template: Optional[bytes]) -> bytearray:
    out = bytearray(len(nautilus_body) + PROGRAM_SIZE_DELTA)
    out[:PROGRAM_SHARED_HEAD_END] = nautilus_body[:PROGRAM_SHARED_HEAD_END]
    if template is not None:
        out[PROGRAM_SHARED_HEAD_END:KRONOS.tail_start] = template[PROGRAM_SHARED_HEAD_END:KRONOS.tail_start]
    out[KRONOS.tail_start:] = nautilus_body[NAUTILUS.tail_start:]
    return out


def convert_program_to_kronos(body: bytes, kind: ProgramKind, rom_map: RomWaveformMap, exs: ExsOptionIndex,
                              template: Optional[bytes] = None,
                              nautilus_init: Optional[bytes] = None) -> ConversionResult:
    """`body` is a Nautilus wire body for `kind`, or a 4954-byte Nautilus PCG slot. `nautilus_init` (an Init Program
    of the Nautilus) lets the result say whether any Nautilus-only setting was actually in use."""
    warnings, refusals, substitutions = [], [], []
    wire_size = NAUTILUS.wire_size(kind)
    if len(body) == NAUTILUS.slot_size and wire_size != NAUTILUS.slot_size:
        body = pcg_slot_to_wire(body, kind)
    if len(body) != wire_size:
        return ConversionResult(None, warnings, [f"Nautilus {kind.value.upper()} Program body is {len(body)} bytes, "
                                                 f"expected {wire_size}."])
    kronos_size = wire_size + PROGRAM_SIZE_DELTA
    if template is None:
        warnings.append("No Kronos template Program supplied - the Kronos-only S/PDIF, Drum Track and KARMA settings "
                        "were zero-filled.")
    elif len(template) != kronos_size:
        return ConversionResult(None, warnings, [f"Kronos template Program is {len(template)} bytes, expected "
                                                 f"{kronos_size} (an EXi template for an EXi Program, HD-1 for HD-1)."])

    out = _to_kronos_layout(bytes(body), template)
    if kind is ProgramKind.HD1:
        reverse = rom_map.reversed()
        approx = program_zones_not_audio_identical(bytes(out), reverse)
        if approx:
            warnings.append("ROM waveforms without an identical Kronos sample, mapped to the nearest equivalent "
                            "(audio differs): " + ", ".join(f"osc {o + 1} zone {z + 1} (#{n})" for o, z, n in approx) + ".")
        notes: List[str] = []
        remapped, unmapped = remap_program_hd1(bytes(out), reverse, exs, substitute=True, notes=notes)
        warnings += notes
        substitutions = [f"{u.where}: {u.reason}" for u in unmapped]
        if substitutions:
            warnings.append(substitution_warning("Kronos", substitutions))
        out = bytearray(remapped)
    if refusals:
        return ConversionResult(None, warnings, refusals)
    if nautilus_init is None or body[PROGRAM_SHARED_HEAD_END:NAUTILUS.tail_start] != \
            nautilus_init[PROGRAM_SHARED_HEAD_END:NAUTILUS.tail_start]:
        warnings.append("Nautilus-only settings (RT Controller, Scenes, Step Sequencer) were not carried over.")
    return ConversionResult(bytes(out), warnings, refusals, substitutions=substitutions)
