"""Multi-resolution min/max summary of one sample buffer — the standard
waveform "mipmap". Port of C#'s `Views/WaveformPyramid.cs`.

Built once per loaded sample (`SampleWaveformControl` caches it by array
identity); every later zoom/pan reads the summary instead of the raw PCM,
which is what stops zooming out on a long sample from having to touch every
frame in view on every repaint.

Deliberately NOT decimation ("keep every Nth sample"): dropping samples
drops PEAKS, so the drawn envelope would shrink and swallow short transients
entirely at any zoom level coarse enough to skip past them. Min/max buckets
are lossless for this purpose instead — the min/max OF a set of min/max
pairs is exactly the min/max of the samples underneath them, so the envelope
drawn from any level is the same envelope the raw PCM would draw, differing
only in which bucket a column boundary happens to land in.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import numpy as np

# The finest summary covers 256 frames; each level above it is 4x coarser.
_BASE_BUCKET = 256
_LEVEL_FACTOR = 4


@dataclass(frozen=True)
class Level:
    min: np.ndarray
    max: np.ndarray
    bucket: int


def _bucket_min_max(values: np.ndarray, bucket: int) -> "tuple[np.ndarray, np.ndarray]":
    """min/max per `bucket`-sized (last one possibly shorter) contiguous
    run of `values` — `np.minimum/maximum.reduceat` handles the ragged
    final bucket correctly with no padding, matching C#'s own
    `e = Math.Min(s + bucket, len)` clamp exactly."""
    starts = np.arange(0, len(values), bucket, dtype=np.int64)
    mn = np.minimum.reduceat(values, starts).astype(np.int16)
    mx = np.maximum.reduceat(values, starts).astype(np.int16)
    return mn, mx


class WaveformPyramid:
    def __init__(self, samples: np.ndarray):
        self.samples = samples
        self._levels: List[Level] = []
        n = len(samples)
        prev_min: Optional[np.ndarray] = None
        prev_max: Optional[np.ndarray] = None
        prev_bucket = 1
        bucket = _BASE_BUCKET
        while bucket <= n:
            if prev_min is None:
                mn, mx = _bucket_min_max(samples, bucket)
            else:
                # Folding the level below, not rescanning the PCM — this is
                # what keeps the whole pyramid to roughly a single pass.
                step = bucket // prev_bucket
                mn, _ = _bucket_min_max(prev_min, step)
                _, mx = _bucket_min_max(prev_max, step)
            self._levels.append(Level(mn, mx, bucket))
            prev_min, prev_max, prev_bucket = mn, mx, bucket
            bucket *= _LEVEL_FACTOR

    def pick(self, samples_per_column: float) -> Optional[Level]:
        """The coarsest level whose buckets still fit within one pixel
        column, so a column never has to combine more than _LEVEL_FACTOR of
        them. None means "zoomed in past the finest summary" — at that
        point a column spans under 256 frames and reading the raw samples
        directly is already cheap."""
        best: Optional[Level] = None
        for level in self._levels:
            if level.bucket > samples_per_column:
                break
            best = level
        return best
