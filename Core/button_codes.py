"""Front-panel button names -> raw NKS4 codes, and the BTN / BTN_DOWN / BTN_UP wire commands (docs/api.md section 7
"BTN / BTN_DOWN / BTN_UP", section 9). The daemon gives codes no meaning of its own, so these are the Kronos codes; a
Nautilus assigns different buttons to some of them (1/2 are MODE/PAGE there), which callers pick per device family."""
from __future__ import annotations

from typing import Dict, List, Tuple

# (category, name, code, description) - the section 9 table.
BUTTON_REFERENCE: List[Tuple[str, str, int, str]] = [
    ("Navigation", "EXIT", 8, "Exit button"),
    ("Navigation", "ENTER", 23, "Enter / confirm button"),
    ("Value control", "INC", 51, "Increment the currently selected value"),
    ("Value control", "DEC", 52, "Decrement the currently selected value"),
    ("Mode select", "SETLIST", 7, "Setlist mode"),
    ("Mode select", "COMBI", 1, "Combi mode"),
    ("Mode select", "PROGRAM", 2, "Program mode"),
    ("Mode select", "SEQUENCE", 3, "Sequence mode"),
    ("Mode select", "SAMPLING", 4, "Sampling mode"),
    ("Mode select", "GLOBAL", 5, "Global mode"),
    ("Mode select", "DISK", 6, "Disk mode"),
    ("Utility", "HELP", 9, "Help button"),
    ("Utility", "COMPARE", 10, "Compare button"),
    ("Utility", "RESET", 75, "Reset Controls button"),
    *[("Numeric pad", f"NUM{d}", 11 + d, f"Numeric key {d}") for d in range(10)],
    ("Numeric pad", "NUM_DASH", 21, "Numeric dash / minus"),
    ("Numeric pad", "NUM_DOT", 22, "Numeric dot / decimal"),
    *[("Mix Play", f"MP{i + 1}", 58 + i, f"Mix Play {i + 1}") for i in range(8)],
    *[("Mix Select", f"MS{i + 1}", 66 + i, f"Mix Select {i + 1}") for i in range(8)],
    *[("Bank", f"BANK_I{chr(ord('A') + i)}", 24 + i, f"Internal bank {chr(ord('A') + i)}") for i in range(7)],
    *[("Bank", f"BANK_U{chr(ord('A') + i)}", 31 + i, f"User bank {chr(ord('A') + i)}") for i in range(7)],
    ("Sequencer", "SEQ_PAUSE", 38, "Pause"),
    ("Sequencer", "SEQ_REW", 39, "Rewind"),
    ("Sequencer", "SEQ_FF", 40, "Fast forward"),
    ("Sequencer", "SEQ_LOCATE", 41, "Locate / return to start"),
    ("Sequencer", "SEQ_REC", 42, "Sequencer record"),
    ("Sequencer", "SEQ_START", 43, "Sequencer start / stop"),
    ("Sequencer", "TAP_TEMPO", 44, "Tap tempo"),
    ("Sampling", "SMPL_REC", 45, "Sampling record"),
    ("Sampling", "SMPL_START", 46, "Sampling start"),
    ("Channel strip", "MIX_KNOBS", 74, "Mixer Knobs selector"),
    ("Channel strip", "SOLO", 76, "Solo (fires on release)"),
    ("Channel strip", "MODULE_CONTROL", 47, "Module Control"),
    ("Channel strip", "KARMA_ONOFF", 48, "Karma On/Off"),
    ("Channel strip", "KARMA_LATCH", 49, "Karma Latch"),
    ("Channel strip", "DRUM_TRACK", 50, "Drum Track select"),
    ("Channel strip", "TIMBRE_TRACK", 53, "Timbre/Track select"),
    ("Channel strip", "AUDIO_TRACK", 54, "Audio select"),
    ("Channel strip", "EXT_TRACK", 55, "Ext select"),
    ("Channel strip", "RTKNOBS_KARMA", 56, "RT Knobs/Karma page select"),
    ("Channel strip", "TONE_ADJUST", 57, "Tone Adjust"),
    ("Channel strip", "SW1", 77, "Front-panel switch 1"),
    ("Channel strip", "SW2", 78, "Front-panel switch 2"),
]
BUTTON_REFERENCE.sort(key=lambda b: b[2])

BUTTON_CODES: Dict[str, int] = {name: code for _cat, name, code, _d in BUTTON_REFERENCE}


def code_of(name: str) -> int:
    """KeyError for an unknown name: the daemon rejects a bad code rather than clamping it, so never guess one."""
    return BUTTON_CODES[name]


def btn(name: str) -> str:
    """Press + release."""
    return f"BTN {code_of(name)}"


def btn_down(name: str) -> str:
    return f"BTN_DOWN {code_of(name)}"


def btn_up(name: str) -> str:
    return f"BTN_UP {code_of(name)}"


def chord_commands(names: List[str]) -> List[str]:
    """Buttons pressed left to right, then released right to left (all held at the midpoint)."""
    if len(names) < 2:
        raise ValueError("a chord needs at least two buttons")
    return [btn_down(n) for n in names] + [btn_up(n) for n in reversed(names)]


def _selftest() -> None:
    assert len(BUTTON_CODES) == len(BUTTON_REFERENCE), "duplicate button name"
    assert len(set(BUTTON_CODES.values())) == len(BUTTON_CODES), "duplicate button code"
    assert all(0 <= c <= 127 for c in BUTTON_CODES.values())
    assert btn("COMBI") == "BTN 1" and btn("NUM0") == "BTN 11" and btn("NUM9") == "BTN 20"
    assert btn("MP1") == "BTN 58" and btn("MS8") == "BTN 73" and btn("BANK_UG") == "BTN 37"
    assert chord_commands(["BANK_UA", "BANK_IA"]) == ["BTN_DOWN 31", "BTN_DOWN 24", "BTN_UP 24", "BTN_UP 31"]
    try:
        btn("NOPE")
        raise AssertionError("unknown name accepted")
    except KeyError:
        pass
    print("button_codes self-test OK")


if __name__ == "__main__":
    _selftest()
