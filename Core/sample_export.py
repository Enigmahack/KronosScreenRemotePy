r"""
Sample export — port of Core/Sample/SampleExport.cs.

Semantics, stated once so they never drift: exporting a single .KSF writes one WAV; exporting a .KSC
bulk-exports every zone .KSF referenced by every .KMP it lists, each to its own WAV, named after the
sample itself (not the zone's cryptic MSxxxyyy.KSF filename) so a folder of WAVs is browsable. No code
path here ever treats writing a .KSC/.KMP/.KSF as "export" — that is the import/build side.
"""
from __future__ import annotations

import logging
import os
import re
import wave
from typing import Set, Tuple

import Core.sample_link_resolver as link_resolver
from Data.kmp_multisample import KmpMultisample
from Data.ksc_collection import KscCollection
from Data.ksf_sample import KsfSample

log = logging.getLogger(__name__)
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def export_sample_to_wav(sample: KsfSample, wav_path: str, kmp_path: str = None) -> bool:
    """Header-only (zero-frame) samples are skipped, never exported as a 0-sample WAV (doc §3.3's real
    failure mode). `kmp_path` lets a doc §3.2 LINKED stub (SMF1 pointing at another .KSF's PCM) export its
    actual audio instead of being skipped as corrupted. False = nothing written, never raises for a skip."""
    playable = link_resolver.resolve_playable(sample, kmp_path) if kmp_path is not None else sample
    if playable.is_header_only:
        return False
    os.makedirs(os.path.dirname(wav_path) or ".", exist_ok=True)
    pcm = playable.samples()
    with wave.open(wav_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(playable.sample_rate))
        w.writeframes(pcm.astype("<i2").tobytes())
    return True


def make_unique_file_name(used: Set[str], desired: str) -> str:
    sanitized = _INVALID_FILENAME_CHARS.sub("_", desired).strip()
    if not sanitized:
        sanitized = "sample"
    candidate, n = sanitized, 1
    while candidate.lower() in used:           # case-insensitive, like the Windows filesystem
        candidate = f"{sanitized}_{n}"
        n += 1
    used.add(candidate.lower())
    return candidate


def _export_multisample_zones(m: KmpMultisample, kmp_path: str, output_dir: str, used: Set[str]) -> Tuple[int, int]:
    exported = skipped = 0
    for zone in m.zones:
        if zone.is_skipped:
            continue
        ksf_path = zone.ksf_path(kmp_path)
        s = None
        try:
            if os.path.exists(ksf_path):
                with open(ksf_path, "rb") as f:
                    s = KsfSample.open(f.read())
        except Exception as e:
            log.warning("Sample export: skipping unreadable sample '%s': %s", ksf_path, e)
        if s is None:
            skipped += 1
            continue
        base = make_unique_file_name(used, f"{s.name}{s.suffix}")
        if export_sample_to_wav(s, os.path.join(output_dir, base + ".wav"), kmp_path):
            exported += 1
        else:
            skipped += 1
    return exported, skipped


def export_multisample(m: KmpMultisample, kmp_path: str, output_dir: str) -> Tuple[int, int]:
    """Every non-skipped zone of ONE multisample -> <output_dir>/<sample-name><suffix>.wav."""
    os.makedirs(output_dir, exist_ok=True)
    return _export_multisample_zones(m, kmp_path, output_dir, set())


def export_collection(collection: KscCollection, ksc_path: str, output_dir: str) -> Tuple[int, int]:
    """Every .KMP the .KSC lists. Returns (exported, skipped); an unreadable multisample or sample counts
    as skipped and never throws out of a bulk export."""
    os.makedirs(output_dir, exist_ok=True)
    exported = skipped = 0
    ksc_dir = os.path.dirname(ksc_path)
    ksc_base = os.path.splitext(os.path.basename(ksc_path))[0]
    used: Set[str] = set()
    for entry in collection.entries:
        if not entry.upper().endswith(".KMP"):
            continue
        kmp_path = os.path.join(ksc_dir, ksc_base, entry)
        m = None
        try:
            if os.path.exists(kmp_path):
                with open(kmp_path, "rb") as f:
                    m = KmpMultisample.open(f.read())
        except Exception as e:
            log.warning("Sample export: skipping unreadable multisample '%s': %s", kmp_path, e)
        if m is None:
            skipped += 1
            continue
        e_, s_ = _export_multisample_zones(m, kmp_path, output_dir, used)
        exported += e_
        skipped += s_
    return exported, skipped
