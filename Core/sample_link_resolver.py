"""Resolve a header-only (stub) `.KSF`'s SMF1 link to the real `.KSF` holding
its PCM. Port of C#'s `Core/Sample/SampleLinkResolver.cs`.

This is a convenience heuristic for THIS APP's own read-side use (waveform/
playback/export) — NOT what a real Kronos uses at runtime. Real hardware
resolves a link via SNO1-collision at `.KSC` bulk-import time and a separate
per-bank resident-sample cache at playback time (kronosology doc §3.2);
SMF1 has no OA.ko consumer at all. In every ground-truthed real fixture this
heuristic lands on the same file the real SNO1 mechanism would, but it is
still just a heuristic: it walks the collection's content directory for a
file named after SMF1's stored filename and prefers (but does not require)
one whose own SNO1 also matches the stub's.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

from Data.ksf_sample import KsfSample

log = logging.getLogger(__name__)


@dataclass
class LinkResolution:
    sample: KsfSample
    target_path: str
    sno1_verified: bool


def resolve(stub: KsfSample, kmp_path: str) -> Optional[LinkResolution]:
    """None if `stub` has no SMF1 target, or nothing playable is found.

    Searches the whole collection content directory (dirname of kmp_path) —
    a stub can link to a zone belonging to a DIFFERENT multisample in the
    same collection, this is real and doc-confirmed, not scoped to the
    stub's own zone folder. Skips any candidate that is itself header-only
    (no multi-hop chasing — deliberate, unobserved in any real fixture).
    Prefers a candidate whose own sno1 matches the stub's; falls back to the
    first playable candidate found if none match by sno1."""
    target_name = stub.stub_target_filename
    if not target_name:
        return None
    content_dir = os.path.dirname(kmp_path)
    fallback: Optional[tuple[KsfSample, str]] = None
    try:
        for root, _dirs, files in os.walk(content_dir):
            for name in files:
                if name.upper() != target_name.upper():
                    continue
                candidate_path = os.path.join(root, name)
                try:
                    with open(candidate_path, "rb") as f:
                        candidate = KsfSample.open(f.read())
                except OSError as ex:
                    log.warning("sample link resolve: couldn't read '%s': %s", candidate_path, ex)
                    continue
                if candidate is None or candidate.is_header_only:
                    continue
                if candidate.sno1 == stub.sno1:
                    return LinkResolution(candidate, candidate_path, True)
                if fallback is None:
                    fallback = (candidate, candidate_path)
    except OSError as ex:
        log.warning("sample link resolve: directory walk failed under '%s': %s", content_dir, ex)
        return None
    if fallback is None:
        return None
    sample, path = fallback
    return LinkResolution(sample, path, False)


def resolve_playable(stub: KsfSample, kmp_path: str) -> KsfSample:
    """Returns `stub` unchanged if it isn't header-only or nothing resolves,
    else a synthetic playable view built from the resolved link."""
    if not stub.is_header_only:
        return stub
    resolution = resolve(stub, kmp_path)
    if resolution is None:
        return stub
    return build_playable_view(stub, resolution.sample)


def build_playable_view(stub: KsfSample, linked_audio: KsfSample) -> KsfSample:
    """A SYNTHETIC KsfSample: every field comes from `stub` except `pcm`,
    which comes from `linked_audio`.

    CRITICAL — never assign this back into a zone's live/selected KsfSample
    slot. It carries no SMF1 chunk and no preserved-loop-dup slot, so saving
    it would silently turn a link into a full duplicate copy on disk. Build
    for read-only use (waveform display, playback, export) only."""
    view = KsfSample(
        name=stub.name,
        suffix=stub.suffix,
        sno1=stub.sno1,
        sample_rate=stub.sample_rate,
        flags=stub.flags,
        channels=stub.channels,
        bits=stub.bits,
        pcm=linked_audio.pcm,
        sample_start=stub.sample_start,
        loop_start=stub.loop_start,
        loop_end=stub.loop_end,
    )
    view.restore_loop_tune(stub.loop_tune)
    return view
