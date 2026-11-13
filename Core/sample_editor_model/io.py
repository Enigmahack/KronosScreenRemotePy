r"""
Sample Editor model — saving, export, new collection / Save As, remote (FTP) pull/push.
Port of SampleEditorViewModel.cs ~271-392, 2617-2794, 3568-3598, 3691-3800, 4543-4569.

The remote source is duck-typed (the window supplies it; it owns the browse dialog and the FTP worker):
    pick_and_pull(extension, local_root) -> RemotePullResult
    push(local_path, remote_path)        -> RemotePushResult
    pick_folder_and_push_collection(ksc_path, collection) -> RemotePushResult
"""
from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from Core import sample_export as exporter
from Core.sample_editor_model.core import is_under
from Core.sample_support import resolve_workspace_root
from Data import ksc_collection as ksc_mod
from Data.ksc_collection import KscCollection
from Data.kmp_multisample import KmpMultisample

log = logging.getLogger(__name__)


@dataclass
class RemotePullResult:
    local_path: Optional[str]
    status_message: str = ""
    remote_map: Dict[str, str] = field(default_factory=dict)      # local path -> remote path


@dataclass
class RemotePushResult:
    status_message: str = ""


def is_user_bank(path: str) -> bool:
    """A live streamed shortcut to library content on the Kronos's own SSD, not real sample data — editing or
    pulling one makes no sense, there is only a pointer to edit."""
    return os.path.basename(path).upper().endswith("_USERBANK.KSC")


@dataclass
class _WriteOutcome:
    saved: List[str] = field(default_factory=list)               # paths that reached disk, in write order
    failure: Optional[Tuple[str, Exception]] = None             # the first path that did not, and why


def _write_files(items) -> _WriteOutcome:
    """WRITE phase (worker thread): saves each (path, sample-or-multisample) in order through its own atomic
    `save()`, stopping at the first failure. Touches no observable model state."""
    out = _WriteOutcome()
    for path, obj in items:
        try:
            obj.save(path)
        except Exception as ex:                          # noqa: BLE001 - handed back for the GUI-thread commit
            out.failure = (path, ex)
            return out
        out.saved.append(path)
    return out


def _delete_orphans(content_dir: str, referenced: Set[str]) -> None:
    """Deletes every bare .KSF in the content folder that is BOTH no longer a listed entry AND not referenced by any
    zone in the loaded tree (see ZoneMixin._sweep_orphaned_repository_files). Worker-safe: plain inputs only."""
    if not os.path.isdir(content_dir):
        return
    for f in os.listdir(content_dir):
        p = os.path.join(content_dir, f)
        if f.upper().endswith(".KSF") and os.path.isfile(p) and f.lower() not in referenced:
            try:
                os.remove(p)
            except OSError as ex:
                log.warning("Sample Editor: couldn't remove orphaned repository file '%s': %s", p, ex)


class IoMixin:
    # ── remote ─────────────────────────────────────────────────────────────────────────────

    def pull_collection_from_kronos(self, source) -> None:
        result = source.pick_and_pull(".KSC", resolve_workspace_root(self.settings))
        if result.local_path is None:
            self.status_text = result.status_message
            return
        self._remote_map.update(result.remote_map)
        self.open_collection(result.local_path)

    def pull_multisample_from_kronos(self, source) -> None:
        result = source.pick_and_pull(".KMP", resolve_workspace_root(self.settings))
        if result.local_path is None:
            self.status_text = result.status_message
            return
        self._remote_map.update(result.remote_map)
        self.open_multisample_direct(result.local_path)

    def _push_target_multisample_path(self) -> Optional[str]:
        if self._selected_node is not None and self._selected_node.multisample_ref is not None:
            return self._selected_node.multisample_ref[1]
        return self._selected_kmp_path if self._selected_zone is not None else None

    @property
    def can_push_selected_sample(self) -> bool:
        s, p = self._selected_sample, self._selected_sample_path
        return s is not None and not s.is_header_only and p is not None and p not in self._dirty_samples and p in self._remote_map

    @property
    def can_push_selected_multisample(self) -> bool:
        p = self._push_target_multisample_path()
        return p is not None and p not in self._dirty_multisamples and p in self._remote_map

    def push_selected_sample(self, source) -> None:
        s, p = self._selected_sample, self._selected_sample_path
        if s is None or p is None:
            self.status_text = "No sample loaded."
            return
        if p in self._dirty_samples:
            self.status_text = "Save the sample locally first (use Save Sample), then push."
            return
        remote = self._remote_map.get(p)
        if remote is None:
            self.status_text = "This sample wasn't pulled from the instrument - nowhere to push it back to."
            return
        # Eva's own Save can write a zero-frame .KSF for a sample that was loaded but never fully read; pushing
        # one over a good on-Kronos sample would silently destroy it.
        if s.is_header_only:
            self.status_text = "Refusing to push: this sample has no audio data (header-only)."
            return
        self.status_text = "Pushing to the instrument..."
        self.status_text = source.push(p, remote).status_message

    def push_selected_multisample(self, source) -> None:
        if (self._selected_node is None or self._selected_node.multisample_ref is None) and self._selected_zone is None:
            self.status_text = "No multisample selected."
            return
        path = self._push_target_multisample_path()
        if path is None:
            self.status_text = "Couldn't resolve the owning multisample."
            return
        if path in self._dirty_multisamples:
            self.status_text = "Save the multisample locally first (use Save Multisample), then push."
            return
        remote = self._remote_map.get(path)
        if remote is None:
            self.status_text = "This multisample wasn't pulled from the instrument - nowhere to push it back to."
            return
        self.status_text = "Pushing to the instrument..."
        self.status_text = source.push(path, remote).status_message

    def push_collection_to_kronos(self, source) -> None:
        if self._collection is None or self._collection_path is None:
            self.status_text = "Open a collection first."
            return
        if self.has_unsaved_changes:
            self.status_text = "Save changes locally first (use Save Changes), then push."
            return
        self.status_text = "Pushing to the instrument..."
        self.status_text = source.pick_folder_and_push_collection(self._collection_path, self._collection).status_message

    # ── saving ─────────────────────────────────────────────────────────────────────────────
    #
    # Every Save is three phases so the disk writes leave the GUI thread without the model's observable state ever
    # changing off it: PLAN (GUI: snapshot what is pending), WRITE (one `run_heavy` call: the files, in the same
    # order as before - every .KSF, then every .KMP, then the orphan sweep only if everything landed - each through
    # the format's own atomic `save()`), COMMIT (GUI: retire what reached disk from the dirty registries, clean
    # flags, status). A failure leaves the failed file and everything after it registered as unsaved.

    def _plan_sample_save(self) -> List[Tuple[str, object]]:
        return list(self._dirty_samples.items())

    def _plan_multisample_save(self) -> Optional[List[Tuple[str, object]]]:
        """Every multisample with pending edits plus the selected one; None = nothing selected and nothing pending."""
        m, path = self._resolve_context_multisample()
        # Nothing selected is only a hard stop when there is ALSO nothing pending: a selection-clearing rebuild
        # must not make already-registered pending edits unreachable.
        if m is None and len(self._dirty_multisamples) == 0:
            return None
        pending = {p: v for p, v in self._dirty_multisamples.items()}
        if m is not None and path is not None:
            pending[path] = m
        return list(pending.items())

    def _plan_orphan_sweep(self) -> Optional[Tuple[str, Set[str]]]:
        """(content folder, referenced names) for the sweep - read from the in-memory tree, so on the GUI thread."""
        if self._collection is None or self._collection_path is None:
            return None
        referenced = {e.lower() for e in self._collection.entries}
        for node in self.all_multisample_nodes():
            for z in node.multisample_ref[0].zones:
                if not z.is_skipped:
                    referenced.add(z.filename.lower())
        return ksc_mod.content_dir_for(self._collection_path), referenced

    def _run_save(self, label: str, work):
        """Runs the WRITE phase via run_heavy. (result, None), or (None, error) when the worker itself failed - then
        nothing is known to have landed and the caller commits nothing."""
        try:
            return self.run_heavy(label, work), None
        except Exception as ex:                          # noqa: BLE001 - reported, the model stays dirty
            log.error("Sample Editor: save failed: %s", ex, exc_info=True)
            return None, ex

    def _commit_sample_save(self, outcome: _WriteOutcome) -> bool:
        for path in outcome.saved:
            self._dirty_samples.remove(path)
        if outcome.failure is not None:
            path, ex = outcome.failure
            log.error("Sample Editor: sample save '%s' failed: %s", path, ex, exc_info=ex)
            self.status_text = f"Save failed for '{os.path.basename(path)}': {ex}"
            return False
        self._sample_dirty_field = False
        self.status_text = (f"Saved {', '.join(repr(os.path.basename(n)) for n in outcome.saved)}."
                            + (f" NOTE: {len(self._dirty_multisamples)} multisample(s) still have unsaved zone edits - use Save Multisample too."
                               if len(self._dirty_multisamples) > 0 else ""))
        return True

    def _commit_multisample_save(self, outcome: _WriteOutcome) -> bool:
        for path in outcome.saved:
            self._dirty_multisamples.remove(path)
        if outcome.failure is not None:
            path, ex = outcome.failure
            log.error("Sample Editor: multisample save '%s' failed: %s", path, ex, exc_info=ex)
            self.status_text = f"Save failed for '{os.path.basename(path)}': {ex}"
            return False
        self._zone_dirty_field = False
        self.status_text = (f"Saved {', '.join(repr(os.path.basename(n)) for n in outcome.saved)}."
                            + (f" NOTE: {len(self._dirty_samples)} sample edit(s) are still unsaved - use Save Sample too."
                               if len(self._dirty_samples) > 0 else ""))
        return True

    def save_selected_multisample(self) -> bool:
        """Writes EVERY multisample with pending edits, not just the selected one: zone edits mutate the LIVE
        objects and survive navigating away, so 'the selection' is not the set of what is unsaved. Returns whether
        everything reached disk."""
        items = self._plan_multisample_save()
        if items is None:
            self.status_text = "No multisample selected."
            return True
        outcome, err = self._run_save("Saving...", lambda: _write_files(items))
        if err is not None:
            self.status_text = f"Save failed: {err}"
            return False
        return self._commit_multisample_save(outcome)

    def save_selected_sample(self) -> bool:
        """Writes every sample with pending edits. Two bugs made 'save just the selected one' wrong: in stereo
        Combine mode the partner is edited by every mirrored operation but was never saved (the pair diverged on
        disk), and edits survive navigation, so several samples can be pending at once."""
        if len(self._dirty_samples) == 0:
            self.status_text = "No sample loaded." if self._selected_sample is None else "No unsaved sample changes."
            return True
        items = self._plan_sample_save()
        outcome, err = self._run_save("Saving...", lambda: _write_files(items))
        if err is not None:
            self.status_text = f"Save failed: {err}"
            return False
        return self._commit_sample_save(outcome)

    def save_all_changes(self) -> None:
        """The window's single 'Save Changes': keyed off what is pending across the WHOLE session, not the
        selected item's own flags (those reset on every navigation). ONE worker run for all of it."""
        had_samples, had_zones = len(self._dirty_samples) > 0, len(self._dirty_multisamples) > 0
        if not had_samples and not had_zones:
            self.status_text = "No unsaved changes."
            return
        sample_items = self._plan_sample_save() if had_samples else None
        ms_items = self._plan_multisample_save() if had_zones else None
        sweep = self._plan_orphan_sweep()

        def work():
            s_out = _write_files(sample_items) if sample_items is not None else None
            # The multisamples are written even after a sample failure (as before): they are independent files.
            m_out = _write_files(ms_items) if ms_items is not None else None
            all_saved = all(o is None or o.failure is None for o in (s_out, m_out))
            # ONLY after every write landed: the sweep decides what to delete from the IN-MEMORY zone names, so
            # after a failed save it would delete .KSF files the on-disk .KMP still points at.
            sweep_error = None
            if all_saved and sweep is not None:
                try:
                    _delete_orphans(*sweep)
                except Exception as ex:                  # noqa: BLE001 - the writes above DID land: still commit them
                    log.warning("Sample Editor: orphan cleanup failed: %s", ex, exc_info=True)
                    sweep_error = ex
            return s_out, m_out, all_saved, sweep_error

        result, err = self._run_save("Saving...", work)
        if err is not None:
            self.status_text = f"Save failed: {err}"
            return
        s_out, m_out, all_saved, sweep_error = result
        messages: List[str] = []
        if s_out is not None:
            self._commit_sample_save(s_out)
            messages.append(self.status_text)
        if m_out is not None:
            self._commit_multisample_save(m_out)
            messages.append(self.status_text)
        if not all_saved:
            messages.append("Orphan cleanup skipped - fix the save error first.")
        elif sweep_error is not None:
            messages.append(f"Orphan cleanup failed: {sweep_error}")
        self.status_text = "  ".join(messages)

    # ── export ─────────────────────────────────────────────────────────────────────────────

    def export_selected_sample_to_wav(self, wav_path: str) -> None:
        if self._selected_sample is None:
            self.status_text = "No sample loaded."
            return
        try:
            sample, kmp_path = self._selected_sample, self._selected_kmp_path
            if not self.run_heavy("Exporting...", lambda: exporter.export_sample_to_wav(sample, wav_path, kmp_path)):
                self.status_text = "Can't export: this sample has no audio data (header-only)."
                return
            self.status_text = f"Exported '{os.path.basename(wav_path)}'."
        except Exception as ex:
            log.error("Sample Editor: WAV export '%s' failed: %s", wav_path, ex, exc_info=True)
            self.status_text = f"Export failed: {ex}"

    def export_collection_to_folder(self, output_dir: str) -> None:
        if self._collection is None or self._collection_path is None:
            self.status_text = "Open a collection first."
            return
        try:
            collection, ksc_path = self._collection, self._collection_path
            exported, skipped = self.run_heavy("Exporting collection...",
                                               lambda: exporter.export_collection(collection, ksc_path, output_dir))
            self.status_text = (f"Exported {exported} WAV file(s) to '{output_dir}'"
                                + (f" ({skipped} skipped - header-only or unreadable)." if skipped > 0 else "."))
        except Exception as ex:
            log.error("Sample Editor: collection export to '%s' failed: %s", output_dir, ex, exc_info=True)
            self.status_text = f"Export failed: {ex}"

    def export_selected_multisample_to_folder(self, output_dir: str) -> None:
        m, kmp_path = self._resolve_context_multisample()
        if m is None or kmp_path is None:
            self.status_text = "No multisample selected."
            return
        try:
            exported, skipped = self.run_heavy("Exporting multisample...",
                                               lambda: exporter.export_multisample(m, kmp_path, output_dir))
            self.status_text = (f"Exported {exported} WAV file(s) to '{output_dir}'"
                                + (f" ({skipped} skipped - header-only or unreadable)." if skipped > 0 else "."))
        except Exception as ex:
            log.error("Sample Editor: multisample export to '%s' failed: %s", output_dir, ex, exc_info=True)
            self.status_text = f"Export failed: {ex}"

    # ── new collection / Save As ───────────────────────────────────────────────────────────

    def new_collection(self, ksc_path: str) -> None:
        try:
            collection = KscCollection()
            collection.path = ksc_path
            os.makedirs(ksc_mod.content_dir_for(ksc_path), exist_ok=True)
            collection.save(ksc_path)
            self._collection, self._collection_path = collection, ksc_path
            self._rebuild_tree_from_collection(ksc_path, collection)
            self._add_recent_file(ksc_path)
            self.status_text = f"Created '{os.path.basename(ksc_path)}'."
        except Exception as ex:
            log.error("Sample Editor: new collection '%s' failed: %s", ksc_path, ex, exc_info=True)
            self.status_text = f"Failed to create collection: {ex}"

    def save_collection_as(self, new_ksc_path: str) -> None:
        """Copies the ACTIVE collection's on-disk content, UNEDITED, to a new path and makes the copy the open
        document. Copy-FIRST, not flush-then-copy: writing pending edits into the original even briefly would
        break the one guarantee Save As makes — open Foo, edit, 'Save As Bar' leaves Foo exactly as it was."""
        if self._collection is None or self._collection_path is None:
            self.status_text = "Open a collection first."
            return
        old_ksc = self._collection_path
        if os.path.normcase(os.path.abspath(old_ksc)) == os.path.normcase(os.path.abspath(new_ksc_path)):
            self.status_text = "Choose a different file name - that's already this collection's own path."
            return
        old_dir, new_dir = ksc_mod.content_dir_for(old_ksc), ksc_mod.content_dir_for(new_ksc_path)
        # The SAME entry list, not a rescan (which could pick up orphaned files never in Entries). The bank
        # UUID is left unset so save generates a FRESH one: two banks sharing a UUID has unknown hardware
        # behaviour.
        new_collection = KscCollection()
        new_collection.path = new_ksc_path
        new_collection.entries = list(self._collection.entries)

        def write() -> None:
            # Disk only (worker thread via run_heavy): the content copy, then the new .KSC (+ _UserBank) last, so
            # a failure part-way never leaves a .KSC pointing at a half-copied folder. The model changes below.
            if os.path.isdir(old_dir):
                os.makedirs(new_dir, exist_ok=True)
                for root, _dirs, files in os.walk(old_dir):
                    for f in files:
                        src = os.path.join(root, f)
                        dest = os.path.join(new_dir, os.path.relpath(src, old_dir))
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        shutil.copyfile(src, dest)
            self._save_collection_with_user_bank(new_collection, new_ksc_path)

        try:
            self.run_heavy("Saving a copy...", write)

            self._rekey_pending_edits(old_dir, new_dir)

            old_root = next((r for r in self.roots if r.collection_ref is not None
                             and os.path.normcase(r.collection_ref[1]) == os.path.normcase(old_ksc)), None)
            from Core.sample_editor_model.tree import is_descendant
            if old_root is not None and is_descendant(old_root, self._selected_node):
                self.select_node(None)
            if old_root is not None:
                self.roots.remove(old_root)

            self._collection, self._collection_path = new_collection, new_ksc_path
            self._rebuild_tree_from_collection(new_ksc_path, new_collection)
            self._add_recent_file(new_ksc_path)
            self.status_text = (f"Saved a copy as '{os.path.basename(new_ksc_path)}' - now the active collection "
                                "(the original file on disk is untouched).")
        except Exception as ex:
            log.error("Sample Editor: Save As '%s' failed: %s", new_ksc_path, ex, exc_info=True)
            self.status_text = f"Save As failed: {ex}"
