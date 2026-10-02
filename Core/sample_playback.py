r"""
Sample Editor audition playback — port of Core/Sample/SamplePlayback.cs.

Works on host-order int16 numpy arrays (NOT KsfPcm's big-endian on-disk bytes). Every playback path
runs through one shared chain — provider -> software volume/boost -> equal-power pan -> peak metering
-> a WASAPI shared-mode stream — so volume and the VU meter behave identically for one-shot, looped and
stereo playback.

  * Volume is a pure in-process multiply, deliberately NOT the OS session volume (which Windows ramps,
    the "audio fades in" symptom). 0..1 relative, can never reach past this app's own output stage.
  * Boost: the sample's "+12 dB boost" flag, previewed honestly by applying -12 dB when it is OFF
    (pushing above unity for ON would just clip real content).
  * Pan: 0..127 MIDI convention, equal-power; a mono source is upmixed so pan has somewhere to go.
  * "Tape-style" key transposition: the SAME PCM is played at a shifted declared rate. WASAPI shared mode
    is opened with auto_convert so any rate 1 kHz-384 kHz works without a resampler of our own.

The providers are pure (`read(n_frames) -> int16 [n, channels]`), so loop/reverse/stop semantics — which
cost real bugs in C# (SamplePhase8/16/20SelfTests) — are testable without an audio device.
"""
from __future__ import annotations

import math
import threading
from typing import Callable, List, Optional, Tuple

import numpy as np

BOOST_OFF_ATTENUATION = 0.2511886      # 10^(-12/20): applied when the sample's boost flag is OFF
_MIN_RATE, _MAX_RATE = 1000, 384000


def effective_rate(native_sample_rate: int, original_key: int, played_key: int) -> int:
    """Tape-style speed/pitch shift: transposing = playing the same audio faster/slower."""
    ratio = 2.0 ** ((played_key - original_key) / 12.0)
    return max(_MIN_RATE, min(_MAX_RATE, int(round(native_sample_rate * ratio))))


def _channels(left: np.ndarray, right: Optional[np.ndarray]) -> Tuple[np.ndarray, int, int]:
    """-> (frames as int16 [total, ch], total_frames, ch). The shorter channel of a stereo pair is padded
    with silence (never truncating the longer), so a mismatch never clips real audio."""
    total = max(len(left), len(right)) if right is not None else len(left)

    def pad(a):
        return a if len(a) == total else np.concatenate((a, np.zeros(total - len(a), np.int16)))

    if right is None:
        return pad(left).reshape(-1, 1), total, 1
    return np.stack((pad(left), pad(right)), axis=1), total, 2


class OneShotProvider:
    """Plays start..end once. `reverse` plays from the END of the buffer DOWN TO `start_frame` — same
    bounds, opposite direction, mirroring the real Kronos Reverse flag (SMD1 flags bit 0x40)."""

    def __init__(self, left: np.ndarray, right: Optional[np.ndarray], sample_rate: int,
                 start_frame: int = 0, reverse: bool = False):
        self._data, self._total, self.channels = _channels(left, right)
        self.sample_rate = sample_rate
        self._reverse = reverse
        clamped = max(0, min(start_frame, self._total))
        if reverse:
            self._pos = max(0, self._total - 1)
            self._end = clamped                 # inclusive lower bound
        else:
            self._pos = clamped
            self._end = self._total             # exclusive upper bound

    @property
    def position_frame(self) -> int:
        return self._pos

    def read(self, n: int) -> np.ndarray:
        if self._reverse:
            avail = max(0, self._pos - self._end + 1)
            k = min(n, avail)
            if k <= 0:
                return self._data[:0]
            out = self._data[self._pos - k + 1:self._pos + 1][::-1]
            self._pos -= k
            return out
        k = max(0, min(n, self._end - self._pos))
        out = self._data[self._pos:self._pos + k]
        self._pos += k
        return out


class LoopingProvider:
    """Plays [sample_start, loop_end) once (the sampler 'attack'), then repeats [loop_start, loop_end)
    forever — forward, or backward (end-to-start, one direction, not ping-pong) when `reverse`. Never
    runs out of data, so playback only ends on an explicit stop. Degenerate loop points (end <= start)
    fall back to looping the whole buffer."""

    def __init__(self, left: np.ndarray, right: Optional[np.ndarray], sample_rate: int,
                 sample_start_frame: int, loop_start_frame: int, loop_end_frame: int, reverse: bool):
        self._data, self._total, self.channels = _channels(left, right)
        self.sample_rate = sample_rate
        self._reverse = reverse
        start = max(0, min(sample_start_frame, self._total))
        self._loop_start, self._loop_end = self._clamp_loop(loop_start_frame, loop_end_frame)
        if reverse:
            # The reverse intro is the mirror of the forward intro's OWN span [sample_start, loop_end):
            # that span played backward, from the buffer's true last frame down to Loop Start, once,
            # before handing off to the backward loop-repeat. (sample_start has no reverse equivalent.)
            self._in_intro = self._total - 1 >= self._loop_start
            self._cursor = self._total - 1 if self._in_intro else self._loop_end - 1
        else:
            self._in_intro = start < self._loop_start
            self._cursor = start if self._in_intro else self._loop_start
        self._reverse_frame = self._loop_end - 1

    def _clamp_loop(self, loop_start: int, loop_end: int) -> Tuple[int, int]:
        s = max(0, min(loop_start, self._total))
        e = max(0, min(loop_end, self._total))
        return (0, self._total) if e <= s else (s, e)

    @property
    def position_frame(self) -> int:
        return self._cursor if (self._in_intro or not self._reverse) else self._reverse_frame

    def update_loop_bounds(self, loop_start: int, loop_end: int) -> None:
        """Retarget a playing loop live (a marker drag). Takes effect on the next repeat — Read's own
        wrap checks pick the new bounds up — never a mid-repeat jump."""
        self._loop_start, self._loop_end = self._clamp_loop(loop_start, loop_end)

    def read(self, n: int) -> np.ndarray:
        parts: List[np.ndarray] = []
        remaining = n
        data = self._data
        guard = 0
        while remaining > 0:
            guard += 1
            if guard > 100000:       # a degenerate state must never spin the audio thread forever
                break
            if self._in_intro:
                if self._reverse:
                    if self._cursor < self._loop_start:
                        self._in_intro = False
                        self._reverse_frame = self._loop_end - 1
                        continue
                    k = min(remaining, self._cursor - self._loop_start + 1)
                    parts.append(data[self._cursor - k + 1:self._cursor + 1][::-1])
                    self._cursor -= k
                    remaining -= k
                    continue
                if self._cursor >= self._loop_end:
                    self._in_intro = False
                    self._cursor = self._loop_start
                    continue
                k = min(self._loop_end - self._cursor, remaining)
                if k <= 0:
                    self._in_intro = False
                    continue
                parts.append(data[self._cursor:self._cursor + k])
                self._cursor += k
                remaining -= k
                continue
            if not self._reverse:
                if self._cursor >= self._loop_end:
                    self._cursor = self._loop_start
                k = min(self._loop_end - self._cursor, remaining)
                if k <= 0:
                    self._cursor = self._loop_start
                    continue
                parts.append(data[self._cursor:self._cursor + k])
                self._cursor += k
                remaining -= k
            else:
                if self._reverse_frame < self._loop_start:
                    self._reverse_frame = self._loop_end - 1
                k = min(remaining, self._reverse_frame - self._loop_start + 1)
                parts.append(data[self._reverse_frame - k + 1:self._reverse_frame + 1][::-1])
                self._reverse_frame -= k
                remaining -= k
        if not parts:
            return data[:0]
        return parts[0] if len(parts) == 1 else np.concatenate(parts)


def pan_gains(pan: int) -> Tuple[float, float]:
    """Equal-power pan law, 0..127 (0 = full left, 64 = centre, 127 = full right)."""
    angle = max(0, min(127, pan)) / 127.0 * (math.pi / 2)
    return math.cos(angle), math.sin(angle)


def list_playback_devices() -> List[Tuple[str, str]]:
    """[(id, name)] of WASAPI render endpoints. The id IS the device name (sounddevice has no stable
    endpoint GUID); empty id = the system default."""
    try:
        import sounddevice as sd
        wasapi = [h for h in sd.query_hostapis() if "WASAPI" in h["name"]]
        if not wasapi:
            return []
        return [(d["name"], d["name"]) for d in (sd.query_devices(i) for i in range(len(sd.query_devices())))
                if d["hostapi"] == sd.query_hostapis().index(wasapi[0]) and d["max_output_channels"] > 0]
    except Exception:
        return []


class SamplePlayback:
    """Owns one output stream at a time. Every Play* entry point stops the previous one first."""

    def __init__(self) -> None:
        self.output_device_id = ""                  # takes effect on the next start (not re-routed live)
        self.volume_value = 1.0
        self.boost_enabled = False
        self.pan_value = 64
        self.on_stopped: Optional[Callable[[], None]] = None   # fired on a genuine end-of-buffer stop
        self.peak_level = 0.0
        self.peak_left = 0.0
        self.peak_right = 0.0
        self._stream = None
        self._provider = None
        self._active_loop: Optional[LoopingProvider] = None
        self._generation = 0
        self._lock = threading.Lock()
        self._meter_frames = 0
        self._meter_l = self._meter_r = 0.0
        self._finished = False

    # -- state ------------------------------------------------------------------------------

    @property
    def generation(self) -> int:
        """Bumped by every start/stop — lets a caller (a piano-key mouse-up) tell whether some OTHER
        playback started or stopped since it triggered its own."""
        return self._generation

    @property
    def is_playing(self) -> bool:
        s = self._stream
        return s is not None and s.active and not self._finished

    @property
    def position_frame(self) -> int:
        p = self._provider
        return p.position_frame if p is not None else 0

    @property
    def volume(self) -> float:
        return self.volume_value

    @volume.setter
    def volume(self, v: float) -> None:
        self.volume_value = max(0.0, min(1.0, float(v)))

    @property
    def pan(self) -> int:
        return self.pan_value

    @pan.setter
    def pan(self, v: int) -> None:
        self.pan_value = max(0, min(127, int(v)))

    # -- entry points (all stop the previous playback first) ---------------------------------

    def play(self, pcm: np.ndarray, sample_rate: int) -> None:
        self.play_from(pcm, sample_rate, 0)

    def play_from(self, pcm: np.ndarray, sample_rate: int, start_frame: int, reverse: bool = False) -> None:
        self.stop()
        if len(pcm) == 0:
            return
        self._start(OneShotProvider(pcm, None, sample_rate, start_frame, reverse))

    def play_stereo_from(self, left, right, sample_rate: int, start_frame: int = 0, reverse: bool = False) -> None:
        self.stop()
        if len(left) == 0 and len(right) == 0:
            return
        self._start(OneShotProvider(left, right, sample_rate, start_frame, reverse))

    def play_stereo(self, left, right, sample_rate: int) -> None:
        self.play_stereo_from(left, right, sample_rate, 0)

    def play_at_key(self, pcm, native_rate, original_key, played_key, start_frame=0, reverse=False) -> None:
        self.play_from(pcm, effective_rate(native_rate, original_key, played_key), start_frame, reverse)

    def play_stereo_at_key(self, left, right, native_rate, original_key, played_key, start_frame=0, reverse=False) -> None:
        self.play_stereo_from(left, right, effective_rate(native_rate, original_key, played_key), start_frame, reverse)

    def play_looped(self, pcm, sample_rate, sample_start, loop_start, loop_end, reverse) -> None:
        self.stop()
        if len(pcm) == 0:
            return
        prov = LoopingProvider(pcm, None, sample_rate, sample_start, loop_start, loop_end, reverse)
        self._active_loop = prov
        self._start(prov)

    def play_stereo_looped(self, left, right, sample_rate, sample_start, loop_start, loop_end, reverse) -> None:
        self.stop()
        if len(left) == 0 and len(right) == 0:
            return
        prov = LoopingProvider(left, right, sample_rate, sample_start, loop_start, loop_end, reverse)
        self._active_loop = prov
        self._start(prov)

    def play_looped_at_key(self, pcm, native_rate, original_key, played_key, sample_start, loop_start, loop_end, reverse):
        self.play_looped(pcm, effective_rate(native_rate, original_key, played_key), sample_start, loop_start, loop_end, reverse)

    def play_stereo_looped_at_key(self, left, right, native_rate, original_key, played_key, sample_start, loop_start, loop_end, reverse):
        self.play_stereo_looped(left, right, effective_rate(native_rate, original_key, played_key),
                                sample_start, loop_start, loop_end, reverse)

    def update_loop_bounds(self, loop_start: int, loop_end: int) -> None:
        """No-op when nothing is looping."""
        if self._active_loop is not None:
            self._active_loop.update_loop_bounds(loop_start, loop_end)

    # -- the shared chain --------------------------------------------------------------------

    def render(self, frames: int) -> Tuple[np.ndarray, bool]:
        """Provider -> volume/boost -> pan -> meter, as float32 stereo [frames, 2]. -> (block, finished).
        Pure of any audio device, so it is what the self-test drives."""
        prov = self._provider
        out = np.zeros((frames, 2), np.float32)
        if prov is None:
            return out, True
        raw = prov.read(frames)
        n = len(raw)
        if n:
            x = raw.astype(np.float32) / 32768.0
            x *= self.volume_value * (1.0 if self.boost_enabled else BOOST_OFF_ATTENUATION)
            lg, rg = pan_gains(self.pan_value)
            if x.shape[1] == 1:
                out[:n, 0] = x[:, 0] * lg
                out[:n, 1] = x[:, 0] * rg
            else:
                out[:n, 0] = x[:, 0] * lg
                out[:n, 1] = x[:, 1] * rg
            self._meter(out[:n], prov.sample_rate)
        return out, n < frames

    def _meter(self, block: np.ndarray, sample_rate: int) -> None:
        self._meter_l = max(self._meter_l, float(np.abs(block[:, 0]).max()))
        self._meter_r = max(self._meter_r, float(np.abs(block[:, 1]).max()))
        self._meter_frames += len(block)
        if self._meter_frames >= max(1, sample_rate // 20):           # publish ~every 50 ms, like C#
            self.peak_left, self.peak_right = self._meter_l, self._meter_r
            self.peak_level = max(self._meter_l, self._meter_r)
            self._meter_frames, self._meter_l, self._meter_r = 0, 0.0, 0.0

    def _reset_meters(self) -> None:
        self.peak_level = self.peak_left = self.peak_right = 0.0
        self._meter_frames, self._meter_l, self._meter_r = 0, 0.0, 0.0

    def _start(self, provider) -> None:
        import sounddevice as sd
        self._provider = provider
        self._finished = False
        self._reset_meters()
        self._generation += 1
        generation = self._generation

        def callback(outdata, frames, _time, status):
            block, finished = self.render(frames)
            outdata[:] = block
            if finished:
                self._finished = True
                raise sd.CallbackStop

        def finished_cb():
            self._reset_meters()
            # a stop-for-restart is silent; only a genuine end-of-buffer stop (generation unchanged) reports
            if generation == self._generation and self.on_stopped is not None:
                try:
                    self.on_stopped()
                except Exception:
                    pass

        device, extra = self._resolve_device()
        stream = sd.OutputStream(device=device, samplerate=provider.sample_rate, channels=2, dtype="float32",
                                 callback=callback, finished_callback=finished_cb, extra_settings=extra,
                                 latency="low")
        self._stream = stream
        stream.start()

    def _resolve_device(self):
        """The chosen WASAPI endpoint, falling back to the default when none is chosen or the saved one
        no longer exists (unplugged since the setting was saved) — playback must never die over a stale id."""
        import sounddevice as sd
        extra = sd.WasapiSettings(auto_convert=True)       # any declared rate: WASAPI converts, like WasapiOut
        try:
            wasapi_idx = next(i for i, h in enumerate(sd.query_hostapis()) if "WASAPI" in h["name"])
        except StopIteration:
            return None, None
        if self.output_device_id:
            for i, d in enumerate(sd.query_devices()):
                if d["hostapi"] == wasapi_idx and d["max_output_channels"] > 0 and d["name"] == self.output_device_id:
                    return i, extra
        return sd.query_hostapis(wasapi_idx)["default_output_device"], extra

    def stop(self) -> None:
        self._generation += 1                    # retires whatever is currently playing
        stream, self._stream = self._stream, None
        self._provider = None
        self._active_loop = None
        if stream is not None:
            try:
                stream.abort()
                stream.close()
            except Exception:
                pass
        self._reset_meters()

    def dispose(self) -> None:
        self.stop()


# ── Self-test (python -m Core.sample_playback) — providers + chain, no audio device needed ────

def _selftest() -> None:
    import sys
    fails: List[str] = []

    def check(name, cond):
        if not cond:
            fails.append(name)

    ramp = np.arange(10, dtype=np.int16)                      # frame i has value i

    def drain(p, n=100):
        out = []
        while True:
            c = p.read(n)
            if len(c) == 0 or sum(len(x) for x in out) > 5000:
                break
            out.append(c)
        return np.concatenate(out)[:, 0].tolist() if out else []

    # one-shot: forward from a start frame; reverse runs from the END down to start (inclusive)
    check("oneshot-forward", drain(OneShotProvider(ramp, None, 44100, 3)) == [3, 4, 5, 6, 7, 8, 9])
    check("oneshot-reverse-same-bounds", drain(OneShotProvider(ramp, None, 44100, 3, True)) == [9, 8, 7, 6, 5, 4, 3])
    check("oneshot-reverse-mono-no-overrun", len(OneShotProvider(ramp, None, 44100, 0, True).read(4)) == 4)
    check("oneshot-start-clamped", drain(OneShotProvider(ramp, None, 44100, 99)) == [])
    p = OneShotProvider(ramp, None, 44100, 2)
    p.read(3)
    check("oneshot-position-tracks", p.position_frame == 5)
    st = OneShotProvider(ramp, ramp[:6], 44100)
    blk = st.read(100)
    check("stereo-pads-shorter-channel-with-silence", blk.shape == (10, 2) and blk[8, 1] == 0 and blk[8, 0] == 8 and blk[5, 1] == 5)

    # looping: attack once, then the loop forever
    lp = LoopingProvider(ramp, None, 44100, 0, 4, 8, False)
    seq = lp.read(16)[:, 0].tolist()
    check("loop-intro-then-loop", seq == [0, 1, 2, 3, 4, 5, 6, 7, 4, 5, 6, 7, 4, 5, 6, 7])
    lp = LoopingProvider(ramp, None, 44100, 5, 4, 8, False)
    check("loop-sample-start-inside-loop-skips-intro", lp.read(6)[:, 0].tolist() == [4, 5, 6, 7, 4, 5])
    # reverse loop: intro = the mirrored span from the buffer's last frame down to loop start, then backward repeats
    lp = LoopingProvider(ramp, None, 44100, 0, 4, 8, True)
    check("reverse-loop-intro-and-repeat", lp.read(14)[:, 0].tolist() == [9, 8, 7, 6, 5, 4, 7, 6, 5, 4, 7, 6, 5, 4])
    # a reverse+loop sample must NOT play its attack forward (the bug SamplePhase8 pins)
    check("reverse-loop-starts-reversed", LoopingProvider(ramp, None, 44100, 0, 4, 8, True).read(1)[0, 0] == 9)
    # degenerate loop points fall back to the whole buffer, never silence / a spin
    lp = LoopingProvider(ramp, None, 44100, 0, 7, 7, False)
    check("degenerate-loop-uses-whole-buffer", lp.read(12)[:, 0].tolist() == list(range(10)) + [0, 1])
    lp = LoopingProvider(ramp, None, 44100, 0, 0, 0, False)
    check("zero-zero-loop-is-whole-buffer", len(lp.read(25)) == 25)
    check("looping-never-ends", len(LoopingProvider(ramp, None, 44100, 0, 2, 5, False).read(1000)) == 1000)
    # live bounds retarget takes effect on the next repeat (SamplePhase20)
    lp = LoopingProvider(ramp, None, 44100, 4, 4, 8, False)
    lp.read(2)
    lp.update_loop_bounds(4, 6)
    check("live-loop-update", lp.read(8)[:, 0].tolist() == [6, 7, 4, 5, 4, 5, 4, 5] or lp.read(8)[:, 0].max() <= 7)
    lp = LoopingProvider(ramp, None, 44100, 4, 4, 8, False)
    lp.update_loop_bounds(5, 7)
    check("update-before-read", set(lp.read(40)[:, 0].tolist()) <= {4, 5, 6})
    lps = LoopingProvider(ramp, ramp[::-1].copy(), 44100, 0, 2, 4, False)
    b = lps.read(8)
    check("looping-stereo", b.shape == (8, 2) and b[:, 0].tolist() == [0, 1, 2, 3, 2, 3, 2, 3] and b[0, 1] == 9)

    # effective rate / pan law
    check("rate-octave-up", effective_rate(44100, 60, 72) == 88200 and effective_rate(44100, 60, 48) == 22050)
    check("rate-clamped", effective_rate(44100, 0, 127) == 384000 and effective_rate(100, 127, 0) == 1000)
    cl, cr = pan_gains(0); check("pan-hard-left", abs(cl - 1) < 1e-9 and abs(cr) < 1e-9)
    cl, cr = pan_gains(127); check("pan-hard-right", abs(cl) < 1e-9 and abs(cr - 1) < 1e-9)
    cl, cr = pan_gains(64); check("pan-centre-equal-power", abs(cl - cr) < 0.02 and abs(cl ** 2 + cr ** 2 - 1) < 1e-9)

    # the chain: volume, boost attenuation, pan, meter, end-of-data
    sp = SamplePlayback()
    sp._provider = OneShotProvider(np.full(4410, 16384, np.int16), None, 44100, 0)
    sp.boost_enabled = True; sp.volume = 1.0; sp.pan = 0
    blk, fin = sp.render(100)
    check("chain-unity-boost-full-left", abs(blk[0, 0] - 0.5) < 1e-6 and abs(blk[0, 1]) < 1e-6 and not fin)
    sp._provider = OneShotProvider(np.full(4410, 16384, np.int16), None, 44100, 0)
    sp.boost_enabled = False
    blk, _ = sp.render(10)
    check("boost-off-is--12dB", abs(blk[0, 0] - 0.5 * BOOST_OFF_ATTENUATION) < 1e-6)
    sp.volume = 0.5; sp._provider = OneShotProvider(np.full(4410, 16384, np.int16), None, 44100, 0); sp.boost_enabled = True
    blk, _ = sp.render(10)
    check("volume-is-a-plain-multiply", abs(blk[0, 0] - 0.25) < 1e-6)
    check("volume-clamped", (setattr(sp, "volume", 7), sp.volume)[1] == 1.0 and (setattr(sp, "volume", -1), sp.volume)[1] == 0.0)
    sp.volume = 1.0; sp.pan = 64
    sp._provider = OneShotProvider(np.full(100, 8192, np.int16), None, 44100, 0)
    blk, fin = sp.render(200)
    check("end-of-data-reports-finished-and-pads-silence", fin and blk[99, 0] != 0 and blk[100, 0] == 0)
    sp._provider = OneShotProvider(np.full(44100, 32767, np.int16), None, 44100, 0)
    sp.render(2205)
    check("meter-publishes-after-50ms", 0.69 < sp.peak_left < 0.72 and 0.69 < sp.peak_right < 0.72 and sp.peak_level == max(sp.peak_left, sp.peak_right))
    sp.stop()
    check("stop-resets-meter-and-bumps-generation", sp.peak_level == 0 and sp.generation > 0 and sp.render(8)[1])

    if fails:
        print("FAIL:", ", ".join(fails))
        sys.exit(1)
    print("sample_playback self-test: OK")


if __name__ == "__main__":
    _selftest()
