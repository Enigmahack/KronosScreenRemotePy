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
        _p("1. Open Settings (<b>Settings → Settings…</b>) and enter your Kronos IP address."),
        _p("2. Use <b>Connection → Connect</b>, or simply launch the app — it attempts "
           "to connect automatically."),
        _p("3. If no credentials are saved, a login dialog appears. Enter the FTP "
           "username and password for the Kronos. The same credentials are used for "
           "both the screen stream and the File Manager."),
        _p("4. Once connected, the screen panel shows the live Kronos display and the "
           "status bar reads <b>Connected — &lt;ip&gt;</b> with a green indicator."),
        _p("5. If the Kronos IP changes or the connection drops, use "
           "<b>Connection → Connect</b> to reconnect."),

        _h1("Screen Panel  (centre)"),
        _p("The screen panel streams the Kronos touchscreen display. The image is "
           "scaled to fill the panel while optionally preserving the original 4:3 "
           "aspect ratio (" + _key("A") + " or <b>View → Aspect Lock</b>)."),
        _kbd_table(
            _kb_row("Click",           "Send a tap to the Kronos touchscreen at that position."),
            _kb_row("Click and drag",  "Send a swipe gesture. Drag must exceed 8 Kronos screen "
                                       "pixels before the touch-down is sent."),
            _kb_row("Mouse scroll",    "Turn the data wheel (works from anywhere in the window)."),
            _kb_row("Right Click",     "Access the context menu for quick actions."),
        ),

        _h1("Value Slider  (left panel)"),
        _p("The left panel mirrors the Kronos front-panel <b>VALUE</b> slider and "
           "increment/decrement buttons."),
        _kbd_table(
            _kb_row("INC / DEC buttons", "Send a single increment or decrement step to the Kronos."),
            _kb_row("Slider thumb",      "Drag up or down to send a continuous value (0–127). "
                                         "Top = 127, bottom = 0. The command is sent only when the "
                                         "value changes."),
        ),
        _p("The left panel is visible in the <b>Full</b> layout when value input is "
           "shown. It hides automatically in <b>Focused</b> layout or via "
           "View → Hide Value Input."),

        _h1("Control Surface  (right panel)"),
        _p("The right panel mirrors the physical Kronos front panel. Clicking any "
           "button sends the corresponding hardware button press to the Kronos."),
        _kbd_table(
            _kb_row("Mode buttons",   "Setlist / Combi / Program / Sequence / Sampling / Global / Disk. "
                                      "The lit button shows the current Kronos operating mode."),
            _kb_row("Help / Compare", "Toggle buttons — each click presses the corresponding "
                                      "hardware button."),
            _kb_row("Number pad",     "Buttons 0–9, dash (–), and dot (.) send numeric entry."),
            _kb_row("Exit / Enter",   "Send the EXIT or ENTER hardware buttons."),
            _kb_row("Data wheel",     "Drag up or down to scroll. Mouse scroll wheel also works "
                                      "everywhere."),
        ),

        _h1("Keyboard Shortcuts"),
        _p("These shortcuts work when the app window is focused and keyboard capture "
           "is not active. All shortcuts (except Ctrl combos) can be rebound in "
           "<b>Settings → Settings… → Keybindings</b>."),
        _kbd_table(
            _kb_row("F1",           "Open this help window"),
            _kb_row("F2 – F8",     "Mode Select (Setlist through Disk)"),
            _kb_row("A",           "Toggle Aspect Lock"),
            _kb_row("C",           "Toggle Calibration Mode"),
            _kb_row("F",           "Toggle Fullscreen"),
            _kb_row("M",           "Toggle VGA Mirror"),
            _kb_row("Q",           "Quit"),
            _kb_row("Z",           "Toggle Zoom Window"),
            _kb_row("+  /  −",     "Zoom in / zoom out (enables zoom automatically if off)"),
            _kb_row("Escape",      "Send EXIT to Kronos (also dismisses overlays / exits fullscreen)"),
            _kb_row("Enter",       "Send ENTER to Kronos"),
            _kb_row("Ctrl+1 – Ctrl+5", "Window size: 75% / 100% / 125% / 150% / 200%"),
            _kb_row("Ctrl+K",      "Open Command Palette"),
            _kb_row("Ctrl+S",      "Quick Save Screenshot"),
            _kb_row("Ctrl+Shift+S", "Save Screenshot As…"),
            _kb_row("Ctrl+Z / Ctrl+Y", "Undo / redo (calibration mode)"),
            _kb_row("~ (fullscreen)", "Show / hide the menu bar while in fullscreen"),
        ),

        _h1("Keyboard Capture  (forwarding keys to the Kronos)"),
        _p("Clicking inside the screen panel activates keyboard capture. "
           "While active, most keystrokes are forwarded to the Kronos as if typed "
           "on a connected USB keyboard."),
        _kbd_table(
            _kb_row("Numpad 0–9",     "Press the matching number pad button on the Kronos. "
                                      "The on-screen button shows a brief indent for visual confirmation."),
            _kb_row("Numpad – / .",    "Press the NUM_DASH or NUM_DOT control surface buttons."),
            _kb_row("Numpad Enter",    "Send ENTER to the Kronos."),
            _kb_row("Escape",          "Send EXIT to the Kronos."),
            _kb_row("Any other key",   "Forward as a USB keypress to the Kronos kernel input system."),
        ),
        _p("The " + _green("⌨") + " indicator in the status bar shows capture state:"),
        _kbd_table(
            _kb_row(_green("⌨  (green)"),  "Capture active — keystrokes are forwarded to the Kronos."),
            _kb_row(_dim("⌨/ (gray)"),     "Capture inactive — click the screen panel to enable."),
            _kb_row("<span style='color:#FF8888;'>⌨/ (red)</span>",
                    "Keyboard send disabled (Tools → Disable Keyboard Send)."),
        ),
        _p("Click outside the screen panel — on the control surface, wheel, or "
           "menu bar — to release keyboard capture."),

        _h1("Layout Presets  (View → Layout Preset)"),
        _kbd_table(
            _kb_row("Full",     "Value slider, screen, and control surface side by side (default)."),
            _kb_row("Focused",  "Screen fills the window. A narrow rail on the right edge can "
                                "be clicked to temporarily overlay the control surface."),
        ),

        _h1("Window Size  (View → Window Size  or  Ctrl+1–5)"),
        _p("Scales the entire window to 75%, 100%, 125%, 150%, or 200%. "
           "The value slider, screen panel, and control surface all scale together. "
           "Fullscreen overrides this setting."),
        _p("<b>View → Always on Top</b> keeps the window in front of all other applications."),

        _h1("Fullscreen"),
        _p("Toggle fullscreen with " + _key("F") + " or <b>View → Fullscreen</b>. "
           "Maximises the window with no title bar. The control surface is still "
           "accessible in fullscreen (unless the layout preset hides it)."),
        _kbd_table(
            _kb_row("~ (tilde)",  "Show or hide the menu bar while in fullscreen."),
            _kb_row("F  or  Esc", "Exit fullscreen and restore the previous window state."),
        ),

        _h1("Zoom Tool"),
        _p("Toggle with " + _key("Z") + " or <b>View → Zoom Window</b>. "
           "Displays a magnified window that follows the mouse cursor over the "
           "screen panel. Press " + _key("+") + " to zoom in and " + _key("−") +
           " to zoom out in 0.5× steps (range: 2.5× – 10×). "
           "Pressing " + _key("+") + " enables zoom automatically if it is off."),

        _h1("Touch Calibration"),
        _p("Corrects for touchscreen coordinate offset on the Kronos display. "
           "Use this if tap positions feel consistently shifted relative to the image. "
           "Enable with " + _key("C") + " or <b>Tools → Calibration</b>."),
        _h2("Observe mode"),
        _kbd_table(
            _kb_row("Click",       "Send a touch tap to the Kronos. Current calibration applies."),
            _kb_row("Right-click", "Add an indicator dot at that position, or remove the nearest."),
            _kb_row("W",           "Enter Warp mode to edit the correction mesh."),
            _kb_row(_key("C"),     "Exit calibration mode."),
        ),
        _h2("Warp mode"),
        _kbd_table(
            _kb_row("Drag blue nodes", "Shift mesh nodes to correct systematic positional offsets."),
            _kb_row("Right-click",     "Remove the nearest bias dot."),
            _kb_row("S",              "Save the mesh to disk."),
            _kb_row("R",              "Reset the mesh to identity (no correction)."),
            _kb_row("X",              "Clear all bias dots."),
            _kb_row("W",              "Return to Observe mode."),
        ),
        _p(_dim("Grid size (3×3, 4×4, 5×5) can be changed in "
                "Tools → Calibration Grid Size. Changing the grid size clears "
                "existing calibration data.")),

        _h1("Test Mode"),
        _p("Access via <b>Tools → Enter Kronos Test Mode</b>. This sends the Kronos "
           "into its built-in hardware test mode."),
        _p("<span style='color:#FF8888;font-weight:bold;'>Warning:</span> "
           "All unsaved changes on the Kronos will be lost, and the Kronos must be "
           "restarted after testing is complete. Only use this if you understand the "
           "risk."),

        _h1("VGA Mirror"),
        _p("When connected, toggle VGA mirror with " + _key("M") + " or "
           "via the command palette (" + _key("Ctrl+K") + "). "
           "The mirror state is pushed to the daemon on every reconnect. "
           "The default mirror setting can be changed in <b>Settings → General</b>."),

        _h1("Bank Select"),
        _p("Change the Kronos bank from the <b>Bank Select</b> menu. "
           "Banks I-A through I-G and U-A through U-G correspond to the internal "
           "and user bank rows. U-XX banks (U-AA, U-BB, …) send a chord of both "
           "the U and I buttons simultaneously, selecting the combined bank slot."),
        _p(_dim("Bank select shortcuts are unassigned by default. Bind them in "
                "Settings → Settings… → Keybindings.")),

        _h1("File Manager"),
        _p("A dual-pane file browser for transferring files between your PC and the "
           "Kronos over FTP. Uses the same credentials as the screen stream."),
        _kbd_table(
            _kb_row("Left pane",           "Local PC (starts at the Desktop folder)."),
            _kb_row("Right pane",          "Kronos filesystem (/ by default)."),
            _kb_row("Drag left → right",   "Upload files to the Kronos."),
            _kb_row("Drag right → left",   "Download files to your PC."),
            _kb_row("Double-click folder", "Navigate into it."),
            _kb_row("Backspace",           "Go up to the parent folder."),
            _kb_row("F2",                  "Rename the selected item."),
            _kb_row("F5",                  "Refresh the active pane."),
            _kb_row("Del",                 "Delete the selected item."),
            _kb_row("Ctrl+A",              "Select all items in the active pane."),
        ),
        _p(_dim("When a file already exists at the destination, a conflict dialog "
                "offers Rename / Overwrite / Skip / Cancel with an option to apply "
                "the choice to all remaining conflicts.")),

        _h1("Settings"),
        _p("Open <b>Settings → Settings…</b> for full configuration:"),
        _kbd_table(
            _kb_row("Kronos Host",            "IP address of the Kronos."),
            _kb_row("Stream Port",            "TCP port for the screen stream (default: 7373)."),
            _kb_row("Ctrl Port",              "TCP port for control commands (default: 7374)."),
            _kb_row("Change / Pull mode",     "Change: stream only when the Kronos screen updates (recommended). "
                                              "Pull: poll at a fixed FPS; uses slightly more Kronos CPU."),
            _kb_row("Max FPS",                "Frame-rate cap for Pull mode (1–15 fps)."),
            _kb_row("VGA Mirror",             "Enable VGA output mirroring on the Kronos."),
            _kb_row("Screensaver Timeout",    "Seconds before the Kronos display dims (0 = disabled)."),
            _kb_row("Prompt before quitting", "Show a confirmation dialog when closing the app."),
            _kb_row("Hide Data Input",         "Hide / show the data input panel (Full layout only)."),
            _kb_row("Hide Value Input",        "Hide / show the value input panel (Full layout only)."),
            _kb_row("Screenshot Directory",   "Default folder for Quick Save screenshots."),
            _kb_row("Debug Logging",          "Write verbose diagnostic output to the console."),
            _kb_row("Zoom Default Level",     "Initial magnification when the zoom window opens (2.5× – 10×)."),
            _kb_row("Zoom Window Size",       "Size of the zoom inset window as a fraction of the frame area."),
            _kb_row("Keybindings",            "Rebind any shortcut listed in the Keyboard Shortcuts section above."),
        ),

        _h1("Command Palette  (Ctrl+K)"),
        _p("A fuzzy-search launcher for all app commands. Start typing to filter; "
           "press Enter or click an entry to run it. Useful for infrequently used "
           "actions — bank select, layout changes, mirror toggle — without "
           "navigating menus."),

        _h1("Screenshot"),
        _p("Saves the current Kronos screen frame as a PNG file. Requires an "
           "active connection."),
        _kbd_table(
            _kb_row("Save Screenshot… (Ctrl+S)", "Shows a save dialog to choose filename and location."),
            _kb_row("Quick Save Screenshot",      "Saves instantly to the Screenshot Directory (or desktop if unset)."),
            _kb_row("Copy Frame to Clipboard",    "Copies the current frame to the system clipboard."),
        ),
        _p(_dim("Use Tools → Open Screenshots Folder to browse previously saved files.")),

        _h1("Status Bar"),
        _p("The status bar at the bottom of the window shows:"),
        _kbd_table(
            _kb_row("Coloured dot + text", "Connection state: green = connected, amber = connecting, gray = disconnected."),
            _kb_row("Change / Pull",       "Active streaming mode for the current connection."),
            _kb_row("N.N fps",             "Measured incoming frame rate while connected."),
            _kb_row("Keyboard Info",       "Opens a keyboard info pane displaying CPU, memory, "
                                           "temperature, and storage stats."),
            _kb_row("VU meter",            "Shows the level of a local audio device (e.g. your DAW output). "
                                           "Click the ▾ button to pick the device. Choice is saved."),
            _kb_row("Mode: …",             "Current Kronos operating mode. Detected from the screen "
                                           "image when reference images are available; otherwise polled "
                                           "from the daemon every 1 s."),
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
