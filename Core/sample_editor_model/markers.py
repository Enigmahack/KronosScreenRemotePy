r"""
Sample Editor model — marker / flag edits (Sample Start, Loop Start, Loop End, loop on/off, reverse, +12 dB
boost, loop tune, loop-region drag) and Use-Zero-crossing snapping. Port of SampleEditorViewModel.cs ~4239-4536.
"""
from __future__ import annotations

import enum
from typing import Optional

import numpy as np

from Core.sample_edit_undo import SampleFieldSnapshot
from Core.sample_editor_model.core import DOMAIN_SAMPLE


class SampleMarkerKind(enum.Enum):
    SAMPLE_START = "SampleStart"
    LOOP_START = "LoopStart"
    LOOP_END = "LoopEnd"


def is_zero_crossing(pcm: np.ndarray, i: int) -> bool:
    a, b = pcm[i - 1], pcm[i]
    return (a <= 0 and b >= 0) or (a >= 0 and b <= 0)


def nearest_zero_crossing(pcm: np.ndarray, frame: int) -> Optional[int]:
    """Nearest zero-crossing to `frame`, searched outward both ways at once so it is the CLOSEST, not the first
    in one direction. None (not the unchanged frame) when there is none — 'not found' must never look like
    'found at distance 0', or a crossing-free channel would win every comparison against a real one."""
    n = len(pcm)
    if n < 2:
        return None
    frame = max(0, min(frame, n - 1))
    # vectorised: crossing indices in [1, n-1]
    a, b = pcm[:-1].astype(np.int32), pcm[1:].astype(np.int32)
    idx = np.nonzero(((a <= 0) & (b >= 0)) | ((a >= 0) & (b <= 0)))[0] + 1
    if len(idx) == 0:
        return None
    # lo side wins an exact tie (C# checks lo before hi at each distance)
    pos = int(np.searchsorted(idx, frame))
    cands = []
    if pos < len(idx):
        cands.append(int(idx[pos]))
    if pos > 0:
        cands.append(int(idx[pos - 1]))
    return min(cands, key=lambda c: (abs(c - frame), c > frame))


class MarkerMixin:
    def _begin_sample_field_edit(self, mirror: bool) -> None:
        self._sample_undo.record_before_edit(SampleFieldSnapshot.of(self._selected_sample))
        self._undo_domains.append(DOMAIN_SAMPLE)
        self._redo_domains.clear()
        if mirror:
            self._partner_undo.record_before_edit(SampleFieldSnapshot.of(self._partner_sample))

    def _end_sample_field_edit(self) -> None:
        self._load_sample_detail_state(self._selected_sample, reload_waveform=False)
        self._refresh_undo_redo_state()

    def move_loop_region(self, new_loop_start: int, new_loop_end: int) -> None:
        """Whole-region drag: both edges move by the same delta. Length is preserved EXPLICITLY — the Sample
        Start floor in _apply_sample_fields_to only clamps loop_start up, so passing both straight through would
        let the region shrink against the wall instead of the block stopping there."""
        s = self._selected_sample
        if s is None:
            return
        mirror = self.should_mirror_to_partner
        length = new_loop_end - new_loop_start
        start = max(new_loop_start, self.sample_start)
        end = start + length
        self._begin_sample_field_edit(mirror)
        self._apply_sample_fields_to(s, int(s.sample_rate), self.sample_loop_enabled, self.sample_start, start, end)
        self._sample_dirty = True
        if mirror:
            p = self._partner_sample
            self._apply_sample_fields_to(p, int(p.sample_rate), self.sample_loop_enabled, self.sample_start, start, end)
        self._end_sample_field_edit()
        self.status_text = f"Loop moved to [{start}, {end}){' (both L/R channels)' if mirror else ''} (unsaved - use Save Changes)."

    def set_marker(self, kind: SampleMarkerKind, proposed_frame: int) -> bool:
        """THE entry point for every Sample Start / Loop Start / Loop End edit (drag or typed). Clamp to the
        buffer first; Use Zero snaps; Loop Lock keeps the loop length and always snaps against LOOP START (never
        Loop End), even when Loop End was the edge physically moved. Returns whether anything was committed —
        LostFocus fires on every focus change, so a no-op must not push an undo step."""
        s = self._selected_sample
        if s is None:
            return False
        proposed_frame = max(0, min(proposed_frame, self.sample_frame_count))
        sample_start, loop_start, loop_end = self.sample_start, self.loop_start, self.loop_end
        loop_len = loop_end - loop_start

        if kind is SampleMarkerKind.SAMPLE_START:
            sample_start = self._snap(proposed_frame) if self.use_zero_crossing else proposed_frame
        elif kind is SampleMarkerKind.LOOP_START:
            loop_start = self._snap(proposed_frame) if self.use_zero_crossing else proposed_frame
            if self.loop_lock_enabled:
                loop_end = loop_start + loop_len
        else:
            if self.loop_lock_enabled:
                cand = proposed_frame - loop_len
                loop_start = self._snap(cand) if self.use_zero_crossing else cand
                loop_end = loop_start + loop_len
            else:
                loop_end = self._snap(proposed_frame) if self.use_zero_crossing else proposed_frame

        # apply the ordering clamp BEFORE the no-op test so "typed something that clamps back to where it was"
        # is a no-op too
        sample_start = max(0, sample_start)
        loop_start = max(loop_start, sample_start)
        loop_end = max(loop_end, loop_start)
        if sample_start == self.sample_start and loop_start == self.loop_start and loop_end == self.loop_end:
            return False

        mirror = self.should_mirror_to_partner
        self._begin_sample_field_edit(mirror)
        self._apply_sample_fields_to(s, int(s.sample_rate), self.sample_loop_enabled, sample_start, loop_start, loop_end)
        self._sample_dirty = True
        if mirror:
            p = self._partner_sample
            self._apply_sample_fields_to(p, int(p.sample_rate), self.sample_loop_enabled, sample_start, loop_start, loop_end)
        self._end_sample_field_edit()
        committed = {SampleMarkerKind.SAMPLE_START: sample_start, SampleMarkerKind.LOOP_START: loop_start}.get(kind, loop_end)
        self.status_text = f"{kind.value} set to {committed}{' (both L/R channels)' if mirror else ''}."
        return True

    def set_loop_enabled(self, enabled: bool) -> bool:
        s = self._selected_sample
        if s is None or self.sample_loop_enabled == enabled:
            return False
        mirror = self.should_mirror_to_partner
        self._begin_sample_field_edit(mirror)
        targets = [s] + ([self._partner_sample] if mirror else [])
        for t in targets:
            # Bit 0x80 = one-shot / loop-off (doc §5.1) — preserve any other flag bits.
            t.flags = (t.flags & ~0x80) & 0xFF if enabled else (t.flags | 0x80) & 0xFF
            # Ticking Loop with no region ever set leaves loop_start == loop_end (0 on a fresh import): the flag
            # is right but the loop is zero-length and plays as silent. Default to the whole sample, once.
            if enabled and t.loop_end <= t.loop_start and t.frame_count > 0:
                t.loop_end = t.frame_count - 1
        self._sample_dirty = True
        self._end_sample_field_edit()
        self.status_text = f"Loop {'enabled' if enabled else 'disabled'}{' (both L/R channels)' if mirror else ''}."
        return True

    def set_reversed(self, enabled: bool) -> None:
        s = self._selected_sample
        if s is None or self.sample_reverse_enabled == enabled:
            return
        mirror = self.should_mirror_to_partner
        self._begin_sample_field_edit(mirror)
        s.is_reversed = enabled
        if mirror:
            self._partner_sample.is_reversed = enabled
        self._sample_dirty = True
        self._end_sample_field_edit()
        self.status_text = f"Reverse {'enabled' if enabled else 'disabled'}{' (both L/R channels)' if mirror else ''}."

    def set_12db_boost_enabled(self, enabled: bool) -> None:
        s = self._selected_sample
        if s is None or self.sample_12db_boost_enabled == enabled:
            return
        mirror = self.should_mirror_to_partner
        self._begin_sample_field_edit(mirror)
        s.is_12db_boost_enabled = enabled
        if mirror:
            self._partner_sample.is_12db_boost_enabled = enabled
        self._sample_dirty = True
        self._playback.boost_enabled = enabled          # live, so toggling mid-playback is audible for A/B
        self._end_sample_field_edit()
        self.status_text = f"+12dB boost {'enabled' if enabled else 'disabled'}{' (both L/R channels)' if mirror else ''}."

    def set_loop_tune(self, value: int) -> None:
        s = self._selected_sample
        if s is None:
            return
        clamped = max(-99, min(99, int(value)))           # the front-panel UI's own hard limit (doc §3.1a)
        if self.sample_loop_tune == clamped:
            return
        mirror = self.should_mirror_to_partner
        self._begin_sample_field_edit(mirror)
        s.loop_tune = clamped
        if mirror:
            self._partner_sample.loop_tune = clamped
        self._sample_dirty = True
        self._end_sample_field_edit()
        self.status_text = f"Loop Tune set to {clamped}{' (both L/R channels)' if mirror else ''}."

    # ── Use Zero ───────────────────────────────────────────────────────────────────────────

    def _snap(self, proposed_frame: int) -> int:
        """Use Zero searches BOTH channels of a resolved pair (Combine); the nearer crossing wins, an exact tie
        picks the LOWER frame. Reuses the cached decoded waveforms — this runs on every marker commit."""
        s = self._selected_sample
        if s is None:
            return proposed_frame
        primary = None if s.is_header_only else nearest_zero_crossing(
            self.sample_waveform if self.sample_waveform is not None else s.samples(), proposed_frame)
        p = self._partner_sample
        partner = None
        if self.should_mirror_to_partner and p is not None and not p.is_header_only:
            partner = nearest_zero_crossing(
                self.partner_sample_waveform if self.partner_sample_waveform is not None else p.samples(), proposed_frame)
        if primary is None and partner is None:
            return proposed_frame
        if primary is None:
            return partner
        if partner is None:
            return primary
        dp, dq = abs(primary - proposed_frame), abs(partner - proposed_frame)
        if dp != dq:
            return primary if dp < dq else partner
        return min(primary, partner)
