"""The single centralized big-endian/little-endian boundary for .KSF sample
data — port of C#'s Core/Sample/KsfPcm.cs.

KSF PCM is big-endian 16-bit signed on disk; everything else in this app
(numpy, WAV, audio playback) is native/little-endian. Every place PCM crosses
into/out of the KSF layer should route through here rather than reinterpreting
KsfSample.pcm's raw bytes directly.
"""
from __future__ import annotations

import numpy as np


def to_host_order(big_endian_pcm: bytes) -> np.ndarray:
    """Big-endian 16-bit signed PCM bytes -> a native-order int16 numpy array."""
    arr = np.frombuffer(big_endian_pcm, dtype=">i2")
    return arr.astype(np.int16)


def to_big_endian_bytes(host_order_pcm: np.ndarray) -> bytes:
    """Native-order int16 samples -> big-endian 16-bit signed PCM bytes."""
    return np.asarray(host_order_pcm, dtype=np.int16).astype(">i2").tobytes()
