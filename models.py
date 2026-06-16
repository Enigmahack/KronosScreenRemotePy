"""
Data models: PaletteEntry, Keybind, CalMesh, undo history types.
"""
from __future__ import annotations
import math
from dataclasses import dataclass, field
from typing import ClassVar, List, Optional, Tuple


@dataclass
class PaletteEntry:
    r: int
    g: int
    b: int


@dataclass
class Keybind:
    key: int           # Qt.Key int value; 0 = no binding
    modifiers: int = 0 # Qt.KeyboardModifier flags

    NONE: ClassVar["Keybind"]   # populated at bottom of module

    def to_display_string(self) -> str:
        from PySide6.QtCore import Qt
        if self.key == 0:
            return "(none)"
        parts: list[str] = []
        m = self.modifiers
        if m & Qt.ControlModifier.value:
            parts.append("Ctrl")
        if m & Qt.AltModifier.value:
            parts.append("Alt")
        if m & Qt.ShiftModifier.value:
            parts.append("Shift")
        if m & Qt.MetaModifier.value:
            parts.append("Meta")
        parts.append(_key_label(self.key))
        return "+".join(parts)

    def serialize(self) -> str:
        if self.key == 0:
            return "None"
        return self.to_display_string()

    @staticmethod
    def parse(s: str) -> "Keybind":
        from PySide6.QtCore import Qt
        if not s or s == "None":
            return Keybind.NONE
        mods = 0
        key_val = 0
        for part in s.split("+"):
            part = part.strip()
            if part == "Ctrl":
                mods |= Qt.ControlModifier.value
            elif part == "Alt":
                mods |= Qt.AltModifier.value
            elif part == "Shift":
                mods |= Qt.ShiftModifier.value
            elif part == "Meta":
                mods |= Qt.MetaModifier.value
            else:
                k = _parse_key_label(part)
                if k:
                    key_val = k
        return Keybind(key_val, mods)


def _key_label(key: int) -> str:
    from PySide6.QtCore import Qt
    if not _QT_KEY_NAMES:
        _build_key_names()
    # Digit row: Qt.Key_0..Qt.Key_9
    if Qt.Key_0 <= key <= Qt.Key_9:
        return chr(key)
    # Function keys
    if Qt.Key_F1 <= key <= Qt.Key_F35:
        return f"F{key - Qt.Key_F1 + 1}"
    # Fall back to Qt key name
    name = _QT_KEY_NAMES.get(key)
    if name:
        return name
    # Generic fallback
    try:
        from PySide6.QtCore import Qt
        for k, v in vars(Qt).items():
            if k.startswith("Key_") and v == key:
                return k[4:]
    except Exception:
        pass
    return str(key)


def _parse_key_label(label: str) -> int:
    from PySide6.QtCore import Qt
    if not _QT_KEY_NAMES:
        _build_key_names()
    # Digit
    if len(label) == 1 and label.isdigit():
        return Qt.Key_0 + int(label)
    # Function key
    if label.startswith("F") and label[1:].isdigit():
        return Qt.Key_F1 + int(label[1:]) - 1
    # Named
    for k, v in _QT_KEY_NAMES.items():
        if v == label:
            return k
    # Try Qt.Key_ directly
    attr = f"Key_{label}"
    val = getattr(Qt, attr, None)
    if val is not None:
        return int(val)
    return 0


# Mapping of Qt.Key values to short display labels
_QT_KEY_NAMES: dict[int, str] = {}

def _build_key_names():
    from PySide6.QtCore import Qt
    labels = {
        Qt.Key_Space:  "Space",
        Qt.Key_Return: "Return",
        Qt.Key_Enter:  "Enter",
        Qt.Key_Backspace: "Backspace",
        Qt.Key_Tab:    "Tab",
        Qt.Key_Delete: "Delete",
        Qt.Key_Insert: "Insert",
        Qt.Key_Home:   "Home",
        Qt.Key_End:    "End",
        Qt.Key_PageUp: "PageUp",
        Qt.Key_PageDown: "PageDown",
        Qt.Key_Up:     "Up",
        Qt.Key_Down:   "Down",
        Qt.Key_Left:   "Left",
        Qt.Key_Right:  "Right",
        Qt.Key_Escape: "Escape",
        Qt.Key_CapsLock: "CapsLock",
        Qt.Key_Shift:  "Shift",
        Qt.Key_Control: "Ctrl",
        Qt.Key_Alt:    "Alt",
        Qt.Key_Meta:   "Meta",
        Qt.Key_A: "A", Qt.Key_B: "B", Qt.Key_C: "C", Qt.Key_D: "D",
        Qt.Key_E: "E", Qt.Key_F: "F", Qt.Key_G: "G", Qt.Key_H: "H",
        Qt.Key_I: "I", Qt.Key_J: "J", Qt.Key_K: "K", Qt.Key_L: "L",
        Qt.Key_M: "M", Qt.Key_N: "N", Qt.Key_O: "O", Qt.Key_P: "P",
        Qt.Key_Q: "Q", Qt.Key_R: "R", Qt.Key_S: "S", Qt.Key_T: "T",
        Qt.Key_U: "U", Qt.Key_V: "V", Qt.Key_W: "W", Qt.Key_X: "X",
        Qt.Key_Y: "Y", Qt.Key_Z: "Z",
        Qt.Key_Minus: "Minus",   Qt.Key_Equal: "Equal",
        Qt.Key_BracketLeft: "BracketLeft", Qt.Key_BracketRight: "BracketRight",
        Qt.Key_Backslash: "Backslash", Qt.Key_Semicolon: "Semicolon",
        Qt.Key_Apostrophe: "Apostrophe", Qt.Key_Comma: "Comma",
        Qt.Key_Period: "Period", Qt.Key_Slash: "Slash",
        Qt.Key_QuoteLeft: "QuoteLeft",
        Qt.Key_Asterisk: "Asterisk", Qt.Key_Plus: "Plus",
    }
    for n in range(1, 36):
        from PySide6.QtCore import Qt
        labels[Qt.Key_F1 + n - 1] = f"F{n}"
    _QT_KEY_NAMES.update(labels)


# Sentinel; properly initialized on first import after Qt is available
Keybind.NONE = Keybind(0, 0)


# ── Palette history ────────────────────────────────────────────────────────────

@dataclass
class HistEntry:
    idx: int
    old_val: Optional[PaletteEntry]
    new_val: Optional[PaletteEntry]
    old_locked: Optional[bool] = None
    new_locked: Optional[bool] = None


# ── Calibration ───────────────────────────────────────────────────────────────

@dataclass
class CalBiasDot:
    nx: int
    ny: int


from enum import Enum, auto

class CalHistKind(Enum):
    NodeMove   = auto()
    DotAdded   = auto()
    DotRemoved = auto()


@dataclass
class CalHistEntry:
    kind:    CalHistKind
    col:     int = 0
    row:     int = 0
    old_off_x: int = 0
    old_off_y: int = 0
    new_off_x: int = 0
    new_off_y: int = 0
    dot_idx: int = -1
    dot:     Optional[CalBiasDot] = None


class CalMesh:
    def __init__(self, cols: int = 5, rows: int = 5):
        self.cols = cols
        self.rows = rows
        self._off_x = [[0] * rows for _ in range(cols)]
        self._off_y = [[0] * rows for _ in range(cols)]

    def nat_x(self, col: int, w: int) -> int:
        return round(col * (w - 1) / (self.cols - 1))

    def nat_y(self, row: int, h: int) -> int:
        return round(row * (h - 1) / (self.rows - 1))

    def get_offset(self, col: int, row: int) -> Tuple[int, int]:
        return self._off_x[col][row], self._off_y[col][row]

    def set_offset(self, col: int, row: int, ox: int, oy: int):
        if 0 <= col < self.cols and 0 <= row < self.rows:
            self._off_x[col][row] = ox
            self._off_y[col][row] = oy

    def node_dst(self, col: int, row: int, w: int, h: int) -> Tuple[int, int]:
        x = max(0, min(w - 1, self.nat_x(col, w) + self._off_x[col][row]))
        y = max(0, min(h - 1, self.nat_y(row, h) + self._off_y[col][row]))
        return x, y

    # ── Forward map ────────────────────────────────────────────────────────────

    def apply(self, px: int, py: int, w: int, h: int) -> Tuple[int, int]:
        fx, fy = self._apply_f(px, py, w, h)
        return (max(0, min(w - 1, round(fx))),
                max(0, min(h - 1, round(fy))))

    def _apply_f(self, px: float, py: float, w: int, h: int) -> Tuple[float, float]:
        cell_w = (w - 1) / (self.cols - 1)
        cell_h = (h - 1) / (self.rows - 1)
        col = max(0, min(self.cols - 2, int(px / cell_w)))
        row = max(0, min(self.rows - 2, int(py / cell_h)))
        tx = max(0.0, min(1.0, (px - col * cell_w) / cell_w))
        ty = max(0.0, min(1.0, (py - row * cell_h) / cell_h))
        tlx, tly = self.node_dst(col,     row,     w, h)
        trx, try_ = self.node_dst(col + 1, row,     w, h)
        blx, bly = self.node_dst(col,     row + 1, w, h)
        brx, bry = self.node_dst(col + 1, row + 1, w, h)
        return (_bilerp(tlx, trx, blx, brx, tx, ty),
                _bilerp(tly, try_, bly, bry, tx, ty))

    # ── Inverse map (Newton) ───────────────────────────────────────────────────

    def inverse_apply(self, px: int, py: int, w: int, h: int) -> Tuple[int, int]:
        x, y = float(px), float(py)
        for _ in range(16):
            fx, fy = self._apply_f(x, y, w, h)
            dx, dy = px - fx, py - fy
            x = max(0.0, min(w - 1, x + dx))
            y = max(0.0, min(h - 1, y + dy))
            if dx * dx + dy * dy < 0.01:
                break
        return (max(0, min(w - 1, round(x))),
                max(0, min(h - 1, round(y))))

    def reset(self):
        self._off_x = [[0] * self.rows for _ in range(self.cols)]
        self._off_y = [[0] * self.rows for _ in range(self.cols)]

    def is_identity(self) -> bool:
        for c in range(self.cols):
            for r in range(self.rows):
                if self._off_x[c][r] != 0 or self._off_y[c][r] != 0:
                    return False
        return True


def _bilerp(tl, tr, bl, br, tx, ty) -> float:
    def lerp(a, b, t): return a + (b - a) * t
    return lerp(lerp(tl, tr, tx), lerp(bl, br, tx), ty)
