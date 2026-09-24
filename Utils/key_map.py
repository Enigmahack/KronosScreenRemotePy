"""
Linux keycode table for Kronos keyboard injection.
Maps Qt.Key values to Linux input keycodes.

Kronos layout remaps documented in inline comments — these are non-obvious;
the Kronos uses a different keyboard layout for several symbols.
"""
from __future__ import annotations
from typing import Optional, Tuple


# Qt.Key → Linux keycode
# Built lazily once Qt is available
_MAP: dict[int, int] = {}
_SHIFTED: dict[int, Tuple[int, bool]] = {}  # Qt.Key → (linux_code, keep_shift)
_initialized = False


def _init():
    global _initialized
    if _initialized:
        return
    from PySide6.QtCore import Qt
    _MAP.update({
        # Letters
        Qt.Key_A: 30, Qt.Key_B: 48, Qt.Key_C: 46, Qt.Key_D: 32, Qt.Key_E: 18,
        Qt.Key_F: 33, Qt.Key_G: 34, Qt.Key_H: 35, Qt.Key_I: 23, Qt.Key_J: 36,
        Qt.Key_K: 37, Qt.Key_L: 38, Qt.Key_M: 50, Qt.Key_N: 49, Qt.Key_O: 24,
        Qt.Key_P: 25, Qt.Key_Q: 16, Qt.Key_R: 19, Qt.Key_S: 31, Qt.Key_T: 20,
        Qt.Key_U: 22, Qt.Key_V: 47, Qt.Key_W: 17, Qt.Key_X: 45, Qt.Key_Y: 21,
        Qt.Key_Z: 44,
        # Top-row digits
        Qt.Key_1: 2,  Qt.Key_2: 3,  Qt.Key_3: 4,  Qt.Key_4: 5,  Qt.Key_5: 6,
        Qt.Key_6: 7,  Qt.Key_7: 8,  Qt.Key_8: 9,  Qt.Key_9: 10, Qt.Key_0: 11,
        # Editing
        Qt.Key_Space:     57,
        Qt.Key_Backspace: 14,
        Qt.Key_Tab:       15,
        Qt.Key_Return:    28,
        Qt.Key_Delete:    111,
        Qt.Key_Insert:    110,
        # Navigation
        Qt.Key_Up:     103, Qt.Key_Down:  108,
        Qt.Key_Left:   105, Qt.Key_Right: 106,
        Qt.Key_Home:   102, Qt.Key_End:   107,
        Qt.Key_PageUp: 104, Qt.Key_PageDown: 109,
        # Modifiers
        Qt.Key_Shift:   42,   # left shift; right handled via extended flag
        Qt.Key_Control: 29,
        Qt.Key_Alt:     56,
        Qt.Key_CapsLock: 58,
        # Symbols — Kronos layout remaps:
        #   The Kronos keyboard layout differs from US; these send the keycode
        #   the Kronos actually maps to the expected character.
        Qt.Key_Minus:        74,  # - → KEY_KPMINUS  (numpad minus)
        Qt.Key_Equal:        13,  # = (same as -)
        Qt.Key_BracketLeft:  40,  # [ → KEY_APOSTROPHE (Kronos maps that to [)
        Qt.Key_BracketRight: 39,  # ] → KEY_SEMICOLON  (Kronos maps that to ])
        Qt.Key_Backslash:    53,  # \ → KEY_SLASH      (Kronos maps that to \)
        Qt.Key_Semicolon:    27,  # ; → KEY_RIGHTBRACE
        Qt.Key_Apostrophe:   26,  # ' → KEY_LEFTBRACE
        Qt.Key_Comma:        51,
        Qt.Key_Period:       52,
        Qt.Key_Slash:        12,  # / → KEY_MINUS (Kronos maps that to /)
        Qt.Key_QuoteLeft:    41,  # `
        # Numpad operators (numpad digits are routed as BUTTON NUM0..9 instead)
        Qt.Key_Asterisk:  55,
        Qt.Key_Plus:      78,
        Qt.Key_division:  98,
        # Function keys
        Qt.Key_F1:  59, Qt.Key_F2:  60, Qt.Key_F3:  61, Qt.Key_F4:  62,
        Qt.Key_F5:  63, Qt.Key_F6:  64, Qt.Key_F7:  65, Qt.Key_F8:  66,
        Qt.Key_F9:  67, Qt.Key_F10: 68, Qt.Key_F11: 87, Qt.Key_F12: 88,
        Qt.Key_Escape: 1,
    })

    # Shifted overrides: Shift+key → special Linux keycode to send.
    # keep_shift=False means bracket with Shift release/re-press so Kronos
    # receives the key without the shift modifier.
    _SHIFTED.update({
        Qt.Key_8:     (55, False),  # Shift+8 → * (KEY_KPASTERISK, no Shift)
        Qt.Key_Equal: (78, False),  # Shift+= → + (KEY_KPPLUS, no Shift)
        Qt.Key_Slash: (13, True),   # Shift+/ → ? (Shift+KEY_EQUAL)
    })
    _initialized = True


def to_linux(qt_key: int) -> Optional[int]:
    """Return the Linux keycode for the given Qt key, or None if unmapped."""
    _init()
    return _MAP.get(qt_key)


def to_linux_shifted(qt_key: int) -> Optional[Tuple[int, bool]]:
    """
    Return (linux_code, keep_shift) override for when Shift is held, or None
    to use the normal passthrough.
    """
    _init()
    return _SHIFTED.get(qt_key)


# Keys that are numpad digits and should route to BUTTON NUM0..9 instead of KEY.
# Also includes numpad Enter, numpad dot, numpad subtract.
def is_numpad_button(qt_key: int) -> bool:
    _init()
    from PySide6.QtCore import Qt
    return qt_key in (
        Qt.Key_0, Qt.Key_1, Qt.Key_2, Qt.Key_3, Qt.Key_4,
        Qt.Key_5, Qt.Key_6, Qt.Key_7, Qt.Key_8, Qt.Key_9,
    )
