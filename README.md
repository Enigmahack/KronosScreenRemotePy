# KronosScreenRemotePy

A Python application for remotely viewing and controlling a **Korg Kronos** synthesizer over Ethernet. It streams the Kronos display in real time, forwards touch/button input back to the device, and provides supplementary tools for file management, audio monitoring, and local display calibration.

> **Note:** This application requires the companion daemon running on the Kronos hardware.
> See [KronosScreenRemoteDaemon](https://github.com/Enigmahack/KronosScreenRemoteDaemon) for setup instructions.

| Repository | Description |
|---|---|
| [KronosScreenRemotePy](https://github.com/Enigmahack/KronosScreenRemotePy) | This repo — Python Desktop Client |
| [KronosScreenRemoteDaemon](https://github.com/Enigmahack/KronosScreenRemoteDaemon) | Kronos-side daemon (required) |

> **Shared context**: this project is part of a larger Kronos RE/modding
> ecosystem (this client + [KronosScreenRemote](../KronosScreenRemote/) (C#,
> the feature-parity reference) + [kronosology](../kronosology/) +
> [KronosScreenRemoteDaemon](../KronosScreenRemoteDaemon/)). Cross-project
> architecture, shared dev environments, credentials/access pointers, and
> agent/tooling policy live in
> [`/home/share/PROJECT_BRAIN/BRAIN.md`](../PROJECT_BRAIN/BRAIN.md) —
> check there before duplicating knowledge into this repo.

---

## Features

- **Live Screen Streaming** — 800×600 8-bit indexed color at up to 15 FPS via TCP; supports full-frame (pull) and change-only modes for bandwidth efficiency
- **Value Slider** — Left-panel INC/DEC buttons and draggable 0–127 value slider mirroring the Kronos front-panel VALUE control; double-click to snap to center (64)
- **Remote Control** — Virtual button panel (mode keys, number pad, data wheel, bank selects) with drag, scroll, and keyboard-shortcut support
- **Mode Detection** — Reference-image OCR to identify the active Kronos operating mode automatically
- **Audio VU Meter** — WASAPI real-time level monitoring (L/R peak + RMS) with device selection
<img width="1415" height="513" alt="2026-06-19 18_10_07-Kronos ScreenRemote — 192 168 100 15" src="https://github.com/user-attachments/assets/85ffbbdc-869f-4fa0-9eb4-fcbc56ebf740" />


- **Touch Calibration** — 3x3 - 5x5 warp mesh with bilinear interpolation for accurate touch-to-screen mapping
<img width="1415" height="513" alt="2026-06-19 18_32_56-GreenshotCalibration" src="https://github.com/user-attachments/assets/5b3e4752-845d-4813-9742-6ed7ff9e85e6" />


- **FTP File Manager** — Browse, upload, and download files on the Kronos SD card with conflict resolution
<img width="1408" height="512" alt="2026-06-19 18_16_51-Kronos ScreenRemote FileManager— 192 168 100 15" src="https://github.com/user-attachments/assets/dbcad5b3-76cb-4fc7-add5-6df8849c80c9" />


- **Test Mode** — Enter the Kronos built-in hardware test mode for diagnostics (Tools menu)
- **Zoom & Layout Presets** — Configurable window sizes (75–200%), fullscreen, always-on-top; data input (right) and value input (left) panels can be independently hidden in Full mode or expanded/collapsed via dedicated rails in Focused mode, with panel state remembered across sessions
<img width="952" height="512" alt="2026-06-19 18_14_45-Kronos ScreenRemote Views— 192 168 100 15" src="https://github.com/user-attachments/assets/d35bfd38-4f8f-4564-9079-45cc449bf4bd" />


- **Hardware Stats Monitoring** — Monitor hard drive space, CPU core usage, Fan speed, CPU temperatures, and more.
<img width="1415" height="513" alt="2026-06-19 18_11_31-Kronos ScreenRemote_PERF — 192 168 100 15" src="https://github.com/user-attachments/assets/9ef81aaf-ea4d-4937-81dd-b210cacd44de" />

- **SysEx / MIDI Bridge** — Live decode of Kronos SysEx (performance name/mode/bank tracking, bulk Object Dump collection) over the daemon's MIDI bridge port
- **Librarian** — Program/Combi/Set-List slot move-and-swap tool with referrer-aware relocation, plus a full **Local Library**: an offline-first, dependency-aware mirror of the Kronos's banks (pull from hardware or a `.pcg` file, stage and dedup via a content-addressed merge cache, place into a local library, then Sync/Commit back to hardware) — see [Local Library](#local-library) below. **Ctrl+Z undo** rolls back every LOCAL (pre-Commit) edit; a successful Sync/Commit clears the stack. Merge→Local placement dedups byte-identical content (per-type toggle in Settings → Librarian). Category/Sub-Category in the Properties dialog show the real names from the Global object when synced.
- **Set List Viewer** — Decoded Set List contents with real hardware slot colors
- **Image Adjustments** — Tone (brightness / contrast / gamma) and saturation curves plus 3×3 unsharp-mask sharpening, applied to the streamed frame via the palette color table
- **Boot Phase Detection** — Reference-image detection of the Kronos boot loading phase, with client-side boot splash overlay when the daemon isn't compositing
- **Input Tester** — Maps host keys to raw Kronos keycodes and records observed hardware behavior (Tools menu)
- **Command Palette** (Ctrl+K) — Live filter-as-you-type launcher for every rebindable action
- **Sequencer Transport + Tap Tempo** — Footer transport row (Locate/Rewind/Fast-Forward/Pause/Record/Start, Write/Save) and tap-tempo, each also reachable via keybind
- **Paste Clipboard to Kronos** — Types the system clipboard's text content to the Kronos via the on-screen keyboard command path

---

## Requirements

| Requirement | Minimum |
|---|---|
| Python | 3.12+ |
| OS | Windows 10/11, macOS, or Linux (Windows recommended) |
| Network | Ethernet connection to a Korg Kronos with the companion daemon installed |

---

## Dependencies

| Package | Purpose |
|---|---|
| [PySide6](https://pypi.org/project/PySide6/) | Qt 6 GUI framework (widgets, threading, signals) |
| [numpy](https://pypi.org/project/numpy/) | Required. Video pipeline (tone/sharpen curves, RGB565 decode for Nautilus) and VU meter buffer processing |
| [sounddevice](https://pypi.org/project/sounddevice/) | Optional — WASAPI audio capture for the VU meter |

> **sounddevice** is the only optional dependency. The application launches and operates without it; the VU meter will simply be unavailable. **numpy** is required — the app will not start without it.

### Installation

```bash
# Clone the repository
git clone https://github.com/Enigmahack/KronosScreenRemotePy.git
cd KronosScreenRemotePy

# Create and activate a virtual environment
python -m venv .venv

# Windows (PowerShell)
.venv\Scripts\Activate.ps1

# macOS / Linux
source .venv/bin/activate

# Install dependencies
pip install PySide6 numpy sounddevice

# Run
python main.py
```

On **macOS**, if `pip install pyside6` fails with a wheel error, ensure you are inside an activated virtual environment before installing.

### Where your data is kept

Settings, caches and the Local Library live in the per-user application-data
directory, **not** next to the program — so the app can be run from a network
share, a read-only image or a USB stick without any of those becoming a
requirement:

| Platform | Location |
|---|---|
| Windows | `%LOCALAPPDATA%\KronosScreenRemote` |
| macOS | `~/Library/Application Support/KronosScreenRemote` |
| Linux | `$XDG_CONFIG_HOME/KronosScreenRemote` (else `~/.config/KronosScreenRemote`) |

To put it somewhere else — a shared library on a file server, a portable install
on the same stick as the program — pass `--data-dir <path>` or set the
`KRONOS_DATA_DIR` environment variable:

```bash
python main.py --data-dir /mnt/kronos/library
```

An existing install that already keeps its data in the program folder carries on
using it, unchanged, for as long as that folder stays writable. If it stops being
writable, the app moves to the per-user directory and offers, once, to copy the
old data across (the originals are never deleted).

---

## Project Structure

The codebase is organized into packages by role; see `PROJECT_STRUCTURE.md`
for the full breakdown. Summary:

```
KronosScreenRemotePy/
  main.py                          Application entry point

  Core/         Device communication: ctrl_client.py (persistent TCP control
                sender), stream_receiver.py (handshake + v3 frame decoding),
                midi_bridge.py (MIDI bridge client), sysex_service.py
                (SysEx decode + passive name/mode/bank tracking),
                kronos_sysex.py

  Models/       app_settings.py (AppSettings + keybinds), storage.py (JSON
                persistence), models.py (Keybind, PaletteEntry, CalMesh),
                cal_text.py (CAL_GET/CAL_SET serialization)

  Views/        main_window.py (primary window: rendering, input, menus),
                librarian_shell_window.py (Local Library shell), settings_
                window.py (9 tabs), sysex_tool_window.py, perf_window.py,
                help_window.py, about_dialog.py, testing_dialogs.py
                (Input Tester / Button Injector), main_window_dialogs.py

  Data/         Local Library subsystem: local_library_store.py (blob store
                + index + oplog), changeset_sync.py (Sync/Commit), pcg_file.py,
                merge_cache.py, dependency_scanner.py, library_pull_pipeline.py,
                librarian_model.py, librarian_undo.py, blank_template_store.py,
                session_dependency_clipboard.py

  Objects/      object_body.py, global_body.py, erase_body.py — object-body
                parsing/decoding shared by the Librarian

  Rendering/    overlay_renderer.py (zoom/calibration/palette-editor paint
                helpers), control_surface.py (virtual button panel),
                vu_meter.py (optional audio capture + VU widget)

  Commands/     command_palette.py (Ctrl+K launcher), batch_clipboard.py,
                session_dependency_clipboard.py

  Tools/        file_manager.py, mode_detector.py, boot_phase_detector.py,
                setlist_data.py, sysex_dump_collector.py, dependency_scanner.py

  Utils/        theme.py, image_adjust.py, key_map.py, char_map.py
```

`Views/audio_*`/`Core/audio_*`/`Views/sample_editor_window.py` and related
are a separate, still-in-progress port of the C# app's Librarian/Sample
Editor sample-management feature — not yet wired to real audio hardware
workflows.

### Local Library

The Local Library is an offline-first, dependency-aware mirror of the Kronos's
Program/Combi/Set-List banks, ported from the C# client's
`Core/LocalLibrary/`+`Core/Pcg/` subsystem. Workflow: **Pull** (from live
hardware, bank-digest-diffed, or from a `.pcg` file) -> **Merge** (a
content-addressed staging area that recursively resolves an object's
references and dedups identical content across sources) -> **Place** into the
Local Library (single-item or batch, with referrer-aware repointing) ->
**Sync Library** (pull then push) or **Commit Changes** (push only). A
locally-edited object is never silently overwritten by a pull or a stale
digest — it's flagged conflicted instead. Deletion is a real hardware
write of a captured blank-body template, since the Kronos protocol has no
delete opcode. See the module list above (`pcg_file.py` through
`changeset_sync.py`) for the implementation, and
[`/home/share/PROJECT_BRAIN/projects/kronos_screen_remote_py.md`](../PROJECT_BRAIN/projects/kronos_screen_remote_py.md)
for the fuller architecture writeup.

---

## Connecting to a Kronos

1. Ensure the Kronos is connected to your local network and its **Global > Ethernet** settings have a valid IP address.
2. Launch **KronosScreenRemotePy** and enter the Kronos IP in the connection dialog.
3. The application connects on **TCP 7373** (screen stream) and **TCP 7374** (control commands).
4. FTP access uses port **21** (configurable in Settings) with the credentials configured on the Kronos.
5. On first connect you will be prompted for FTP credentials; these are saved for subsequent sessions.

---

## File Manager

Open via **Connection > File Manager** or right-click the frame and select **File Manager**.

- **Left pane** — local filesystem with drive selector (Windows) or root/home (Linux/macOS)
- **Right pane** — Kronos filesystem via FTP
- **Transfer files** — select files and click the toolbar buttons, use the right-click context menu, or drag files between panes
- **Move files** — drag files onto a folder within the same pane, or onto the Up button to move to the parent directory; cut/paste also moves within the same host
- **Keyboard shortcuts** — Ctrl+C/X/V (copy/cut/paste), Ctrl+A (select all), Del (delete), F2 (rename), F5 (refresh), Backspace (navigate up), Enter (open folder)
- **Column sorting** — click column headers to sort by name, size, or date

---

## Keyboard Shortcuts

| Shortcut | Action |
|---|---|
| F1 | Open help window |
| F2–F8 | Switch Kronos operating mode (Setlist through Disk) |
| A | Toggle aspect lock |
| C | Toggle calibration grid overlay |
| F | Toggle fullscreen |
| M | Toggle VGA mirror |
| Q | Quit |
| Z | Toggle zoom window |
| = / − | Zoom in / zoom out (enables zoom automatically if off) |
| Esc | Send EXIT to Kronos / exit fullscreen / dismiss overlays |
| Enter | Send ENTER to Kronos |
| Ctrl+1–5 | Window size: 75% / 100% / 125% / 150% / 200% |
| Ctrl+K | Open command palette |
| Ctrl+S | Quick save screenshot |
| Ctrl+Shift+S | Save screenshot as |
| Ctrl+Scroll | Adjust zoom level |
| ~ (fullscreen) | Show / hide menu bar while in fullscreen |
| Ctrl+Z (Librarian) | Undo last local edit |
| Seq Locate/Rewind/FF/Pause/Rec/Start, Tap Tempo | Sequencer transport + tap tempo (unbound by default; assign in Settings → Key Bindings) |

All shortcuts (except Ctrl combos) are rebindable via **Settings → Settings… → Keybindings**. Click in the frame to capture keyboard input for forwarding to the Kronos. **Settings → General** adds a *Reverse mouse scrolling direction* toggle that swaps which wheel direction turns the Kronos data wheel CW vs CCW.

---

## License

All rights reserved. This source code is provided for reference purposes only.

---

## Contributing

Issues and pull requests are welcome. Please open an issue first for any significant change so the approach can be discussed before implementation.
