"""`.KMP` — multisample/keymap (binary, CKorgRiff-framed, big-endian). Port of
C#'s Core/Sample/KmpMultisample.cs + KmpZone.cs.

Chunk order: MSP1 -> MNO1 -> NAME -> RLP1 -> RLP3 -> RLP2. See
kronosology/docs/interfaces/ksc_kmp_ksf_file_format.md §2 for the full
hardware-verified spec this implements — including why a stereo instrument is
TWO separate .KMP files (never two zones in one), and why the .KMP's own
filename must never carry the -L/-R suffix (real Kronos audio fails to load
otherwise, even though the entries still register).
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import Data.korg_riff_chunk as riff
import Data.sample_path_guard as path_guard
from Models.storage import atomic_write_bytes

_STEREO_PARTNER_SUFFIX = {"-L": "-R", "-R": "-L"}


@dataclass
class KmpZone:
    original_key: int = 60    # RLP1 offset 0 — root/tracking key
    top_key: int = 60         # RLP1 offset 1 — top of this zone's trigger key
                               # range; no separate bottom-key field — the
                               # range runs from (previous zone's top_key + 1)
                               # to this zone's own top_key
    filename: str = ""        # up to 12 bytes, e.g. "MS001000.KSF"
    # RLP1 bytes 2-5 — constant in every real record seen; confirmed NOT
    # original-key. Write this constant unless a future test finds real
    # meaning in it.
    unknown4: bytes = field(default_factory=lambda: bytes([0x00, 0x00, 0x40, 0x00]))
    rlp3: bytes = field(default_factory=lambda: bytes(6))  # opaque; offset+5 forced 0 on write
    rlp2: bytes = field(default_factory=lambda: bytes(4))  # opaque; contents unconfirmed

    @property
    def is_skipped(self) -> bool:
        """A deliberately-unsampled key position: real files mark these with
        the literal filename "SKIPPEDSAMPLE" (truncated to 12 chars) — no
        real .KSF backs it, don't try to open one."""
        return self.filename.upper().startswith("SKIPPEDSAMPL")

    def ksf_path(self, kmp_path: str) -> str:
        """Resolve this zone's .KSF path per the confirmed folder convention:
        <kmp-dir>/<kmp-basename-no-ext>/<filename>."""
        p = pathlib.Path(kmp_path)
        zone_dir = str(p.parent / p.stem)
        candidate = str(pathlib.Path(zone_dir) / self.filename)
        return path_guard.ensure_under(zone_dir, candidate, self.filename)


@dataclass
class KmpMultisample:
    name: str = "New Multisample"
    suffix: str = ""       # "", "-L", or "-R" — stereo channel marker
    mno1: int = 0           # the multisample's own numeric ID — matches the
                             # number in its own zones' .KSF filenames
    zones: List[KmpZone] = field(default_factory=list)
    path: Optional[str] = None

    @staticmethod
    def open(data: bytes) -> Optional["KmpMultisample"]:
        """None if `data` isn't a recognizable .KMP (first chunk isn't MSP1)
        rather than raising — expected file-picker input, not a bug."""
        chunks = riff.read_chunks(data)
        if not chunks or chunks[0][0] != "MSP1" or len(chunks[0][1]) < 18:
            return None

        m = KmpMultisample()
        rlp1: List[tuple] = []
        rlp3: List[bytes] = []
        rlp2: List[bytes] = []

        for tag, payload in chunks:
            if tag == "MSP1" and len(payload) >= 18:
                # Name/Suffix come from the 24-byte NAME chunk, not this
                # 16-byte short field. The trailing 2 bytes (zone count) are
                # NOT preserved from the source — to_bytes() always
                # recomputes them from len(zones).
                pass
            elif tag == "MNO1" and len(payload) >= 4:
                m.mno1 = riff.read_u32be(payload, 0)
            elif tag == "NAME" and len(payload) >= 24:
                name, suffix = riff.split_name_suffix(
                    payload[:24].decode("ascii", errors="replace"))
                m.name = name
                m.suffix = suffix
            elif tag == "RLP1":
                i = 0
                while i + 18 <= len(payload):
                    orig_key = payload[i]
                    top_key = payload[i + 1]
                    unknown4 = bytes(payload[i + 2:i + 6])
                    fname = payload[i + 6:i + 18].decode("ascii", errors="replace").rstrip("\x00 ")
                    rlp1.append((orig_key, top_key, fname, unknown4))
                    i += 18
            elif tag == "RLP3":
                i = 0
                while i + 6 <= len(payload):
                    rlp3.append(bytes(payload[i:i + 6]))
                    i += 6
            elif tag == "RLP2":
                i = 0
                while i + 4 <= len(payload):
                    rlp2.append(bytes(payload[i:i + 4]))
                    i += 4

        for idx, (orig_key, top_key, fname, unknown4) in enumerate(rlp1):
            z = KmpZone(original_key=orig_key, top_key=top_key, filename=fname, unknown4=unknown4)
            if idx < len(rlp3):
                z.rlp3 = rlp3[idx]
            if idx < len(rlp2):
                z.rlp2 = rlp2[idx]
            m.zones.append(z)
        return m

    def to_bytes(self) -> bytes:
        # MSP1's trailing 2 bytes: the multisample's own zone count, LE u16.
        # Left at 0, a real Kronos treats the multisample as zero-zone and
        # refuses to select it ("Create New Sample" prompt) even though the
        # underlying .KSF loads fine standalone. Always derive from
        # len(zones) — never round-trip a stale/loaded value.
        n = len(self.zones)
        msp1_tail = bytes([n & 0xFF, (n >> 8) & 0xFF])
        msp1 = riff.encode_name_field(self.name, self.suffix, 16) + msp1_tail
        mno1 = riff.u32be_bytes(self.mno1)
        name_chunk = riff.encode_name_field(self.name, self.suffix, 24)

        rlp1 = bytearray()
        rlp3 = bytearray()
        rlp2 = bytearray()
        for z in self.zones:
            rlp1.append(z.original_key & 0xFF)
            rlp1.append(z.top_key & 0xFF)
            u4 = bytearray(4)
            u4[:min(4, len(z.unknown4))] = z.unknown4[:4]
            rlp1 += u4
            rlp1 += riff.pad_bytes(z.filename, 12)

            # Offset+5 is always written 0 — matches Korg's own writer, which
            # masks it unconditionally regardless of the in-memory value.
            r3 = bytearray(6)
            r3[:min(5, len(z.rlp3))] = z.rlp3[:5]
            r3[5] = 0
            rlp3 += r3

            r2 = bytearray(4)
            r2[:min(4, len(z.rlp2))] = z.rlp2[:4]
            rlp2 += r2

        return b"".join([
            riff.build_chunk("MSP1", msp1),
            riff.build_chunk("MNO1", mno1),
            riff.build_chunk("NAME", name_chunk),
            riff.build_chunk("RLP1", bytes(rlp1)),
            riff.build_chunk("RLP3", bytes(rlp3)),
            riff.build_chunk("RLP2", bytes(rlp2)),
        ])

    def save(self, path: Optional[str] = None) -> None:
        path = path or self.path
        if path is None:
            raise ValueError("no path given and none stored")
        atomic_write_bytes(pathlib.Path(path), self.to_bytes())
        self.path = path

    @property
    def _zone_file_prefix(self) -> str:
        """"MS<mno1:03d>" for mno1 0-999, "M<mno1:04d>" for mno1 1000-3999 —
        both 5 characters, shared by next_ksf_filename/next_free_zone_filename."""
        return f"MS{self.mno1:03d}" if self.mno1 <= 999 else f"M{self.mno1:04d}"

    def next_ksf_filename(self) -> str:
        """The real naming convention for a brand-new zone being appended.
        ONLY valid for that case: len(zones) is "the new zone's own future
        index" exactly because the new zone hasn't been inserted yet. Do NOT
        reuse this for replacing an EXISTING zone's sample — see
        next_free_zone_filename for that case."""
        return f"{self._zone_file_prefix}{len(self.zones):03d}.KSF"

    def next_free_zone_filename(self) -> str:
        """Collision-free filename for giving an EXISTING zone new/different
        audio — scans every OTHER zone's own current filename for the
        numeric suffix already in use and returns the smallest index not
        currently claimed."""
        prefix = self._zone_file_prefix
        used = set()
        for z in self.zones:
            fn = z.filename
            if (len(fn) == len(prefix) + 7  # prefix + "###" + ".KSF"
                    and fn.upper().startswith(prefix.upper())
                    and fn.upper().endswith(".KSF")):
                try:
                    used.add(int(fn[len(prefix):len(prefix) + 3]))
                except ValueError:
                    pass
        i = 0
        while i in used:
            i += 1
        return f"{prefix}{i:03d}.KSF"

    @staticmethod
    def auto_file_name(name: str, mno1: int) -> str:
        """<first 5 chars of Name, sanitized+uppercased, underscore-padded>
        <mno1:03d>.KMP — the real Kronos auto-naming convention for a .KMP's
        own FILENAME (e.g. "GAGA LEAD" -> "GAGA_000.KMP"). Derived from Name,
        NEVER Suffix — a stereo pair saved as "<Name>-L.KMP"/"<Name>-R.KMP"
        registers correctly but its audio never actually loads on real
        hardware; the L/R marker belongs only in the internal Suffix field.
        Tiered above mno1 999 (the format's own ~3999-keymap ceiling): name
        width shrinks 5->4 chars, index width grows 3->4 digits, always
        summing to the fixed 8-char DOS 8.3 stem."""
        mno1 = min(mno1, 3999)
        sanitized = "".join(c if c.isalnum() else "_" for c in name.upper())
        name_width, index_width = (5, 3) if mno1 <= 999 else (4, 4)
        prefix = sanitized[:name_width] if len(sanitized) >= name_width else sanitized.ljust(name_width, "_")
        return f"{prefix}{mno1:0{index_width}d}.KMP"


def find_stereo_sibling(
        kmp_cache: Dict[str, "KmpMultisample"],
        m: "KmpMultisample") -> Optional[Tuple[str, "KmpMultisample"]]:
    """The other half of `m`'s stereo pair among the collection's already-
    loaded multisamples, or None — a mono multisample (empty suffix) has no
    sibling. No stored pairing relationship exists on disk in either the C#
    or Python data model (§2 of the format doc: stereo pairing is resolved
    dynamically); matched here the same way C# does it throughout
    SampleEditorViewModel.cs — same Name, opposite -L/-R Suffix, among
    multisamples already known to the caller (never a fresh disk scan)."""
    partner_suffix = _STEREO_PARTNER_SUFFIX.get(m.suffix)
    if partner_suffix is None:
        return None
    for path, other in kmp_cache.items():
        if other is not m and other.name == m.name and other.suffix == partner_suffix:
            return path, other
    return None
