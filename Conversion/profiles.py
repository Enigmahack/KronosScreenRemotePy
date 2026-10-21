from __future__ import annotations

import enum


class ProgramKind(enum.Enum):
    """Passed explicitly: an HD-1 PCG slot and an EXi slot are both 4960 bytes, so length can't tell them apart."""
    EXI = "exi"
    HD1 = "hd1"


class Profile:
    """Program body geometry of one instrument family."""

    def __init__(self, name: str, slot_size: int, wire_exi: int, wire_hd1: int, tail_start: int) -> None:
        self.name, self.slot_size, self.wire_exi, self.wire_hd1, self.tail_start = name, slot_size, wire_exi, wire_hd1, tail_start

    def wire_size(self, kind: ProgramKind) -> int:
        return self.wire_exi if kind is ProgramKind.EXI else self.wire_hd1


KRONOS = Profile("Kronos", 4960, 4960, 3706, 2558)
NAUTILUS = Profile("Nautilus", 4954, 4954, 3700, 2552)

# Program head [0, 1280) is identical on both; the Kronos S/PDIF+DrumTrack+KARMA block that follows has no
# Nautilus equivalent and is replaced from a template.
PROGRAM_SHARED_HEAD_END = 1280
PROGRAM_SIZE_DELTA = KRONOS.tail_start - NAUTILUS.tail_start

COMBI_KRONOS_SIZE, COMBI_NAUTILUS_SIZE = 7810, 5640
COMBI_SHARED_HEAD_END, COMBI_KRONOS_TAIL, COMBI_NAUTILUS_TAIL = 1268, 4790, 2620

DRUM_KIT_SIZE, WAVE_SEQ_SIZE = 38424, 2216
