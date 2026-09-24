"""
ModeDetector — identifies the active Kronos mode from the top-left 140×55 region
of the raw 8bpp frame, by comparing against reference PNGs.

Reference PNGs live in Resources/Refs/ relative to the C# project (one level up).
Transparent pixels (alpha=0) are ignored; row bands are enforced at load time.

Mode indices: Setlist=1  Combi=2  Program=3  Sequence=4  Sampling=5  Global=6  Disk=7
"""
from __future__ import annotations
import logging
import pathlib
from dataclasses import dataclass
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)


COLOR_TOLERANCE = 30    # ±30 per channel
MODE_THRESHOLD  = 0.85  # 85 % of masked pixels must match
HELP_THRESHOLD  = 0.97  # 97 % — help banner must be fully visible


@dataclass
class _PixelRef:
    x: int
    y: int
    r: int
    g: int
    b: int


class ModeDetector:
    def __init__(self, refs_dir: Optional[pathlib.Path] = None):
        if refs_dir is None:
            refs_dir = pathlib.Path(__file__).parent / "Resources" / "Refs"
        self._refs_dir = refs_dir
        self._mode_refs: list[Optional[list[_PixelRef]]] = [None] * 8  # indices 1–7
        self._help_ref:  Optional[list[_PixelRef]] = None
        self._loaded = False

    # ── Public API ─────────────────────────────────────────────────────────────

    def identify(self, frame8bpp: bytes, frame_w: int, lut: list[int]) -> int:
        """Return 1–7 on a confident mode match, 0 otherwise."""
        self._ensure_loaded()
        best_mode  = 0
        best_score = MODE_THRESHOLD - 1e-9
        for m in range(1, 8):
            s = self._score(self._mode_refs[m], frame8bpp, frame_w, lut)
            if s > best_score:
                best_score = s
                best_mode  = m
        return best_mode

    def is_help_active(self, frame8bpp: bytes, frame_w: int, lut: list[int]) -> bool:
        self._ensure_loaded()
        return self._score(self._help_ref, frame8bpp, frame_w, lut) >= HELP_THRESHOLD

    def has_any(self) -> bool:
        self._ensure_loaded()
        return any(r is not None for r in self._mode_refs[1:])

    # ── Internals ──────────────────────────────────────────────────────────────

    def _ensure_loaded(self):
        if not self._loaded:
            self._load_all()
            self._loaded = True

    def _load_all(self):
        for m in range(1, 8):
            self._mode_refs[m] = self._try_load(f"mode_{m}.png", 0, 26)
        self._help_ref = self._try_load("help.png", 27, 55)
        loaded = sum(1 for r in self._mode_refs[1:] if r is not None)
        log.debug("%d/7 refs loaded from %s", loaded, self._refs_dir)

    def _try_load(self, filename: str, y_min: int, y_max: int) -> Optional[list[_PixelRef]]:
        path = self._refs_dir / filename
        if not path.exists():
            return None
        try:
            return _load_ref(path, y_min, y_max)
        except Exception as e:
            log.warning("failed to load %s: %s", filename, e)
            return None

    @staticmethod
    def _score(refs: Optional[list[_PixelRef]],
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
            lg = (packed >> 8)  & 0xFF
            lb =  packed        & 0xFF
            if (abs(lr - p.r) <= COLOR_TOLERANCE and
                    abs(lg - p.g) <= COLOR_TOLERANCE and
                    abs(lb - p.b) <= COLOR_TOLERANCE):
                matches += 1
        return matches / len(refs)


_BLACK_LEVEL = 20   # per-channel ceiling for "near-black"

_mask_cache_key: Optional[tuple] = None
_mask_cache: Optional[np.ndarray] = None


def _black_palette_mask(lut: list[int]) -> np.ndarray:
    """256-entry bool mask: which palette indices are near-black. Cached on the
    LUT's contents, since the palette changes only on a handshake or a
    palette-editor edit, never per frame."""
    global _mask_cache_key, _mask_cache
    key = tuple(lut)
    if key != _mask_cache_key:
        packed = np.array(lut, dtype=np.uint32)
        _mask_cache = (((packed >> 16) & 0xFF) <= _BLACK_LEVEL) \
            & (((packed >> 8) & 0xFF) <= _BLACK_LEVEL) \
            & ((packed & 0xFF) <= _BLACK_LEVEL)
        _mask_cache_key = key
    return _mask_cache


def frame_black_fraction(frame8bpp: bytes, lut: list[int]) -> float:
    """Fraction of pixels that are near-black (all channels ≤ 20).

    Counts palette indices with bincount and weights them by the near-black
    mask, so the cost is one pass over the frame in C rather than 480k Python
    iterations — this runs on the GUI thread for every frame received.
    """
    if not frame8bpp:
        return 0.0
    counts = np.bincount(np.frombuffer(frame8bpp, dtype=np.uint8), minlength=256)
    return float(counts[_black_palette_mask(lut)].sum()) / len(frame8bpp)




# ── Combi Program-Edit Detector ────────────────────────────────────────────────

_COMBI_SCAN_X   = 696
_COMBI_SCAN_Y   = 39
_COMBI_THRESHOLD = 0.98

class CombiProgramEditDetector:
    """
    Detects the program-edit-from-Combi indicator at frame position (696, 39).
    Reference PNG: Resources/Refs/program_edit_from_combi.png (70×18, RGBA).
    Transparent pixels are skipped; ≥98% match within ±30 per channel required.
    """
    def __init__(self, refs_dir: Optional[pathlib.Path] = None):
        if refs_dir is None:
            refs_dir = pathlib.Path(__file__).parent / "Resources" / "Refs"
        self._refs_dir = refs_dir
        self._refs: Optional[list[_PixelRef]] = None
        self._loaded = False

    def is_active(self, frame8bpp: bytes, frame_w: int, lut: list[int]) -> bool:
        self._ensure_loaded()
        return self._score(frame8bpp, frame_w, lut) >= _COMBI_THRESHOLD

    def _ensure_loaded(self):
        if not self._loaded:
            self._loaded = True
            path = self._refs_dir / "program_edit_from_combi.png"
            if path.exists():
                try:
                    self._refs = _load_combi_ref(path)
                    log.debug("combi-edit ref: %d pixels from %s", len(self._refs), path.name)
                except Exception as e:
                    log.warning("combi-edit ref load failed: %s", e)

    def _score(self, frame8bpp: bytes, frame_w: int, lut: list[int]) -> float:
        refs = self._refs
        if not refs:
            return 0.0
        matches = 0
        for p in refs:
            fi = p.y * frame_w + p.x
            if fi < 0 or fi >= len(frame8bpp):
                continue
            packed = lut[frame8bpp[fi]]
            lr = (packed >> 16) & 0xFF
            lg = (packed >>  8) & 0xFF
            lb =  packed        & 0xFF
            if (abs(lr - p.r) <= COLOR_TOLERANCE and
                    abs(lg - p.g) <= COLOR_TOLERANCE and
                    abs(lb - p.b) <= COLOR_TOLERANCE):
                matches += 1
        return matches / len(refs)


def _load_combi_ref(path: pathlib.Path) -> list[_PixelRef]:
    """Load program_edit_from_combi.png; pixel coords are offset by SCAN_X/Y."""
    from PySide6.QtGui import QImage
    img = QImage(str(path))
    if img.isNull():
        raise ValueError(f"Cannot load {path}")
    img = img.convertToFormat(QImage.Format_ARGB32)
    w, h = img.width(), img.height()
    refs: list[_PixelRef] = []
    for y in range(h):
        for x in range(w):
            px = img.pixel(x, y)
            if ((px >> 24) & 0xFF) == 0:
                continue
            refs.append(_PixelRef(
                _COMBI_SCAN_X + x, _COMBI_SCAN_Y + y,
                (px >> 16) & 0xFF,
                (px >>  8) & 0xFF,
                 px        & 0xFF,
            ))
    return refs


def _load_ref(path: pathlib.Path, y_min: int, y_max: int) -> list[_PixelRef]:
    from PySide6.QtGui import QImage
    img = QImage(str(path))
    if img.isNull():
        raise ValueError(f"Cannot load {path}")
    # Format_ARGB32 gives pixel() as 0xAARRGGBB — reliable across all PySide6 versions
    img = img.convertToFormat(QImage.Format_ARGB32)
    w, h = img.width(), img.height()
    refs: list[_PixelRef] = []
    for y in range(max(y_min, 0), min(y_max + 1, h)):
        for x in range(w):
            px = img.pixel(x, y)          # QRgb = 0xAARRGGBB
            a  = (px >> 24) & 0xFF
            if a == 0:
                continue
            refs.append(_PixelRef(x, y,
                                  (px >> 16) & 0xFF,
                                  (px >>  8) & 0xFF,
                                   px        & 0xFF))
    return refs
