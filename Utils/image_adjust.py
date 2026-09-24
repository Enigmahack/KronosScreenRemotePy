"""
Image-adjustment math — Python port of Rendering/ImageAdjust.cs.

Tone (brightness / contrast / gamma) and saturation collapse into a 256-entry
per-channel curve, folded into the display colour table so the whole frame is
adjusted for free (no per-pixel cost beyond the palette lookup already done).
Sharpen is the only spatial operation: a 3×3 unsharp mask over the RGB frame,
run once per frame via numpy.

Colour packing matches the C#: 0x00RRGGBB.
"""
from __future__ import annotations

import numpy as np
from PySide6.QtGui import QImage

# Unsharp-mask strength at the top of the 0..100 sharpen slider.
MAX_SHARPEN = 1.25


def tone_is_identity(brightness: int, contrast: int, gamma: float) -> bool:
    return brightness == 0 and contrast == 0 and abs(gamma - 1.0) < 1e-6


def is_identity(brightness: int, contrast: int, gamma: float,
                saturation: int, sharpen: int = 0) -> bool:
    return tone_is_identity(brightness, contrast, gamma) and saturation == 0 and sharpen == 0


def _clamp(v: int, lo: int, hi: int) -> int:
    return lo if v < lo else hi if v > hi else v


def build_tone_curve(brightness: int, contrast: int, gamma: float) -> bytes:
    """256-entry per-channel curve: contrast (about mid-grey) → brightness → gamma.

    brightness/contrast in [-100,100]; gamma > 0 (1.0 = linear).
    """
    cf   = 1.0 + _clamp(contrast, -100, 100) / 100.0     # 0..2 contrast gain
    bo   = _clamp(brightness, -100, 100) / 100.0 * 0.5   # ±0.5 lightness offset
    invg = 1.0 / min(10.0, max(0.1, gamma))
    curve = bytearray(256)
    for i in range(256):
        n = i / 255.0
        n = (n - 0.5) * cf + 0.5 + bo
        if n < 0.0:
            n = 0.0
        elif n > 1.0:
            n = 1.0
        n = n ** invg
        v = int(n * 255.0 + 0.5)
        curve[i] = 0 if v < 0 else 255 if v > 255 else v
    return bytes(curve)


def saturation_factor(saturation: int) -> float:
    """saturation in [-100,100] → factor 0..2 (1.0 = unchanged, 0 = grey, 2 = double)."""
    return 1.0 + _clamp(saturation, -100, 100) / 100.0


def _clamp8(v: float) -> int:
    i = int(v + 0.5)
    return 0 if i < 0 else 255 if i > 255 else i


def apply_to_channel(r: int, g: int, b: int, curve: bytes, sat_factor: float) -> int:
    """Apply the tone curve, then saturation, to one (r,g,b) → packed 0x00RRGGBB."""
    rr, gg, bb = curve[r], curve[g], curve[b]
    if sat_factor != 1.0:
        luma = 0.299 * rr + 0.587 * gg + 0.114 * bb
        rr = _clamp8(luma + (rr - luma) * sat_factor)
        gg = _clamp8(luma + (gg - luma) * sat_factor)
        bb = _clamp8(luma + (bb - luma) * sat_factor)
    return (rr << 16) | (gg << 8) | bb


def sharpen_rgb32(img: QImage, amount: float) -> QImage:
    """3×3 unsharp mask over a Format_RGB32 QImage → new sharpened QImage.

    dst = src + amount * (src − boxblur3(src)), with edge-replicated sampling so
    the border is sharpened consistently rather than darkened (matches the C#).
    Format_RGB32 is 0xAARRGGBB, i.e. bytes B,G,R,A in little-endian memory; the
    alpha byte is left untouched.
    """
    if amount <= 0.0:
        return img
    w, h = img.width(), img.height()
    if w <= 0 or h <= 0:
        return img
    if img.format() != QImage.Format.Format_RGB32:
        img = img.convertToFormat(QImage.Format.Format_RGB32)

    bpl = img.bytesPerLine()
    # constBits() → read-only buffer of bpl*h bytes; reshape rows then take w*4.
    raw = np.frombuffer(img.constBits(), dtype=np.uint8).reshape(h, bpl)
    src = raw[:, : w * 4].reshape(h, w, 4).astype(np.float32)   # BGRA

    bgr = src[:, :, :3]
    p = np.pad(bgr, ((1, 1), (1, 1), (0, 0)), mode="edge")
    blur = (p[:-2, :-2] + p[:-2, 1:-1] + p[:-2, 2:]
            + p[1:-1, :-2] + p[1:-1, 1:-1] + p[1:-1, 2:]
            + p[2:, :-2] + p[2:, 1:-1] + p[2:, 2:]) / 9.0

    out = src.copy()
    out[:, :, :3] = np.clip(bgr + amount * (bgr - blur) + 0.5, 0.0, 255.0)
    result = out.astype(np.uint8)

    buf = result.tobytes()
    # QImage(buf) references buf; .copy() detaches into QImage-owned memory.
    return QImage(buf, w, h, QImage.Format.Format_RGB32).copy()
