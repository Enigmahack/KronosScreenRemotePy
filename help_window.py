"""
Help window — rich scrollable popup matching the C# HelpWindow structure.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QTextBrowser, QVBoxLayout,
)

# Color scheme (mirrors C# HelpWindow)
_C_BODY  = "#C8C8C8"
_C_HEAD  = "#88AADD"
_C_KEY   = "#FFD246"
_C_DIM   = "#888888"
_C_TITLE = "#DDEEFF"
_C_GREEN = "#AACC88"
_C_RED   = "#FF8888"

_BG = "#1A1A1A"


def _h1(text: str) -> str:
    return f"<p style='color:{_C_TITLE};font-size:15px;font-weight:bold;margin-top:18px;margin-bottom:4px;border-bottom:1px solid #334466;'>{text}</p>"


def _h2(text: str) -> str:
    return f"<p style='color:{_C_HEAD};font-size:12px;font-weight:bold;margin-top:12px;margin-bottom:2px;'>{text}</p>"


def _p(text: str) -> str:
    return f"<p style='color:{_C_BODY};margin:3px 0;'>{text}</p>"


def _key(text: str) -> str:
    return f"<span style='color:{_C_KEY};font-family:monospace;background:#2A2200;padding:1px 4px;border-radius:3px;'>{text}</span>"


def _dim(text: str) -> str:
    return f"<span style='color:{_C_DIM};'>{text}</span>"


def _green(text: str) -> str:
    return f"<span style='color:{_C_GREEN};'>{text}</span>"


def _kb_row(key: str, desc: str) -> str:
    return (
        f"<tr>"
        f"<td width='160' style='padding:2px 8px 2px 0;'>{_key(key)}</td>"
        f"<td style='color:{_C_BODY};padding:2px 0;'>{desc}</td>"
        f"</tr>"
    )


def _kbd_table(*rows) -> str:
    inner = "".join(rows)
    return f"<table style='margin-left:8px;margin-bottom:6px;'>{inner}</table>"


def _build_html() -> str:
    parts = [
        f"<html><body style='background:{_BG};color:{_C_BODY};font-size:12px;"
        f"font-family:Segoe UI,Arial,sans-serif;margin:12px;'>",

        _h1("Getting Started"),
        _p("Connect to a Korg Kronos synthesizer over a network connection (USB or Ethernet). "
           "Go to <b>Connection → Connect</b> or press <b>Reconnect</b> to begin. "
           "Enter the Kronos IP address when prompted. "),
        _p("The Kronos must have the <i>screenremote</i> daemon running. "
           "See the README for setup instructions."),

        _h1("Screen Panel"),
        _h2("Navigation"),
        _p("Click anywhere on the Kronos screen to interact with it via touch injection. "
           "A brief marker appears at the touch point."),
        _p("Click inside the screen area once to activate <b>keyboard capture</b> — "
           "further key presses are forwarded to the Kronos until you click outside or "
           "press Escape."),
        _h2("Zoom Tool"),
        _p("Toggle the zoom loupe with the Zoom Window key (default " + _key("Z") + ") "
           "or via <b>View → Zoom Window</b>. Scroll the mouse wheel while holding "
           + _key("Ctrl") + " to adjust zoom level."),
        _h2("Aspect Lock"),
        _p("Maintains the native 4:3 aspect ratio when the window is resized. "
           "Toggle with " + _key("A") + " or <b>View → Aspect Lock</b>."),

        _h1("Control Surface"),
        _p("The right panel mirrors the Kronos physical buttons: mode select, "
           "navigation pad, numpad, and data wheel."),
        _p("The data wheel responds to the host mouse wheel (over either the wheel "
           "widget or the screen panel)."),
        _p("Numpad keys on the host keyboard always route to the Kronos numpad "
           "regardless of keyboard capture state."),

        _h1("Keyboard Shortcuts"),
        _kbd_table(
            _kb_row("Q",           "Quit"),
            _kb_row("F",           "Toggle Fullscreen"),
            _kb_row("Z",           "Toggle Zoom Window"),
            _kb_row("A",           "Toggle Aspect Lock"),
            _kb_row("M",           "Toggle VGA Mirror"),
            _kb_row("F1",          "Toggle Help Window"),
            _kb_row("C",           "Toggle Calibration Mode"),
            _kb_row("F2 – F8",     "Mode Select (Setlist through Disk)"),
            _kb_row("Ctrl+S",      "Save Screenshot"),
            _kb_row("Ctrl+K",      "Open Command Palette"),
            _kb_row("Escape",      "Close Help / Exit Fullscreen / Send EXIT"),
        ),
        _p("All shortcuts are rebindable in <b>Settings → Key Bindings</b>."),

        _h1("Keyboard Capture"),
        _p("When keyboard capture is active (" + _green("⌨") + " in the status bar), "
           "all key presses are forwarded to the Kronos instead of triggering local "
           "shortcuts."),
        _p("Activate by clicking on the screen panel. Deactivate by clicking outside "
           "the screen, pressing Escape, or switching to another application."),
        _p("Disable keyboard forwarding entirely with " +
           _key("Keyboard Send") + " in Tools or the right-click menu on the keyboard indicator. "
           "Numpad keys always route to the Kronos."),

        _h1("Layout Presets"),
        _kbd_table(
            _kb_row("Full",     "Screen panel + full control surface (default)"),
            _kb_row("Focused",  "Screen panel only — control surface hidden"),
        ),
        _p("Switch via <b>View → Layout Preset</b> or the status bar right-click menu."),

        _h1("Window Size"),
        _p("Resize the window freely, or use the preset scales under "
           "<b>View → Window Size</b> (75% through 200% of the default 1590×650 layout)."),

        _h1("Fullscreen"),
        _p("Toggle fullscreen with " + _key("F") + " or <b>View → Fullscreen</b>. "
           "Press " + _key("Escape") + " or " + _key("F") + " again to exit."),

        _h1("Zoom Tool"),
        _p("The zoom loupe overlays a magnified region around the cursor. "
           "Adjust the default zoom level and loupe size in "
           "<b>Settings → View</b>. "
           "Scroll with " + _key("Ctrl+Wheel") + " to change zoom level on the fly."),

        _h1("Touch Calibration"),
        _p("If touch events land at the wrong position, enable calibration mode with "
           + _key("C") + " or <b>Tools → Calibration</b>."),
        _p("In calibration mode, left-click passes through as touch input. "
           "Drag grid intersections to correct per-node distortion. "
           "Right-click to place or remove bias dots."),
        _p(_key("S") + " save, " + _key("X") + " clear all, "
           + _key("R") + " reset mesh, "
           + _key("Ctrl+Z") + " / " + _key("Ctrl+Y") + " undo/redo, "
           + _key("Esc") + " exit calibration."),

        _h1("VGA Mirror"),
        _p("When connected, toggle VGA mirror with " + _key("M") + " or "
           "<b>Tools → Toggle VGA Mirror</b>. "
           "The mirror state is pushed to the daemon on every reconnect."),

        _h1("Bank Select"),
        _p("Change the Kronos bank from the <b>Bank Select</b> menu. "
           "I-A through I-G and U-A through U-GG are supported. "
           "U-AA through U-GG are chord banks (two buttons pressed simultaneously)."),

        _h1("File Manager"),
        _p("The Kronos filesystem is accessible via FTP. Enter credentials in "
           "<b>Settings → Connection</b>. The FTP server runs on the Kronos at port 21."),

        _h1("Settings"),
        _p("Open <b>Settings → Settings…</b> for full configuration:"),
        _kbd_table(
            _kb_row("General",      "Screenshot folder, VGA output, screensaver, reset"),
            _kb_row("Connection",   "IP address, stream/control ports, FTP credentials"),
            _kb_row("Streaming",    "Change-driven vs. pull mode; max frame rate"),
            _kb_row("View",         "Default zoom level and zoom window size"),
            _kb_row("Key Bindings", "Rebind all shortcuts"),
            _kb_row("Macros",       "Record and play back key sequences"),
            _kb_row("Debug",        "Debug logging; custom raw key mappings"),
        ),

        _h1("Command Palette"),
        _p("Press " + _key("Ctrl+K") + " or <b>Help → Command Palette</b> to "
           "open a searchable list of all actions and their current key bindings."),

        _h1("Screenshot"),
        _p("Save a screenshot with " + _key("Ctrl+S") + " or <b>Tools → Save Screenshot…</b>. "
           "Quick-save (no dialog) saves directly to the configured screenshot folder "
           "with a timestamped filename. Right-click the screen for both options."),
        _p("Configure the output folder in <b>Settings → General → Output folder</b>. "
           "Leave empty to save next to the app executable."),

        _h1("Status Bar"),
        _p("The status bar at the bottom shows:"),
        _kbd_table(
            _kb_row("Left area",    "Connection status and host IP — right-click for reconnect/disconnect"),
            _kb_row(_green("⌨"),    "Keyboard capture active — right-click to enable/disable"),
            _kb_row("FPS",          "Measured frame rate — right-click to set the max FPS limit"),
            _kb_row("Mode",         "Current Kronos mode — right-click to change mode"),
        ),

        "</body></html>",
    ]
    return "".join(parts)


class HelpWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Kronos ScreenRemote — Help")
        self.setMinimumSize(600, 520)
        self.resize(680, 640)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        browser = QTextBrowser()
        browser.setOpenExternalLinks(False)
        browser.setStyleSheet(f"QTextBrowser {{ background: {_BG}; border: none; }}")
        browser.setHtml(_build_html())
        layout.addWidget(browser)

        btns = QDialogButtonBox(QDialogButtonBox.Close)
        btns.rejected.connect(self.accept)
        layout.addWidget(btns)
