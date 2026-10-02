"""MIDI note number <-> name (0-127) for the Sample Editor's key-range fields — port of
Core/Sample/MidiNoteName.cs. C4 = 60, matching the Kronos' own on-screen naming (some DAWs use C3 or C5)."""
from __future__ import annotations

import re
from typing import Optional

_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
_BASE = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_INT = re.compile(r"^\s*[+-]?\d+\s*$")


def to_name(midi_number: int) -> str:
    n = max(0, min(127, int(midi_number)))
    return f"{_NAMES[n % 12]}{n // 12 - 1}"


def try_parse(text: Optional[str]) -> Optional[int]:
    """'C4', 'C#4', 'Db4', 'e1', 'G#-1'... -> 0-127, or None if unparseable (bad input is expected
    user-editing state, not an error)."""
    if text is None or not text.strip():
        return None
    text = text.strip()
    if len(text) < 2:
        return None
    base = _BASE.get(text[0].upper())
    if base is None:
        return None
    i = 1
    if text[i] in "#sS":
        base += 1
        i += 1
    elif text[i] in "bB":
        base -= 1
        i += 1
    rest = text[i:]
    if not _INT.match(rest):
        return None
    midi = (int(rest) + 1) * 12 + base
    return midi if 0 <= midi <= 127 else None


def _selftest() -> None:
    import sys
    fails = []

    def check(name, cond):
        if not cond:
            fails.append(name)
    check("c4", to_name(60) == "C4" and try_parse("C4") == 60)
    check("clamps", to_name(-5) == "C-1" and to_name(500) == "G9")
    check("sharp-flat", try_parse("C#4") == 61 and try_parse("Db4") == 61 and try_parse("c#4") == 61)
    check("lowercase", try_parse("e1") == 28)
    check("negative-octave", try_parse("G#-1") == 8 and try_parse("C-1") == 0)
    check("range", try_parse("G9") == 127 and try_parse("G#9") is None and try_parse("B-2") is None)
    check("bad-input", all(try_parse(t) is None for t in (None, "", " ", "C", "H4", "C#", "C4x", "C_4", "4")))
    check("flat-of-b", try_parse("Bb3") == 58)
    check("round-trip-all", all(try_parse(to_name(n)) == n for n in range(128)))
    if fails:
        print("FAIL:", ", ".join(fails)); sys.exit(1)
    print("midi_note_name self-test: OK")


if __name__ == "__main__":
    _selftest()
