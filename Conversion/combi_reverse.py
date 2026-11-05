"""Nautilus -> Kronos Combination / Song Timbre Set body conversion: the inverse of Conversion.combi.

  - [0, 1268) is shared.
  - Kronos [1268, 4790) (audio inputs, drum track, KARMA Common + Modules A-D) has no Nautilus counterpart: it comes from
    `template` (a Kronos body, normally an Init Combination), so KARMA stays off.
  - Nautilus [2620, end) goes back to Kronos [4790, end); the Assignable Switch/Knob 5-8 block, which carries a different
    enumeration on each instrument, comes from the template as well.
  - Nautilus-only settings (Scenes, ARP A/B, Step Sequencer, USB audio) are dropped and reported.
Timbre Program references are re-pointed from a Nautilus factory Program to the Kronos Program it replaced (the inverse
of ProgramRefMap); other references keep their slot numbers. Not verified on a Kronos: no Kronos was available."""
from __future__ import annotations

from typing import Callable, Dict, List, Optional

from Conversion.combi import _ASSIGN_END, _ASSIGN_START, _TIMBRE_BASE, _TIMBRE_COUNT, _TIMBRE_STRIDE
from Conversion.data import ProgramRefMap, kronos_program_label, nautilus_program_label, nautilus_user_to_kronos_bank
from Conversion.profiles import (COMBI_KRONOS_SIZE, COMBI_KRONOS_TAIL, COMBI_NAUTILUS_SIZE, COMBI_NAUTILUS_TAIL,
                                 COMBI_SHARED_HEAD_END)
from Conversion.result import ConversionResult

_KRONOS_TIMBRE_BASE = _TIMBRE_BASE + (COMBI_KRONOS_TAIL - COMBI_NAUTILUS_TAIL)


def convert_combi_to_kronos(body: bytes, template: Optional[bytes] = None,
                            ref_map: Optional[ProgramRefMap] = None,
                            nautilus_program_name: Optional[Callable[[int, int], Optional[str]]] = None) -> ConversionResult:
    """`nautilus_program_name(bank byte, slot)` names the Program in the source set; a Timbre is only re-pointed to a
    Kronos factory Program when that name still matches the factory Program the table was built from."""
    warnings: List[str] = []
    refusals: List[str] = []
    if len(body) != COMBI_NAUTILUS_SIZE:
        refusals.append(f"Nautilus Combi body is {len(body)} bytes, expected {COMBI_NAUTILUS_SIZE}.")
    if template is not None and len(template) != COMBI_KRONOS_SIZE:
        refusals.append(f"Kronos template Combi is {len(template)} bytes, expected {COMBI_KRONOS_SIZE}.")
    if refusals:
        return ConversionResult(None, warnings, refusals)
    if template is None:
        warnings.append("No Kronos template Combi supplied - the Kronos-only KARMA/Drum Track/audio-input settings "
                        "were zero-filled.")
    out = bytearray(COMBI_KRONOS_SIZE)
    out[:COMBI_SHARED_HEAD_END] = body[:COMBI_SHARED_HEAD_END]
    if template is not None:
        out[COMBI_SHARED_HEAD_END:COMBI_KRONOS_TAIL] = template[COMBI_SHARED_HEAD_END:COMBI_KRONOS_TAIL]
    out[COMBI_KRONOS_TAIL:] = body[COMBI_NAUTILUS_TAIL:]
    shift = COMBI_KRONOS_TAIL - COMBI_NAUTILUS_TAIL
    for i in range(_ASSIGN_START + shift, _ASSIGN_END + shift + 1):
        out[i] = template[i] if template is not None else 0
    warnings.append("Nautilus-only settings (Scenes, ARP A/B, Step Sequencer, USB audio) were not carried over; the "
                    "Kronos KARMA modules, Drum Track and audio inputs come from the template, and the Assignable "
                    "Switch/Knob 5-8 assignments were reset.")
    _repoint_timbre_programs(out, ref_map, warnings, nautilus_program_name)
    return ConversionResult(bytes(out), warnings, refusals)


def _repoint_timbre_programs(out: bytearray, ref_map: Optional[ProgramRefMap], warnings: List[str],
                             program_name: Optional[Callable[[int, int], Optional[str]]] = None) -> None:
    remapped: List[str] = []
    kept: Dict[str, List[int]] = {}
    for t in range(_TIMBRE_COUNT):
        o = _KRONOS_TIMBRE_BASE + t * _TIMBRE_STRIDE
        if out[o + 2] >> 5 == 0:
            continue   # Status Off
        index, bank = out[o], out[o + 1]
        e = ref_map.get_by_nautilus(bank, index) if ref_map is not None else None
        actual = program_name(bank, index) if program_name is not None and e is not None else None
        if e is None or (actual is not None and actual.strip() != e.nautilus_name.strip()):
            kept.setdefault(nautilus_program_label(bank, index), []).append(t + 1)
            shifted = nautilus_user_to_kronos_bank(bank)
            if shifted is not None:
                out[o + 1] = shifted
            continue
        out[o], out[o + 1] = e.kronos_index & 0xFF, e.kronos_bank & 0xFF
        remapped.append(f'{t + 1}: {nautilus_program_label(bank, index)} "{e.nautilus_name}" -> '
                        f'{kronos_program_label(e.kronos_bank, e.kronos_index)} "{e.kronos_name}"')
    if remapped:
        warnings.append(f"Timbre Programs re-pointed to Kronos factory equivalents: {'; '.join(remapped)}.")
    if kept:
        text = "; ".join(f"{'Timbre' if len(ts) == 1 else 'Timbres'} {','.join(map(str, ts))} ({label})"
                         for label, ts in kept.items())
        warnings.append(f"{text} kept their slot numbers (no Kronos factory equivalent; a user bank keeps its position, "
                        "G -> U-A) - those Programs must exist at the same Kronos slots.")
