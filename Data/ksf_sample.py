"""`.KSF` — single sample (binary, CKorgRiff-framed, big-endian, holds real
PCM). Port of C#'s Core/Sample/KsfSample.cs.

Chunk order: SMP1 -> SNO1 -> NAME -> SMF1 (optional) -> SMD1. See
kronosology/docs/interfaces/ksc_kmp_ksf_file_format.md §3 for the full
hardware-verified spec this implements.
"""
from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

import Data.korg_riff_chunk as riff
import Data.ksf_pcm as ksf_pcm
from Models.storage import atomic_write_bytes

FLAG_ONE_SHOT = 0x80   # loop disabled
FLAG_REVERSE  = 0x40   # playback-direction only, does not touch PCM in place
FLAG_BOOST_12DB = 0x01  # +12dB gain boost, default on


def _clamp_loop_tune(value: int) -> int:
    """Front-panel UI clamp: -99..+99, even though the on-disk signed byte
    has headroom to -128..127. Values outside the clamp were never
    producible to test on real hardware, so writing them isn't known-safe."""
    return max(-99, min(99, value))


@dataclass
class KsfSample:
    name: str = "New Sample"
    suffix: str = ""          # "", "-L", or "-R" — stereo channel marker
    sno1: int = 0
    sample_rate: int = 44100
    # SMD1 sub-header offset 4: 0x80=one-shot, 0x40=reverse, 0x01=+12dB boost.
    # Bits 1-5 unobserved in any real sample checked.
    flags: int = 0x81
    channels: int = 1          # always 1 in every real file (no on-disk stereo)
    bits: int = 16             # the only value ever seen
    pcm: bytes = b""           # raw big-endian 16-bit signed samples — route through ksf_pcm
    path: Optional[str] = None

    # SMP1 payload offsets 16/20/28.
    sample_start: int = 0
    loop_start: int = 0
    loop_end: int = 0

    # SMP1 offset-24 duplicate slot: mirrors loop_start in the overwhelming
    # majority of real files but not always (rare legacy/converted content).
    # None means "not read from a file" (a brand-new sample) — to_bytes()
    # then falls back to mirroring loop_start. An editor that changes
    # loop_start on an existing sample should call clear_preserved_loop_dup()
    # to re-sync it.
    _preserved_loop_dup: Optional[int] = field(default=None, repr=False)

    # SMF1 chunk (12 bytes, optional): Eva-side bookkeeping. Hardware/objdump-
    # confirmed (kronosology doc §3.2) NEVER read by OA.ko's real import path
    # for a normal resident sample — leave unset when writing a sample with
    # its own real audio. DOES need writing for a deliberately-authored
    # header-only STUB (see set_stub_target): a header-only .KSF with no SMF1
    # hung a real Kronos's Disk-page Load solid (needed a power-cycle) — every
    # real header-only file ever observed carries one, so this is a hard
    # safety rule for a writer, not a nicety.
    _smf1: Optional[bytes] = field(default=None, repr=False)

    _loop_tune: int = field(default=0, repr=False)

    @property
    def loop_tune(self) -> int:
        return self._loop_tune

    @loop_tune.setter
    def loop_tune(self, value: int) -> None:
        self._loop_tune = _clamp_loop_tune(value)

    def restore_loop_tune(self, value: int) -> None:
        """Bypass the clamp — for undo/redo restoring a value that was
        already on disk outside the front-panel's own clamp range."""
        self._loop_tune = value

    @property
    def is_reversed(self) -> bool:
        return bool(self.flags & FLAG_REVERSE)

    @is_reversed.setter
    def is_reversed(self, value: bool) -> None:
        self.flags = (self.flags | FLAG_REVERSE) if value else (self.flags & ~FLAG_REVERSE)

    @property
    def is_12db_boost_enabled(self) -> bool:
        return bool(self.flags & FLAG_BOOST_12DB)

    @is_12db_boost_enabled.setter
    def is_12db_boost_enabled(self, value: bool) -> None:
        self.flags = (self.flags | FLAG_BOOST_12DB) if value else (self.flags & ~FLAG_BOOST_12DB)

    @property
    def is_loop_enabled(self) -> bool:
        return (self.flags & FLAG_ONE_SHOT) == 0

    @property
    def frame_count(self) -> int:
        return len(self.pcm) // 2

    @property
    def is_header_only(self) -> bool:
        """A real, hardware-observed failure mode (doc §3.3): Eva's own Save
        can silently write a 124-byte header-only .KSF (frame_count==0) for a
        sample loaded but never fully read into memory. The predicate every
        consumer (waveform view, FTP push guard, export) checks before
        trusting pcm."""
        return self.frame_count == 0

    @property
    def preserved_loop_dup(self) -> Optional[int]:
        return self._preserved_loop_dup

    def clear_preserved_loop_dup(self) -> None:
        self._preserved_loop_dup = None

    def restore_preserved_loop_dup(self, value: Optional[int]) -> None:
        self._preserved_loop_dup = value

    @property
    def stub_target_filename(self) -> Optional[str]:
        """Decode _smf1 as the filename it holds when present — tells a
        caller "this .KSF is a stub, its real audio lives in file X" apart
        from "this is a resident file with its own real audio"."""
        if self._smf1 is None:
            return None
        return self._smf1.decode("ascii", errors="replace").rstrip("\x00 ")

    @staticmethod
    def is_valid_stub_target(filename: str) -> bool:
        return len(filename.encode("ascii", errors="replace")) <= 12

    def set_stub_target(self, filename: str) -> None:
        if not self.is_valid_stub_target(filename):
            raise ValueError(
                f"'{filename}' is {len(filename.encode('ascii', errors='replace'))} characters — "
                "a Kronos SMF1 link can only name a 12-character-or-shorter filename.")
        b = filename.encode("ascii", errors="replace")
        self._smf1 = b + b" " * (12 - len(b))

    # ── Parsing ──────────────────────────────────────────────────────────────

    @staticmethod
    def open(data: bytes) -> Optional["KsfSample"]:
        """None if `data` isn't a recognizable .KSF (first chunk isn't SMP1,
        or no SMD1 chunk at all) rather than raising — a genuinely truncated
        download must fail loudly here, not silently produce a default/empty
        object indistinguishable from a real header-only-corrupted file."""
        chunks = riff.read_chunks(data)
        if not chunks or chunks[0][0] != "SMP1" or len(chunks[0][1]) < 32:
            return None
        if not any(tag == "SMD1" for tag, _ in chunks):
            return None

        s = KsfSample()
        for tag, payload in chunks:
            if tag == "SMP1" and len(payload) >= 32:
                # Name/Suffix come from the 24-byte NAME chunk, not this
                # 16-byte short field — not simple re-truncations of each
                # other (16 bytes holds only 14 base chars).
                s.sample_start = riff.read_u32be(payload, 16)
                s.loop_start = riff.read_u32be(payload, 20)
                s._preserved_loop_dup = riff.read_u32be(payload, 24)
                s.loop_end = riff.read_u32be(payload, 28)
            elif tag == "SNO1" and len(payload) >= 4:
                s.sno1 = riff.read_u32be(payload, 0)
            elif tag == "NAME" and len(payload) >= 24:
                name, suffix = riff.split_name_suffix(
                    payload[:24].decode("ascii", errors="replace"))
                s.name = name
                s.suffix = suffix
            elif tag == "SMF1":
                s._smf1 = payload
            elif tag == "SMD1" and len(payload) >= 12:
                s.sample_rate = riff.read_u32be(payload, 0)
                s.flags = payload[4]
                s._loop_tune = payload[5] - 256 if payload[5] >= 128 else payload[5]
                s.channels = payload[6]
                s.bits = payload[7]
                frame_count = riff.read_u32be(payload, 8)
                pcm_bytes_wanted = frame_count * 2
                pcm_bytes_available = max(0, min(pcm_bytes_wanted, len(payload) - 12))
                s.pcm = bytes(payload[12:12 + pcm_bytes_available])
        return s

    def to_bytes(self) -> bytes:
        frame_count = max(0, self.frame_count)

        # loop_end is written exactly as stored — NOT auto-recomputed from
        # frame_count. A header-only-corrupted file can have frame_count==0
        # while its SMP1 tail still carries the ORIGINAL sample's loop_end.
        # Serialization must be lossless pass-through; a caller that resizes
        # pcm (crop, WAV re-import) owns re-deriving loop_start/loop_end/
        # sample_start itself.
        tail = bytearray(16)
        riff.write_u32be(tail, 0, self.sample_start)
        riff.write_u32be(tail, 4, self.loop_start)
        riff.write_u32be(tail, 8, self._preserved_loop_dup if self._preserved_loop_dup is not None else self.loop_start)
        riff.write_u32be(tail, 12, self.loop_end)

        smp1 = riff.encode_name_field(self.name, self.suffix, 16) + bytes(tail)
        sno1 = riff.u32be_bytes(self.sno1)
        name_chunk = riff.encode_name_field(self.name, self.suffix, 24)

        loop_tune_byte = self._loop_tune & 0xFF
        sub = bytearray([0, 0, 0, 0, self.flags, loop_tune_byte, self.channels, self.bits])
        riff.write_u32be(sub, 0, self.sample_rate)
        smd1 = bytes(sub) + riff.u32be_bytes(frame_count) + self.pcm

        parts = [
            riff.build_chunk("SMP1", smp1),
            riff.build_chunk("SNO1", sno1),
            riff.build_chunk("NAME", name_chunk),
        ]
        if self._smf1 is not None:
            parts.append(riff.build_chunk("SMF1", self._smf1))
        parts.append(riff.build_chunk("SMD1", smd1))
        return b"".join(parts)

    def samples(self) -> np.ndarray:
        return ksf_pcm.to_host_order(self.pcm)

    def set_samples(self, values: np.ndarray) -> None:
        self.pcm = ksf_pcm.to_big_endian_bytes(values)

    @staticmethod
    def read_sno1(path: str) -> Optional[int]:
        """Read ONLY the SNO1 chunk, seeking past every other payload instead
        of materializing the whole file (which can be megabytes of PCM) —
        used by NextFreeSno1-style scans that touch every .KSF in a
        collection just to read a 4-byte field. Chunk order puts SNO1 second
        (SMP1 -> SNO1 -> ...), so in practice this touches ~60 bytes."""
        try:
            with open(path, "rb") as f:
                while True:
                    header = f.read(8)
                    if len(header) < 8:
                        return None
                    tag = header[0:4].decode("ascii", errors="replace")
                    length = riff.read_u32be(header, 4)
                    if tag == "SNO1":
                        if length < 4:
                            return None
                        payload = f.read(4)
                        return riff.read_u32be(payload, 0) if len(payload) == 4 else None
                    size = os.fstat(f.fileno()).st_size
                    remaining = size - f.tell()
                    f.seek(min(length, max(0, remaining)), os.SEEK_CUR)
        except OSError:
            return None

    def save(self, path: Optional[str] = None) -> None:
        path = path or self.path
        if path is None:
            raise ValueError("no path given and none stored")
        atomic_write_bytes(pathlib.Path(path), self.to_bytes())
        self.path = path
