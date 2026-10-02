r"""
Sample Editor model — field edits, waveform effects (stereo-mirrored), clipboard, channel move, tempo/pitch
and the multi-domain undo/redo. Port of ViewModels/SampleEditorViewModel.cs lines ~1457-2300.

Three rules this file lives by (each cost a real bug in C#, see SamplePhase7/8/9/13SelfTests):
  * Every PCM edit goes through `_apply_effect`, which replays the SAME effect instance against the stereo
    partner in Combine mode — never an ad-hoc splice on one channel (Cut/Paste once did, and L/R drifted).
  * An edit RECORDS its pre-state first, on the sample's own stack, and pushes a domain marker so Ctrl+Z walks
    sample/zone/partner edits in the order they actually happened.
  * Length-changing edits clamp the three markers back into range (`_clamp_markers_to_buffer`): to_bytes
    deliberately writes them verbatim, and nothing else re-derives them.
"""
from __future__ import annotations

import logging
from typing import List, Optional, Tuple

import numpy as np

from Core.sample_dsp import (
    CropEffect, DcOffsetEffect, DeleteRangeEffect, GainAdjustEffect, GainNormalizeEffect, InsertSilenceEffect,
    PasteRangeEffect, ReverseEffect, SampleEffect, SilenceEffect, SilenceTrimEffect, change_tempo_and_pitch)
from Core.sample_edit_undo import SampleFieldSnapshot, ZoneListSnapshot
from Core.sample_editor_model.core import (
    DOMAIN_PARTNER, DOMAIN_SAMPLE, DOMAIN_ZONE, cascade_top_keys, zone_index)
from Core.sample_support import SampleClipboard
from Data.kmp_multisample import KmpMultisample, KmpZone
from Data.ksf_sample import KsfSample

log = logging.getLogger(__name__)

MIN_TEMPO_RATIO, MAX_TEMPO_RATIO = 0.25, 4.0
MAX_PITCH_SEMITONES = 24.0


def _g(x: float) -> str:
    return f"{x:g}"


def clamp_markers_to_buffer(sample: KsfSample) -> None:
    """A length-changing edit can leave Sample Start / Loop Start / Loop End past the end of the PCM, and
    KsfSample.to_bytes writes all three verbatim (it must — a header-only file's stale LoopEnd is real
    recoverable data, doc §3.3). Header-only samples are skipped rather than clamped to 0, which would destroy
    exactly the preserved pre-corruption value §3.3 exists to keep."""
    if sample.is_header_only:
        return
    end = sample.frame_count
    if sample.sample_start <= end and sample.loop_start <= end and sample.loop_end <= end:
        return
    sample.sample_start = min(sample.sample_start, end)
    sample.loop_start = min(sample.loop_start, end)
    sample.loop_end = min(sample.loop_end, end)
    # LoopStart may have just moved, and the offset-24 duplicate slot mirrors it.
    sample.clear_preserved_loop_dup()


class EditMixin:
    # ── zone key range ─────────────────────────────────────────────────────────────────────

    def apply_zone_edits(self, original_key: int, top_key: int) -> None:
        zone = self._selected_zone
        if zone is None:
            return
        floor, idx, old_next_top = 0, -1, None
        bounds = self.current_multisample_zones
        if bounds is not None:
            idx = zone_index(bounds, zone)
            if idx > 0:
                floor = bounds[idx - 1].top_key + 1
            if 0 <= idx < len(bounds) - 1:
                old_next_top = bounds[idx + 1].top_key

        new_orig = max(0, min(127, original_key))
        new_top = max(floor, min(127, top_key))

        # These fields commit on LostFocus, which fires on every focus change — not just real edits. Without
        # this guard, tabbing through the row pushed dead undo steps and flagged the file unsaved.
        if new_orig == zone.original_key and new_top == zone.top_key:
            self.zone_original_key, self.zone_top_key = new_orig, new_top
            return

        # Mirrored onto the stereo sibling's matching zone, both lists snapshotted as ONE undo step: a pair
        # is matched by exact (OriginalKey, TopKey), so editing one half alone silently breaks it and drops
        # the shared L/R view back to mono.
        sibling_zones, sibling_m, sibling_path = (self._resolve_sibling_zones_for(bounds)
                                                  if bounds is not None else (None, None, None))
        if bounds is not None:
            self._zone_undo.record_before_edit(ZoneListSnapshot.of(bounds, sibling_zones))
            self._undo_domains.append(DOMAIN_ZONE)
            self._redo_domains.clear()

        old_top = zone.top_key
        zone.original_key, zone.top_key = new_orig, new_top
        if sibling_zones is not None and 0 <= idx < len(sibling_zones):
            sibling_zones[idx].original_key, sibling_zones[idx].top_key = new_orig, new_top

        # new_top >= old_next_top implies new_top > old_top, so delta is positive: the "ran past the next
        # zone" case cascade_top_keys exists for.
        cascaded = old_next_top is not None and new_top >= old_next_top and bounds is not None
        if cascaded:
            delta = new_top - old_top
            cascade_top_keys(bounds, idx, delta)
            if sibling_zones is not None:
                cascade_top_keys(sibling_zones, idx, delta)

        self.zone_original_key, self.zone_top_key = zone.original_key, zone.top_key
        self._zone_dirty = True
        if sibling_m is not None and sibling_path is not None:
            self._register_dirty_multisample(sibling_m, sibling_path)
        self._refresh_undo_redo_state()
        self.status_text = ("Zone key range updated" + (" (both L/R channels)" if sibling_zones is not None else "")
                            + (" - pushed the following zone(s) up to make room" if cascaded else "")
                            + " (unsaved - use Save Multisample).")

    # ── sample fields ──────────────────────────────────────────────────────────────────────

    def apply_sample_edits(self, sample_rate: int, loop_enabled: bool, sample_start: int,
                           loop_start: int, loop_end: int) -> None:
        s = self._selected_sample
        if s is None:
            return
        mirror = self.should_mirror_to_partner
        self._sample_undo.record_before_edit(SampleFieldSnapshot.of(s))
        self._undo_domains.append(DOMAIN_SAMPLE)
        self._redo_domains.clear()
        if mirror:
            self._partner_undo.record_before_edit(SampleFieldSnapshot.of(self._partner_sample))

        self._apply_sample_fields_to(s, sample_rate, loop_enabled, sample_start, loop_start, loop_end)
        self._sample_dirty = True
        if mirror:
            self._apply_sample_fields_to(self._partner_sample, sample_rate, loop_enabled, sample_start, loop_start, loop_end)

        self._load_sample_detail_state(s, reload_waveform=False)
        self._refresh_undo_redo_state()
        self.status_text = f"Sample fields updated{' (both L/R channels)' if mirror else ''} (unsaved - use Save Sample)."

    def _apply_sample_fields_to(self, sample: KsfSample, sample_rate: int, loop_enabled: bool,
                                sample_start: int, loop_start: int, loop_end: int) -> None:
        """THE funnel every loop/sample-start edit (set_marker, move_loop_region, set_loop_from_selection, the
        bulk field-apply) routes through, so the 'Loop Start can never precede Sample Start' invariant can't be
        bypassed by editing one path and not another — and the one place that retargets an already-playing loop
        live. Only for `sample` itself, never a mirrored partner's own call (the two share one combined provider
        keyed to the primary side)."""
        sample_start = max(0, sample_start)
        loop_start = max(loop_start, sample_start)
        loop_end = max(loop_end, loop_start)

        sample.sample_rate = max(1, int(sample_rate))
        # Bit 0x80 = one-shot/loop-off (doc §5.1) — preserve any other flag bits.
        sample.flags = (sample.flags & ~0x80) & 0xFF if loop_enabled else (sample.flags | 0x80) & 0xFF
        sample.sample_start, sample.loop_start, sample.loop_end = sample_start, loop_start, loop_end
        sample.clear_preserved_loop_dup()

        if sample is self._selected_sample:
            self._playback.update_loop_bounds(loop_start, loop_end)

    # ── the effect pipeline ────────────────────────────────────────────────────────────────

    def _apply_effect(self, effect: SampleEffect, description: str) -> None:
        """Every effect funnels through here: record the pre-edit state for undo, apply, refresh detail state,
        mark dirty. In Combine mode the SAME effect instance is replayed against the partner's own PCM —
        effects are pure functions of (pcm, rate), so reuse is trivially correct."""
        s = self._selected_sample
        if s is None:
            self.status_text = "No sample loaded."
            return
        if s.is_header_only:
            self.status_text = "No audio data to edit (header-only sample)."
            return
        mirror = self.should_mirror_to_partner

        self._sample_undo.record_before_edit(SampleFieldSnapshot.of(s))
        self._undo_domains.append(DOMAIN_SAMPLE)
        self._redo_domains.clear()
        s.set_samples(effect.apply(s.samples(), int(s.sample_rate)))
        clamp_markers_to_buffer(s)
        self._sample_dirty = True

        if mirror:
            p = self._partner_sample
            self._partner_undo.record_before_edit(SampleFieldSnapshot.of(p))
            p.set_samples(effect.apply(p.samples(), int(p.sample_rate)))
            clamp_markers_to_buffer(p)
            self.partner_sample_waveform = None if p.is_header_only else p.samples()

        self._load_sample_detail_state(s, reload_waveform=True)
        self._refresh_undo_redo_state()
        self.status_text = f"{description}{' (both L/R channels)' if mirror else ''} (unsaved - use Save Sample)."

    def _selection_or_whole_buffer(self) -> Tuple[int, int]:
        if self.selection_end_frame > self.selection_start_frame:
            return self.selection_start_frame, self.selection_end_frame
        return 0, (self._selected_sample.frame_count if self._selected_sample is not None else 0)

    def apply_crop(self) -> None:
        if self.selection_end_frame <= self.selection_start_frame:
            self.status_text = "Select a range in the waveform to crop first."
            return
        self._apply_effect(CropEffect(self.selection_start_frame, self.selection_end_frame),
                           f"Cropped to [{self.selection_start_frame}, {self.selection_end_frame})")

    def apply_normalize(self, target_peak_db: float = -0.1) -> None:
        """A stereo pair normalizes as ONE track: the shared peak (whichever channel is louder) is measured up
        front and baked into one effect instance, so both scale by the same factor. Independent measurement
        would boost the quieter channel more and audibly shift the stereo image."""
        s = self._selected_sample
        if s is None:
            self.status_text = "No sample loaded."
            return
        start, end = self._selection_or_whole_buffer()
        shared_peak = None
        if self.should_mirror_to_partner and not s.is_header_only:
            host = s.samples()
            primary = GainNormalizeEffect.compute_peak(host, start, min(end, len(host)))
            ph = self._partner_sample.samples()
            partner = GainNormalizeEffect.compute_peak(ph, min(start, len(ph)), min(end, len(ph)))
            shared_peak = max(primary, partner)
        self._apply_effect(GainNormalizeEffect(target_peak_db, shared_peak, start, end),
                           "Normalized gain" if end - start >= s.frame_count else f"Normalized gain [{start}, {end})")

    def apply_silence_trim(self, threshold_amplitude: int = 32) -> None:
        """A stereo pair trims as ONE track: only a leading/trailing run silent in BOTH channels is deleted,
        or the channels get cropped to different lengths and end up offset. The UNION of each channel's own
        non-silent bounds, baked into one shared instance."""
        shared = None
        s = self._selected_sample
        if self.should_mirror_to_partner and s is not None and not s.is_header_only:
            a = SilenceTrimEffect.compute_bounds(s.samples(), threshold_amplitude)
            b = SilenceTrimEffect.compute_bounds(self._partner_sample.samples(), threshold_amplitude)
            shared = (min(a[0], b[0]), max(a[1], b[1]))
        self._apply_effect(SilenceTrimEffect(threshold_amplitude, shared), "Trimmed silence")

    def apply_gain_adjust(self, decibels: float) -> None:
        """Amplify (+dB) and Soften (-dB): the toolbar presets. The selection if there is one, else the whole sample."""
        s = self._selected_sample
        if s is None:
            self.status_text = "No sample loaded."
            return
        start, end = self._selection_or_whole_buffer()
        self._apply_effect(GainAdjustEffect(decibels, start, end),
                           f"Applied {'+' if decibels >= 0 else ''}{_g(decibels)} dB gain"
                           + ("" if end - start >= s.frame_count else f" [{start}, {end})"))

    def apply_fade_in_selection(self) -> None:
        self._apply_selection_fade(True)

    def apply_fade_out_selection(self) -> None:
        self._apply_selection_fade(False)

    def _apply_selection_fade(self, fade_in: bool) -> None:
        s = self._selected_sample
        if s is None:
            self.status_text = "No sample loaded."
            return
        if s.is_header_only:
            self.status_text = "No audio data to edit (header-only sample)."
            return
        if self.selection_end_frame <= self.selection_start_frame:
            self.status_text = "Select a range in the waveform first."
            return
        mirror = self.should_mirror_to_partner
        self._apply_fade_to(s, self._sample_undo, self.selection_start_frame, self.selection_end_frame, fade_in)
        self._undo_domains.append(DOMAIN_SAMPLE)
        self._redo_domains.clear()
        self._sample_dirty = True
        if mirror:
            p = self._partner_sample
            self._apply_fade_to(p, self._partner_undo, self.selection_start_frame, self.selection_end_frame, fade_in)
            self.partner_sample_waveform = None if p.is_header_only else p.samples()
        self._load_sample_detail_state(s, reload_waveform=True)
        self._refresh_undo_redo_state()
        self.status_text = (f"Applied fade {'in' if fade_in else 'out'} to the selection"
                            f"{' (both L/R channels)' if mirror else ''} (unsaved - use Save Sample).")

    @staticmethod
    def _apply_fade_to(sample: KsfSample, undo, sel_start: int, sel_end: int, fade_in: bool) -> None:
        undo.record_before_edit(SampleFieldSnapshot.of(sample))
        host = sample.samples().astype(np.float64)
        start = max(0, min(sel_start, len(host)))
        end = max(start, min(sel_end, len(host)))
        n = end - start
        if n > 0:
            t = np.ones(n) if n <= 1 else np.arange(n) / (n - 1)
            gain = t if fade_in else 1.0 - t
            host[start:end] = host[start:end] * gain
        sample.set_samples(np.clip(host, -32768, 32767).astype(np.int16))     # truncation toward zero, like (short)

    def set_loop_from_selection(self) -> None:
        """'Loop Selected Area': the sample's Loop Start/End = the current selection. Mirrored to the partner in
        Combine mode — mismatched loop points between stereo channels is a real playback bug on the Kronos."""
        s = self._selected_sample
        if s is None:
            self.status_text = "No sample loaded."
            return
        if self.selection_end_frame <= self.selection_start_frame:
            self.status_text = "Select a range in the waveform first."
            return
        mirror = self.should_mirror_to_partner
        self._sample_undo.record_before_edit(SampleFieldSnapshot.of(s))
        self._undo_domains.append(DOMAIN_SAMPLE)
        self._redo_domains.clear()
        if mirror:
            self._partner_undo.record_before_edit(SampleFieldSnapshot.of(self._partner_sample))
        a, b = self.selection_start_frame, self.selection_end_frame
        self._apply_sample_fields_to(s, int(s.sample_rate), self.sample_loop_enabled, self.sample_start, a, b)
        self._sample_dirty = True
        if mirror:
            p = self._partner_sample
            self._apply_sample_fields_to(p, int(p.sample_rate), self.sample_loop_enabled, self.sample_start, a, b)
        self._load_sample_detail_state(s, reload_waveform=False)
        self._refresh_undo_redo_state()
        self.status_text = f"Loop set to [{a}, {b}){' (both L/R channels)' if mirror else ''} (unsaved - use Save Changes)."

    # ── clipboard ──────────────────────────────────────────────────────────────────────────

    def copy_selection(self) -> None:
        s = self._selected_sample
        if s is None or s.is_header_only:
            self.status_text = "No sample loaded."
            return
        if self.selection_end_frame <= self.selection_start_frame:
            self.status_text = "Select a range in the waveform first."
            return
        host = s.samples()
        start = max(0, min(self.selection_start_frame, len(host)))
        end = max(start, min(self.selection_end_frame, len(host)))
        SampleClipboard.set(host[start:end], int(s.sample_rate))
        self.status_text = f"Copied {end - start} frame(s) to the clipboard."

    def cut_selection(self) -> None:
        s = self._selected_sample
        if s is None or s.is_header_only:
            self.status_text = "No sample loaded."
            return
        if self.selection_end_frame <= self.selection_start_frame:
            self.status_text = "Select a range in the waveform first."
            return
        # Clipboard capture BEFORE the effect runs, from the primary channel only — the clipboard is mono by
        # design, so a stereo Combine-mode cut removes both channels but copies the selected one.
        host = s.samples()
        start = max(0, min(self.selection_start_frame, len(host)))
        end = max(start, min(self.selection_end_frame, len(host)))
        SampleClipboard.set(host[start:end], int(s.sample_rate))
        self._apply_effect(DeleteRangeEffect(start, end), f"Cut {end - start} frame(s) to the clipboard")
        self.selection_start_frame = 0
        self.selection_end_frame = 0

    def paste_at_selection(self) -> None:
        s = self._selected_sample
        if s is None or s.is_header_only:
            self.status_text = "No sample loaded."
            return
        if not SampleClipboard.has_content():
            self.status_text = "Clipboard is empty."
            return
        n = s.frame_count
        start = max(0, min(self.selection_start_frame, n))
        end = max(start, min(self.selection_end_frame, n))
        clip = SampleClipboard.pcm
        self._apply_effect(PasteRangeEffect(start, end, clip), f"Pasted {len(clip)} frame(s)")
        self.selection_start_frame = start
        self.selection_end_frame = start + len(clip)

    def apply_reverse(self) -> None:
        s = self._selected_sample
        if s is None:
            self.status_text = "No sample loaded."
            return
        start, end = self._selection_or_whole_buffer()
        self._apply_effect(ReverseEffect(start, end),
                           "Reversed the whole sample" if end - start >= s.frame_count else f"Reversed [{start}, {end})")

    def apply_silence_selection(self) -> None:
        if self._selected_sample is None:
            self.status_text = "No sample loaded."
            return
        if self.selection_end_frame <= self.selection_start_frame:
            self.status_text = "Select a range in the waveform first."
            return
        self._apply_effect(SilenceEffect(self.selection_start_frame, self.selection_end_frame),
                           f"Silenced [{self.selection_start_frame}, {self.selection_end_frame})")

    def apply_insert_silence(self, frame_count: int, apply_to_left: bool, apply_to_right: bool) -> None:
        """Silence at the selection start (or the scrub cursor), pushing the rest later. Unlike every other edit,
        WHICH channel(s) are touched is chosen by the dialog's explicit checkboxes rather than inferred from
        Split L/R — so this can mirror even while Split is on, or touch one channel in Combine."""
        s = self._selected_sample
        if s is None:
            self.status_text = "No sample loaded."
            return
        if frame_count <= 0:
            self.status_text = "Enter a positive number of frames to insert."
            return
        has_partner = self.has_stereo_pair and self._partner_sample is not None and not self._partner_sample.is_header_only
        apply_primary = (not has_partner) or (apply_to_left if self.is_primary_left_channel else apply_to_right)
        apply_partner = has_partner and (apply_to_right if self.is_primary_left_channel else apply_to_left)
        if not apply_primary and not apply_partner:
            self.status_text = "Select at least one channel to insert silence into."
            return
        at = (self.selection_start_frame if self.selection_end_frame > self.selection_start_frame
              else self._cursor_frame if self._cursor_frame >= 0 else 0)

        # Both channels checked AND the ambient mirror condition agrees: identical to every other mirrored
        # edit, so reuse the generic path (one Sample-domain undo step; undo's own recompute picks the partner up).
        if apply_primary and apply_partner and self.should_mirror_to_partner:
            self._apply_effect(InsertSilenceEffect(at, frame_count), f"Inserted {frame_count} frame(s) of silence at {at}")
            return

        effect = InsertSilenceEffect(at, frame_count)
        self._redo_domains.clear()
        if apply_primary:
            self._sample_undo.record_before_edit(SampleFieldSnapshot.of(s))
            self._undo_domains.append(DOMAIN_SAMPLE)
            s.set_samples(effect.apply(s.samples(), int(s.sample_rate)))
            clamp_markers_to_buffer(s)
            self._sample_dirty = True
        if apply_partner:
            p = self._partner_sample
            self._partner_undo.record_before_edit(SampleFieldSnapshot.of(p))
            self._undo_domains.append(DOMAIN_PARTNER)
            p.set_samples(effect.apply(p.samples(), int(p.sample_rate)))
            clamp_markers_to_buffer(p)
            self._register_dirty_partner_sample()
            self.partner_sample_waveform = None if p.is_header_only else p.samples()
            self._refresh_partner_markers()
        elif apply_primary and has_partner and self.should_mirror_to_partner:
            # Primary only, but the ambient mirror condition is ON: undo's Sample branch ALWAYS also pops the
            # partner stack when that condition holds. Push the partner's CURRENT (unedited) state so that
            # automatic pop restores exactly what's already there instead of reverting an earlier partner edit.
            self._partner_undo.record_before_edit(SampleFieldSnapshot.of(self._partner_sample))

        if apply_primary:
            self._load_sample_detail_state(s, reload_waveform=True)
        self._refresh_undo_redo_state()
        if apply_primary and apply_partner:
            channels = " (both L/R channels)"
        elif apply_primary:
            channels = f" ({'L' if self.is_primary_left_channel else 'R'} channel only)"
        else:
            channels = f" ({'R' if self.is_primary_left_channel else 'L'} channel only)"
        self.status_text = (f"Inserted {frame_count} frame(s) of silence at {at}"
                            f"{channels if has_partner else ''} (unsaved - use Save Sample).")

    # ── Move tool ──────────────────────────────────────────────────────────────────────────

    def apply_channel_move(self, target_partner: bool, delta_frames: int) -> None:
        """The Move tool's 'drag the bare waveform' (only meaningful with Split L/R on). Always targets the
        channel whose PANE was dragged. Positive pads that channel's own leading edge with silence (content
        moves LATER); negative trims up to -delta frames of PREVIOUSLY-ADDED padding off the SAME channel — never
        into real audio. SampleStart/LoopStart/LoopEnd are NEVER touched (the overlay is a fixed frame-number
        reference; a channel move is not a loop move)."""
        if delta_frames == 0:
            return
        if not self.has_stereo_pair or not self.split_lr:
            self.status_text = "Move only offsets a channel relative to its stereo partner - turn on Split L/R first."
            return
        if not target_partner:
            s = self._selected_sample
            if s is None or s.is_header_only:
                self.status_text = "No audio data to move."
                return
            applied = self._clamp_to_trimmable_padding(s, delta_frames)
            if applied == 0:
                self.status_text = "Already at the earliest position for this channel - nothing left to move back."
                return
            self._sample_undo.record_before_edit(SampleFieldSnapshot.of(s))
            self._undo_domains.append(DOMAIN_SAMPLE)
            self._redo_domains.clear()
            self._shift_channel_edge(s, applied)
            self._sample_dirty = True
            self._load_sample_detail_state(s, reload_waveform=True)
            self._refresh_undo_redo_state()
            self.status_text = (f"Moved channel {applied} frame(s) later (unsaved - use Save Sample)." if applied > 0
                                else f"Moved channel {-applied} frame(s) back toward 0 (unsaved - use Save Sample).")
        else:
            p = self._partner_sample
            if p is None or p.is_header_only:
                self.status_text = "No stereo partner audio to offset."
                return
            applied = self._clamp_to_trimmable_padding(p, delta_frames)
            if applied == 0:
                self.status_text = "Already at the earliest position for the sibling channel - nothing left to move back."
                return
            self._partner_undo.record_before_edit(SampleFieldSnapshot.of(p))
            self._undo_domains.append(DOMAIN_PARTNER)
            self._redo_domains.clear()
            self._shift_channel_edge(p, applied)
            self._register_dirty_partner_sample()
            self.partner_sample_waveform = None if p.is_header_only else p.samples()
            self._refresh_partner_markers()
            self._refresh_undo_redo_state()
            self.status_text = (f"Moved sibling channel {applied} frame(s) later (unsaved - use Save Sample)." if applied > 0
                                else f"Moved sibling channel {-applied} frame(s) back toward 0 (unsaved - use Save Sample).")

    def _clamp_to_trimmable_padding(self, s: KsfSample, requested: int) -> int:
        """Positive always goes through and grows this sample's tracked padding balance. Negative is clamped so
        its magnitude never exceeds that balance (0 once exhausted) — the 'hard stop' that keeps a leftward drag
        from ever eating real audio."""
        key = id(s)
        if requested > 0:
            self._move_tool_padding[key] = self._move_tool_padding.get(key, 0) + requested
            return requested
        available = self._move_tool_padding.get(key, 0)
        trim = min(-requested, available)
        if trim <= 0:
            return 0
        self._move_tool_padding[key] = available - trim
        return -trim

    @staticmethod
    def _shift_channel_edge(s: KsfSample, frame_count: int) -> None:
        host = s.samples()
        if frame_count > 0:
            s.set_samples(InsertSilenceEffect(0, frame_count).apply(host, int(s.sample_rate)))
        else:
            trim = min(-frame_count, len(host))
            if trim <= 0:
                return
            s.set_samples(DeleteRangeEffect(0, trim).apply(host, int(s.sample_rate)))
        clamp_markers_to_buffer(s)

    # ── DC offset / tempo-pitch ────────────────────────────────────────────────────────────

    def apply_dc_offset_removal(self) -> None:
        s = self._selected_sample
        if s is None:
            self.status_text = "No sample loaded."
            return
        if s.is_header_only:
            self.status_text = "No audio data to edit (header-only sample)."
            return
        offset = DcOffsetEffect.measure_offset(s.samples())
        if offset == 0:
            self.status_text = "No DC offset to remove - the waveform is already centred."
            return
        self._apply_effect(DcOffsetEffect(), f"Removed a DC offset of {offset}")

    def apply_tempo_pitch(self, tempo_ratio: float, pitch_semitones: float) -> None:
        s = self._selected_sample
        if s is None:
            self.status_text = "No sample loaded."
            return
        if s.is_header_only:
            self.status_text = "No audio data to edit (header-only sample)."
            return
        # Bounds matter: the output buffer is sized by 1/tempo, so a typo'd 0.001 asks for a thousand times the
        # input — a hang or MemoryError on any real-length sample.
        req_t, req_p = tempo_ratio, pitch_semitones
        tempo_ratio = max(MIN_TEMPO_RATIO, min(MAX_TEMPO_RATIO, tempo_ratio))
        pitch_semitones = max(-MAX_PITCH_SEMITONES, min(MAX_PITCH_SEMITONES, pitch_semitones))
        if tempo_ratio != req_t or pitch_semitones != req_p:
            self.status_text = (f"Out of range - clamped to tempo x{_g(round(tempo_ratio, 2))}, pitch {_g(round(pitch_semitones, 2))} "
                                f"(limits: tempo {MIN_TEMPO_RATIO}-{MAX_TEMPO_RATIO}, pitch ±{MAX_PITCH_SEMITONES:g}).")
        if tempo_ratio == 1.0 and pitch_semitones == 0.0:
            self.status_text = "Tempo x1 and pitch 0 - nothing to apply."
            return
        mirror = self.should_mirror_to_partner
        self._sample_undo.record_before_edit(SampleFieldSnapshot.of(s))
        self._undo_domains.append(DOMAIN_SAMPLE)
        self._redo_domains.clear()
        s.set_samples(change_tempo_and_pitch(s.samples(), int(s.sample_rate), tempo_ratio, pitch_semitones))
        clamp_markers_to_buffer(s)          # a tempo change resizes the buffer
        self._sample_dirty = True
        if mirror:
            p = self._partner_sample
            self._partner_undo.record_before_edit(SampleFieldSnapshot.of(p))
            p.set_samples(change_tempo_and_pitch(p.samples(), int(p.sample_rate), tempo_ratio, pitch_semitones))
            clamp_markers_to_buffer(p)
            self.partner_sample_waveform = None if p.is_header_only else p.samples()
        self._load_sample_detail_state(s, reload_waveform=True)
        self._refresh_undo_redo_state()
        pitch_txt = f"{pitch_semitones:+g}" if pitch_semitones != 0 else "0"
        self.status_text = (f"Applied tempo x{_g(round(tempo_ratio, 2))}, pitch {pitch_txt} semitones"
                            f"{' (both L/R channels)' if mirror else ''} (unsaved - use Save Sample).")

    # ── undo / redo ────────────────────────────────────────────────────────────────────────

    def _sync_zone_count_dependent_tree_nodes(self, zones: List[KmpZone], sibling_zones: Optional[List[KmpZone]]) -> None:
        """A zone-list undo/redo can change a multisample's child COUNT (delete is on the same stack as the
        count-preserving edits); apply_to restores the lists but knows nothing about the tree. Reuses each
        surviving zone's node so _selected_node stays valid (see _sync_multisample_node_children)."""
        for node in self.all_multisample_nodes():
            nz = node.multisample_ref[0].zones
            if nz is zones or (sibling_zones is not None and nz is sibling_zones):
                self._sync_multisample_node_children(node)
        self._tree_refreshed()

    def _refresh_undo_redo_state(self) -> None:
        self.can_undo = len(self._undo_domains) > 0
        self.can_redo = len(self._redo_domains) > 0

    def undo(self) -> None:
        """Also undoes the partner's own stack when one exists, the mirror condition holds, and that stack actually
        has something — guarding on the partner's own can_undo rather than assuming lockstep means a mid-session
        Combine/Split toggle degrades gracefully instead of throwing."""
        if not self._undo_domains:
            return
        # Any undo can hand a sample's PCM back to an earlier state, after which the tracked padding balance can
        # no longer be trusted. Cleared wholesale; under-counting is the safe direction.
        self._move_tool_padding.clear()
        domain = self._undo_domains.pop()

        if domain == DOMAIN_ZONE:
            zones = self.current_multisample_zones
            if zones is None:
                return
            sibling_zones, sibling_m, sibling_path = self._resolve_sibling_zones_for(zones)
            restored = self._zone_undo.undo(ZoneListSnapshot.of(zones, sibling_zones))
            if restored is None:
                return
            restored.apply_to()
            self._zone_dirty = True
            if sibling_m is not None and sibling_path is not None:
                self._register_dirty_multisample(sibling_m, sibling_path)
            self._sync_zone_count_dependent_tree_nodes(zones, sibling_zones)
            if self._selected_zone is not None:           # undo can restore a TopKey the selected zone had before
                self.zone_original_key, self.zone_top_key = self._selected_zone.original_key, self._selected_zone.top_key
            self._redo_domains.append(DOMAIN_ZONE)
            self._refresh_undo_redo_state()
            self.status_text = "Undid zone edit (unsaved - use Save Multisample)."
            return

        if domain == DOMAIN_PARTNER:
            p = self._partner_sample
            if p is None:
                self._refresh_undo_redo_state()
                return
            restored = self._partner_undo.undo(SampleFieldSnapshot.of(p))
            if restored is None:
                self._refresh_undo_redo_state()
                return
            restored.apply_to(p)
            self._register_dirty_partner_sample()
            self.partner_sample_waveform = None if p.is_header_only else p.samples()
            self._refresh_partner_markers()
            self._redo_domains.append(DOMAIN_PARTNER)
            self._refresh_undo_redo_state()
            self.status_text = "Undid sibling channel move (unsaved - use Save Sample)."
            return

        s = self._selected_sample
        if s is None:
            self._refresh_undo_redo_state()
            return
        restored = self._sample_undo.undo(SampleFieldSnapshot.of(s))
        if restored is None:
            self._refresh_undo_redo_state()
            return
        restored.apply_to(s)
        self._sample_dirty = True

        # (not split_lr or split_both_active) must track should_mirror_to_partner's condition exactly: it is what
        # decided whether the forward edit pushed a partner-undo step to begin with. Skipping it here would strand
        # that step and leave the partner's PCM un-reverted despite "both L/R channels" in the status text.
        partner_also = (self.has_stereo_pair and (not self.split_lr or self.split_both_active)
                        and self._partner_sample is not None and self._partner_undo.can_undo)
        if partner_also:
            pr = self._partner_undo.undo(SampleFieldSnapshot.of(self._partner_sample))
            if pr is not None:
                pr.apply_to(self._partner_sample)
                self.partner_sample_waveform = None if self._partner_sample.is_header_only else self._partner_sample.samples()
        self._load_sample_detail_state(s, reload_waveform=True)
        self._redo_domains.append(DOMAIN_SAMPLE)
        self._refresh_undo_redo_state()
        evicted = self._sample_undo.take_evicted_count()
        self.status_text = (f"Undid last edit{' (both L/R channels)' if partner_also else ''} (unsaved - use Save Sample)."
                            + (f" ({evicted} earlier step(s) no longer available - undo history is capped.)" if evicted > 0 else ""))

    def redo(self) -> None:
        if not self._redo_domains:
            return
        self._move_tool_padding.clear()
        domain = self._redo_domains.pop()

        if domain == DOMAIN_ZONE:
            zones = self.current_multisample_zones
            if zones is None:
                return
            sibling_zones, sibling_m, sibling_path = self._resolve_sibling_zones_for(zones)
            restored = self._zone_undo.redo(ZoneListSnapshot.of(zones, sibling_zones))
            if restored is None:
                return
            restored.apply_to()
            self._zone_dirty = True
            if sibling_m is not None and sibling_path is not None:
                self._register_dirty_multisample(sibling_m, sibling_path)
            self._sync_zone_count_dependent_tree_nodes(zones, sibling_zones)
            if self._selected_zone is not None:
                self.zone_original_key, self.zone_top_key = self._selected_zone.original_key, self._selected_zone.top_key
            self._undo_domains.append(DOMAIN_ZONE)
            self._refresh_undo_redo_state()
            self.status_text = "Redid zone edit (unsaved - use Save Multisample)."
            return

        if domain == DOMAIN_PARTNER:
            p = self._partner_sample
            if p is None:
                self._refresh_undo_redo_state()
                return
            restored = self._partner_undo.redo(SampleFieldSnapshot.of(p))
            if restored is None:
                self._refresh_undo_redo_state()
                return
            restored.apply_to(p)
            self._register_dirty_partner_sample()
            self.partner_sample_waveform = None if p.is_header_only else p.samples()
            self._refresh_partner_markers()
            self._undo_domains.append(DOMAIN_PARTNER)
            self._refresh_undo_redo_state()
            self.status_text = "Redid sibling channel move (unsaved - use Save Sample)."
            return

        s = self._selected_sample
        if s is None:
            self._refresh_undo_redo_state()
            return
        restored = self._sample_undo.redo(SampleFieldSnapshot.of(s))
        if restored is None:
            self._refresh_undo_redo_state()
            return
        restored.apply_to(s)
        self._sample_dirty = True
        partner_also = (self.has_stereo_pair and (not self.split_lr or self.split_both_active)
                        and self._partner_sample is not None and self._partner_undo.can_redo)
        if partner_also:
            pr = self._partner_undo.redo(SampleFieldSnapshot.of(self._partner_sample))
            if pr is not None:
                pr.apply_to(self._partner_sample)
                self.partner_sample_waveform = None if self._partner_sample.is_header_only else self._partner_sample.samples()
        self._load_sample_detail_state(s, reload_waveform=True)
        self._undo_domains.append(DOMAIN_SAMPLE)
        self._refresh_undo_redo_state()
        self.status_text = f"Redid edit{' (both L/R channels)' if partner_also else ''} (unsaved - use Save Sample)."
