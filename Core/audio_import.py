r"""
Audio file -> the Sample Editor's native format: mono (or L/R), 44100 Hz, 16-bit signed host-order
int16 — port of Core/Sample/AudioImport.cs.

  * WAV is parsed here (numpy) — PCM 8/16/24/32-bit integer, 32/64-bit float, plus WAVE_FORMAT_EXTENSIBLE
    — the formats NAudio's WaveFileReader understands.
  * Everything else (MP3, MP4/M4A, WMA, FLAC…) goes through Qt's QAudioDecoder, i.e. the platform codecs —
    the Python counterpart of C#'s MediaFoundationReader, with no extra dependency.
  * Resampling to 44100 Hz uses `soxr` (high quality; C# uses the WDL resampler).

Channel handling matches C#: mono import AVERAGES every channel (not a left-only drop); stereo import
duplicates a mono source into both channels and uses only the first two channels of anything wider.
float -> int16 truncates toward zero after clamping, like (short)Math.Clamp(x * 32768f, ...).
"""
from __future__ import annotations

import os
import struct
from typing import Tuple

import numpy as np

TARGET_SAMPLE_RATE = 44100


class AudioImportError(Exception):
    pass


def _to_short(x: np.ndarray) -> np.ndarray:
    return np.clip(np.asarray(x, np.float64) * 32768.0, -32768, 32767).astype(np.int16)


# ── WAV ──────────────────────────────────────────────────────────────────────────────────────

def _read_wav(path: str) -> Tuple[np.ndarray, int]:
    """-> (float32 [frames, channels] in [-1, 1), sample_rate)."""
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 12 or data[:4] not in (b"RIFF", b"RF64") or data[8:12] != b"WAVE":
        raise AudioImportError(f"{os.path.basename(path)} is not a RIFF/WAVE file")
    pos, fmt, pcm = 12, None, None
    while pos + 8 <= len(data):
        tag, size = data[pos:pos + 4], struct.unpack_from("<I", data, pos + 4)[0]
        body = pos + 8
        if tag == b"fmt ":
            fmt = data[body:body + size]
        elif tag == b"data":
            end = len(data) if size == 0xFFFFFFFF else min(len(data), body + size)
            pcm = data[body:end]
            break
        pos = body + size + (size & 1)
    if fmt is None or pcm is None or len(fmt) < 16:
        raise AudioImportError("WAV is missing its fmt/data chunk")
    tag, ch, rate, _br, _ba, bits = struct.unpack_from("<HHIIHH", fmt, 0)
    if tag == 0xFFFE and len(fmt) >= 26:                 # WAVE_FORMAT_EXTENSIBLE: real tag in the sub-format GUID
        tag = struct.unpack_from("<H", fmt, 24)[0]
    if ch < 1 or rate < 1:
        raise AudioImportError("WAV has an invalid channel count or sample rate")
    width = bits // 8
    usable = len(pcm) - len(pcm) % (width * ch)
    raw = pcm[:usable]
    if tag == 1:                                          # integer PCM
        if bits == 8:
            x = (np.frombuffer(raw, np.uint8).astype(np.float32) - 128.0) / 128.0
        elif bits == 16:
            x = np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
        elif bits == 24:
            b = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.int32)
            v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
            v = np.where(v & 0x800000, v - 0x1000000, v)
            x = v.astype(np.float32) / 8388608.0
        elif bits == 32:
            x = np.frombuffer(raw, "<i4").astype(np.float32) / 2147483648.0
        else:
            raise AudioImportError(f"unsupported WAV bit depth: {bits}")
    elif tag == 3:                                        # IEEE float
        x = np.frombuffer(raw, "<f4" if bits == 32 else "<f8").astype(np.float32)
    else:
        raise AudioImportError(f"unsupported WAV format tag {tag}")
    return x.reshape(-1, ch), rate


# ── Everything else: Qt's platform decoders ──────────────────────────────────────────────────

def _read_with_qt(path: str, timeout_s: float = 120.0) -> Tuple[np.ndarray, int]:
    from PySide6.QtCore import QCoreApplication, QElapsedTimer, QEventLoop, QTimer, QUrl
    from PySide6.QtMultimedia import QAudioDecoder, QAudioFormat

    if QCoreApplication.instance() is None:
        raise AudioImportError("audio decoding needs a running Qt application")
    dec = QAudioDecoder()
    dec.setSource(QUrl.fromLocalFile(os.path.abspath(path)))
    chunks, fmt_seen, err = [], [None], []
    loop = QEventLoop()

    def on_ready():
        while dec.bufferAvailable():
            buf = dec.read()
            f = buf.format()
            fmt_seen[0] = f
            n = buf.byteCount()
            raw = bytes(buf.constData())[:n]
            sf = f.sampleFormat()
            if sf == QAudioFormat.SampleFormat.Float:
                a = np.frombuffer(raw, np.float32)
            elif sf == QAudioFormat.SampleFormat.Int16:
                a = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
            elif sf == QAudioFormat.SampleFormat.Int32:
                a = np.frombuffer(raw, np.int32).astype(np.float32) / 2147483648.0
            elif sf == QAudioFormat.SampleFormat.UInt8:
                a = (np.frombuffer(raw, np.uint8).astype(np.float32) - 128.0) / 128.0
            else:
                err.append(f"unsupported decoded sample format {sf}")
                loop.quit()
                return
            chunks.append(a.reshape(-1, f.channelCount()))

    dec.bufferReady.connect(on_ready)
    dec.finished.connect(loop.quit)
    dec.error.connect(lambda e: (err.append(dec.errorString() or str(e)), loop.quit()))
    QTimer.singleShot(int(timeout_s * 1000), lambda: (err.append("timed out decoding audio"), loop.quit()))
    dec.start()
    loop.exec()
    dec.stop()
    if err and not chunks:
        raise AudioImportError(f"could not decode {os.path.basename(path)}: {err[0]}")
    if not chunks or fmt_seen[0] is None:
        raise AudioImportError(f"{os.path.basename(path)} decoded to no audio")
    return np.concatenate(chunks), fmt_seen[0].sampleRate()


def read_audio(path: str) -> Tuple[np.ndarray, int]:
    """-> (float32 [frames, channels], sample_rate) from any file the platform can decode."""
    if os.path.splitext(path)[1].lower() == ".wav":
        return _read_wav(path)
    return _read_with_qt(path)


def get_source_channel_count(path: str) -> int:
    """Cheap peek at the source's own channel count so a caller can choose mono vs stereo import."""
    if os.path.splitext(path)[1].lower() == ".wav":
        with open(path, "rb") as f:
            head = f.read(4096)
        i = head.find(b"fmt ")
        if i >= 0 and len(head) >= i + 12:
            return struct.unpack_from("<H", head, i + 10)[0]
    return read_audio(path)[0].shape[1]


# ── Conversion ───────────────────────────────────────────────────────────────────────────────

def _resample(x: np.ndarray, rate: int) -> np.ndarray:
    if rate == TARGET_SAMPLE_RATE or len(x) == 0:
        return x
    import soxr
    return soxr.resample(x, rate, TARGET_SAMPLE_RATE, quality="HQ").astype(np.float32, copy=False)


def convert_to_mono_44100(data: np.ndarray, rate: int) -> np.ndarray:
    """float [frames, ch] at `rate` -> int16 mono 44100 (channels averaged)."""
    return _to_short(_resample(data, rate).mean(axis=1))


def convert_to_stereo_44100(data: np.ndarray, rate: int) -> Tuple[np.ndarray, np.ndarray]:
    """-> (left, right) int16 at 44100. A mono source is duplicated into both channels (a deliberate
    way to build a true stereo pair from mono material); more than 2 channels uses only the first two."""
    x = _resample(data, rate)
    left = x[:, 0]
    right = x[:, 1] if x.shape[1] >= 2 else left
    return _to_short(left), _to_short(right)


def import_to_mono_44100(path: str) -> np.ndarray:
    data, rate = read_audio(path)
    return convert_to_mono_44100(data, rate)


def import_stereo_to_lr_44100(path: str) -> Tuple[np.ndarray, np.ndarray]:
    data, rate = read_audio(path)
    return convert_to_stereo_44100(data, rate)


# ── Self-test (python -m Core.audio_import) ───────────────────────────────────────────────────

def _selftest() -> None:
    import sys, tempfile, wave
    fails = []

    def check(name, cond):
        if not cond:
            fails.append(name)

    tmp = tempfile.mkdtemp(prefix="kr_audioimport_")

    def write_wav(name, frames_int16, rate, channels, sampwidth=2):
        p = os.path.join(tmp, name)
        with wave.open(p, "wb") as w:
            w.setnchannels(channels); w.setsampwidth(sampwidth); w.setframerate(rate)
            w.writeframes(frames_int16)
        return p

    # 16-bit mono 44100 passes through bit-exact
    mono = (np.sin(np.arange(4410) * 0.05) * 10000).astype("<i2")
    p = write_wav("mono.wav", mono.tobytes(), 44100, 1)
    out = import_to_mono_44100(p)
    check("mono16-bit-exact", out.tolist() == mono.tolist())
    check("source-channels-mono", get_source_channel_count(p) == 1)

    # stereo averaged to mono; L/R preserved for stereo import; mono duplicated into both
    l = (np.arange(1000) % 200 * 50).astype("<i2"); r = (-(np.arange(1000) % 200) * 50).astype("<i2")
    inter = np.empty(2000, "<i2"); inter[0::2] = l; inter[1::2] = r
    p2 = write_wav("st.wav", inter.tobytes(), 44100, 2)
    check("source-channels-stereo", get_source_channel_count(p2) == 2)
    avg = import_to_mono_44100(p2)
    check("stereo-downmix-averages", np.abs(avg.astype(int) - ((l.astype(int) + r.astype(int)) // 2)).max() <= 1)
    L, R = import_stereo_to_lr_44100(p2)
    check("stereo-import-keeps-channels", L.tolist() == l.tolist() and R.tolist() == r.tolist())
    L, R = import_stereo_to_lr_44100(p)
    check("mono-duplicated-into-both", L.tolist() == R.tolist() == mono.tolist())

    # resampling: 22050 -> 44100 doubles the frame count and keeps the tone
    t = np.arange(22050) / 22050
    tone = (np.sin(2 * np.pi * 1000 * t) * 12000).astype("<i2")
    p3 = write_wav("r22.wav", tone.tobytes(), 22050, 1)
    up = import_to_mono_44100(p3)
    check("resample-doubles-length", abs(len(up) - 44100) <= 8)
    spec = np.abs(np.fft.rfft(up.astype(float) * np.hanning(len(up))))
    check("resample-keeps-pitch", abs(np.argmax(spec) * 44100 / len(up) - 1000) < 5)

    # 24-bit, 8-bit and float WAV
    v = (np.array([0, 1000000, -1000000, 8388607, -8388608]) & 0xFFFFFF).astype(np.uint32)
    b24 = b"".join(int(x).to_bytes(3, "little") for x in v)
    p24 = write_wav("w24.wav", b24, 44100, 1, sampwidth=3)
    o24 = import_to_mono_44100(p24)
    check("pcm24", o24.tolist()[:3] == [0, 3906, -3906])
    check("pcm24-full-scale", o24[3] == 32767 and o24[4] == -32768)
    p8 = write_wav("w8.wav", bytes([128, 255, 0]), 44100, 1, sampwidth=1)
    o8 = import_to_mono_44100(p8)
    check("pcm8", o8.tolist() == [0, 32512, -32768])
    pf = os.path.join(tmp, "f32.wav")
    fl = np.array([0.0, 0.5, -0.5, 2.0], "<f4")
    hdr = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + fl.nbytes, b"WAVE", b"fmt ", 16, 3, 1, 44100, 44100 * 4, 4, 32, b"data", fl.nbytes)
    open(pf, "wb").write(hdr + fl.tobytes())
    of = import_to_mono_44100(pf)
    check("float32", of.tolist() == [0, 16384, -16384, 32767])     # 2.0 clamps; 0.5 -> exactly 16384

    # junk / truncated
    open(os.path.join(tmp, "junk.wav"), "wb").write(b"not a wave file")
    try:
        import_to_mono_44100(os.path.join(tmp, "junk.wav")); check("junk-raises", False)
    except AudioImportError:
        check("junk-raises", True)
    odd = write_wav("odd.wav", mono.tobytes()[:-1], 44100, 1)
    check("trailing-odd-byte-ignored", len(import_to_mono_44100(odd)) == len(mono) - 1)

    if fails:
        print("FAIL:", ", ".join(fails)); sys.exit(1)
    print("audio_import self-test: OK")


if __name__ == "__main__":
    _selftest()
