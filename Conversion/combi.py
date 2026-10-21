"""Kronos -> Nautilus Combination body conversion.

Layout, from a field-by-field diff of both platforms' CombiAndSongTimbreSet docs:
  - [0, 1268) is shared (Vector Control -> X-Y Control keeps its offsets).
  - Kronos [1268, 4790) (audio inputs, drum track, KARMA Common + Modules A-D) is replaced on the Nautilus by
    [1268, 2620) (audio USB, Scene Common, Step Seq, 4 Scenes with ARP A/B + drum), taken from `template`.
  - From Common/Category (Kronos 4790, Nautilus 2620) to the end everything shifts by -2170, including the
    16 x 188-byte Timbres; only a few Kronos-only bits in it are cleared.
Song Timbre Sets use the same layout and go through the same function.
Timbre Program references name Nautilus slots after conversion; by default they are re-pointed at the Nautilus
factory equivalent of the Kronos factory Program. Unmapped references keep their slot numbers, so those Programs
have to be converted and placed at the same Nautilus slots."""
from __future__ import annotations

import enum
from typing import Callable, Dict, List, Optional, Tuple

from Conversion import karma
from Conversion.data import (KarmaGeCatalog, KarmaGePatternLibrary, ProgramRefMap, kronos_program_label,
                             nautilus_program_label)
from Conversion.profiles import (COMBI_KRONOS_SIZE, COMBI_KRONOS_TAIL, COMBI_NAUTILUS_SIZE, COMBI_NAUTILUS_TAIL,
                                 COMBI_SHARED_HEAD_END)
from Conversion.result import ConversionResult

SIZE_DELTA = COMBI_KRONOS_TAIL - COMBI_NAUTILUS_TAIL   # 2170


class TimbreProgramRefs(enum.Enum):
    FACTORY_EQUIVALENTS = "factory"
    KEEP_SLOTS = "keep"


# Kronos KARMA Module m (A-D): 1814 + 744*m. +0 GE (u16 LE), +2 bits 4-0 input channel, +3 output channel,
# +5 key zone top, +6 key zone bottom.
_KARMA_MODULE_BASE, _KARMA_MODULE_STRIDE = 1814, 744
_NAUTILUS_ARP_A_ZONES, _NAUTILUS_ARP_B_ZONES = 2612, 2616   # key top, key bottom, velocity top, velocity bottom
# Assignable Switch 1/2 + Knob 5-8 carry a different enumeration on the Nautilus than on the Kronos, so they
# come from the template like the rest of the Nautilus-owned block.
_ASSIGN_START, _ASSIGN_END = 2626, 2631
_TIMBRE_BASE, _TIMBRE_STRIDE, _TIMBRE_COUNT = 2632, 188, 16
# (offset in Timbre, mask, label): Kronos-only bits. The (Filter) Knob byte (+33) stays whole: Korg's own
# factory Combis keep its bits 4-5 set exactly as on the Kronos.
_TIMBRE_KRONOS_ONLY_BITS = ((30, 0b0111_1000, "Chord Source"), (31, 0b0110_0000, "Chord Mode"),
                            (35, 0b1000_0000, "(Filter) Waveform SysEx"))


def convert_combi(body: bytes, ge_catalog: KarmaGeCatalog, template: Optional[bytes] = None,
                  options: Optional[karma.ArpOptions] = None, ge_patterns: Optional[KarmaGePatternLibrary] = None,
                  program_refs: TimbreProgramRefs = TimbreProgramRefs.FACTORY_EQUIVALENTS,
                  ref_map: Optional[ProgramRefMap] = None,
                  kronos_program_name: Optional[Callable[[int, int], Optional[str]]] = None) -> ConversionResult:
    """`kronos_program_name(bank, index)` names the Program stored at a Kronos slot in the source set; a reference
    is only re-pointed when that name still matches the factory Program the map was built from. Without it the
    source slots are assumed to hold factory content."""
    options = options or karma.ArpOptions()
    warnings: List[str] = []
    refusals: List[str] = []
    to_store: List[Tuple[int, bytes]] = []

    if len(body) != COMBI_KRONOS_SIZE:
        refusals.append(f"Kronos Combi body is {len(body)} bytes, expected {COMBI_KRONOS_SIZE}.")
    if template is not None and len(template) != COMBI_NAUTILUS_SIZE:
        refusals.append(f"Nautilus template Combi is {len(template)} bytes, expected {COMBI_NAUTILUS_SIZE}.")
    if refusals:
        return ConversionResult(None, warnings, refusals, to_store)
    if template is None:
        warnings.append("No Nautilus template Combi supplied - the Nautilus-only Scene/ARP/Step Seq/audio "
                        "settings were zero-filled.")

    out = bytearray(COMBI_NAUTILUS_SIZE)
    out[:COMBI_SHARED_HEAD_END] = body[:COMBI_SHARED_HEAD_END]
    if template is not None:
        out[COMBI_SHARED_HEAD_END:COMBI_NAUTILUS_TAIL] = template[COMBI_SHARED_HEAD_END:COMBI_NAUTILUS_TAIL]
    out[COMBI_NAUTILUS_TAIL:] = body[COMBI_KRONOS_TAIL:]

    used, assigned = set(), False
    arps = ((options.combi_arp_a_source, 0, _NAUTILUS_ARP_A_ZONES, options.transcribed_pattern_slot, "ARP A"),
            (options.combi_arp_b_source, karma.COMBI_ARP_B_OFFSET, _NAUTILUS_ARP_B_ZONES,
             options.transcribed_pattern_slot_b, "ARP B"))
    for source, arp_offset, zones, slot, label in arps:
        if source is None or not 0 <= source <= 3:
            continue
        used.add(source)
        k = _KARMA_MODULE_BASE + source * _KARMA_MODULE_STRIDE
        ge = body[k] | (body[k + 1] << 8)
        if ge == 0:
            continue   # treated as unused, as for Programs
        where = f"{label} ({karma.scene_list(options.combi_scenes)})"
        r = karma.resolve_ge(ge, ge_catalog, ge_patterns, slot, options.min_transcription_score, where)
        letter = chr(65 + source)
        if options.combi_scenes == 0 and r.pattern_number is not None:
            r = karma.ArpResolution(None, None, None, f"KARMA Module {letter} GE #{ge} was not converted: no Scene was chosen.")
        if r.warning:
            warnings.append(f"Module {letter}: {r.warning}")
        if r.pattern_number is None:
            continue
        assigned = True
        karma.apply_to_scenes(out, options.combi_scenes, karma.COMBI_SCENE_STRIDE, arp_offset, r)
        for n in range(karma.SCENE_COUNT):
            if options.combi_scenes & (1 << n):
                o = karma.SCENE_BASE + n * karma.COMBI_SCENE_STRIDE + arp_offset
                out[o + 12] = body[k + 3]            # output channel: the timbres KARMA was driving
                out[o + 13] = body[k + 2] & 0x1F     # input channel
        out[zones], out[zones + 1] = body[k + 5], body[k + 6]
        if r.to_store:
            to_store.append(r.to_store)
    if len(to_store) == 2 and to_store[0][0] == to_store[1][0]:
        refusals.append(f"ARP A and ARP B were given the same bank-B slot (B{to_store[0][0]:03d}).")

    for m in (m for m in range(4) if m not in used):
        k = _KARMA_MODULE_BASE + m * _KARMA_MODULE_STRIDE
        ge = body[k] | (body[k + 1] << 8)
        if ge:
            warnings.append(f'KARMA Module {chr(65 + m)} (GE #{ge} "{ge_catalog.name(ge) or "?"}") was dropped - '
                            "the Nautilus has two arpeggiators.")
    karma.carry_karma_common(bytes(body), out, assigned)

    cleared: List[str] = []
    for i in range(_ASSIGN_START, _ASSIGN_END + 1):
        out[i] = template[i] if template is not None else 0
    cleared.append("Assignable Switch 1/2 and Knob 5-8 assignments (Nautilus values kept)")
    for off, mask, what in _TIMBRE_KRONOS_ONLY_BITS:
        any_set = False
        for t in range(_TIMBRE_COUNT):
            a = _TIMBRE_BASE + t * _TIMBRE_STRIDE + off
            any_set |= bool(out[a] & mask)
            out[a] &= ~mask & 0xFF
        if any_set:
            cleared.append(f"Timbre {what}")
    warnings.append(f"Kronos-only settings removed: {', '.join(cleared)}.")
    warnings.append("KARMA performance data (Realtime Controls, Dynamic MIDI, Scenes), Drum Track and audio "
                    "input settings were not carried over.")
    _remap_timbre_programs(out, program_refs, ref_map, kronos_program_name, warnings)

    return ConversionResult(None if refusals else bytes(out), warnings, refusals, to_store)


def _remap_timbre_programs(out: bytearray, mode: TimbreProgramRefs, ref_map: Optional[ProgramRefMap],
                           kronos_program_name, warnings: List[str]) -> None:
    if mode is TimbreProgramRefs.KEEP_SLOTS or ref_map is None or len(ref_map) == 0:
        warnings.append("Timbre Program references were kept as-is; the referenced Programs must be converted "
                        "and placed at the same Nautilus slots.")
        return
    remapped: List[str] = []
    kept: List[Tuple[int, str]] = []
    for t in range(_TIMBRE_COUNT):
        o = _TIMBRE_BASE + t * _TIMBRE_STRIDE
        if out[o + 2] >> 5 == 0:
            continue   # Status Off: not played, leave it alone
        index, bank = out[o], out[o + 1]
        e = ref_map.get(bank, index)
        actual = kronos_program_name(bank, index) if kronos_program_name else None
        if e is None or (actual is not None and actual.strip() != e.kronos_name.strip()):
            label = kronos_program_label(bank, index) + (f' "{actual.strip()}"' if actual is not None else "")
            kept.append((t + 1, label))
            continue
        out[o], out[o + 1] = e.nautilus_index & 0xFF, e.nautilus_bank & 0xFF
        remapped.append(f'{t + 1}: {kronos_program_label(bank, index)} "{e.kronos_name}" -> '
                        f'{nautilus_program_label(e.nautilus_bank, e.nautilus_index)} "{e.nautilus_name}"'
                        f'{" (as in Korg" + chr(39) + "s Nautilus factory Combis)" if e.source == "korg" else ""}')
    if remapped:
        warnings.append(f"Timbre Programs re-pointed to Nautilus factory equivalents: {'; '.join(remapped)}.")
    if kept:
        groups: Dict[str, List[int]] = {}
        for t, label in kept:
            groups.setdefault(label, []).append(t)
        text = "; ".join(f"{'Timbre' if len(ts) == 1 else 'Timbres'} {','.join(map(str, ts))} ({label})"
                         for label, ts in groups.items())
        why = ", or the source slot no longer holds the factory Program" if kronos_program_name else ""
        warnings.append(f"{text} kept their slot numbers (no factory equivalent{why}) - convert and place "
                        "those Programs at the same Nautilus slots.")
