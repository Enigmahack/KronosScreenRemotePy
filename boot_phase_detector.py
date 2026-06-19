"""
BootPhaseDetector — identifies the Kronos boot loading phase from a 200x30
pixel region at (302, 530) in the raw 800x600 8bpp frame.

Phases advance strictly forward:
  PreloadKSC (1) → BankData (2) → Finishing (3)

Reference PNGs are 800x600 screenshots in Resources/BootPhase/; only the
scan region is compared.  Scoring mirrors ModeDetector: >=98% of scan pixels
must match within ±30 per channel.
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass
from enum import IntEnum
from typing import List, Optional


class Phase(IntEnum):
    NONE = 0
    PRELOAD_KSC = 1
    BANK_DATA = 2
    FINISHING = 3


_SCAN_X, _SCAN_Y = 302, 530
_SCAN_W, _SCAN_H = 200, 30
_MATCH_THRESHOLD = 0.98
_COLOR_TOL = 30


@dataclass
class _PixelRef:
    x: int
    y: int
    r: int
    g: int
    b: int


class BootPhaseDetector:
    def __init__(self, refs_dir: Optional[pathlib.Path] = None):
        if refs_dir is None:
            refs_dir = pathlib.Path(__file__).parent / "Resources" / "BootPhase"
        self._refs_dir = refs_dir
        self._preload_ref: Optional[List[_PixelRef]] = None
        self._bankdata_ref: Optional[List[_PixelRef]] = None
        self._finishing_ref: Optional[List[_PixelRef]] = None
        self._loaded = False

    def identify(self, frame8bpp: bytes, frame_w: int, lut: list[int]) -> Phase:
        self._ensure_loaded()
        if self._score(self._finishing_ref, frame8bpp, frame_w, lut) >= _MATCH_THRESHOLD:
            return Phase.FINISHING
        if self._score(self._bankdata_ref, frame8bpp, frame_w, lut) >= _MATCH_THRESHOLD:
            return Phase.BANK_DATA
        if self._score(self._preload_ref, frame8bpp, frame_w, lut) >= _MATCH_THRESHOLD:
            return Phase.PRELOAD_KSC
        return Phase.NONE

    def _ensure_loaded(self):
        if not self._loaded:
            self._load_all()
            self._loaded = True

    def _load_all(self):
        self._preload_ref = self._try_load("phase_preload.png")
        self._bankdata_ref = self._try_load("phase_bankdata.png")
        self._finishing_ref = self._try_load("phase_finishing.png")
        count = sum(1 for r in (self._preload_ref, self._bankdata_ref, self._finishing_ref)
                    if r is not None)
        print(f"[boot] {count}/3 phase refs loaded from {self._refs_dir}")

    def _try_load(self, filename: str) -> Optional[List[_PixelRef]]:
        path = self._refs_dir / filename
        if not path.exists():
            return None
        try:
            return _load_ref(path)
        except Exception as e:
            print(f"[boot] failed to load {filename}: {e}")
            return None

    @staticmethod
    def _score(refs: Optional[List[_PixelRef]],
               frame8bpp: bytes, frame_w: int, lut: list[int]) -> float:
        if not refs:
            return 0.0
        matches = 0
        for p in refs:
            fi = p.y * frame_w + p.x
            if fi < 0 or fi >= len(frame8bpp):
                continue
            packed = lut[frame8bpp[fi]]
            lr = (packed >> 16) & 0xFF
            lg = (packed >> 8) & 0xFF
            lb = packed & 0xFF
            if (abs(lr - p.r) <= _COLOR_TOL and
                    abs(lg - p.g) <= _COLOR_TOL and
                    abs(lb - p.b) <= _COLOR_TOL):
                matches += 1
        return matches / len(refs)


def _load_ref(path: pathlib.Path) -> List[_PixelRef]:
    from PySide6.QtGui import QImage
    img = QImage(str(path))
    if img.isNull():
        raise ValueError(f"Cannot load {path}")
    img = img.convertToFormat(QImage.Format_ARGB32)
    w, h = img.width(), img.height()
    refs: List[_PixelRef] = []
    for y in range(_SCAN_Y, min(_SCAN_Y + _SCAN_H, h)):
        for x in range(_SCAN_X, min(_SCAN_X + _SCAN_W, w)):
            px = img.pixel(x, y)
            refs.append(_PixelRef(
                x, y,
                (px >> 16) & 0xFF,
                (px >> 8) & 0xFF,
                px & 0xFF,
            ))
    return refs
