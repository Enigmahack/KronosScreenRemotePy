"""
Printable-ASCII (+ Tab/Backspace/Enter) -> Kronos KEY-command-sequence table.
Port of Core/CharMap.cs. Distinct from key_map.py (physical Qt key -> Linux
keycode, for live keyboard forwarding) -- this maps typed *characters* to the
KEY command strings needed to type that character, e.g. for pasting text.

Eva (Kronos UI) text-field case behavior (confirmed via inject_test 2026-06-09):
  unshifted letter key -> UPPERCASE in Eva text field
  Shift + letter key    -> lowercase in Eva text field
This is the OPPOSITE of standard keyboard convention. All letter entries
reflect this.
"""
from __future__ import annotations
from typing import Dict, List, NamedTuple, Optional

SHIFT_CODE = 42  # KEY_LEFTSHIFT


class _Entry(NamedTuple):
    code: int
    shift: bool


_MAP: Dict[str, _Entry] = {
    '\b': _Entry(14, False),  # Backspace  KEY_BACKSPACE
    '\t': _Entry(15, False),  # Tab        KEY_TAB
    '\n': _Entry(28, False),  # Enter      KEY_ENTER
    ' ':  _Entry(57, False),  # Space      KEY_SPACE
    '!':  _Entry(2,  True),   # Shift+1
    # '"' -- Shift+26 triggers Kronos character picker; no direct Eva key confirmed
    '#':  _Entry(43, False),  # KEY_BACKSLASH -- confirmed via inject_test
    '$':  _Entry(5,  True),   # Shift+4
    '%':  _Entry(6,  True),   # Shift+5
    '&':  _Entry(8,  True),   # Shift+7 -- confirmed Eva UI
    # '\'' -- Shift+43 gives ' on console but NOT in Eva; no direct Eva key confirmed
    '(':  _Entry(10, True),   # Shift+9
    ')':  _Entry(11, True),   # Shift+0
    '*':  _Entry(55, False),  # shifted override: KEY_KPASTERISK, no shift
    '+':  _Entry(78, False),  # shifted override: KEY_KPPLUS, no shift
    ',':  _Entry(51, False),  # OemComma
    '-':  _Entry(74, False),  # OemMinus -> KEY_KPMINUS
    '.':  _Entry(52, False),  # OemPeriod
    '/':  _Entry(12, False),  # OemQuestion -> KEY_MINUS
    '0':  _Entry(11, False), '1': _Entry(2, False), '2': _Entry(3, False),
    '3':  _Entry(4,  False), '4': _Entry(5, False), '5': _Entry(6, False),
    '6':  _Entry(7,  False), '7': _Entry(8, False), '8': _Entry(9, False),
    '9':  _Entry(10, False),
    ':':  _Entry(27, True),   # Shift+OemSemicolon -> KEY_RIGHTBRACE + Shift
    ';':  _Entry(27, False),  # OemSemicolon -> KEY_RIGHTBRACE
    '<':  _Entry(51, True),   # Shift+OemComma
    '=':  _Entry(13, False),  # OemPlus -> KEY_EQUAL
    '>':  _Entry(52, True),   # Shift+OemPeriod
    '?':  _Entry(13, True),   # shifted override: KEY_EQUAL + Shift
    '@':  _Entry(3,  True),   # Shift+2 -- confirmed Eva UI
    # Uppercase letters: unshifted key -> uppercase in Eva (inverted case convention)
    'A':  _Entry(30, False), 'B': _Entry(48, False), 'C': _Entry(46, False),
    'D':  _Entry(32, False), 'E': _Entry(18, False), 'F': _Entry(33, False),
    'G':  _Entry(34, False), 'H': _Entry(35, False), 'I': _Entry(23, False),
    'J':  _Entry(36, False), 'K': _Entry(37, False), 'L': _Entry(38, False),
    'M':  _Entry(50, False), 'N': _Entry(49, False), 'O': _Entry(24, False),
    'P':  _Entry(25, False), 'Q': _Entry(16, False), 'R': _Entry(19, False),
    'S':  _Entry(31, False), 'T': _Entry(20, False), 'U': _Entry(22, False),
    'V':  _Entry(47, False), 'W': _Entry(17, False), 'X': _Entry(45, False),
    'Y':  _Entry(21, False), 'Z': _Entry(44, False),
    '[':  _Entry(40, False),  # OemOpenBrackets -> KEY_APOSTROPHE
    '\\': _Entry(53, False),  # Oem5 -> KEY_SLASH
    ']':  _Entry(39, False),  # Oem6 -> KEY_SEMICOLON
    '^':  _Entry(7,  True),   # Shift+6
    '_':  _Entry(74, True),   # Shift+OemMinus -> KEY_KPMINUS + Shift
    # '`' (41 unshifted) not accepted by Kronos text fields -- omitted
    # Lowercase letters: Shift + key -> lowercase in Eva (inverted case convention)
    'a':  _Entry(30, True),  'b': _Entry(48, True),  'c': _Entry(46, True),
    'd':  _Entry(32, True),  'e': _Entry(18, True),  'f': _Entry(33, True),
    'g':  _Entry(34, True),  'h': _Entry(35, True),  'i': _Entry(23, True),
    'j':  _Entry(36, True),  'k': _Entry(37, True),  'l': _Entry(38, True),
    'm':  _Entry(50, True),  'n': _Entry(49, True),  'o': _Entry(24, True),
    'p':  _Entry(25, True),  'q': _Entry(16, True),  'r': _Entry(19, True),
    's':  _Entry(31, True),  't': _Entry(20, True),  'u': _Entry(22, True),
    'v':  _Entry(47, True),  'w': _Entry(17, True),  'x': _Entry(45, True),
    'y':  _Entry(21, True),  'z': _Entry(44, True),
    '{':  _Entry(40, True),   # Shift+OemOpenBrackets -> KEY_APOSTROPHE + Shift
    '|':  _Entry(53, True),   # Shift+Oem5 -> KEY_SLASH + Shift
    '}':  _Entry(39, True),   # Shift+Oem6 -> KEY_SEMICOLON + Shift
    # '~' -- Shift+41 opens Eva's argument editor; omitted to prevent UI hijack
}


def get_commands(c: str) -> Optional[List[str]]:
    """Return the KEY command sequence to type character c, or None if unmapped."""
    e = _MAP.get(c)
    if e is None:
        return None
    if e.shift:
        return [f"KEY {SHIFT_CODE} 1", f"KEY {e.code} 1", f"KEY {e.code} 0", f"KEY {SHIFT_CODE} 0"]
    return [f"KEY {e.code} 1", f"KEY {e.code} 0"]


def get_description(c: str) -> str:
    e = _MAP.get(c)
    if e is None:
        return "(no mapping)"
    return f"KEY {e.code} + Shift" if e.shift else f"KEY {e.code}"
