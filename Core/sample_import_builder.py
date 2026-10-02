r"""
Builds brand-new .KSF + KmpZone content from already-decoded 44100 Hz PCM and adds it to target
multisample(s) at a key range — port of Core/Sample/SampleImportBuilder.cs. "Import audio" is really
"construct real zones the same way the Kronos itself would, then let the existing Save path write them".

Covers mono (one multisample, one zone) and stereo (a matched PAIR of multisamples, one zone each) —
kronosology ksc_kmp_ksf_file_format.md §2.2: a Kronos stereo instrument is two full multisamples with the
same Name and opposite "-L"/"-R" Suffix, NOT two zones inside one .KMP (RLP1 zones have no channel field).
"""
from __future__ import annotations

import os
from typing import Optional, Tuple

import numpy as np

import Data.ksc_collection as ksc_mod
from Data.kmp_multisample import KmpMultisample, KmpZone
from Data.ksc_collection import KscCollection
from Data.ksf_sample import KsfSample

# Hardware limit (RLP1 has no room past 128 entries) — enforced in the single function every zone-adding
# path funnels through, so no entry point can bypass the cap.
MAX_ZONES_PER_MULTISAMPLE = 128

SKIPPED_SAMPLE_FILENAME = "SKIPPEDSAMPLE"


def add_sample_zone(m: KmpMultisample, kmp_path: str, sample_name: str, pcm: np.ndarray,
                    sample_rate: int, original_key: int, top_key: int, suffix: str = "",
                    sno1: Optional[int] = None) -> KmpZone:
    """Inserts the new zone in TOP-KEY order (zone order IS key-range order) and writes its .KSF into
    <kmp-dir>/<kmp-basename>/. Does NOT save the .KMP — that is the caller's job. `suffix` bakes
    "-L"/"-R"/"" into the .KSF Name+Suffix. `sno1` lets a batch hand out ids from one Sno1Allocator scan
    instead of rescanning the disk per import."""
    if len(m.zones) >= MAX_ZONES_PER_MULTISAMPLE:
        raise ValueError(f"'{m.name}{m.suffix}' already has {MAX_ZONES_PER_MULTISAMPLE} zones (the maximum) "
                         "- remove one before adding another.")
    zone = KmpZone(original_key=max(0, min(127, original_key)), top_key=max(0, min(127, top_key)),
                   filename=m.next_ksf_filename())
    insert_at = next((i for i, z in enumerate(m.zones) if z.top_key > zone.top_key), len(m.zones))
    m.zones.insert(insert_at, zone)

    # flags 0x81 deliberately, not inherited: one-shot + +12 dB boost on, Reverse off, tune 0 — the right
    # state for brand-new audio, so nothing can leak over from whatever was previously loaded.
    # sno1 must be unique across the collection or .KSC bulk loading silently drops the colliding audio.
    content_dir = os.path.dirname(kmp_path) or "."
    ksf = KsfSample(name=sample_name, suffix=suffix, sample_rate=int(sample_rate), flags=0x81,
                    sno1=ksc_mod.next_free_sno1(content_dir) if sno1 is None else sno1)
    ksf.set_samples(pcm)
    ksf_path = zone.ksf_path(kmp_path)
    os.makedirs(os.path.dirname(ksf_path), exist_ok=True)
    ksf.save(ksf_path)
    return zone


def add_stereo_sample_zone_pair(left: KmpMultisample, left_kmp_path: str, right: KmpMultisample,
                                right_kmp_path: str, sample_name: str, left_pcm: np.ndarray,
                                right_pcm: np.ndarray, sample_rate: int, original_key: int,
                                top_key: int) -> Tuple[KmpZone, KmpZone]:
    """A matching zone (same key range, same base name, opposite -L/-R suffix) in both halves of an
    existing stereo pair. Does NOT save either .KMP. Both halves are checked BEFORE either is written so
    a full right half cannot leave an orphaned left zone/file behind."""
    for m in (left, right):
        if len(m.zones) >= MAX_ZONES_PER_MULTISAMPLE:
            raise ValueError(f"'{m.name}{m.suffix}' already has {MAX_ZONES_PER_MULTISAMPLE} zones (the maximum) "
                             "- remove one before adding another.")
    l = add_sample_zone(left, left_kmp_path, sample_name, left_pcm, sample_rate, original_key, top_key, "-L")
    r = add_sample_zone(right, right_kmp_path, sample_name, right_pcm, sample_rate, original_key, top_key, "-R")
    return l, r


def make_default_first_zone() -> KmpZone:
    """A brand-new multisample's auto-created first zone (Create Multisample, mono or stereo): original
    key C2 (36) and top key C2, so its trigger range runs C-1..C2 (MIDI 0..36) — matching real Kronos
    behaviour — with the SKIPPEDSAMPLE placeholder; real audio is attached afterwards via Import Sample /
    Assign. Deliberately NOT the full 0-127 range AddPlaceholderZone gives a manually-added first zone."""
    return KmpZone(filename=SKIPPED_SAMPLE_FILENAME, original_key=36, top_key=36)


def create_stereo_multisample_pair(collection: KscCollection, collection_path: str, base_name: str,
                                   mno1_left: int):
    """Two multisamples with identical Name, Suffix -L/-R, MNO1 = mno1_left / +1 (adjacency matches every
    real Kronos-authored pair; nothing reads it as load-bearing). The .KMP FILENAMES follow Kronos'
    auto-naming (Name prefix + MNO1) and must NOT bake -L/-R in: a real Kronos silently fails to load the
    audio behind a "<Name>-L.KMP"/"-R.KMP" pair even though every other byte is right. Saves both .KMP
    files, adds both to the collection and saves it. -> (left, left_path, right, right_path)."""
    kmp_dir = ksc_mod.content_dir_for(collection_path)
    os.makedirs(kmp_dir, exist_ok=True)
    left = KmpMultisample(name=base_name, suffix="-L", mno1=mno1_left)
    right = KmpMultisample(name=base_name, suffix="-R", mno1=mno1_left + 1)
    left_name = KmpMultisample.auto_file_name(base_name, mno1_left)
    right_name = KmpMultisample.auto_file_name(base_name, mno1_left + 1)
    left_path, right_path = os.path.join(kmp_dir, left_name), os.path.join(kmp_dir, right_name)
    left.save(left_path)
    right.save(right_path)
    collection.entries.append(left_name)
    collection.entries.append(right_name)
    collection.save(collection_path)
    return left, left_path, right, right_path


def find_stereo_sibling_on_disk(collection: KscCollection, m: KmpMultisample,
                                kmp_path: str) -> Tuple[Optional[KmpMultisample], Optional[str]]:
    """`m`'s stereo-pair sibling within the same collection, read from disk: same Name, opposite Suffix
    ("-L" <-> "-R"), AND adjacent MNO1 (m.mno1 +/- 1). Name+Suffix alone isn't enough: several unrelated,
    never-renamed multisamples in one collection carry the Kronos' unedited default name, so name-only
    matching would 'pair' two multisamples that were never a stereo instrument. Every genuine pair examined
    has adjacent MNO1; requiring it rejects that false positive. (None, None) if `m` isn't -L/-R or no match
    (a lone "-L" with no "-R" counterpart is valid and just plays mono).

    Known limitation, same as C#: an unrelated mono -L/-R multisample that happens to land MNO1-adjacent to
    a real pair can still match — the format has no stronger pairing signal (§2.2)."""
    if m.suffix not in ("-L", "-R"):
        return None, None
    want = "-R" if m.suffix == "-L" else "-L"
    kmp_dir = os.path.dirname(kmp_path)
    for entry in collection.entries:
        if not entry.upper().endswith(".KMP"):
            continue
        candidate_path = os.path.join(kmp_dir, entry)
        if os.path.normcase(candidate_path) == os.path.normcase(kmp_path) or not os.path.exists(candidate_path):
            continue
        try:
            with open(candidate_path, "rb") as f:
                cand = KmpMultisample.open(f.read())
        except Exception:
            cand = None
        if cand is None:
            continue
        adjacent = cand.mno1 == m.mno1 + 1 or (m.mno1 > 0 and cand.mno1 == m.mno1 - 1)
        if cand.name == m.name and cand.suffix == want and adjacent:
            return cand, candidate_path
    return None, None
