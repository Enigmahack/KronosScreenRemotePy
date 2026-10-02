r"""
Sample Editor waveform edits — port of Core/Sample/Dsp/*.cs.

Every effect is `apply(pcm, sample_rate) -> pcm` over HOST-order (not KSF's on-disk big-endian)
int16 numpy arrays. Arrays are IMMUTABLE by convention: an effect returns a new array (or the very
same one for a no-op) and never writes into its input. That is load-bearing — SampleEditUndo keeps
snapshots by reference and relies on a PCM-mutating edit replacing the array rather than mutating it
in place (the same contract C#'s KsfSample.SetSamples documents).

Numeric semantics match C# exactly where it is observable:
  * float -> int16 conversion TRUNCATES toward zero after clamping ((short)Math.Clamp(...)),
  * DC-offset rounds half-to-even (Math.Round's default),
  * peak/abs are computed in a wider int so |-32768| = 32768 does not overflow.

Tempo/pitch: C# wraps SoundTouch (no Python wheel exists). `change_tempo_and_pitch` is an in-house
WSOLA time-stretch + `soxr` resample — the same family of algorithm, NOT bit-identical output.
"""
from __future__ import annotations

from typing import Optional, Protocol, Tuple

import numpy as np

_I16_MIN, _I16_MAX = -32768, 32767


class SampleEffect(Protocol):
    def apply(self, pcm: np.ndarray, sample_rate: int) -> np.ndarray: ...


def _to_i16(values: np.ndarray) -> np.ndarray:
    """Clamp then truncate toward zero — C#'s (short)Math.Clamp(x, short.MinValue, short.MaxValue)."""
    return np.clip(values, _I16_MIN, _I16_MAX).astype(np.int16)


def _range(pcm: np.ndarray, start: Optional[int], end: Optional[int]) -> Tuple[int, int]:
    s = int(np.clip(0 if start is None else start, 0, len(pcm)))
    e = int(np.clip(len(pcm) if end is None else end, s, len(pcm)))
    return s, e


class CropEffect:
    """Keeps [start, end) and nothing else. Callers that care about LoopStart/LoopEnd/SampleStart
    staying in range own re-deriving them (KsfSample.to_bytes never auto-adjusts)."""

    def __init__(self, start_frame: int, end_frame_exclusive: int):
        self.start, self.end = start_frame, end_frame_exclusive

    def apply(self, pcm, sample_rate):
        s, e = _range(pcm, self.start, self.end)
        return pcm[s:e].copy()


class DcOffsetEffect:
    """Subtracts the buffer's mean, recentering on zero. Whole-buffer only, deliberately: removing it
    from a selection would create a click-producing step at each edge."""

    def apply(self, pcm, sample_rate):
        off = self.measure_offset(pcm)
        if off == 0:
            return pcm
        return _to_i16(pcm.astype(np.int32) - off)

    @staticmethod
    def measure_offset(pcm: np.ndarray) -> int:
        if len(pcm) == 0:
            return 0
        return int(round(float(pcm.astype(np.int64).sum()) / len(pcm)))   # half-to-even, like Math.Round


class GainAdjustEffect:
    """Fixed-dB gain (the Amplify/Soften presets) over [start, end). Clips on overshoot — the honest
    outcome of 'amplify by a fixed amount'; silently re-normalizing would make it Normalize."""

    def __init__(self, decibels: float, start_frame: Optional[int] = None, end_frame_exclusive: Optional[int] = None):
        self.db, self.start, self.end = decibels, start_frame, end_frame_exclusive

    def apply(self, pcm, sample_rate):
        factor = 10.0 ** (self.db / 20.0)
        s, e = _range(pcm, self.start, self.end)
        out = pcm.copy()
        out[s:e] = _to_i16(pcm[s:e].astype(np.float64) * factor)
        return out


class GainNormalizeEffect:
    """Scales [start, end) so its peak hits target_peak_db (default -0.1 dBFS). A silent range is
    returned unchanged. `shared_peak`, when given, is used INSTEAD of measuring this call's own range —
    the stereo case: a pair is normalized as ONE track, by whichever channel is louder."""

    def __init__(self, target_peak_db: float = -0.1, shared_peak: Optional[int] = None,
                 start_frame: Optional[int] = None, end_frame_exclusive: Optional[int] = None):
        self.target_db, self.shared_peak = target_peak_db, shared_peak
        self.start, self.end = start_frame, end_frame_exclusive

    def apply(self, pcm, sample_rate):
        if len(pcm) == 0:
            return pcm
        s, e = _range(pcm, self.start, self.end)
        if e == s:
            return pcm.copy()
        peak = self.shared_peak if self.shared_peak is not None else self.compute_peak(pcm, s, e)
        if peak == 0:
            return pcm.copy()
        scale = (10.0 ** (self.target_db / 20.0)) * _I16_MAX / peak
        out = pcm.copy()
        out[s:e] = _to_i16(pcm[s:e].astype(np.float64) * scale)
        return out

    @staticmethod
    def compute_peak(pcm: np.ndarray, start: int = 0, end: Optional[int] = None) -> int:
        seg = pcm[start:len(pcm) if end is None else end]
        return int(np.abs(seg.astype(np.int32)).max()) if len(seg) else 0


class ReverseEffect:
    """Plays [start, end) backwards in place — length unchanged, so every marker stays meaningful.
    Physically rewrites the PCM (survives the save); unrelated to the loop-reverse preview flag."""

    def __init__(self, start_frame: int, end_frame_exclusive: int):
        self.start, self.end = start_frame, end_frame_exclusive

    def apply(self, pcm, sample_rate):
        s, e = _range(pcm, self.start, self.end)
        out = pcm.copy()
        out[s:e] = pcm[s:e][::-1]
        return out


class SilenceEffect:
    """Zeroes [start, end) without removing it (the 'mute this bit' counterpart to Cut)."""

    def __init__(self, start_frame: int, end_frame_exclusive: int):
        self.start, self.end = start_frame, end_frame_exclusive

    def apply(self, pcm, sample_rate):
        s, e = _range(pcm, self.start, self.end)
        out = pcm.copy()
        out[s:e] = 0
        return out


class SilenceTrimEffect:
    """Trims leading/trailing frames whose |amplitude| <= threshold. A buffer silent throughout (or
    empty) returns empty. `shared_bounds` makes a stereo pair crop to the SAME (start, end) — the
    union of each channel's non-silent range — so the pair never goes out of alignment."""

    def __init__(self, threshold_amplitude: int = 32, shared_bounds: Optional[Tuple[int, int]] = None):
        self.threshold, self.shared_bounds = threshold_amplitude, shared_bounds

    def apply(self, pcm, sample_rate):
        if self.shared_bounds is not None:
            s = int(np.clip(self.shared_bounds[0], 0, len(pcm)))
            e = int(np.clip(self.shared_bounds[1], 0, len(pcm)))
        else:
            s, e = self.compute_bounds(pcm, self.threshold)
        if e < s:
            e = s
        return pcm[s:e].copy()

    @staticmethod
    def compute_bounds(pcm: np.ndarray, threshold_amplitude: int) -> Tuple[int, int]:
        loud = np.nonzero(np.abs(pcm.astype(np.int32)) > threshold_amplitude)[0]
        if len(loud) == 0:
            return len(pcm), len(pcm)        # all silent: C#'s two while-loops end at start == end == len
        return int(loud[0]), int(loud[-1]) + 1


# ── The three length-CHANGING edits ──────────────────────────────────────────────────────────
# Expressed as effects (not ad-hoc splices in the model) so the stereo partner is replayed through
# the SAME path: Cut/Paste once changed only one channel's length, and everything after the edit
# point played back time-offset between L and R. Routing them through one shape makes that class of
# divergence unrepresentable.

class DeleteRangeEffect:
    """Removes [start, end), closing the gap."""

    def __init__(self, start_frame: int, end_frame_exclusive: int):
        self.start, self.end = start_frame, end_frame_exclusive

    def apply(self, pcm, sample_rate):
        s, e = _range(pcm, self.start, self.end)
        if e == s:
            return pcm
        return np.concatenate((pcm[:s], pcm[e:]))


class PasteRangeEffect:
    """Replaces [start, end) with `clip`, or inserts at `start` when the range is empty. A clip at a
    different rate is used as-is (visible in the duration rather than silently 'corrected')."""

    def __init__(self, start_frame: int, end_frame_exclusive: int, clip: np.ndarray):
        self.start, self.end, self.clip = start_frame, end_frame_exclusive, clip

    def apply(self, pcm, sample_rate):
        s, e = _range(pcm, self.start, self.end)
        return np.concatenate((pcm[:s], self.clip.astype(np.int16, copy=False), pcm[e:]))


class InsertSilenceEffect:
    """Inserts `frame_count` frames of silence at `at_frame`, pushing everything after it later."""

    def __init__(self, at_frame: int, frame_count: int):
        self.at, self.count = at_frame, frame_count

    def apply(self, pcm, sample_rate):
        at = int(np.clip(self.at, 0, len(pcm)))
        n = max(0, self.count)
        if n == 0:
            return pcm
        return np.concatenate((pcm[:at], np.zeros(n, dtype=np.int16), pcm[at:]))


# ── Tempo / pitch ────────────────────────────────────────────────────────────────────────────

_WSOLA_SEQUENCE_S = 0.040     # analysis/synthesis window
_WSOLA_SEEK_S = 0.015         # +/- search for the best-matching splice point


def _wsola_stretch(x: np.ndarray, sample_rate: int, tempo: float) -> np.ndarray:
    """Time-stretch float32 mono `x` by `tempo` (>1 = faster/shorter) WITHOUT changing pitch.
    Waveform-similarity overlap-add: each synthesis frame is taken from near the nominal analysis
    position, nudged to where it best continues the previous frame (normalised cross-correlation via
    FFT), then Hann-windowed and overlap-added at 50 %."""
    n_in = len(x)
    n = max(64, int(sample_rate * _WSOLA_SEQUENCE_S) // 2 * 2)      # even window length
    hs = n // 2                                                       # synthesis hop
    ha = hs * tempo                                                   # analysis hop
    seek = max(8, int(sample_rate * _WSOLA_SEEK_S))
    if n_in < 3 * n or tempo <= 0:
        return _linear_resample_to(x, max(1, int(round(n_in / tempo)))) if tempo > 0 else x.copy()

    pad = 2 * n + 2 * seek
    xp = np.concatenate((np.zeros(seek, np.float32), x, np.zeros(pad, np.float32)))   # index +seek
    win = np.hanning(n + 1)[:-1].astype(np.float32)                  # periodic Hann: 50 % overlap sums to 1
    n_frames = int(np.ceil((n_in - n) / ha)) + 2
    out = np.zeros(int(n_frames * hs + n + 1), np.float32)
    fft_n = 1 << int(np.ceil(np.log2(n + 2 * seek)))

    prev = seek                                                       # first frame taken at 0 (+seek pad)
    out[:n] += win * xp[prev:prev + n]
    for k in range(1, n_frames):
        nominal = int(round(k * ha)) + seek
        if nominal - seek + n + 2 * seek > len(xp) - n:      # region and target must both stay inside xp
            out = out[:k * hs + n]
            break
        target = xp[prev + hs:prev + hs + n]                          # the natural continuation
        lo = nominal - seek
        region = xp[lo:lo + n + 2 * seek]
        corr = np.fft.irfft(np.fft.rfft(region, fft_n) * np.conj(np.fft.rfft(target, fft_n)), fft_n)[:2 * seek + 1]
        csq = np.concatenate(([0.0], np.cumsum(region.astype(np.float64) ** 2)))
        energy = csq[n:n + 2 * seek + 1] - csq[:2 * seek + 1]
        score = corr / np.sqrt(np.maximum(energy, 1e-9))
        best = lo + int(np.argmax(score))
        out[k * hs:k * hs + n] += win * xp[best:best + n]
        prev = best
    return out[:max(1, int(round(n_in / tempo)))]


def _linear_resample_to(x: np.ndarray, n_out: int) -> np.ndarray:
    if len(x) == 0:
        return x.copy()
    return np.interp(np.linspace(0, len(x) - 1, n_out), np.arange(len(x)), x).astype(np.float32)


def change_tempo_and_pitch(pcm: np.ndarray, sample_rate: int, tempo_ratio: float, semitones: float) -> np.ndarray:
    """Independent tempo (duration) and pitch control over mono int16 PCM. Pitch is a time-stretch to
    p-times the length followed by a resample back down by p (so duration is preserved); tempo is the
    remaining stretch. Output length ~= len / tempo_ratio."""
    if len(pcm) == 0:
        return pcm
    if tempo_ratio == 1.0 and semitones == 0.0:
        return pcm.copy()
    x = (pcm.astype(np.float32) / 32768.0)
    p = 2.0 ** (semitones / 12.0)
    y = _wsola_stretch(x, sample_rate, tempo_ratio / p)               # length n * p / tempo
    if p != 1.0:
        import soxr
        y = soxr.resample(y, sample_rate * p, sample_rate, quality="HQ")  # -> n / tempo, pitch x p
    return _to_i16(np.asarray(y, np.float64) * 32768.0)


def change_tempo(pcm, sample_rate, tempo_ratio):
    return change_tempo_and_pitch(pcm, sample_rate, tempo_ratio, 0.0)


def change_pitch_semitones(pcm, sample_rate, semitones):
    return change_tempo_and_pitch(pcm, sample_rate, 1.0, semitones)


class TempoPitchEffect:
    def __init__(self, tempo_ratio: float, pitch_semitones: float):
        self.tempo, self.pitch = tempo_ratio, pitch_semitones

    def apply(self, pcm, sample_rate):
        return change_tempo_and_pitch(pcm, sample_rate, self.tempo, self.pitch)


# ── Self-test (python -m Core.sample_dsp) ─────────────────────────────────────────────────────

def _selftest() -> None:
    import sys
    fails = []

    def check(name, cond):
        if not cond:
            fails.append(name)

    def arr(*v):
        return np.array(v, dtype=np.int16)

    p = arr(1, 2, 3, 4, 5, 6)
    # crop / reverse / silence / delete / paste / insert-silence
    check("crop", CropEffect(1, 4).apply(p, 44100).tolist() == [2, 3, 4])
    check("crop-clamps", CropEffect(-5, 99).apply(p, 44100).tolist() == p.tolist())
    check("crop-inverted-empty", len(CropEffect(4, 2).apply(p, 44100)) == 0)
    check("reverse-range", ReverseEffect(1, 4).apply(p, 44100).tolist() == [1, 4, 3, 2, 5, 6])
    check("silence-range", SilenceEffect(2, 4).apply(p, 44100).tolist() == [1, 2, 0, 0, 5, 6])
    check("delete", DeleteRangeEffect(1, 3).apply(p, 44100).tolist() == [1, 4, 5, 6])
    check("delete-empty-returns-input", DeleteRangeEffect(2, 2).apply(p, 44100) is p)
    check("paste-replace", PasteRangeEffect(1, 3, arr(9, 9, 9)).apply(p, 44100).tolist() == [1, 9, 9, 9, 4, 5, 6])
    check("paste-insert-at-cursor", PasteRangeEffect(2, 2, arr(7)).apply(p, 44100).tolist() == [1, 2, 7, 3, 4, 5, 6])
    check("insert-silence", InsertSilenceEffect(2, 2).apply(p, 44100).tolist() == [1, 2, 0, 0, 3, 4, 5, 6])
    check("insert-silence-zero-returns-input", InsertSilenceEffect(2, 0).apply(p, 44100) is p)
    check("inputs-never-mutated", p.tolist() == [1, 2, 3, 4, 5, 6])

    # DC offset: rounds half-to-even, whole buffer, clamps
    check("dc-measure", DcOffsetEffect.measure_offset(arr(10, 12)) == 11)
    check("dc-half-to-even", DcOffsetEffect.measure_offset(arr(0, 1, 1, 0)) == 0 and DcOffsetEffect.measure_offset(arr(1, 2)) == 2)
    check("dc-removed", DcOffsetEffect().apply(arr(10, 12), 44100).tolist() == [-1, 1])
    check("dc-zero-returns-input", (lambda a: DcOffsetEffect().apply(a, 44100) is a)(arr(-5, 5)))
    check("dc-clamps", DcOffsetEffect().apply(arr(-32768, -32768, 32767, 32767, 32767), 1).min() >= -32768)

    # gain: truncation toward zero, clipping, selection-only
    check("gain-+6dB-truncates", GainAdjustEffect(6.0).apply(arr(1000, -1000), 1).tolist() == [1995, -1995])
    check("gain-clips", GainAdjustEffect(12.0).apply(arr(30000, -30000), 1).tolist() == [32767, -32768])
    check("gain-selection-only", GainAdjustEffect(6.0, 1, 2).apply(arr(1000, 1000, 1000), 1).tolist() == [1000, 1995, 1000])

    # normalize: peak hits -0.1 dBFS; silent unchanged; shared peak; selection only; stereo-shared peak
    q = GainNormalizeEffect().apply(arr(1000, -2000, 500), 1)
    check("normalize-peak", abs(int(np.abs(q).max()) - int(10 ** (-0.1 / 20) * 32767)) <= 1)
    check("normalize-silent-unchanged", GainNormalizeEffect().apply(arr(0, 0), 1).tolist() == [0, 0])
    q = GainNormalizeEffect(shared_peak=4000).apply(arr(1000, -2000), 1)       # scaled by the OTHER channel's peak
    check("normalize-shared-peak", int(q[1]) == int(-2000 * (10 ** (-0.1 / 20) * 32767) / 4000))
    q = GainNormalizeEffect(start_frame=1, end_frame_exclusive=2).apply(arr(30000, 1000, 30000), 1)
    check("normalize-selection-confined", q[0] == 30000 and q[2] == 30000 and q[1] > 1000)
    check("normalize-compute-peak", GainNormalizeEffect.compute_peak(arr(-32768, 5)) == 32768)

    # silence trim
    check("trim-bounds", SilenceTrimEffect.compute_bounds(arr(0, 1, 100, 5, 0), 32) == (2, 3))
    check("trim", SilenceTrimEffect(32).apply(arr(0, 1, 100, 5, 0), 1).tolist() == [100])
    check("trim-all-silent-is-empty", len(SilenceTrimEffect(32).apply(arr(0, 3, -4), 1)) == 0)
    check("trim-shared-bounds", SilenceTrimEffect(32, (1, 4)).apply(arr(0, 1, 100, 5, 0), 1).tolist() == [1, 100, 5])
    check("trim-empty", len(SilenceTrimEffect(32).apply(arr(), 1)) == 0)

    # tempo / pitch: a 440 Hz sine
    sr = 44100
    t = np.arange(sr) / sr
    sine = (np.sin(2 * np.pi * 440 * t) * 12000).astype(np.int16)

    def dom_freq(a):
        spec = np.abs(np.fft.rfft(a.astype(np.float64) * np.hanning(len(a))))
        return float(np.argmax(spec)) * sr / len(a)

    check("tempo-identity", change_tempo_and_pitch(sine, sr, 1.0, 0.0).tolist() == sine.tolist())
    fast = change_tempo(sine, sr, 2.0)
    check("tempo-2x-halves-length", abs(len(fast) - sr // 2) <= 2)
    check("tempo-keeps-pitch", abs(dom_freq(fast) - 440) < 12)
    slow = change_tempo(sine, sr, 0.5)
    check("tempo-0.5x-doubles-length", abs(len(slow) - 2 * sr) <= 2)
    check("tempo-0.5x-keeps-pitch", abs(dom_freq(slow) - 440) < 12)
    up = change_pitch_semitones(sine, sr, 12.0)
    check("pitch-+12-keeps-duration", abs(len(up) - sr) <= 2)
    check("pitch-+12-doubles-freq", abs(dom_freq(up) - 880) < 20)
    down = change_pitch_semitones(sine, sr, -12.0)
    check("pitch--12-halves-freq", abs(dom_freq(down) - 220) < 12)
    both = change_tempo_and_pitch(sine, sr, 2.0, 12.0)
    check("tempo+pitch", abs(len(both) - sr // 2) <= 3 and abs(dom_freq(both) - 880) < 40)
    short = change_tempo(sine[:500], sr, 2.0)
    check("tiny-input-still-works", 240 <= len(short) <= 260)
    check("tempo-no-clip-blowup", int(np.abs(fast.astype(np.int32)).max()) <= 13000)
    check("empty-in-empty-out", len(change_tempo_and_pitch(arr(), sr, 2.0, 3.0)) == 0)

    if fails:
        print("FAIL:", ", ".join(fails))
        sys.exit(1)
    print("sample_dsp self-test: OK")


if __name__ == "__main__":
    _selftest()
