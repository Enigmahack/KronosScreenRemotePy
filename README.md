# KronosScreenRemotePy

A python application for remotely viewing and controlling a **Korg Kronos** synthesizer over Ethernet. It streams the Kronos display in real time, forwards touch/button input back to the device, and provides supplementary tools for audio monitoring,  and local display calibration.

> **Note:** This application requires the companion daemon running on the Kronos hardware.
> See [KronosScreenRemoteDaemon](https://github.com/Enigmahack/KronosScreenRemoteDaemon) for setup instructions.

| Repository | Description |
|---|---|
| [KronosScreenRemotePy](https://github.com/Enigmahack/KronosScreenRemotePy) | This repo — Python Desktop Client |
| [KronosScreenRemoteDaemon](https://github.com/Enigmahack/KronosScreenRemoteDaemon) | Kronos-side daemon (required) |

---

## Features

- **Live Screen Streaming** — 800×600 8-bit indexed color at up to 15 FPS via TCP; supports full-frame (pull) and change-only modes for bandwidth efficiency
- **Remote Control** — Virtual button panel (mode keys, number pad, data wheel, bank selects) with drag, scroll, and keyboard-shortcut support
- **Touch Calibration** — 5×5 warp mesh with bilinear interpolation for accurate touch-to-screen mapping
- **Mode Detection** — Reference-image OCR to identify the active Kronos operating mode automatically
- **Audio VU Meter** — WASAPI real-time level monitoring (L/R peak + RMS) with device selection
- **Command Palette** — Searchable keyboard-driven command interface (Ctrl+K)
- **Zoom & Layout Presets** — Configurable window sizes (75–200%), fullscreen, always-on-top, and collapsible control rail

---

## Requirements

### Runtime

| Requirement | Minimum |
|---|---|
| Python Runtime | Python 3.12 or higher|
| PySide6 Runtime | PySide6 |

### Build

| Requirement | Version |
|---|---|
TBD

---

## Dependencies

| Package | Version | Purpose |
|---|---|---|
TBD

The application requires PySide6. If you have pip installed, simply run "pip install pyside6", however for Mac you may need to do the following: 

First, create and activate a virtual environment using 
`python3 -m venv .venv && source .venv/bin/activate` 

then 
`run pip install pyside6`

---

## Building

``` Provided git is installed on your OS, with Python 3.12 or better
# Clone the repository
git clone https://github.com/Enigmahack/KronosScreenRemotePy.git
cd KronosScreenRemotePy

# Run the main.py:
python -m venv .venv && source .venv/bin/activate
python ./main.py

or

python3 -m venv .venv && source .venv/bin/activate
python3 ./main.py

```

---

## Project Structure

```
TBD
```


---

## Connecting to a Kronos

1. Ensure the Kronos is connected to your local network and its **Global > Ethernet** settings have a valid IP address.
2. Launch **KronosScreenRemotePy** and enter the Kronos IP in the connection bar.
3. The application connects on **TCP 7373** (screen stream) and **TCP 7374** (control commands).
4. Access uses the standard FTP port **21** with the credentials configured on the Kronos.

---

## Keyboard Shortcuts

| Shortcut | Action |
|---|---|
| Ctrl+K | Open command palette |
| F1–F8 | Switch Kronos operating mode |
| Ctrl+1–5 | Window size preset (75%–200%) |
| C | Toggle calibration grid overlay |
| W | Enter warp/mesh editing mode |
| F | Toggle fullscreen |

Shortcuts are rebindable via **Settings → Keybinds**.

---

## License

All rights reserved. This source code is provided for reference purposes only.

---

## Contributing

Issues and pull requests are welcome. Please open an issue first for any significant change so the approach can be discussed before implementation.
