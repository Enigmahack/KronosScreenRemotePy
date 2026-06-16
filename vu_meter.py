"""
VU meter widget + optional sounddevice audio capture.

VuMeterWidget: QPainter-based two-channel level meter (mirrors C# VuMeterBar).
AudioCapture: QThread that streams audio levels from a sounddevice input.

sounddevice is an optional dependency.  When absent, the widget simply shows a
flat/silent state and the device list returns [].
"""
from __future__ import annotations
import math
import time
from typing import List, Optional, Tuple

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget

# ── Meter constants (mirrors C# VuMeterBar) ────────────────────────────────────

_MIN_DB     = -60.0
_MAX_DB     =   0.0
_YELLOW_DB  = -12.0
_RED_DB     =  -6.0
_BAR_GAP    =   2       # px between L and R bars
_DECAY_RATE =  22.0     # dB/s fall-off
_PEAK_HOLD  =   1.5     # seconds
_PEAK_DECAY =   3.5     # dB/s peak fall-off after hold expires
_INPUT_TRIM =   7.5     # dB trim applied to raw values (WASAPI headroom offset)

# Two-segment scale: bottom 60% = MinDb…YellowDb, top 40% = YellowDb…MaxDb
def _db_to_frac(db: float) -> float:
    if db <= _MIN_DB:  return 0.0
    if db >= _MAX_DB:  return 1.0
    if db <= _YELLOW_DB:
        return 0.6 * (db - _MIN_DB) / (_YELLOW_DB - _MIN_DB)
    return 0.6 + 0.4 * (db - _YELLOW_DB) / (_MAX_DB - _YELLOW_DB)


class VuMeterWidget(QWidget):
    """Two-channel horizontal VU meter. Click to reset clip latch."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(110, 14)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Audio output levels — click to reset clip")

        self._level_db = [_MIN_DB, _MIN_DB]
        self._peak_db  = [_MIN_DB, _MIN_DB]
        self._hold_t   = [0.0,     0.0    ]
        self._clip     = [False,   False  ]
        self._last_t   = time.monotonic()

    def update_levels(self, left_db: float, right_db: float):
        now = time.monotonic()
        dt  = now - self._last_t
        self._last_t = now
        self._advance(0, left_db  + _INPUT_TRIM, dt)
        self._advance(1, right_db + _INPUT_TRIM, dt)
        self.update()

    def _advance(self, ch: int, raw: float, dt: float):
        if raw >= self._level_db[ch]:
            self._level_db[ch] = raw
        else:
            self._level_db[ch] = max(raw, self._level_db[ch] - _DECAY_RATE * dt)

        if raw >= self._peak_db[ch]:
            self._peak_db[ch] = raw
            self._hold_t[ch]  = _PEAK_HOLD
        else:
            self._hold_t[ch] -= dt
            if self._hold_t[ch] <= 0:
                self._peak_db[ch] -= _PEAK_DECAY * dt
                if self._peak_db[ch] < self._level_db[ch]:
                    self._peak_db[ch] = self._level_db[ch]

        if raw >= 0.0:
            self._clip[ch] = True

    def reset_clip(self):
        self._clip[0] = self._clip[1] = False
        self.update()

    def reset(self):
        self._level_db = [_MIN_DB, _MIN_DB]
        self._peak_db  = [_MIN_DB, _MIN_DB]
        self._hold_t   = [0.0,     0.0    ]
        self._clip     = [False,   False  ]
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.reset_clip()

    def paintEvent(self, _ev):
        p = QPainter(self)
        w, h   = self.width(), self.height()
        bar_h  = max(1, (h - _BAR_GAP) // 2)
        for ch in range(2):
            y = 0 if ch == 0 else bar_h + _BAR_GAP
            self._draw_bar(p, w, y, bar_h,
                           self._level_db[ch], self._peak_db[ch], self._clip[ch])
        p.end()

    def _draw_bar(self, p: QPainter, w: int, y: int, h: int,
                  level: float, peak: float, clip: bool):
        p.fillRect(0, y, w, h, QColor(5, 18, 5))

        fill_x   = int(_db_to_frac(level)     * w)
        yellow_x = int(_db_to_frac(_YELLOW_DB) * w)
        red_x    = int(_db_to_frac(_RED_DB)    * w)

        if fill_x > 0:
            g = min(fill_x, yellow_x)
            if g > 0:
                p.fillRect(0, y, g, h, QColor(0, 224, 64))
            if fill_x > yellow_x:
                yl = min(fill_x, red_x) - yellow_x
                if yl > 0:
                    p.fillRect(yellow_x, y, yl, h, QColor(216, 200, 0))
            if fill_x > red_x:
                p.fillRect(red_x, y, fill_x - red_x, h, QColor(232, 32, 0))

        peak_x = int(_db_to_frac(peak) * w)
        if peak_x > 1:
            p.fillRect(peak_x - 1, y, 2, h, QColor(255, 255, 255))

        if clip:
            p.fillRect(w - 2, y, 2, h, QColor(232, 32, 0))


# ── Audio device listing ───────────────────────────────────────────────────────

def list_audio_devices() -> List[Tuple[str, str]]:
    """Return list of (device_id, display_name) for input-capable devices."""
    try:
        import sounddevice as sd
        devices = []
        for i, dev in enumerate(sd.query_devices()):
            if dev["max_input_channels"] > 0:
                name = dev["name"]
                devices.append((str(i), f"{i}: {name}"))
        return devices
    except Exception:
        return []


# ── Audio capture thread ───────────────────────────────────────────────────────

class AudioCapture(QThread):
    """Captures audio from a sounddevice input and emits dBFS levels."""
    levels_updated = Signal(float, float)  # left_db, right_db

    def __init__(self, device_id: Optional[str] = None, parent=None):
        super().__init__(parent)
        self._device_id = device_id
        self._running   = False

    def set_device(self, device_id: Optional[str]):
        self._device_id = device_id

    def stop(self):
        self._running = False
        self.quit()
        self.wait(2000)

    def run(self):
        self._running = True
        try:
            import sounddevice as sd
            import numpy as np
        except ImportError:
            return

        def callback(indata, frames, time_info, status):
            if not self._running:
                raise sd.CallbackStop()
            n_ch = indata.shape[1]
            ch0 = indata[:, 0]
            ch1 = indata[:, min(1, n_ch - 1)]
            rms_l = float(np.sqrt(np.mean(ch0 ** 2) + 1e-12))
            rms_r = float(np.sqrt(np.mean(ch1 ** 2) + 1e-12))
            db_l  = 20.0 * math.log10(rms_l)
            db_r  = 20.0 * math.log10(rms_r)
            self.levels_updated.emit(db_l, db_r)

        kwargs: dict = {"channels": 2, "callback": callback, "blocksize": 2048}
        if self._device_id is not None:
            try:
                kwargs["device"] = int(self._device_id)
            except ValueError:
                pass

        try:
            with sd.InputStream(**kwargs):
                while self._running:
                    self.msleep(100)
        except Exception:
            pass
