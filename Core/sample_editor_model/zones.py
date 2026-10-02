r"""
Sample Editor model — zone management, import into zones/repository, link/assign, remove sample,
multisample create/delete. Port of SampleEditorViewModel.cs ~2796-3180, 3259-3566, 3600-3690, 3802-4237.

Every zone-ADDING method below does a full Save + tree rebuild, which replaces every KmpZone instance — that is
incompatible with the zone-undo stack's live-object-identity design, so none of them are on it. Delete Zone is
the manual undo for an accidental Add. Delete Zone / boundary move / reorder, by contrast, patch the tree in
place and ARE undoable.
"""
from __future__ import annotations

import logging
import os
import shutil
from typing import List, Optional, Sequence, Tuple

from Core import audio_import
from Core import sample_import_builder as builder
from Core.sample_edit_undo import ZoneListSnapshot
from Core.sample_editor_model.core import DOMAIN_ZONE, cascade_top_keys, is_under, zone_index
from Core.sample_editor_model.tree import SampleTreeNode
from Core.sample_support import (
    ORIGINAL_KEY_CENTER, ORIGINAL_KEY_TOP, ZONE_POSITION_LEFT)
from Data import ksc_collection as ksc_mod
from Data.ksf_sample import KsfSample
from Data.kmp_multisample import KmpMultisample, KmpZone
from Utils.midi_note_name import to_name

log = logging.getLogger(__name__)

SKIPPED = builder.SKIPPED_SAMPLE_FILENAME


def _open_ksf(path: str) -> Optional[KsfSample]:
    with open(path, "rb") as f:
        return KsfSample.open(f.read())


class ZoneMixin:
    last_added_zone_index = -1
    last_imported_zone_index = -1
    last_deleted_zone_index = -1

    # ── helpers ────────────────────────────────────────────────────────────────────────────

    def _target_multisample(self, none_message: str = "Select a multisample (or one of its zones) first."
                            ) -> Tuple[Optional[KmpMultisample], Optional[str]]:
        if self._selected_node is None and self._selected_zone is None:
            self.status_text = none_message
            return None, None
        m, path = self._resolve_context_multisample()
        if m is None or path is None:
            self.status_text = "Couldn't resolve the target multisample."
            return None, None
        return m, path

    def _refresh_tree_after_mutation(self, m: KmpMultisample, kmp_path: str) -> None:
        """Same tree rebuild the open_* methods do at load time — adding a zone changes the tree shape exactly
        the way opening one freshly would."""
        if self._collection is not None and self._collection_path is not None:
            self._rebuild_tree_from_collection(self._collection_path, self._collection)
        else:
            self.select_node(None)
            self.roots.clear()
            self.roots.append(self._build_multisample_node(m, kmp_path))
            self._tree_refreshed()

    def _zone_prefs(self) -> Tuple[str, int, str]:
        s = self.settings
        return (getattr(s, "sample_zone_create_position", "Right"),
                max(1, min(127, int(getattr(s, "sample_zone_create_range", 12)))),
                getattr(s, "sample_zone_original_key_position", "Bottom"))

    # ── import audio / placeholder zones ───────────────────────────────────────────────────

    def import_audio_as_new_zone(self, audio_path: str, original_key: int, top_key: int) -> None:
        m, kmp_path = self._target_multisample()
        if m is None:
            return
        try:
            pcm = audio_import.import_to_mono_44100(audio_path)
            name = os.path.splitext(os.path.basename(audio_path))[0]
            zone = builder.add_sample_zone(m, kmp_path, name, pcm, audio_import.TARGET_SAMPLE_RATE, original_key, top_key)
            self._save_multisample_now(m, kmp_path)
            self._refresh_tree_after_mutation(m, kmp_path)
            self.status_text = f"Imported '{os.path.basename(audio_path)}' as zone '{zone.filename}' (key {original_key}-{top_key})."
        except Exception as ex:
            log.error("Sample Editor: audio import '%s' failed: %s", audio_path, ex, exc_info=True)
            self.status_text = f"Import failed: {ex}"

    def add_placeholder_zone(self) -> Optional[str]:
        """An EMPTY SKIPPEDSAMPLE zone — the doc's own 'no real .KSF backs this' convention. Carved per Settings >
        Sample Editor > Create Zone Preferences; a real sample is attached afterwards via import_sample_into_zone,
        which keeps this zone's key range. Returns the .KMP path on success."""
        self.last_added_zone_index = -1
        m, kmp_path = self._target_multisample()
        if m is None:
            return None
        if len(m.zones) >= builder.MAX_ZONES_PER_MULTISAMPLE:
            self.status_text = (f"'{m.name}{m.suffix}' already has {builder.MAX_ZONES_PER_MULTISAMPLE} zones (the maximum) "
                                "- remove one before adding another.")
            return None
        try:
            position, range_setting, orig_pos = self._zone_prefs()
            insert_index = len(m.zones)
            shrunk_prev_top: Optional[int] = None
            if not m.zones:
                new_low, new_top = 0, 127
            else:
                last = m.zones[-1]
                last_low = m.zones[-2].top_key + 1 if len(m.zones) > 1 else 0
                if position == ZONE_POSITION_LEFT:
                    # takes the BOTTOM `width` keys of the current last zone; that zone keeps its TopKey and stays
                    # last, the new one goes just before it so list order still matches ascending key order
                    available = last.top_key - last_low + 1
                    width = max(1, min(range_setting, available - 1))
                    new_low, new_top = last_low, last_low + width - 1
                    insert_index = len(m.zones) - 1
                elif last.top_key < 127:
                    # appended ABOVE the top zone, claiming unassigned range instead of carving into it — only the
                    # FIRST zone is pinned (to 0/C-1), a top zone has no requirement to reach 127
                    width = max(1, min(range_setting, 127 - last.top_key))
                    new_low, new_top = last.top_key + 1, last.top_key + width
                else:
                    available = last.top_key - last_low + 1
                    width = max(1, min(range_setting, available - 1))
                    new_low, new_top = last.top_key - width + 1, last.top_key
                    last.top_key = max(last_low, new_low - 1)
                    shrunk_prev_top = last.top_key

            orig_key = (new_top if orig_pos == ORIGINAL_KEY_TOP
                        else (new_low + new_top) // 2 if orig_pos == ORIGINAL_KEY_CENTER else new_low)
            m.zones.insert(insert_index, KmpZone(filename=SKIPPED, original_key=orig_key, top_key=new_top))
            self._save_multisample_now(m, kmp_path)

            # Mirror onto a resolved stereo sibling, or the halves' key ranges drift apart and the exact
            # (OriginalKey, TopKey) pair match stops resolving. The LIVE in-tree sibling, not a disk copy: a
            # pending sibling edit survives the rebuild below, and a fresh disk copy would leave the tree holding
            # the pending object WITHOUT the new zone.
            sibling_path = None
            if self._collection is not None and m.suffix in ("-L", "-R"):
                sibling, sib_path = self._resolve_stereo_sibling(m, kmp_path)
                if sibling is not None and sib_path is not None and len(sibling.zones) == len(m.zones) - 1:
                    if shrunk_prev_top is not None and sibling.zones:
                        sibling.zones[-1].top_key = shrunk_prev_top
                    sibling.zones.insert(insert_index, KmpZone(filename=SKIPPED, original_key=orig_key, top_key=new_top))
                    self._save_multisample_now(sibling, sib_path)
                    sibling_path = sib_path

            self.last_added_zone_index = insert_index
            self._refresh_tree_after_mutation(m, kmp_path)
            self.status_text = (f"Added an empty zone ({to_name(new_low)}-{to_name(new_top)})"
                                + (" to both stereo channels" if sibling_path else "")
                                + ' - right-click it and choose "Import Sample..." to attach audio.')
            return kmp_path
        except Exception as ex:
            log.error("Sample Editor: add placeholder zone failed: %s", ex, exc_info=True)
            self.status_text = f"Failed to add zone: {ex}"
            return None

    def _needs_fresh_filename(self, m: KmpMultisample, kmp_path: str, zone: KmpZone) -> bool:
        """Can `zone` be given new audio by overwriting its OWN .KSF in place, or does it need a fresh name?
        A placeholder has nothing to overwrite. Overwriting a real file is only safe if nothing else depends on
        that exact name: (a) the zone's own file is a stub, or (b) another zone's SMF1 names THIS zone's filename
        as its audio source — overwriting would silently change that sibling's sound too."""
        if zone.is_skipped:
            return True
        own_path = zone.ksf_path(kmp_path)
        try:
            if os.path.isfile(own_path):
                own = _open_ksf(own_path)
                if own is not None and own.stub_target_filename is not None:
                    return True
        except Exception:
            return True
        for sib in m.zones:
            if sib is zone or sib.is_skipped:
                continue
            try:
                sp = sib.ksf_path(kmp_path)
                if os.path.isfile(sp):
                    s = _open_ksf(sp)
                    if s is not None and (s.stub_target_filename or "").lower() == zone.filename.lower():
                        return True
            except Exception:
                pass
        return False

    def import_sample_into_zone(self, zone: KmpZone, audio_paths: Sequence[str]) -> Optional[str]:
        """Every import ALSO populates the repository (bare .KSF entries, 'Un-referenced Samples'); with several
        files all are written there but only the FIRST is assigned to `zone`."""
        self.last_imported_zone_index = -1
        if not audio_paths:
            return None
        written = self.import_samples_to_collection(audio_paths)
        if not written:
            return None
        kmp_path = self.assign_existing_ksf_to_zone(zone, written[0])
        if kmp_path is not None:
            self.status_text = (f"Imported '{os.path.basename(written[0])}' and assigned it to zone '{zone.filename}'."
                                if len(written) == 1 else
                                f"Imported {len(written)} sample(s) to the repository; assigned "
                                f"'{os.path.basename(written[0])}' to zone '{zone.filename}'.")
        return kmp_path

    def add_zone_from_existing_ksf(self, source_ksf_path: str, original_key: int, top_key: int) -> None:
        """A new zone at a key range using an EXISTING .KSF's audio (a duplicate copy, not a shared reference)."""
        m, kmp_path = self._target_multisample()
        if m is None:
            return
        try:
            src = _open_ksf(source_ksf_path)
            if src is None or src.is_header_only:
                self.status_text = "That file isn't a readable .KSF with audio data."
                return
            zone = builder.add_sample_zone(m, kmp_path, src.name, src.samples(), int(src.sample_rate),
                                           original_key, top_key, src.suffix)
            self._save_multisample_now(m, kmp_path)
            self._refresh_tree_after_mutation(m, kmp_path)
            self.status_text = f"Added zone '{zone.filename}' from '{os.path.basename(source_ksf_path)}' (key {original_key}-{top_key})."
        except Exception as ex:
            log.error("Sample Editor: new zone from '%s' failed: %s", source_ksf_path, ex, exc_info=True)
            self.status_text = f"Failed to add zone: {ex}"

    # ── repository (bare, un-referenced .KSF entries) ──────────────────────────────────────

    def bare_sample_entries(self) -> List[str]:
        """Every bare .KSF directly in the collection's content folder. Reads the FOLDER, not Entries: once a
        repository sample is assigned into a zone its Entries line is retired (so the saved .KSC matches real
        Kronos output), but the picker must still offer that audio for reuse. A zone's own .KSF lives one level
        deeper, so a non-recursive scan is exactly 'every sample available to reuse'."""
        if self._collection is None or self._collection_path is None:
            return []
        kmp_dir = ksc_mod.content_dir_for(self._collection_path)
        if not os.path.isdir(kmp_dir):
            return []
        return sorted((os.path.join(kmp_dir, f) for f in os.listdir(kmp_dir)
                       if f.upper().endswith(".KSF") and os.path.isfile(os.path.join(kmp_dir, f))), key=str.lower)

    def _unique_bare_ksf_file_name(self, sample_name: str) -> str:
        bad = '<>:"/\\|?*' + "".join(chr(i) for i in range(32))
        safe = "".join("_" if c in bad else c for c in sample_name) or "Sample"
        kmp_dir = ksc_mod.content_dir_for(self._collection_path)
        candidate, n = f"{safe}.KSF", 1
        while os.path.exists(os.path.join(kmp_dir, candidate)):
            candidate = f"{safe}_{n}.KSF"
            n += 1
        return candidate

    def import_samples_to_collection(self, audio_paths: Sequence[str]) -> List[str]:
        """Decodes each file and writes it as a standalone resident .KSF in the collection's content folder
        (+ a bare Entries line), touching no multisample. A genuinely stereo source becomes a matched -L/-R bare
        pair so assign can auto-detect it later. One collection Save at the end — a multi-file import is one user
        action. Returns the written paths in order; failures are skipped, not padded."""
        written: List[str] = []
        if self._collection is None or self._collection_path is None:
            self.status_text = "Open or create a collection first."
            return written
        kmp_dir = ksc_mod.content_dir_for(self._collection_path)
        os.makedirs(kmp_dir, exist_ok=True)
        failures: List[str] = []
        alloc = ksc_mod.Sno1Allocator(kmp_dir)          # one scan for the whole batch
        rate = audio_import.TARGET_SAMPLE_RATE
        for audio_path in audio_paths:
            try:
                name = os.path.splitext(os.path.basename(audio_path))[0]
                if audio_import.get_source_channel_count(audio_path) >= 2:
                    left, right = audio_import.import_stereo_to_lr_44100(audio_path)
                    lf, rf = self._unique_bare_ksf_file_name(f"{name}-L"), self._unique_bare_ksf_file_name(f"{name}-R")
                    lk = KsfSample(name=name, suffix="-L", sample_rate=rate, flags=0x81, sno1=alloc.next())
                    lk.set_samples(left)
                    lp = os.path.join(kmp_dir, lf)
                    lk.save(lp)
                    rk = KsfSample(name=name, suffix="-R", sample_rate=rate, flags=0x81, sno1=alloc.next())
                    rk.set_samples(right)
                    rp = os.path.join(kmp_dir, rf)
                    rk.save(rp)
                    self._collection.entries.extend([lf, rf])
                    written.extend([lp, rp])
                else:
                    pcm = audio_import.import_to_mono_44100(audio_path)
                    fn = self._unique_bare_ksf_file_name(name)
                    k = KsfSample(name=name, sample_rate=rate, flags=0x81, sno1=alloc.next())
                    k.set_samples(pcm)
                    kp = os.path.join(kmp_dir, fn)
                    k.save(kp)
                    self._collection.entries.append(fn)
                    written.append(kp)
            except Exception as ex:
                log.error("Sample Editor: import to repository '%s' failed: %s", audio_path, ex, exc_info=True)
                failures.append(os.path.basename(audio_path))
        if written:
            self._save_collection_with_user_bank(self._collection, self._collection_path)
        self.status_text = (f"Imported {len(written)} sample(s) to the repository (Un-referenced Samples)." if not failures
                            else f"Imported {len(written)} sample(s), {len(failures)} failed: {', '.join(failures)}")
        return written

    def _write_assigned_sample(self, m: KmpMultisample, kmp_path: str, zone: KmpZone, name: str, suffix: str,
                               sample_rate: int, pcm) -> None:
        if self._needs_fresh_filename(m, kmp_path, zone):
            zone.filename = m.next_free_zone_filename()
        content_dir = os.path.dirname(kmp_path) or "."
        ksf = KsfSample(name=name, suffix=suffix, sample_rate=int(sample_rate), flags=0x81,
                        sno1=ksc_mod.next_free_sno1(content_dir))       # collection-unique, never the field default
        ksf.set_samples(pcm)
        ksf_path = zone.ksf_path(kmp_path)
        os.makedirs(os.path.dirname(ksf_path), exist_ok=True)
        ksf.save(ksf_path)

    def _write_linked_sample(self, m: KmpMultisample, kmp_path: str, zone: KmpZone, src: KsfSample, source_ksf_path: str) -> None:
        """A deliberately header-only .KSF sharing `src`'s SNO1 (the hardware-confirmed sharing mechanism, doc
        §3.2/§7) plus the REQUIRED SMF1 naming `src`'s file — a header-only .KSF with no SMF1 hangs the Disk-page
        Load solid. `src`'s start/loop/flags/tune seed the link's own fields, independently editable afterwards."""
        if self._needs_fresh_filename(m, kmp_path, zone):
            zone.filename = m.next_free_zone_filename()
        ksf = KsfSample(name=src.name, suffix=src.suffix, sample_rate=int(src.sample_rate), flags=src.flags,
                        sno1=src.sno1, sample_start=src.sample_start, loop_start=src.loop_start, loop_end=src.loop_end)
        ksf.restore_loop_tune(src.loop_tune)
        ksf.set_stub_target(os.path.basename(source_ksf_path))
        ksf_path = zone.ksf_path(kmp_path)
        os.makedirs(os.path.dirname(ksf_path), exist_ok=True)
        ksf.save(ksf_path)

    def link_existing_ksf_to_zone(self, zone: KmpZone, source_ksf_path: str) -> Optional[str]:
        """The 'Link' checkbox: share `source`'s audio instead of copying. Refuses a self-link, a source with no
        audio of its own (no multi-hop chains), and a source filename too long for SMF1's 12 characters — all
        BEFORE writing anything."""
        self.last_imported_zone_index = -1
        m, kmp_path = self._find_owner_of_zone(zone)
        if m is None:
            self.status_text = "Couldn't resolve this zone's multisample."
            return None
        # Writing the stub to the zone's own current path when that IS the source would overwrite the source with
        # a stub pointing at itself — permanently broken.
        if not zone.is_skipped and os.path.normcase(zone.ksf_path(kmp_path)) == os.path.normcase(source_ksf_path):
            self.status_text = "Can't link a zone to its own current sample - pick a different one to share audio with."
            return None
        try:
            src = _open_ksf(source_ksf_path)
            if src is None or src.is_header_only:
                self.status_text = "Can't link to that file - it has no audio data of its own. Pick a sample with real audio, not another link."
                return None
            file_name = os.path.basename(source_ksf_path)
            if not KsfSample.is_valid_stub_target(file_name):
                self.status_text = (f"Can't link to '{file_name}' - its filename is {len(file_name.encode('ascii', 'replace'))} characters, "
                                    "longer than the Kronos's own 12-character SMF1 link limit. Uncheck Link to copy its audio into this zone instead.")
                return None
            self._write_linked_sample(m, kmp_path, zone, src, source_ksf_path)
            self.last_imported_zone_index = zone_index(m.zones, zone)
            self._save_multisample_now(m, kmp_path)
            self._refresh_tree_after_mutation(m, kmp_path)
            self.status_text = (f"Linked zone '{zone.filename}' to '{file_name}' - shares its audio "
                                f"(Sample Number {src.sno1}), own loop points.")
            return kmp_path
        except Exception as ex:
            log.error("Sample Editor: link to existing .KSF '%s' failed: %s", source_ksf_path, ex, exc_info=True)
            self.status_text = f"Link failed: {ex}"
            return None

    def _find_owner_of_zone(self, zone: KmpZone) -> Tuple[Optional[KmpMultisample], Optional[str]]:
        from Core.sample_editor_model.tree import find_multisample_and_path_containing
        return find_multisample_and_path_containing(self.roots, zone)

    def _try_find_repository_stereo_partner(self, src: KsfSample) -> Tuple[Optional[str], Optional[KsfSample]]:
        """A bare -L/-R .KSF with a same-Name, opposite-Suffix sibling ALSO bare is one stereo sample."""
        if src.suffix not in ("-L", "-R"):
            return None, None
        want = "-R" if src.suffix == "-L" else "-L"
        for path in self.bare_sample_entries():
            try:
                c = _open_ksf(path)
                if c is not None and c.name == src.name and c.suffix == want:
                    return path, c
            except Exception:
                pass
        return None, None

    @staticmethod
    def _resolve_corresponding_zone(m: KmpMultisample, zone: KmpZone, sibling: KmpMultisample) -> Optional[KmpZone]:
        """Which of `sibling`'s zones corresponds to `zone`: exact key range first, same list position as
        fallback. Unlike the display-side partner resolution this does NOT skip placeholders — a placeholder
        sibling zone is exactly the normal target of a write."""
        match = next((z for z in sibling.zones if z.original_key == zone.original_key and z.top_key == zone.top_key), None)
        if match is not None:
            return match
        idx = zone_index(m.zones, zone)
        return sibling.zones[idx] if 0 <= idx < len(sibling.zones) else None

    def assign_existing_ksf_to_zone(self, zone: KmpZone, source_ksf_path: str) -> Optional[str]:
        """Assigns an already-imported repository sample (or any readable .KSF) to `zone`, replacing its audio.
        If the source is one half of a repository stereo pair AND the zone's multisample has a stereo sibling,
        both channels are assigned at once."""
        self.last_imported_zone_index = -1
        m, kmp_path = self._find_owner_of_zone(zone)
        if m is None:
            self.status_text = "Couldn't resolve this zone's multisample."
            return None
        try:
            src = _open_ksf(source_ksf_path)
            if src is None or src.is_header_only:
                self.status_text = "That file isn't a readable .KSF with audio data."
                return None

            if m.suffix in ("-L", "-R"):
                partner_path, partner_src = self._try_find_repository_stereo_partner(src)
                if partner_src is not None:
                    sibling, sibling_path = self._resolve_stereo_sibling(m, kmp_path)
                    sibling_zone = (self._resolve_corresponding_zone(m, zone, sibling)
                                    if sibling is not None and sibling_path is not None else None)
                    if sibling is not None and sibling_path is not None and sibling_zone is not None:
                        # Sources and targets are two INDEPENDENT choices: m.suffix says which multisample the
                        # clicked zone is in, not which channel `src` is. Deriving both from one discriminator
                        # wrote R audio into the L multisample whenever the R half was picked — an audible L/R
                        # inversion with no structural corruption to make it obvious.
                        left_src, right_src = (src, partner_src) if src.suffix == "-L" else (partner_src, src)
                        if m.suffix == "-L":
                            lm, lp, lz, rm, rp, rz = m, kmp_path, zone, sibling, sibling_path, sibling_zone
                        else:
                            lm, lp, lz, rm, rp, rz = sibling, sibling_path, sibling_zone, m, kmp_path, zone
                        self._write_assigned_sample(lm, lp, lz, left_src.name, "-L", left_src.sample_rate, left_src.samples())
                        self._write_assigned_sample(rm, rp, rz, right_src.name, "-R", right_src.sample_rate, right_src.samples())
                        self.last_imported_zone_index = zone_index(m.zones, zone)
                        self._save_multisample_now(lm, lp)
                        if os.path.normcase(lp) != os.path.normcase(rp):
                            self._save_multisample_now(rm, rp)
                        self._retire_consumed_repository_entry(source_ksf_path)
                        if partner_path:
                            self._retire_consumed_repository_entry(partner_path)
                        self._refresh_tree_after_mutation(m, kmp_path)
                        self.status_text = f"Assigned stereo sample '{src.name}' to both channels."
                        return kmp_path

            self._write_assigned_sample(m, kmp_path, zone, src.name, src.suffix, src.sample_rate, src.samples())
            self.last_imported_zone_index = zone_index(m.zones, zone)
            self._save_multisample_now(m, kmp_path)
            self._retire_consumed_repository_entry(source_ksf_path)
            self._refresh_tree_after_mutation(m, kmp_path)
            self.status_text = f"Assigned '{os.path.basename(source_ksf_path)}' to zone '{zone.filename}'."
            return kmp_path
        except Exception as ex:
            log.error("Sample Editor: assign existing .KSF '%s' to zone failed: %s", source_ksf_path, ex, exc_info=True)
            self.status_text = f"Assign failed: {ex}"
            return None

    def _retire_consumed_repository_entry(self, ksf_path: str) -> None:
        """Once a bare entry's audio is copied into a real zone it leaves the SAVED .KSC's unreferenced list (a
        real Kronos collection never carries a bare line for audio a keymap owns; the extras are suspected to
        confuse OA.ko's array-sizing pre-scan). The FILE stays so the audio is reusable this session."""
        if self._collection is None or self._collection_path is None:
            return
        name = os.path.basename(ksf_path).lower()
        kept = [e for e in self._collection.entries if e.lower() != name]
        if len(kept) != len(self._collection.entries):
            self._collection.entries[:] = kept
            self._save_collection_with_user_bank(self._collection, self._collection_path)

    def _sweep_orphaned_repository_files(self) -> None:
        """Deletes every bare .KSF in the content folder that is BOTH no longer a listed entry AND not referenced
        by any zone in the loaded tree — dead weight a real Kronos-authored collection does not have. Run from
        save_all_changes, NOT from retire (which would break same-session reuse into a second zone)."""
        if self._collection is None or self._collection_path is None:
            return
        content_dir = ksc_mod.content_dir_for(self._collection_path)
        if not os.path.isdir(content_dir):
            return
        referenced = {e.lower() for e in self._collection.entries}
        for node in self.all_multisample_nodes():
            for z in node.multisample_ref[0].zones:
                if not z.is_skipped:
                    referenced.add(z.filename.lower())
        for f in os.listdir(content_dir):
            p = os.path.join(content_dir, f)
            if f.upper().endswith(".KSF") and os.path.isfile(p) and f.lower() not in referenced:
                try:
                    os.remove(p)
                except OSError as ex:
                    log.warning("Sample Editor: couldn't remove orphaned repository file '%s': %s", p, ex)

    # ── stereo pairs / new multisamples ────────────────────────────────────────────────────

    def new_stereo_multisample_pair_in_collection(self, base_name: str, mno1_left: int) -> Optional[SampleTreeNode]:
        if self._collection is None or self._collection_path is None:
            self.status_text = "Open or create a collection first."
            return None
        try:
            left, left_path, right, right_path = builder.create_stereo_multisample_pair(
                self._collection, self._collection_path, base_name, mno1_left)
            # The default first zone goes on BOTH halves (identical key range, required for stereo parity, §2.2).
            # Deliberately not inside the builder primitive: self-tests call it expecting a genuinely empty pair.
            left.zones.append(builder.make_default_first_zone())
            right.zones.append(builder.make_default_first_zone())
            left.save(left_path)
            right.save(right_path)
            self._rebuild_tree_from_collection(self._collection_path, self._collection)
            self.status_text = (f"Created stereo multisample pair '{base_name}' "
                                f"('{os.path.basename(left_path)}' + '{os.path.basename(right_path)}').")
            return self._node_for_path(left_path)
        except Exception as ex:
            log.error("Sample Editor: new stereo multisample pair failed: %s", ex, exc_info=True)
            self.status_text = f"Failed to create stereo pair: {ex}"
            return None

    def _node_for_path(self, path: str) -> Optional[SampleTreeNode]:
        key = os.path.normcase(path)
        return next((n for n in self.all_multisample_nodes() if os.path.normcase(n.multisample_ref[1]) == key), None)

    def import_stereo_audio_as_new_zone_pair(self, audio_path: str, original_key: int, top_key: int) -> None:
        m, kmp_path = self._target_multisample("Select one half of a stereo multisample pair first.")
        if m is None:
            return
        if self._collection is None or self._collection_path is None:
            self.status_text = "Open a collection first - stereo pairing needs to search its other multisamples."
            return
        sibling, sibling_path = self._resolve_stereo_sibling(m, kmp_path)
        if sibling is None or sibling_path is None:
            self.status_text = (f"'{m.name}{m.suffix}' has no matching stereo sibling in this collection "
                                "- create a stereo pair first (File > New Stereo Multisample Pair).")
            return
        lm, lp, rm, rp = (m, kmp_path, sibling, sibling_path) if m.suffix == "-L" else (sibling, sibling_path, m, kmp_path)
        try:
            left_pcm, right_pcm = audio_import.import_stereo_to_lr_44100(audio_path)
            name = os.path.splitext(os.path.basename(audio_path))[0]
            lz, rz = builder.add_stereo_sample_zone_pair(lm, lp, rm, rp, name, left_pcm, right_pcm,
                                                         audio_import.TARGET_SAMPLE_RATE, original_key, top_key)
            self._save_multisample_now(lm, lp)
            self._save_multisample_now(rm, rp)
            self._rebuild_tree_from_collection(self._collection_path, self._collection)
            self.status_text = (f"Imported '{os.path.basename(audio_path)}' as stereo zone pair "
                                f"'{lz.filename}'/'{rz.filename}' (key {original_key}-{top_key}).")
        except Exception as ex:
            log.error("Sample Editor: stereo audio import '%s' failed: %s", audio_path, ex, exc_info=True)
            self.status_text = f"Stereo import failed: {ex}"

    def next_free_mno1(self, slots_needed: int = 1) -> int:
        """Smallest non-negative MNO1 unused in the LIVE tree (a just-created, unsaved multisample counts).
        slots_needed=2 for a stereo pair: candidate and candidate+1 both free. Freed slots are reused."""
        used = {n.multisample_ref[0].mno1 for n in self.all_multisample_nodes()}
        candidate = 0
        while any((candidate + i) in used for i in range(slots_needed)):
            candidate += 1
        return candidate

    def new_multisample_in_collection(self, name: str, mno1: int) -> Optional[SampleTreeNode]:
        """Returns the freshly created node (found in the rebuilt tree by the exact path written here) so the
        caller can select it — the rebuild drops the selection."""
        if self._collection is None or self._collection_path is None:
            self.status_text = "Open or create a collection first."
            return None
        try:
            kmp_dir = ksc_mod.content_dir_for(self._collection_path)
            os.makedirs(kmp_dir, exist_ok=True)
            file_name = KmpMultisample.auto_file_name(name, mno1)       # Kronos-correct filename from the start
            kmp_path = os.path.join(kmp_dir, file_name)
            m = KmpMultisample(name=name, mno1=mno1)
            m.zones.append(builder.make_default_first_zone())
            self._save_multisample_now(m, kmp_path)
            self._collection.entries.append(file_name)
            self._save_collection_with_user_bank(self._collection, self._collection_path)
            self._rebuild_tree_from_collection(self._collection_path, self._collection)
            self.status_text = f"Created multisample '{file_name}'."
            return self._node_for_path(kmp_path)
        except Exception as ex:
            log.error("Sample Editor: new multisample failed: %s", ex, exc_info=True)
            self.status_text = f"Failed to create multisample: {ex}"
            return None

    def delete_selected_multisample(self) -> None:
        """Deletes the selected .KMP: manifest entry, file, and its zone subfolder (only ever that multisample's
        own — it can't orphan a sibling's audio). Does NOT cascade to a stereo sibling. Not undoable; the caller
        confirms first."""
        m, kmp_path = self._resolve_context_multisample()
        if m is None or kmp_path is None:
            self.status_text = "Select a multisample first."
            return
        if self._collection is None or self._collection_path is None:
            self.status_text = "No collection is open."
            return
        label = m.name + m.suffix
        try:
            file_name = os.path.basename(kmp_path)
            zone_dir = os.path.join(os.path.dirname(kmp_path), os.path.splitext(file_name)[0])
            self._collection.entries[:] = [e for e in self._collection.entries if e.lower() != file_name.lower()]
            self._save_collection_with_user_bank(self._collection, self._collection_path)
            if os.path.isdir(zone_dir):
                shutil.rmtree(zone_dir)
            if os.path.isfile(kmp_path):
                os.remove(kmp_path)
            self._dirty_multisamples.remove(kmp_path)       # a pending claim would write the deleted .KMP back on Save
            for p in [p for p in self._dirty_samples.keys() if is_under(p, zone_dir)]:
                self._dirty_samples.remove(p)
            self.select_node(None)
            self._rebuild_tree_from_collection(self._collection_path, self._collection)
            self.status_text = f"Deleted multisample '{label}' ('{file_name}')."
        except Exception as ex:
            log.error("Sample Editor: delete multisample '%s' failed: %s", kmp_path, ex, exc_info=True)
            self.status_text = f"Failed to delete multisample: {ex}"

    # ── remove sample / delete zone ────────────────────────────────────────────────────────

    def remove_selected_sample(self) -> None:
        """'Remove Sample': deletes the zone's own .KSF AND the matching repository entries (import writes the
        audio twice — a repository copy and the zone's own — and clearing the reference alone left the 'removed'
        sample selectable in the combo), marks the zone SKIPPEDSAMPLE but keeps its key range. Mirrored onto the
        stereo sibling's matching zone. Not on the zone undo stack: that captures field values, not filesystem
        state, so 'undoing' would point the zone at a file that is gone."""
        zone = self._selected_zone
        if zone is None:
            self.status_text = "No zone selected."
            return
        if zone.is_skipped:
            self.status_text = "This zone has no sample assigned - nothing to remove."
            return
        m, kmp_path = self._resolve_context_multisample()
        if m is None or kmp_path is None:
            self.status_text = "Couldn't resolve the owning multisample."
            return

        sibling_zones = sibling_m = sibling_path = None
        idx = -1
        zones = self.current_multisample_zones
        if zones is not None:
            idx = zone_index(zones, zone)
            sibling_zones, sibling_m, sibling_path = self._resolve_sibling_zones_for(zones)
        sibling_zone = sibling_zones[idx] if sibling_zones is not None and 0 <= idx < len(sibling_zones) else None

        own_path = zone.ksf_path(kmp_path)
        own_name = own_suffix = None
        try:
            if os.path.isfile(own_path):
                own = _open_ksf(own_path)
                if own is not None:
                    own_name, own_suffix = own.name, own.suffix
        except Exception as ex:
            log.warning("Sample Editor: couldn't read '%s' before removing it: %s", own_path, ex)

        removed_sibling = sibling_zone is not None and not sibling_zone.is_skipped
        sibling_own = sibling_zone.ksf_path(sibling_path) if removed_sibling and sibling_path else None

        zone.filename = SKIPPED
        if sibling_zone is not None:
            sibling_zone.filename = SKIPPED
        self._delete_file_quietly(own_path)
        if sibling_own:
            self._delete_file_quietly(sibling_own)
        repo_deleted = self._delete_matching_repository_entries(own_name, own_suffix) if own_name is not None else 0

        self.zone_is_skipped = True
        self.zone_filename = "(skipped - no sample)"
        self.has_sample_loaded = False
        self.sample_waveform = None
        self.has_stereo_pair = False
        self.partner_sample_waveform = None
        self._zone_dirty = True
        if sibling_m is not None and sibling_path is not None:
            self._register_dirty_multisample(sibling_m, sibling_path)
        self._refresh_undo_redo_state()
        self.status_text = (f"Removed sample '{own_name or os.path.basename(own_path)}'"
                            f"{' (both L/R channels)' if removed_sibling else ''} from the session"
                            + (f" - also removed {repo_deleted} matching repository entr{'y' if repo_deleted == 1 else 'ies'}." if repo_deleted > 0 else ".")
                            + " (unsaved zone change - use Save Multisample).")

    @staticmethod
    def _delete_file_quietly(path: str) -> None:
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError as ex:
            log.warning("Sample Editor: couldn't delete '%s': %s", path, ex)

    def _delete_matching_repository_entries(self, name: str, suffix: Optional[str]) -> int:
        """Physically deletes every bare repository .KSF whose Name (and, for stereo, Suffix) matches, plus its
        manifest line — the 'actually gone' counterpart of retire. Accepted imprecision, same as the Sample
        combo's own de-dup: two unrelated imports with the identical Name+Suffix are indistinguishable."""
        if self._collection is None or self._collection_path is None:
            return 0
        deleted = 0
        for path in self.bare_sample_entries():
            try:
                k = _open_ksf(path)
                if k is None or k.name != name:
                    continue
                ok = suffix is None or k.suffix == suffix or (suffix in ("-L", "-R") and k.suffix in ("-L", "-R"))
                if not ok:
                    continue
                fn = os.path.basename(path).lower()
                self._collection.entries[:] = [e for e in self._collection.entries if e.lower() != fn]
                os.remove(path)
                deleted += 1
            except Exception as ex:
                log.warning("Sample Editor: couldn't remove repository sample '%s': %s", path, ex)
        if deleted > 0:
            self._save_collection_with_user_bank(self._collection, self._collection_path)
        return deleted

    def delete_zone_completely(self) -> Optional[str]:
        """'Delete Zone': removes the zone ENTRY (the underlying .KSF is left on disk). Each zone's range is
        implicit (previous TopKey + 1 .. own TopKey), so whatever follows absorbs the vacated range. Refuses to
        leave either half of a pair empty. Undoable: the tree is patched in place, not rebuilt, so the Zones list
        reference — and the zone-undo scope — survives."""
        self.last_deleted_zone_index = -1
        zone = self._selected_zone
        if zone is None:
            self.status_text = "No zone selected."
            return None
        m, kmp_path = self._resolve_context_multisample()
        if m is None or kmp_path is None:
            self.status_text = "Couldn't resolve the owning multisample."
            return None
        idx = zone_index(m.zones, zone)
        if idx < 0:
            self.status_text = "This zone is no longer in the keymap."
            return None

        sibling_zones, sibling_m, sibling_path = self._resolve_sibling_zones_for(m.zones)
        would_remove_sibling = sibling_zones is not None and idx < len(sibling_zones)
        if len(m.zones) <= 1 or (would_remove_sibling and len(sibling_zones) <= 1):
            self.status_text = ("Can't delete the last zone - a multisample always needs at least one keymap zone, "
                                "even with no sample assigned (the Kronos itself never allows an empty keymap).")
            return None

        self._zone_undo.record_before_edit(ZoneListSnapshot.of(m.zones, sibling_zones))
        self._undo_domains.append(DOMAIN_ZONE)
        self._redo_domains.clear()

        label = "(skipped)" if zone.is_skipped else zone.filename
        del m.zones[idx]
        if would_remove_sibling:
            del sibling_zones[idx]

        # Bypasses the _zone_dirty setter's auto-registration, which re-resolves the owner by walking the zone
        # lists for _selected_zone — just removed, so that lookup would now fail and skip registering `m`.
        self._zone_dirty_field = True
        self._register_dirty_multisample(m, kmp_path)
        if sibling_m is not None and sibling_path is not None:
            self._register_dirty_multisample(sibling_m, sibling_path)

        for node in self.all_multisample_nodes():
            nm = node.multisample_ref[0]
            if nm is m or (would_remove_sibling and nm is sibling_m):
                self._sync_multisample_node_children(node)
        self._tree_refreshed()
        self._refresh_undo_redo_state()

        self.last_deleted_zone_index = min(max(0, idx - 1), len(m.zones) - 1)
        self.status_text = (f"Deleted zone '{label}'{' on both L/R channels' if would_remove_sibling else ''} "
                            "(unsaved - use Save Multisample). Ctrl+Z to undo.")
        return kmp_path

    # ── keymap drags ───────────────────────────────────────────────────────────────────────

    def move_zone_boundary(self, zone: KmpZone, new_top_key: int) -> None:
        """Dragging a boundary in the piano keymap. Pulled DOWN shrinks `zone`; dragged UP past the next zone's
        Top Key it cascades every following zone up by the same amount (same rule as the typed Top Key field).
        Mirrored onto the sibling's zone at the SAME INDEX — a well-formed pair has identical zone lists."""
        new_top = max(0, min(127, new_top_key))
        if zone.top_key == new_top:
            return                                           # a drag that ended where it started
        sibling_zones = sibling_m = sibling_path = None
        idx = -1
        old_next_top: Optional[int] = None
        zones = self.current_multisample_zones
        if zones is not None:
            idx = zone_index(zones, zone)
            if 0 <= idx < len(zones) - 1:
                old_next_top = zones[idx + 1].top_key
            sibling_zones, sibling_m, sibling_path = self._resolve_sibling_zones_for(zones)
            self._zone_undo.record_before_edit(ZoneListSnapshot.of(zones, sibling_zones))
            self._undo_domains.append(DOMAIN_ZONE)
            self._redo_domains.clear()

        old_top = zone.top_key
        zone.top_key = new_top
        if sibling_zones is not None and 0 <= idx < len(sibling_zones):
            sibling_zones[idx].top_key = new_top
        if zone is self._selected_zone:
            self.zone_top_key = zone.top_key

        cascaded = old_next_top is not None and new_top >= old_next_top and zones is not None
        if cascaded:
            delta = new_top - old_top
            cascade_top_keys(zones, idx, delta)
            if sibling_zones is not None:
                cascade_top_keys(sibling_zones, idx, delta)

        self._zone_dirty = True
        if sibling_m is not None and sibling_path is not None:
            self._register_dirty_multisample(sibling_m, sibling_path)
        self._refresh_undo_redo_state()
        self.status_text = (f"Zone '{'(skipped)' if zone.is_skipped else zone.filename}' now extends to {to_name(zone.top_key)}"
                            f"{' (both L/R channels)' if sibling_zones is not None else ''}"
                            + (" - pushed the following zone(s) up to make room" if cascaded else "")
                            + " (unsaved - use Save Multisample).")

    def reorder_zone(self, dragged: KmpZone, drop_target: KmpZone) -> None:
        """Drag/drop reorder: each zone keeps its own key-range WIDTH and only its POSITION changes, so its
        absolute range shifts to wherever that width lands. Widths are captured BEFORE the move. toIndex is used
        as-is, NOT decremented for a forward drag: after the pop, inserting at the original index is what lands
        `dragged` in the target's slot (decrementing makes an adjacent forward drag a no-op)."""
        zones = self.current_multisample_zones
        if zones is None or dragged is drop_target:
            return
        from_i, to_i = zone_index(zones, dragged), zone_index(zones, drop_target)
        if from_i < 0 or to_i < 0:
            return
        # Of the three key-range edits this one most needs mirroring: it rewrites EVERY TopKey in the list.
        sibling_zones, sibling_m, sibling_path = self._resolve_sibling_zones_for(zones)
        self._zone_undo.record_before_edit(ZoneListSnapshot.of(zones, sibling_zones))
        self._undo_domains.append(DOMAIN_ZONE)
        self._redo_domains.clear()

        width = {}
        prev = -1
        for z in zones:
            width[id(z)] = z.top_key - prev
            prev = z.top_key

        zones.pop(from_i)
        zones.insert(to_i, dragged)
        running = -1
        for z in zones:
            running += width[id(z)]
            z.top_key = max(0, min(127, running))

        if sibling_zones is not None and from_i < len(sibling_zones) and to_i < len(sibling_zones):
            sd = sibling_zones.pop(from_i)
            sibling_zones.insert(to_i, sd)
            for i in range(len(sibling_zones)):
                sibling_zones[i].top_key = zones[i].top_key

        if self._selected_zone is not None:
            self.zone_original_key, self.zone_top_key = self._selected_zone.original_key, self._selected_zone.top_key
        self._zone_dirty = True
        if sibling_m is not None and sibling_path is not None:
            self._register_dirty_multisample(sibling_m, sibling_path)
        self._refresh_undo_redo_state()
        self.status_text = (f"Reordered zone '{'(skipped)' if dragged.is_skipped else dragged.filename}'"
                            f"{' (both L/R channels)' if sibling_zones is not None else ''} (unsaved - use Save Multisample).")
