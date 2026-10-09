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
import Utils.theme as T

_C_BODY  = T.TEXT
_C_HEAD  = T.ACCENT
_C_KEY   = "#FFD246"     # key-cap highlight (intentional, kept)
_C_DIM   = T.TEXT_DIM
_C_TITLE = "#DDEEFF"     # title tint (intentional, kept)
_C_GREEN = "#AACC88"     # muted informational green (kept)
_C_RED   = "#FF8888"     # bright warning red (kept)

_BG = T.BG


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
        _p("1. Open Settings (<b>Settings → Settings…</b>) and enter your instrument IP address."),
        _p("2. Use <b>Connection → Connect</b>, or simply launch the app — it attempts "
           "to connect automatically."),
        _p("3. If no credentials are saved, a login dialog appears. Enter the FTP "
           "username and password for the instrument. The same credentials are used for "
           "both the screen stream and the File Manager."),
        _p("4. Once connected, the screen panel shows the live instrument display and the "
           "status bar reads <b>Connected — &lt;ip&gt;</b> with a green indicator."),
        _p("5. If the instrument IP changes or the connection drops, use "
           "<b>Connection → Connect</b> to reconnect."),

        _h1("Screen Panel  (centre)"),
        _p("The screen panel streams the instrument touchscreen display. The image is "
           "always letterboxed to preserve the original 4:3 aspect ratio."),
        _kbd_table(
            _kb_row("Click",           "Send a tap to the instrument touchscreen at that position."),
            _kb_row("Click and drag",  "Send a swipe gesture. Drag must exceed 8 instrument screen "
                                       "pixels before the touch-down is sent."),
            _kb_row("Mouse scroll",    "Turn the data wheel (works from anywhere in the window)."),
            _kb_row("Right Click",     "Access the context menu for quick actions."),
        ),

        _h1("Value Slider  (left panel)"),
        _p("The left panel mirrors the instrument front-panel <b>VALUE</b> slider and "
           "increment/decrement buttons."),
        _kbd_table(
            _kb_row("INC / DEC buttons", "Send a single increment or decrement step to the instrument."),
            _kb_row("Slider thumb",      "Drag up or down to send a continuous value (0–127). "
                                         "Top = 127, bottom = 0. The command is sent only when the "
                                         "value changes."),
        ),
        _p("The left panel is visible in the <b>Full</b> layout when value input is "
           "shown. It hides automatically in <b>Focused</b> layout or via "
           "View → Hide Value Input."),

        _h1("Control Surface  (right panel)"),
        _p("The right panel mirrors the physical instrument front panel. Clicking any "
           "button sends the corresponding hardware button press to the instrument."),
        _kbd_table(
            _kb_row("Mode buttons",   "Setlist / Combi / Program / Sequence / Sampling / Global / Disk. "
                                      "The lit button shows the current instrument operating mode."),
            _kb_row("Help / Compare", "Toggle buttons — each click presses the corresponding "
                                      "hardware button."),
            _kb_row("Number pad",     "Buttons 0–9, dash (–), and dot (.) send numeric entry."),
            _kb_row("Exit / Enter",   "Send the EXIT or ENTER hardware buttons."),
            _kb_row("Data wheel",     "Drag up or down to scroll. Mouse scroll wheel also works "
                                      "everywhere."),
        ),
        _p(_dim("On a Nautilus the panel switches to the Nautilus layout, and the <b>Mode Select</b> "
                "menu lists the front panel's own Mode, Page and A–F buttons instead of the seven "
                "Kronos modes (F is the Write/Save quick-access slot). Bank Select and the "
                "status-bar mode menu are Kronos-only and are greyed out on a Nautilus.")),

        _h1("Keyboard Shortcuts"),
        _p("These shortcuts work when the app window is focused and keyboard capture "
           "is not active. All shortcuts (except Ctrl combos) can be rebound in "
           "<b>Settings → Settings… → Keybindings</b>."),
        _kbd_table(
            _kb_row("F1",           "Open this help window"),
            _kb_row("F2 – F8",     "Mode Select (Setlist through Disk)"),
            _kb_row("C",           "Toggle Calibration Mode"),
            _kb_row("F",           "Toggle Fullscreen"),
            _kb_row("M",           "Toggle VGA Mirror"),
            _kb_row("Q",           "Quit"),
            _kb_row("Z",           "Toggle Zoom Window"),
            _kb_row("=  /  −",     "Zoom in / zoom out (enables zoom automatically if off)"),
            _kb_row("Escape",      "Send EXIT to Instrument (also dismisses overlays / exits fullscreen)"),
            _kb_row("Enter",       "Send ENTER to Instrument"),
            _kb_row("Ctrl+1 – Ctrl+5", "Window size: 75% / 100% / 125% / 150% / 200%"),
            _kb_row("Ctrl+K",      "Open Command Palette"),
            _kb_row("Ctrl+S",      "Quick Save Screenshot"),
            _kb_row("Ctrl+Shift+S", "Save Screenshot As…"),
            _kb_row("Ctrl+Z / Ctrl+Y", "Undo / redo (calibration mode)"),
            _kb_row("~ (fullscreen)", "Show / hide the menu bar while in fullscreen"),
        ),

        _h1("Keyboard Capture  (forwarding keys to the instrument)"),
        _p("Clicking inside the screen panel activates keyboard capture. "
           "While active, most keystrokes are forwarded to the instrument as if typed "
           "on a connected USB keyboard."),
        _kbd_table(
            _kb_row("Numpad 0–9",     "Press the matching number pad button on the instrument. "
                                      "The on-screen button shows a brief indent for visual confirmation."),
            _kb_row("Numpad – / .",    "Press the NUM_DASH or NUM_DOT control surface buttons."),
            _kb_row("Numpad Enter",    "Send ENTER to the instrument."),
            _kb_row("Escape",          "Send EXIT to the instrument."),
            _kb_row("Any other key",   "Forward as a USB keypress to the instrument kernel input system."),
        ),
        _p("The " + _green("⌨") + " indicator in the status bar shows capture state:"),
        _kbd_table(
            _kb_row(_green("⌨  (green)"),  "Capture active — keystrokes are forwarded to the instrument."),
            _kb_row(_dim("⌨/ (gray)"),     "Capture inactive — click the screen panel to enable."),
            _kb_row("<span style='color:#FF8888;'>⌨/ (red)</span>",
                    "Keyboard send disabled (Tools → Disable Remote Typing)."),
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
        _p("Corrects for touchscreen coordinate offset on the instrument display. "
           "Use this if tap positions feel consistently shifted relative to the image. "
           "Enable with " + _key("C") + " or <b>Tools → Calibration</b>. The calibration is "
           "<b>stored on the unit</b> (daemon 3.1.2 or later), not on this PC: it is read "
           "every time you connect and follows the instrument, and it can only be saved while "
           "connected. The bar at the bottom of the screen shows <b>[SAVED]</b> or "
           "<b>[UNSAVED]</b>."),
        _kbd_table(
            _kb_row("Click",           "Send a touch tap to the instrument. Current calibration applies."),
            _kb_row("Drag blue nodes", "Shift mesh nodes to correct systematic positional offsets "
                                       "(unsaved until you press S)."),
            _kb_row("Right-click",     "Add an indicator dot at that position, or remove the nearest "
                                       "one. Dot edits are saved to the unit immediately."),
            _kb_row("S",               "Save the mesh to the unit."),
            _kb_row("R",               "Reset the mesh to identity (no correction). Press S to keep it."),
            _kb_row("X",               "Clear all bias dots (saved immediately)."),
            _kb_row("Ctrl+Z / Ctrl+Y", "Undo / redo (Ctrl+Shift+Z also redoes)."),
            _kb_row(_key("C"),         "Exit calibration mode."),
        ),
        _p(_dim("Grid size (3×3, 4×4, 5×5) can be changed in "
                "Tools → Calibration Grid Size. Changing the grid size asks first, then clears "
                "existing calibration data. If a save fails (not connected, unit did not answer, "
                "too many dots, daemon too old) the reason appears in the status bar and the "
                "mesh stays [UNSAVED]; quitting with unsaved changes offers to save first.")),

        _h1("Test Mode"),
        _p("Access via <b>Settings → Debug → Enter Instrument Test Mode…</b> (Kronos only; hidden on a "
           "Nautilus). This sends the instrument into its built-in hardware test mode."),
        _p("<span style='color:#FF8888;font-weight:bold;'>Warning:</span> "
           "All unsaved changes on the instrument will be lost, and the instrument must be "
           "restarted after testing is complete. Only use this if you understand the "
           "risk."),

        _h1("VGA Mirror"),
        _p("When connected, toggle VGA mirror with " + _key("M") + " or "
           "via the command palette (" + _key("Ctrl+K") + "). "
           "The mirror state is pushed to the daemon on every reconnect. "
           "The default mirror setting can be changed in <b>Settings → General</b>."),

        _h1("Bank Select"),
        _p("Change the instrument bank from the <b>Bank Select</b> menu. "
           "Banks I-A through I-G and U-A through U-G correspond to the internal "
           "and user bank rows. U-XX banks (U-AA, U-BB, …) send a chord of both "
           "the U and I buttons simultaneously, selecting the combined bank slot."),
        _p(_dim("Bank select shortcuts are unassigned by default. Bind them in "
                "Settings → Settings… → Keybindings.")),

        _h1("File Manager"),
        _p("A dual-pane file browser for transferring files between your PC and the "
           "instrument over FTP. Uses the same credentials as the screen stream."),
        _kbd_table(
            _kb_row("Left pane",           "Local PC (starts at the Desktop folder)."),
            _kb_row("Right pane",          "Instrument filesystem (/ by default)."),
            _kb_row("Drag left → right",   "Upload files to the instrument."),
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
            _kb_row("Saved connections",      "<b>Settings → Connection</b>: <b>+</b> saves a named entry (IP address and FTP "
                                              "login), <b>-</b> removes it, <b>Edit…</b> changes it. Choosing an entry "
                                              "fills in the IP address, FTP username, password and port."),
            _kb_row("Instrument IP address",  "IP address of the instrument."),
            _kb_row("Change / Pull mode",     "Change: stream only when the instrument screen updates (recommended). "
                                              "Pull: poll at a fixed FPS; uses slightly more instrument CPU."),
            _kb_row("Max FPS",                "Frame-rate cap for Pull mode (1–15 fps)."),
            _kb_row("VGA Mirror",             "Enable VGA output mirroring on the instrument."),
            _kb_row("Screensaver Timeout",    "Seconds before the instrument display dims (0 = disabled)."),
            _kb_row("Prompt before quitting", "Show a confirmation dialog when closing the app."),
            _kb_row("Hide Data Input",         "Hide / show the data input panel (Full layout only)."),
            _kb_row("Hide Value Input",        "Hide / show the value input panel (Full layout only)."),
            _kb_row("Screenshot Directory",   "Default folder for Quick Save screenshots."),
            _kb_row("Debug Logging",          "Write verbose diagnostic output to the console. While it is "
                                              "ticked, the Debug tab also shows <b>Button Injector…</b> "
                                              "(send any named front-panel button; for mapping a Nautilus)."),
            _kb_row("Input Mapping",          "Map a host key to a raw instrument keycode (overrides the default "
                                              "key map immediately)."),
            _kb_row("Recent Connections",     "<b>Connection → Recent Connections</b> keeps the last five hosts "
                                              "and the FTP login that last worked for each."),
            _kb_row("Zoom Default Level",     "Initial magnification when the zoom window opens (2.5× – 10×)."),
            _kb_row("Zoom Window Size",       "Size of the zoom inset window as a fraction of the frame area."),
            _kb_row("Key Bindings",           "Rebind any shortcut listed in the Keyboard Shortcuts section above."),
            _kb_row("Librarian tab",          "Merge behavior, duplicate handling, <b>Full sync on launch</b> and "
                                              "<b>Force destructive write</b> — see the Librarian section below."),
            _kb_row("Sample Editor tab",      "Playback output device, and where a newly created zone goes."),
        ),

        _h1("MIDI / SysEx  (status bar + Tools → MIDI Monitor…)"),
        _p("The app listens to the instrument's live MIDI output through the daemon's MIDI bridge "
           "(port 9875). It drives the footer performance name, program-change follow, the on-screen "
           "VALUE slider mirror and the MIDI Monitor. The footer shows <b>TCP</b> when the bridge is "
           "connected and a pair of RX / TX dots that flash on traffic; click the dots to open the "
           "Monitor."),
        _p("<b>Tools → MIDI Monitor…</b> shows live MIDI and SysEx traffic with per-type filters, and a "
           "virtual piano that sends notes on the chosen <b>OUT CH</b> (remembered between sessions)."),
        _kbd_table(
            _kb_row("Monitor MIDI",              "Master switch. Off: nothing incoming is processed, the MIDI "
                                                 "Monitor and the footer dots are faded, and the Librarian "
                                                 "cannot sync. Takes effect immediately, while connected."),
            _kb_row("SysEx Poll on Changes",     "When a Program Change arrives that can't be decoded from the "
                                                 "stream, ask the instrument for the current performance (a Bank "
                                                 "Select / PC burst becomes one query). A bank-storage change "
                                                 "reported by the instrument always refreshes."),
            _kb_row("Pull Names on Program Change", "When you select a program/combi whose name isn't cached, "
                                                 "fetch just that name. Only where a fast scroll settles. Over "
                                                 "the daemon this can briefly flash the instrument display."),
            _kb_row("Proactive SysEx Polling",   "Re-query the current performance on a fixed interval (30 / 45 "
                                                 "/ 60 / 120 s) regardless of MIDI activity. Can slow the instrument "
                                                 "during the check-in — leave off unless you need it."),
            _kb_row("Value slider CC#",          "The controller number the instrument's VALUE slider transmits "
                                                 "(default 18). The on-screen slider follows it, except while "
                                                 "you are dragging it. 0 and 32 (Bank Select) are not allowed."),
        ),
        _p(_dim("These settings are in <b>Settings → MIDI/SysEx</b>. Current performance, program-change "
                "follow and the slider mirror read the instrument's MIDI stream, which is not decoded for a "
                "Nautilus yet; SysEx must be enabled on the instrument itself (GLOBAL › MIDI). A direct USB-MIDI "
                "connection is not supported by this app — only the network bridge.")),

        _h1("Librarian  (Tools → Librarian…)"),
        _p("Manages programs, combis, set lists, drum kits and wave sequences: sync a <b>Keyboard Library</b> with the instrument, "
           "open .pcg files, stage objects in the <b>Merge Window</b>, and place them back with "
           "dependency tracking. Browsing, staging and editing work offline; only Sync talks to "
           "the instrument."),
        _kbd_table(
            _kb_row("Keyboard Library", "The on-disk copy of the instrument's objects. Cut/Copy/Paste, Rename, "
                                        "Properties and Delete change this library only — the instrument is "
                                        "untouched until you sync."),
            _kb_row("Merge Window",     "A staging area. <b>Auto-Fill to Library</b> places everything staged "
                                        "into the next free slots of the right type; it sends nothing to the "
                                        "instrument. <b>Force Overwrite</b> places onto a slot another Combi or Set "
                                        "List still references (those referrers then point at the new object)."),
            _kb_row("Loaded PCG File",  "Open a .pcg (or pull one from the instrument) and move objects, with "
                                        "their dependencies, into the Merge Window."),
            _kb_row("Right-click a tree", "Expand / Collapse Selected or All, in all three panes."),
        ),
        _h2("Object Dependencies panel"),
        _p("Select one or more objects in any pane and the panel lists what they reference, nested "
           "references included: a Set List's Combis and their Programs, a Combi's Programs, and a "
           "Program's Drum Track. Sample banks a Program uses are listed too. Double-click a row, or "
           "right-click it and choose <b>More Info…</b>, to see who referenced it and what it in turn "
           "references."),
        _kbd_table(
            _kb_row("Red, bold rows", "Dependencies the Merge Window needs and nothing staged provides. They "
                                      "are always listed first, whatever is selected. Right-click one and "
                                      "choose <b>Search a PCG for this object…</b>: pick a .pcg and everything "
                                      "it holds of the missing objects is staged in the Merge Window (undo "
                                      "with Ctrl+Z), so the gap closes before you sync."),
            _kb_row("ROM rows",       "A reference into a read-only ROM bank (GM, g(1)–g(d)). Shown for "
                                      "completeness, never as missing: it resolves on the instrument."),
            _kb_row("INIT placeholder", "The reference is satisfied, but by an INIT Program rather than the "
                                      "sound the Combi expects."),
            _kb_row("Sample rows",    "Coloured by type: <span style='color:#D9C23A'>EXs</span>, "
                                      "<span style='color:#4FA3D8'>User / 3rd-party bank</span>, "
                                      "<span style='color:#5C7FA3'>Sampling Mode (RAM)</span>, "
                                      "<span style='color:#E0954A'>EXi external bank</span> (the legend sits "
                                      "under the panel). Factory ROM samples are not listed."),
        ),
        _p(_dim("EXs and 3rd-party bank names come from the EXs product catalog shipped with the app. A name "
                "identifies the product; it does not prove the pack is installed on your instrument. To pick up "
                "packs released later, drop a newer <b>exs_catalog.json</b> in the data folder. Drum Kit "
                "and Wave Sequence references are not shown yet.")),
        _h2("Sync button"),
        _p("One button whose label names what a plain click does; the <b>▾</b> beside it picks the mode, "
           "and your choice is remembered."),
        _kbd_table(
            _kb_row("2-Way Sync", "Pull the library from the instrument, then push every pending local change. "
                                  "Tick <b>Force Full Sync</b> to re-read every bank instead of only the banks "
                                  "whose digest changed."),
            _kb_row("Pull Only",  "Make the library a mirror of the instrument. Pending edits <i>and</i> slots "
                                  "marked for deletion are discarded first — you are asked before anything "
                                  "is lost, and it cannot be undone."),
            _kb_row("Push Only",  "Write every pending local change to the instrument without pulling. If a "
                                  "bank changed on the instrument since the last sync, nothing is written and "
                                  "you are asked whether to overwrite."),
        ),
        _h2("Banners"),
        _kbd_table(
            _kb_row("Red: Instrument not answering SysEx", "Sync is disabled until it answers. On the instrument: "
                                  "GLOBAL › MIDI, and check every MIDI Filter box. Press <b>Re-check</b> "
                                  "after fixing it."),
            _kb_row("Amber: conflicts", "Local changes whose banks changed on the instrument since the last "
                                  "pull were <b>not</b> pushed. Run a 2-Way or Pull Only sync to take the "
                                  "instrument copy, or <b>Resolve Conflicts</b> to push your copy over it."),
            _kb_row("Red: force destructive write ON", "Shown while that setting is armed."),
            _kb_row("Amber: warning", "The reason a sync was refused or only partly done. ✕ dismisses it."),
        ),
        _h2("Librarian settings  (Settings → Librarian)"),
        _p("<b>Full sync on launch</b> (off by default) pulls every bank as soon as the Librarian opens — "
           "a pull only, it never writes to the instrument. <b>Force destructive write</b> (off by default) "
           "treats the Keyboard Library as the source of truth: 2-Way Sync overwrites banks that changed "
           "on the instrument without asking. Front-panel edits made since the last pull are lost, and the "
           "pre-write backup does not cover them. It skips only the conflict check; missing-reference "
           "and bank-type refusals still apply."),
        _p(_dim("Delete and Clear Changes affect the library only; a fresh pull restores deleted "
                "objects. Clear History deletes the local audit log alone.")),

        _h1("Sample Editor  (Tools → Sample Editor…)"),
        _p("View and edit .KSC / .KMP / .KSF sample content: key ranges, loop points, flags and destructive "
           "waveform edits. Edits happen on a local copy; <b>File → Pull … from Instrument</b> brings content "
           "over FTP and <b>Push …</b> puts a saved file back where it came from."),
        _p("Open a .KSC with <b>File → Open</b> or drag a .KSC / .KMP onto the window; dropping an audio file "
           "imports it as a new zone. The tree on the left lists the open collections; pick a multisample "
           "from the MS dropdown, then a zone from the keymap, the Index box or the Sample dropdown."),
        _kbd_table(
            _kb_row("Keymap",            "Click a piano key to hear that key's zone (held, not latched). Drag a "
                                         "zone-bar boundary to resize a zone, drag a zone onto another to "
                                         "reorder, click a zone to select it. Ctrl+Click a key while Orig.Key "
                                         "or Top Key has focus to type it in."),
            _kb_row("SAMPLE panel",      "Index / Sample / Orig.Key / Top Key. <b>Create</b> adds an empty zone, "
                                         "<b>Import Sample…</b> decodes audio files into the collection and "
                                         "assigns the first, <b>Remove Sample</b> deletes the audio but keeps "
                                         "the key range. <b>Sample Shortcut</b> shares another sample's audio instead of "
                                         "copying it."),
            _kb_row("INSTRUMENT panel",      "Fields written into the .KSF: Reverse, +12dB Boost, Loop Enabled, "
                                         "Sample Start, Loop Start, Loop End and Loop Tune."),
            _kb_row("LOCAL EDITS panel", "Select / Move tool, Use Zero, Loop Lock, Split L/R and the destructive "
                                         "edits: Normalize, Amplify, Soften, Trim Silence, Reverse, Remove DC "
                                         "Offset, Insert Silence. Each acts on the selection, or the whole "
                                         "sample when nothing is highlighted. They only touch this app's "
                                         "in-memory copy and undo stack."),
        ),
        _kbd_table(
            _kb_row("Space",             "Play / stop from the scrub line (or Sample Start)."),
            _kb_row("Home / End",        "Move the scrub line to the start / end."),
            _kb_row("Ctrl+Z / Ctrl+Y",   "Undo / redo, in the order edits happened."),
            _kb_row("Ctrl+X / C / V",    "Cut / copy / paste in the waveform."),
            _kb_row("Ctrl+A",            "Select the whole sample."),
            _kb_row("Delete",            "Cut the selection; with none, delete the zone."),
            _kb_row("Ctrl++ / Ctrl+-",   "Zoom in / out. Ctrl+0 fits; so does a double-click on the waveform."),
            _kb_row("Ctrl+S",            "Save Changes."),
            _kb_row("Wheel on waveform", "Zoom around the cursor (turn off with <b>Scroll to Zoom</b>)."),
        ),
        _p("A stereo instrument (two multisamples with the same name and opposite -L / -R suffix) shows "
           "both channels stacked, and an edit to one mirrors onto the other. <b>Split L/R</b> edits only "
           "the pane you click; the Move tool then offsets one channel against the other."),
        _p(_dim("Save Changes writes every pending edit across all open collections; it is greyed out until "
                "something is unsaved. Edit → Revert KSC Changes / Revert ALL Changes discards edits instead. "
                "Playback device and new-zone defaults are in Settings → Sample Editor.")),

        _h1("Command Palette  (Ctrl+K)"),
        _p("A fuzzy-search launcher for all app commands. Start typing to filter; "
           "press Enter or click an entry to run it. Useful for infrequently used "
           "actions — bank select, layout changes, mirror toggle — without "
           "navigating menus."),

        _h1("Screenshot"),
        _p("Saves the current instrument screen frame as a PNG file. Requires an "
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
            _kb_row("Mode: …",             "Current instrument operating mode. Detected from the screen "
                                           "image when reference images are available; otherwise polled "
                                           "from the daemon every 1 s."),
        ),

        "</body></html>",
    ]
    return "".join(parts)


class HelpWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Keyboard Screen Remote — Help")
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
