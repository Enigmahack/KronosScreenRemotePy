"""
Application settings — mirrors AppSettings.cs.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List
from models import Keybind


@dataclass
class MacroDef:
    description: str = ""
    trigger_key: int = 0
    trigger_mods: int = 0
    step_delay_ms: int = 100
    steps: List[str] = field(default_factory=list)

    @property
    def step_count(self) -> int:
        return len(self.steps)

    @property
    def trigger_display(self) -> str:
        if self.trigger_key == 0:
            return "(none)"
        kb = Keybind(self.trigger_key, self.trigger_mods)
        return kb.to_display_string()

    @property
    def delay_display(self) -> str:
        return f"{self.step_delay_ms} ms"


@dataclass
class RawKeyMap:
    label: str = ""
    host_key: int = 0
    host_mods: int = 0
    raw_code: int = 0
    send_shift: bool = False

    @property
    def host_key_display(self) -> str:
        if self.host_key == 0:
            return "(none)"
        kb = Keybind(self.host_key, self.host_mods)
        return kb.to_display_string()

    @property
    def raw_display(self) -> str:
        s = f"KEY {self.raw_code}"
        if self.send_shift:
            s += " (+Shift)"
        return s


@dataclass
class AppSettings:
    # Connection
    kronos_host: str = ""
    stream_port: int = 7373
    ctrl_port:   int = 7374

    # Streaming
    pull_mode: bool = False
    max_fps:   int  = 15

    # General
    prompt_before_quitting: bool = True
    hide_controls:          bool = False
    screenshot_dir:         str  = ""

    # FTP authentication (required by the stream daemon)
    ftp_username: str = ""
    ftp_password: str = ""
    ftp_port:     int = 21

    # VGA output
    vga_mirror_enabled:  bool = False
    screensaver_timeout: int  = 300

    # UI layout
    layout_preset: str = "Full"  # "Full" | "Focused" | "Detached"

    # View
    zoom_default_level: float = 2.5
    zoom_window_size:   float = 1.0

    # Debug
    debug_logging: bool = False

    # Key bindings: action name → serialized Keybind string
    keybinds: Dict[str, str] = field(default_factory=dict)

    # Macros
    macros: List[MacroDef] = field(default_factory=list)

    # Always on top
    always_on_top: bool = False

    # Recent connections (most-recent-first, capped at 10)
    recent_hosts: List[str] = field(default_factory=list)

    # Raw key mappings (host Qt key → linux keycode)
    raw_key_maps: List[RawKeyMap] = field(default_factory=list)

    def get_keybind(self, action: str) -> Keybind:
        if action in self.keybinds:
            return Keybind.parse(self.keybinds[action])
        for a, _label, dk in get_rebindable():
            if a == action:
                return Keybind(dk)
        return Keybind.NONE

    def set_keybind(self, action: str, kb: Keybind):
        self.keybinds[action] = kb.serialize()

    def get_key_name(self, action: str) -> str:
        return self.get_keybind(action).to_display_string()

    def get_raw_map(self, qt_key: int, mods: int) -> "RawKeyMap | None":
        for rm in self.raw_key_maps:
            if rm.host_key == qt_key and rm.host_mods == mods:
                return rm
        return None

    def get_macro_for_trigger(self, qt_key: int, mods: int) -> "MacroDef | None":
        for m in self.macros:
            if m.trigger_key == qt_key and m.trigger_mods == mods and m.steps:
                return m
        return None


# (action_id, display_label, default_qt_key_int)
# Default Qt key values are filled lazily (Qt not yet imported at module load)
def _qt_key(name: str) -> int:
    from PySide6.QtCore import Qt
    return int(getattr(Qt, f"Key_{name}", 0))


REBINDABLE_DEFS: list[tuple[str, str, str]] = [
    ("Quit",          "Quit",                   "Q"),
    ("Fullscreen",    "Toggle Fullscreen",       "F"),
    ("Zoom Window",   "Toggle Zoom Window",      "Z"),
    ("AspectLock",    "Toggle Aspect Lock",      "A"),
    ("Mirror",        "Toggle VGA Mirror",       "M"),
    ("Help",          "Toggle Help",             "F1"),
    ("Calibrate",     "Toggle Calibration Mode", "C"),
    ("HideControls",  "Hide/Show Controls",      ""),
    ("Mode Setlist",  "Mode: Setlist",           "F2"),
    ("Mode Combi",    "Mode: Combi",             "F3"),
    ("Mode Program",  "Mode: Program",           "F4"),
    ("Mode Sequence", "Mode: Sequence",          "F5"),
    ("Mode Sampling", "Mode: Sampling",          "F6"),
    ("Mode Global",   "Mode: Global",            "F7"),
    ("Mode Disk",     "Mode: Disk",              "F8"),
    ("Bank I-A",  "Bank: I-A",   ""), ("Bank I-B",  "Bank: I-B",   ""),
    ("Bank I-C",  "Bank: I-C",   ""), ("Bank I-D",  "Bank: I-D",   ""),
    ("Bank I-E",  "Bank: I-E",   ""), ("Bank I-F",  "Bank: I-F",   ""),
    ("Bank I-G",  "Bank: I-G",   ""), ("Bank U-A",  "Bank: U-A",   ""),
    ("Bank U-B",  "Bank: U-B",   ""), ("Bank U-C",  "Bank: U-C",   ""),
    ("Bank U-D",  "Bank: U-D",   ""), ("Bank U-E",  "Bank: U-E",   ""),
    ("Bank U-F",  "Bank: U-F",   ""), ("Bank U-G",  "Bank: U-G",   ""),
    ("Bank U-AA", "Bank: U-AA",  ""), ("Bank U-BB", "Bank: U-BB",  ""),
    ("Bank U-CC", "Bank: U-CC",  ""), ("Bank U-DD", "Bank: U-DD",  ""),
    ("Bank U-EE", "Bank: U-EE",  ""), ("Bank U-FF", "Bank: U-FF",  ""),
    ("Bank U-GG", "Bank: U-GG",  ""),
]

_REBINDABLE_CACHE: list[tuple[str, str, int]] | None = None


def get_rebindable() -> list[tuple[str, str, int]]:
    global _REBINDABLE_CACHE
    if _REBINDABLE_CACHE is None:
        from PySide6.QtCore import Qt
        result = []
        fk = {f"F{n}": int(Qt.Key_F1) + n - 1 for n in range(1, 13)}
        letters = {c: int(getattr(Qt, f"Key_{c}")) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"}
        named = {**fk, **letters, "": 0}
        for action, label, key_name in REBINDABLE_DEFS:
            dk = named.get(key_name, 0)
            result.append((action, label, dk))
        _REBINDABLE_CACHE = result
    return _REBINDABLE_CACHE
