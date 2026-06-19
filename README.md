# KronosScreenRemotePy

A Python application for remotely viewing and controlling a **Korg Kronos** synthesizer over Ethernet. It streams the Kronos display in real time, forwards touch/button input back to the device, and provides supplementary tools for file management, audio monitoring, and local display calibration.

> **Note:** This application requires the companion daemon running on the Kronos hardware.
> See [KronosScreenRemoteDaemon](https://github.com/Enigmahack/KronosScreenRemoteDaemon) for setup instructions.

| Repository | Description |
|---|---|
| [KronosScreenRemotePy](https://github.com/Enigmahack/KronosScreenRemotePy) | This repo — Python Desktop Client |
| [KronosScreenRemoteDaemon](https://github.com/Enigmahack/KronosScreenRemoteDaemon) | Kronos-side daemon (required) |

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
| [numpy](https://pypi.org/project/numpy/) | Audio buffer processing for the VU meter |
| [sounddevice](https://pypi.org/project/sounddevice/) | WASAPI audio capture for the VU meter |

> **numpy** and **sounddevice** are only required for the audio VU meter feature. The application launches and operates without them; the VU meter will simply be unavailable.

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

---

## Project Structure

```
KronosScreenRemotePy/
  main.py               Application entry point
  main_window.py        Primary window — frame rendering, input, menus
  control_surface.py    Virtual button panel / data wheel widget
  stream_receiver.py    TCP stream client — handshake, frame decoding
  ctrl_client.py        UDP control command sender
  file_manager.py       Dual-pane FTP file manager window
  settings_window.py    Settings dialog (7 tabs)
  app_settings.py       AppSettings dataclass and keybind definitions
  storage.py            JSON persistence for settings, calibration, palette
  overlay_renderer.py   Paint helpers for zoom, calibration, palette editor
  mode_detector.py      Frame-based Kronos mode/help detection
  models.py             Shared data models (Keybind, PaletteEntry, CalMesh)
  key_map.py            Qt key → Linux keycode mapping tables
  vu_meter.py           WASAPI audio capture and VU meter widget
  perf_window.py        Performance / keyboard info window
  help_window.py        Help overlay content
  about_dialog.py       About dialog
```

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
| + / − | Zoom in / zoom out (enables zoom automatically if off) |
| Esc | Send EXIT to Kronos / exit fullscreen / dismiss overlays |
| Enter | Send ENTER to Kronos |
| Ctrl+1–5 | Window size: 75% / 100% / 125% / 150% / 200% |
| Ctrl+K | Open command palette |
| Ctrl+S | Quick save screenshot |
| Ctrl+Shift+S | Save screenshot as |
| Ctrl+Scroll | Adjust zoom level |
| ~ (fullscreen) | Show / hide menu bar while in fullscreen |

All shortcuts (except Ctrl combos) are rebindable via **Settings → Settings… → Keybindings**. Click in the frame to capture keyboard input for forwarding to the Kronos.

---

## License

All rights reserved. This source code is provided for reference purposes only.

---

## Contributing

Issues and pull requests are welcome. Please open an issue first for any significant change so the approach can be discussed before implementation.
