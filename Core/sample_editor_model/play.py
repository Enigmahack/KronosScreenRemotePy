r"""
Sample Editor model — transport and piano-key playback. Port of SampleEditorViewModel.cs ~2297-2615.
"""
from __future__ import annotations

import logging
import os
from typing import Callable, List, Optional

from Core import sample_link_resolver as link_resolver
from Core.sample_editor_model.core import zone_index
from Data.kmp_multisample import KmpZone
from Data.ksf_sample import KsfSample

log = logging.getLogger(__name__)


class PlayMixin:
    def _init_play(self) -> None:
        self._piano_key_generation = -1
        self.cursor_moved_listeners: List[Callable[[int], None]] = []
        # The audio thread fires on_stopped; the window swaps this for a queued call onto the GUI thread.
        self.dispatch: Callable[[Callable[[], None]], None] = lambda fn: fn()
        self._playback.on_stopped = lambda: self.dispatch(self._on_playback_finished)

    def _on_playback_finished(self) -> None:
        self.is_playing = False

    # ── whole-sample transport ─────────────────────────────────────────────────────────────

    def play_selected_sample(self) -> None:
        """Plays from the grey scrub cursor when one is set this session, else from Sample Start (looped) / 0
        (one-shot) — 'play back from the last place the user selected', not just an audition-on-click."""
        s = self._selected_sample
        if s is None or self.sample_waveform is None:
            return
        self._playback.boost_enabled = self.sample_12db_boost_enabled
        stereo = self.has_stereo_pair and not self.split_lr and self.partner_sample_waveform is not None
        loop = self.sample_loop_enabled
        loop_start_frame = self._cursor_frame if self._cursor_frame >= 0 else self.sample_start
        one_shot_start = self._cursor_frame if self._cursor_frame >= 0 else 0
        rate = int(s.sample_rate)

        if stereo:
            left, right = self.left_sample_waveform, self.right_sample_waveform
            if loop:
                self._playback.play_stereo_looped(left, right, rate, loop_start_frame, self.loop_start,
                                                  self.loop_end, self.sample_reverse_enabled)
            else:
                self._playback.play_stereo_from(left, right, rate, one_shot_start, self.sample_reverse_enabled)
        elif loop:
            self._playback.play_looped(self.sample_waveform, rate, loop_start_frame, self.loop_start,
                                       self.loop_end, self.sample_reverse_enabled)
        else:
            self._playback.play_from(self.sample_waveform, rate, one_shot_start, self.sample_reverse_enabled)
        self.playback_matches_selection = True
        self.is_playing = True
        self.is_paused = False

    def play_zone_at_key(self, zone: KmpZone, played_key: int) -> None:
        """Keymap piano-key trigger: a one-shot audition of THIS zone's own sample (very often not the one
        loaded in the Sample panel), pitched as if struck at `played_key`. A pending live edit wins over disk.
        Combine mode plays a stereo pair together. No polyphony, deliberately."""
        if zone.is_skipped:
            return
        m, kmp_path = self._resolve_context_multisample()
        if m is None or kmp_path is None:
            return

        def open_or_pending(path: str) -> Optional[KsfSample]:
            pending = self._dirty_samples.get(path)
            if pending is not None:
                return pending
            try:
                if os.path.isfile(path):
                    with open(path, "rb") as f:
                        return KsfSample.open(f.read())
            except Exception as ex:
                log.warning("Sample Editor: couldn't open '%s' for key trigger: %s", path, ex)
            return None

        opened = open_or_pending(zone.ksf_path(kmp_path))
        if opened is None:
            return
        # Zone-specific fields (loop points, flags, rate) still come off the STUB; only its PCM is borrowed
        # when this zone is a doc §3.2 linked stub.
        s = link_resolver.resolve_playable(opened, kmp_path)
        if s.is_header_only:
            return

        start_frame = max(0, min(int(s.sample_start), max(0, s.frame_count - 1)))
        # Own sample's flags, not the panel's: edits through the panel are mirrored onto a partner, so reading
        # them off `s` is authoritative for either side of a pair.
        self._playback.boost_enabled = s.is_12db_boost_enabled
        loop = s.is_loop_enabled
        loop_start, loop_end = int(s.loop_start), int(s.loop_end)
        rate = int(s.sample_rate)
        matches = zone is self._selected_zone

        if not self.split_lr:
            sibling, sibling_path = self._resolve_stereo_sibling(m, kmp_path)
            if sibling is not None and sibling_path is not None:
                match = next((z for z in sibling.zones if not z.is_skipped
                              and z.original_key == zone.original_key and z.top_key == zone.top_key), None)
                if match is None:
                    idx = zone_index(m.zones, zone)
                    if 0 <= idx < len(sibling.zones) and not sibling.zones[idx].is_skipped:
                        match = sibling.zones[idx]
                if match is not None:
                    op = open_or_pending(match.ksf_path(sibling_path))
                    partner = link_resolver.resolve_playable(op, sibling_path) if op is not None else None
                    if partner is not None and not partner.is_header_only:
                        left_is_primary = m.suffix == "-L"
                        left = s.samples() if left_is_primary else partner.samples()
                        right = partner.samples() if left_is_primary else s.samples()
                        if loop:
                            self._playback.play_stereo_looped_at_key(left, right, rate, zone.original_key, played_key,
                                                                     start_frame, loop_start, loop_end, s.is_reversed)
                        else:
                            self._playback.play_stereo_at_key(left, right, rate, zone.original_key, played_key,
                                                              start_frame, s.is_reversed)
                        self._after_piano_start(matches)
                        return

        if loop:
            self._playback.play_looped_at_key(s.samples(), rate, zone.original_key, played_key,
                                              start_frame, loop_start, loop_end, s.is_reversed)
        else:
            self._playback.play_at_key(s.samples(), rate, zone.original_key, played_key, start_frame, s.is_reversed)
        self._after_piano_start(matches)

    def _after_piano_start(self, matches_selection: bool) -> None:
        self.playback_matches_selection = matches_selection
        self.is_playing = True
        self.is_paused = False
        self._piano_key_generation = self._playback.generation

    def release_piano_key(self) -> None:
        """Mouse-up on the keymap piano: 'plays only while held' — but ONLY stops playback that is still the
        piano trigger's own (generation unchanged). The transport Play button is a click, not a hold, and
        must keep a loop going."""
        if self._piano_key_generation >= 0 and self._piano_key_generation == self._playback.generation:
            self.stop_playback()
        self._piano_key_generation = -1

    # ── cursor / stop / seek ───────────────────────────────────────────────────────────────

    def set_cursor_frame(self, frame: int) -> None:
        """Moves the grey scrub line WITHOUT starting playback; the next Play starts from here."""
        if self._selected_sample is None:
            return
        self._cursor_frame = max(0, min(frame, max(0, self.sample_frame_count - 1)))

    def stop_playback(self) -> None:
        self._playback.stop()
        self.is_playing = False
        self.is_paused = False

    def play_from_frame(self, frame: int) -> None:
        """Scrub-click 'play from here', and the Pause/Resume + Rewind/FF restart path. Ignores LOOP (an
        audition gesture) but NOT Reverse — a reversed sample that played backward from Play but forward from
        Resume would be worse. Under reverse, `frame` is the LOWER bound to read down to, not where to begin."""
        s = self._selected_sample
        if s is None or self.sample_waveform is None:
            return
        self._playback.boost_enabled = self.sample_12db_boost_enabled
        stereo = self.has_stereo_pair and not self.split_lr and self.partner_sample_waveform is not None
        reverse = self.sample_reverse_enabled
        if stereo:
            self._playback.play_stereo_from(self.left_sample_waveform, self.right_sample_waveform,
                                            int(s.sample_rate), frame, reverse)
        else:
            self._playback.play_from(self.sample_waveform, int(s.sample_rate), frame, reverse)
        self.playback_matches_selection = True
        self.is_playing = True
        self.is_paused = False

    # ── transport bar ──────────────────────────────────────────────────────────────────────

    def transport_toggle_pause(self) -> None:
        """Resume is a ONE-SHOT from the paused frame, not a resumed loop — a stated simplification."""
        if self.is_paused:
            self.is_paused = False
            self.play_from_frame(0 if self._cursor_frame < 0 else self._cursor_frame)
        elif self.is_playing:
            self._cursor_frame = self.get_playback_frame()
            self._playback.stop()
            self.is_playing = False
            self.is_paused = True

    def transport_locate_start(self) -> None:
        self._transport_seek_to(0)

    def transport_locate_end(self) -> None:
        self._transport_seek_to(max(0, self.sample_frame_count - 1))

    def transport_seek_relative(self, direction: int) -> None:
        """A fixed-FRACTION step (10 %, floored at 1 frame) so it scales from a one-shot to a long recording."""
        step = max(1, self.sample_frame_count // 10)
        current = self.get_playback_frame() if self.is_playing else (0 if self._cursor_frame < 0 else self._cursor_frame)
        self._transport_seek_to(current + direction * step)

    def _transport_seek_to(self, frame: int) -> None:
        s = self._selected_sample
        if s is None or s.is_header_only:
            return
        frame = max(0, min(frame, max(0, self.sample_frame_count - 1)))
        self._cursor_frame = frame
        if self.is_playing or self.is_paused:
            self.is_paused = False
            self.play_from_frame(frame)
        else:
            for cb in list(self.cursor_moved_listeners):
                cb(frame)

    # ── volume / pan / meters (polled by the window's timer) ───────────────────────────────

    @property
    def volume(self) -> float:
        return self._playback.volume

    @volume.setter
    def volume(self, v: float) -> None:
        self._playback.volume = v

    @property
    def pan(self) -> int:
        return self._playback.pan

    @pan.setter
    def pan(self, v: int) -> None:
        self._playback.pan = v

    def get_playback_level(self) -> float:
        return self._playback.peak_level

    def get_playback_level_left(self) -> float:
        return self._playback.peak_left

    def get_playback_level_right(self) -> float:
        return self._playback.peak_right

    def get_playback_frame(self) -> int:
        return self._playback.position_frame
