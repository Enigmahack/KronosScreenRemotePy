r"""
Sample Editor undo — port of Core/Sample/SampleEditUndo.cs + SampleZoneUndo.cs.

TWO independent stacks (arbitrated by the model's `_undo_domains`, see Core/sample_editor_model.py):

  SampleEditUndo — full pre-edit SNAPSHOTS of one sample (PCM + the marker/flag fields a drag can
      change without touching PCM). A waveform snapshot is a multi-MB buffer and most PCM edits (crop,
      tempo/pitch, destructive filters) have no cheap analytic inverse, so this is bounded by a BYTE
      cap (AppSettings.sample_undo_byte_cap_mb), FIFO-evicting the oldest step, not by a step count.
      Cap accounting counts len(pcm) even for field-only edits that reuse the same Pcm reference — a
      deliberate over-count that evicts a little eagerly rather than needing per-kind accounting.

  SampleZoneUndo — snapshots of one multisample's whole zone LIST (order + every mutable per-zone
      field), a handful of small structs, so a plain step-count cap.

Snapshots hold the PCM BY REFERENCE. That is correct only because every PCM-mutating edit REPLACES
KsfSample.pcm with a brand-new bytes object (bytes are immutable in Python anyway).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Optional, Tuple

from Data.kmp_multisample import KmpZone
from Data.ksf_sample import KsfSample

DEFAULT_BYTE_CAP_MB = 256


@dataclass(frozen=True)
class SampleFieldSnapshot:
    """Everything Undo/Redo restores for one sample: PCM plus the fields marker drags and field edits
    change without touching PCM. ONE snapshot type (not separate PCM-edit / field-edit stacks) keeps
    Undo chronologically correct for free: the most recent edit of either kind is simply the top."""
    pcm: bytes
    sample_start: int
    loop_start: int
    loop_end: int
    flags: int
    preserved_loop_dup: Optional[int] = None   # the offset-24 duplicate slot; must round-trip or an
    loop_tune: int = 0                         # undo silently changes bytes that were byte-identical

    @staticmethod
    def of(s: KsfSample) -> "SampleFieldSnapshot":
        return SampleFieldSnapshot(s.pcm, s.sample_start, s.loop_start, s.loop_end, s.flags,
                                   s.preserved_loop_dup, s.loop_tune)

    def apply_to(self, s: KsfSample) -> None:
        s.pcm = self.pcm
        s.sample_start = self.sample_start
        s.loop_start = self.loop_start
        s.loop_end = self.loop_end
        s.flags = self.flags
        s.restore_preserved_loop_dup(self.preserved_loop_dup)
        s.restore_loop_tune(self.loop_tune)


class SampleEditUndo:
    def __init__(self, byte_cap: int = DEFAULT_BYTE_CAP_MB * 1024 * 1024):
        self.byte_cap = byte_cap
        self._undo: Deque[SampleFieldSnapshot] = deque()
        self._redo: Deque[SampleFieldSnapshot] = deque()
        self._undo_bytes = 0
        self._redo_bytes = 0
        self.evicted_since_last_check = 0

    @property
    def undo_count(self) -> int: return len(self._undo)
    @property
    def redo_count(self) -> int: return len(self._redo)
    @property
    def can_undo(self) -> bool: return bool(self._undo)
    @property
    def can_redo(self) -> bool: return bool(self._redo)

    def take_evicted_count(self) -> int:
        """Steps silently dropped past the cap since last read — the UI reports 'N earlier step(s) no
        longer available' instead of undo just quietly stopping."""
        n, self.evicted_since_last_check = self.evicted_since_last_check, 0
        return n

    def record_before_edit(self, pre_edit: SampleFieldSnapshot) -> None:
        """Call BEFORE applying an edit. A fresh edit invalidates whatever was undone."""
        self._redo.clear()
        self._redo_bytes = 0
        self._undo.append(pre_edit)
        self._undo_bytes += len(pre_edit.pcm)
        self._evict(self._undo, "_undo_bytes")

    def undo(self, current: SampleFieldSnapshot) -> Optional[SampleFieldSnapshot]:
        if not self._undo:
            return None
        restored = self._undo.pop()
        self._undo_bytes -= len(restored.pcm)
        self._redo.append(current)
        self._redo_bytes += len(current.pcm)
        self._evict(self._redo, "_redo_bytes")
        return restored

    def redo(self, current: SampleFieldSnapshot) -> Optional[SampleFieldSnapshot]:
        if not self._redo:
            return None
        restored = self._redo.pop()
        self._redo_bytes -= len(restored.pcm)
        self._undo.append(current)
        self._undo_bytes += len(current.pcm)
        self._evict(self._undo, "_undo_bytes")
        return restored

    def _evict(self, stack: Deque[SampleFieldSnapshot], bytes_attr: str) -> None:
        while getattr(self, bytes_attr) > self.byte_cap and stack:
            oldest = stack.popleft()
            setattr(self, bytes_attr, getattr(self, bytes_attr) - len(oldest.pcm))
            self.evicted_since_last_check += 1


# ── Zone lists ───────────────────────────────────────────────────────────────────────────────

def _clone_zone(z: KmpZone) -> KmpZone:
    return KmpZone(original_key=z.original_key, top_key=z.top_key, filename=z.filename,
                   unknown4=bytes(z.unknown4), rlp3=bytes(z.rlp3), rlp2=bytes(z.rlp2))


def _copy_zone_into(src: KmpZone, dst: KmpZone) -> None:
    dst.original_key, dst.top_key, dst.filename = src.original_key, src.top_key, src.filename
    dst.unknown4, dst.rlp3, dst.rlp2 = bytes(src.unknown4), bytes(src.rlp3), bytes(src.rlp2)


@dataclass
class ZoneListSnapshot:
    """One-or-more zone lists captured as one atomic step (a stereo pair mirrors every key-range edit
    onto the sibling's list — undoing only the clicked half would re-introduce the very divergence the
    mirroring prevents). Each entry pairs the LIVE KmpZone (never recreated) with a CLONE of its fields,
    so a drag-reorder restores both the field values AND the list order through the SAME objects —
    anything holding a direct reference to a zone (selected zone, tree nodes, the keymap) stays valid.
    A zone added after the snapshot is simply absent from `lists` and drops out; one deleted after it
    still has a live reference to add back."""
    lists: List[Tuple[List[KmpZone], List[Tuple[KmpZone, KmpZone]]]]

    @staticmethod
    def of(*zone_lists: Optional[List[KmpZone]]) -> "ZoneListSnapshot":
        # None is skipped — the sibling list is legitimately absent for a mono multisample.
        return ZoneListSnapshot([(zl, [(z, _clone_zone(z)) for z in zl]) for zl in zone_lists if zl is not None])

    def apply_to(self) -> None:
        for target, entries in self.lists:
            for live, snap in entries:
                _copy_zone_into(snap, live)
            target[:] = [live for live, _ in entries]


class SampleZoneUndo:
    def __init__(self, step_cap: int = 50):
        self.step_cap = step_cap
        self._undo: Deque[ZoneListSnapshot] = deque()
        self._redo: Deque[ZoneListSnapshot] = deque()

    @property
    def can_undo(self) -> bool: return bool(self._undo)
    @property
    def can_redo(self) -> bool: return bool(self._redo)

    def record_before_edit(self, pre_edit: ZoneListSnapshot) -> None:
        self._redo.clear()
        self._undo.append(pre_edit)
        if len(self._undo) > self.step_cap:
            self._undo.popleft()

    def undo(self, current: ZoneListSnapshot) -> Optional[ZoneListSnapshot]:
        if not self._undo:
            return None
        restored = self._undo.pop()
        self._redo.append(current)
        return restored

    def redo(self, current: ZoneListSnapshot) -> Optional[ZoneListSnapshot]:
        if not self._redo:
            return None
        restored = self._redo.pop()
        self._undo.append(current)
        return restored


# ── Self-test (python -m Core.sample_edit_undo) ───────────────────────────────────────────────

def _selftest() -> None:
    import sys
    fails: List[str] = []

    def check(name, cond):
        if not cond:
            fails.append(name)

    def snap(n=10, **kw):
        base = dict(pcm=bytes(n), sample_start=0, loop_start=0, loop_end=0, flags=0x81)
        base.update(kw)
        return SampleFieldSnapshot(**base)

    u = SampleEditUndo(byte_cap=100)
    check("starts-empty", not u.can_undo and not u.can_redo)
    a, b, c = snap(10, loop_start=1), snap(10, loop_start=2), snap(10, loop_start=3)
    u.record_before_edit(a); u.record_before_edit(b)
    check("two-steps", u.undo_count == 2 and u.can_undo)
    r = u.undo(c)
    check("undo-returns-latest", r is b and u.can_redo and u.undo_count == 1)
    r2 = u.redo(b)
    check("redo-returns-undone-state", r2 is c and not u.can_redo)
    u.undo(c)
    u.record_before_edit(a)
    check("new-edit-clears-redo", not u.can_redo)
    check("undo-with-nothing-is-none", SampleEditUndo(1000).undo(a) is None and SampleEditUndo(1000).redo(a) is None)

    # byte cap: FIFO eviction, counted, resettable
    u = SampleEditUndo(byte_cap=35)
    for i in range(5):
        u.record_before_edit(snap(10, loop_start=i))     # 50 bytes recorded, cap 35 -> keeps the 3 newest
    check("fifo-eviction-keeps-newest", u.undo_count == 3 and u._undo[0].loop_start == 2)
    check("evicted-count-reported-once", u.take_evicted_count() == 2 and u.take_evicted_count() == 0)

    # redo side evicts too, and counts
    u = SampleEditUndo(byte_cap=20)
    u.record_before_edit(snap(10)); u.record_before_edit(snap(10))
    u.undo(snap(15)); u.undo(snap(15))
    check("redo-side-capped", u._redo_bytes <= 20 and u.take_evicted_count() >= 1)

    # snapshot of / apply_to round-trips every persisted field incl. the two easy-to-forget ones
    s = KsfSample(pcm=b"\x00\x01\x00\x02", sample_start=1, loop_start=2, loop_end=3, flags=0x91)
    s.restore_loop_tune(-7); s.restore_preserved_loop_dup(99)
    sn = SampleFieldSnapshot.of(s)
    s.pcm = b""; s.sample_start = 9; s.loop_start = 9; s.loop_end = 9; s.flags = 0
    s.restore_loop_tune(5); s.restore_preserved_loop_dup(None)
    sn.apply_to(s)
    check("apply-restores-pcm-and-markers", s.pcm == b"\x00\x01\x00\x02" and (s.sample_start, s.loop_start, s.loop_end) == (1, 2, 3))
    check("apply-restores-flags-tune-dup", s.flags == 0x91 and s.loop_tune == -7 and s.preserved_loop_dup == 99)

    # zone undo: identity + order + fields survive; add/delete correct for free
    z1, z2, z3 = KmpZone(60, 70, "A.KSF"), KmpZone(60, 80, "B.KSF"), KmpZone(60, 90, "C.KSF")
    lst = [z1, z2, z3]
    zu = SampleZoneUndo(3)
    pre = ZoneListSnapshot.of(lst)
    zu.record_before_edit(pre)
    lst[:] = [z3, z1]                      # reorder + delete z2
    z1.top_key = 65                        # and a field edit
    cur = ZoneListSnapshot.of(lst)
    restored = zu.undo(cur)
    restored.apply_to()
    check("zone-undo-order", lst == [z1, z2, z3])
    check("zone-undo-fields", z1.top_key == 70)
    check("zone-undo-identity", lst[0] is z1 and lst[1] is z2 and lst[2] is z3)
    zu.redo(ZoneListSnapshot.of(lst)).apply_to()
    check("zone-redo", lst == [z3, z1] and z1.top_key == 65)
    # add after snapshot: dropped by undo
    z4 = KmpZone(60, 100, "D.KSF")
    zu2 = SampleZoneUndo(); zu2.record_before_edit(ZoneListSnapshot.of(lst)); lst.append(z4)
    zu2.undo(ZoneListSnapshot.of(lst)).apply_to()
    check("zone-undo-drops-added", z4 not in lst)
    # a stereo pair: two lists restored atomically; None skipped
    la, lb = [KmpZone(60, 70, "L.KSF")], [KmpZone(60, 70, "R.KSF")]
    pair = ZoneListSnapshot.of(la, lb, None)
    la[0].top_key = lb[0].top_key = 99
    pair.apply_to()
    check("pair-restored-atomically", la[0].top_key == 70 and lb[0].top_key == 70 and len(pair.lists) == 2)
    # step cap
    zu3 = SampleZoneUndo(step_cap=2)
    for i in range(4):
        zu3.record_before_edit(ZoneListSnapshot.of([KmpZone(i, i, "x")]))
    check("zone-step-cap", len(zu3._undo) == 2)

    if fails:
        print("FAIL:", ", ".join(fails))
        sys.exit(1)
    print("sample_edit_undo self-test: OK")


if __name__ == "__main__":
    _selftest()
