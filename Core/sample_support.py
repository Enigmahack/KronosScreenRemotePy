r"""
Small Sample Editor support types — ports of Core/Sample/SampleClipboard.cs, SampleNormalizationReport.cs,
SampleWorkspace.cs and SampleZoneCreatePreferences.cs.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, replace
from typing import List, Optional

import numpy as np

from Data.kmp_multisample import KmpMultisample
from Data.ksc_collection import KscCollection
from Data.ksf_sample import KsfSample


# ── In-app waveform clipboard ────────────────────────────────────────────────────────────────

class SampleClipboard:
    """The Sample Editor's Cut/Copy/Paste slot — deliberately NOT the OS clipboard (raw PCM has no
    standard clipboard format worth interoperating with). One slot, process lifetime."""
    pcm: Optional[np.ndarray] = None
    sample_rate: int = 0

    @classmethod
    def has_content(cls) -> bool:
        return cls.pcm is not None and len(cls.pcm) > 0

    @classmethod
    def set(cls, pcm: np.ndarray, sample_rate: int) -> None:
        cls.pcm = np.array(pcm, dtype=np.int16, copy=True)
        cls.sample_rate = int(sample_rate)

    @classmethod
    def clear(cls) -> None:
        cls.pcm, cls.sample_rate = None, 0


# ── Workspace root ───────────────────────────────────────────────────────────────────────────

def resolve_workspace_root(settings) -> str:
    """Local root for content pulled from the Kronos. Defaults under the OS temp dir, NOT the data dir:
    that is routinely an SMB share, and pulled sample audio is disposable working content that belongs
    neither on a slow mount nor in anything that must survive a reinstall."""
    root = (getattr(settings, "sample_workspace_root", "") or "").strip()
    return root if root else os.path.join(tempfile.gettempdir(), "kronos_sample_workspace")


# ── Create-zone preferences ──────────────────────────────────────────────────────────────────

ZONE_POSITION_RIGHT, ZONE_POSITION_LEFT = "Right", "Left"
ORIGINAL_KEY_BOTTOM, ORIGINAL_KEY_CENTER, ORIGINAL_KEY_TOP = "Bottom", "Center", "Top"


# ── Normalization report ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SampleNormalizationEntry:
    location: str
    sample_name: str
    sample_rate: int
    bits: int
    channels: int
    is_header_only: bool
    flagged: bool


def build_normalization_report(collection: KscCollection, ksc_path: str) -> List[SampleNormalizationEntry]:
    """Sample-rate/bit-depth consistency across a whole collection. The Kronos itself never warns about
    mixed-format content; this is a quick way to spot the outlier before finalizing/pushing."""
    entries: List[SampleNormalizationEntry] = []
    ksc_dir = os.path.dirname(ksc_path)
    ksc_base = os.path.splitext(os.path.basename(ksc_path))[0]
    for entry in collection.entries:
        if not entry.upper().endswith(".KMP"):
            continue
        kmp_path = os.path.join(ksc_dir, ksc_base, entry)
        try:
            with open(kmp_path, "rb") as f:
                m = KmpMultisample.open(f.read()) if os.path.exists(kmp_path) else None
        except Exception:
            m = None
        if m is None:
            continue
        for zone in m.zones:
            if zone.is_skipped:
                continue
            ksf_path = zone.ksf_path(kmp_path)
            try:
                with open(ksf_path, "rb") as f:
                    s = KsfSample.open(f.read()) if os.path.exists(ksf_path) else None
            except Exception:
                s = None
            if s is None:
                continue
            entries.append(SampleNormalizationEntry(
                f"{os.path.basename(kmp_path)}/{zone.filename}", s.name + s.suffix,
                int(s.sample_rate), s.bits, s.channels, s.is_header_only, False))
    if entries:
        # majority = most common value; ties resolve to the first one seen (LINQ OrderByDescending is stable)
        def majority(values):
            counts: dict = {}
            for v in values:
                counts[v] = counts.get(v, 0) + 1
            return max(counts, key=lambda k: counts[k])
        rate = majority(e.sample_rate for e in entries)
        bits = majority(e.bits for e in entries)
        entries = [replace(e, flagged=e.is_header_only or e.sample_rate != rate or e.bits != bits) for e in entries]
    return entries
