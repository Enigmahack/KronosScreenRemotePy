"""KARMA -> Nautilus arpeggiator handling: stripping Kronos-only KARMA bytes and assigning arp patterns to Scenes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from Conversion.data import (ARP_ENCODING_FIXED, KarmaGeCatalog, KarmaGePatternLibrary)

# Kronos "KARMA Common" through the last "Master Scene" byte; identical in the EXi and HD-1 bodies.
KARMA_START, KARMA_END = 1304, 2557

# Two Kronos-only bit fields share bytes with fields that do carry over, so only their bits are masked.
_CHORD_MODE = (2558, 0b0001_1000)
_CHORD_SOURCE = (2562, 0b0111_0000)

GE_NUMBER_OFFSET = 1814   # u16 LE; the one GE assigned to the whole Program


def strip_karma_fields(body: bytes) -> Tuple[bytes, bool]:
    """Returns (body, stripped); stripped is False when there was nothing but zeroes to remove."""
    out = bytearray(body)
    stripped = False
    for i in range(KARMA_START, min(KARMA_END, len(out) - 1) + 1):
        if out[i]:
            stripped = True
            out[i] = 0
    for offset, mask in (_CHORD_MODE, _CHORD_SOURCE):
        if offset < len(out):
            if out[offset] & mask:
                stripped = True
            out[offset] &= ~mask & 0xFF
    return bytes(out), stripped


def read_ge_number(body: bytes) -> Optional[int]:
    if GE_NUMBER_OFFSET + 1 >= len(body):
        return None
    return body[GE_NUMBER_OFFSET] | (body[GE_NUMBER_OFFSET + 1] << 8)


def describe_ge_assignment(body: bytes, catalog: KarmaGeCatalog) -> Optional[str]:
    ge = read_ge_number(body)
    if not ge:
        return None
    name = f'"{catalog.name(ge)}"' if catalog.name(ge) else "(name unknown)"
    return (f"KARMA GE #{ge} {name} was assigned - Nautilus has no automatic translation for this "
            "(Kronos and Nautilus factory arp/GE catalogs are different content, not just differently "
            "numbered); assign a Nautilus Arp Pattern to Scene 1 manually if wanted.")


# Nautilus Program Scene n ARP Pattern Number: u16 LE at 2440 + 28*n. A Combi Scene is 44 bytes and carries
# two arpeggiators (B is 16 bytes after A). ARP block: +0 pattern, +2 octave/resolution, +3 flags, +4 gate,
# +5 velocity, +6 swing, +12 output channel, +13 input channel.
SCENE_BASE, SCENE_STRIDE, SCENE_COUNT = 2440, 28, 4
COMBI_SCENE_STRIDE, COMBI_ARP_B_OFFSET = 44, 16
BANK_A_PATTERN_BASE, BANK_B_PATTERN_BASE = 5, 2053   # pattern number: 0-4 presets, 5+i = A i, 2053+i = B i

# Hardware-confirmed: the only GEs with a real Nautilus equivalent (Basic Arp UP/DOWN/ALT1/ALT2/RANDOM).
_GE_TO_PRESET = {26: 0, 24: 1, 22: 2, 23: 3, 25: 4}


def preset_for_ge(ge_number: int) -> Optional[int]:
    return _GE_TO_PRESET.get(ge_number)


def resolution_index(steps_per_quarter: int) -> int:
    """Nautilus Scene "ARP Resolution" (bits 4-2 of the Scene flags byte) for each transcription grid."""
    return {12: 0, 8: 1, 6: 2, 4: 3, 3: 4, 2: 5}.get(steps_per_quarter, 3)


@dataclass(frozen=True)
class ArpOptions:
    program_scenes: int = 0b0001        # bit n = Nautilus Scene n+1 receiving the converted arp
    combi_scenes: int = 0b0001
    combi_arp_a_source: Optional[int] = 0   # Kronos KARMA module (0-3 = A-D) feeding each Nautilus ARP
    combi_arp_b_source: Optional[int] = 1
    transcribed_pattern_slot: Optional[int] = None    # bank-B slots the caller allows us to claim
    transcribed_pattern_slot_b: Optional[int] = None
    min_transcription_score: float = 0.9


@dataclass(frozen=True)
class ArpResolution:
    pattern_number: Optional[int]
    steps_per_quarter: Optional[int]
    to_store: Optional[Tuple[int, bytes]]
    warning: Optional[str]


def scene_list(mask: int) -> str:
    return ", ".join(f"Scene {n + 1}" for n in range(SCENE_COUNT) if mask & (1 << n))


def resolve_ge(ge_number: int, catalog: KarmaGeCatalog, patterns: Optional[KarmaGePatternLibrary],
               slot: Optional[int], min_score: float, where: str) -> ArpResolution:
    name = f'"{catalog.name(ge_number)}"' if catalog.name(ge_number) else "(name unknown)"
    preset = preset_for_ge(ge_number)
    if preset is not None:
        return ArpResolution(preset, None, None,
                             f"KARMA GE #{ge_number} {name} converted to Nautilus preset arp P{preset} in {where}; "
                             "its KARMA realtime parameters are not carried over.")
    entry = patterns.for_ge(ge_number) if patterns else None
    if entry is not None and entry.score >= min_score:
        if slot is not None and 0 <= slot <= 127:
            kind = ("fixed notes - does not follow the chord played" if entry.mode == ARP_ENCODING_FIXED
                    else "follows the chord played")
            return ArpResolution(BANK_B_PATTERN_BASE + slot, entry.steps_per_quarter, (slot, entry.pattern),
                                 f"KARMA GE #{ge_number} {name} replaced by a transcribed arp pattern (B{slot:03d}, "
                                 f"{entry.score:.0%} of steps match the Kronos, {kind}) in {where}; "
                                 f"the pattern must be stored to B{slot:03d} for it to play.")
        return ArpResolution(None, None, None,
                             f"KARMA GE #{ge_number} {name} has a transcribed arp pattern ({entry.score:.0%} match) "
                             "but no bank-B slot was chosen, so it was not used.")
    return ArpResolution(None, None, None,
                         f"KARMA GE #{ge_number} {name} was assigned - Nautilus has no translation for it; "
                         f"assign a Nautilus Arp Pattern to {where} manually if wanted.")


def apply_to_scenes(body: bytearray, scene_mask: int, scene_stride: int, arp_offset: int, r: ArpResolution) -> None:
    """A transcription also sets what it assumes: resolution = its grid, gate 100 (the pattern's own gates),
    velocity = Step."""
    if r.pattern_number is None:
        return
    for n in range(SCENE_COUNT):
        if not scene_mask & (1 << n):
            continue
        o = SCENE_BASE + n * scene_stride + arp_offset
        body[o] = r.pattern_number & 0xFF
        body[o + 1] = (r.pattern_number >> 8) & 0xFF
        if r.steps_per_quarter is not None:
            body[o + 2] = (body[o + 2] & ~0x1C & 0xFF) | (resolution_index(r.steps_per_quarter) << 2)
            body[o + 4] = 100
            body[o + 5] = 0x81


# KARMA Common (Kronos 1304-1306) and Scene Common (Nautilus 1348-1351) sit at the same place in a Program
# and a Combi: tempo u16 LE (40.00-300.00 x100), latch 1306 bit 6 -> 1349 bit 0, KARMA switch 1306 bit 7 ->
# arp switch 1349 bit 1. The arp switch only follows KARMA when an arp was assigned.
_K_TEMPO, _K_SWITCHES, _N_SWITCHES, _N_TEMPO = 1304, 1306, 1349, 1350


def carry_karma_common(kronos_body: bytes, nautilus_body: bytearray, arp_assigned: bool) -> None:
    tempo = kronos_body[_K_TEMPO] | (kronos_body[_K_TEMPO + 1] << 8)
    if 4000 <= tempo <= 30000:
        nautilus_body[_N_TEMPO] = tempo & 0xFF
        nautilus_body[_N_TEMPO + 1] = (tempo >> 8) & 0xFF
    k, s = kronos_body[_K_SWITCHES], nautilus_body[_N_SWITCHES]
    s = (s & ~0x01 & 0xFF) | ((k >> 6) & 1)
    if arp_assigned:
        s = (s & ~0x02 & 0xFF) | (((k >> 7) & 1) << 1)
    nautilus_body[_N_SWITCHES] = s
