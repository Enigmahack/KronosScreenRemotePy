r"""
Sample Editor model — state, tree, open/unload/revert, selection, stereo partner, pending-edit
registries and the rename cascade. Port of the first half of ViewModels/SampleEditorViewModel.cs.

UI-independent: no Qt. The window subscribes with `add_listener(cb)`; `cb(name)` fires for every observable
attribute that changes (name = the attribute) and with "tree" whenever the tree is rebuilt/relabelled.

IDENTITY, NOT EQUALITY: KmpZone is a dataclass, so `zone in zones` / `zones.index(zone)` compare by VALUE
and would conflate two identical-looking zones; C#'s List<KmpZone> compares by reference. Every zone lookup
in this package goes through `zone_index` / `zone_in` below.
"""
from __future__ import annotations

import logging
import os
import shutil
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

import Core.sample_link_resolver as link_resolver
from Core.sample_edit_undo import SampleEditUndo, SampleZoneUndo
from Core.sample_import_builder import find_stereo_sibling_on_disk
from Core.sample_playback import SamplePlayback
from Core.sample_support import SampleClipboard, build_normalization_report
from Core.sample_editor_model.tree import (
    PathDict, SampleTreeNode, enumerate_nodes, find_multisample_and_path_containing,
    find_multisample_containing, is_descendant)
import Data.ksc_collection as ksc_mod
from Data.kmp_multisample import KmpMultisample, KmpZone
from Data.ksc_collection import KscCollection
from Data.ksf_sample import KsfSample
import Utils.midi_note_name as note_name

log = logging.getLogger(__name__)

# Edit domains: which undo stack a logical edit belongs to, so Ctrl+Z walks a MIXED history of sample and
# zone edits in the order they actually happened (two stacks, one arbitration list).
DOMAIN_SAMPLE, DOMAIN_ZONE, DOMAIN_PARTNER = "Sample", "Zone", "PartnerSample"

RECENT_FILES_MAX = 8


def zone_index(zones: List[KmpZone], zone: Optional[KmpZone]) -> int:
    for i, z in enumerate(zones):
        if z is zone:
            return i
    return -1


def zone_in(zones: List[KmpZone], zone: Optional[KmpZone]) -> bool:
    return zone_index(zones, zone) >= 0


def multisample_node_label(m: KmpMultisample, path: str) -> str:
    # Shared with refresh_multisample_node_label so a rename produces EXACTLY the text a fresh build would.
    return f"{m.name}{m.suffix} ({os.path.basename(path)})"


def zone_node_label(z: KmpZone) -> str:
    top = note_name.to_name(z.top_key)
    return f"(skipped) up to {top}" if z.is_skipped else f"{z.filename}  up to {top}"


def is_under(file_path: str, directory: str) -> bool:
    """The trailing separator is load-bearing: collections 'Foo.KSC' and 'FooBar.KSC' have content dirs
    '<...>/Foo' and '<...>/FooBar'; a bare startswith would make unloading Foo discard FooBar's edits."""
    file_dir = os.path.dirname(file_path)
    nd, nf = os.path.normcase(os.path.normpath(directory)), os.path.normcase(os.path.normpath(file_dir))
    return nf == nd or nf.startswith(nd + os.sep)


def is_user_bank(path: str) -> bool:
    """A _UserBank.KSC is a live streamed shortcut to library content on the Kronos' SSD, not real sample
    data (Kronos-generated-output-only — the same guard KscCollection.to_bytes enforces on write)."""
    return os.path.basename(path).upper().endswith("_USERBANK.KSC")


def is_ignorable_placeholder_kmp(entry_name: str) -> bool:
    """NEWMS000/NEWMS001 are the Kronos' own default placeholder multisample names, always present in a
    brand-new library — missing/unreadable is their NORMAL state, not a data problem worth a warning."""
    return os.path.splitext(entry_name)[0].upper() in ("NEWMS000", "NEWMS001")


def compute_kmp_base_name(name: str, mno1: int) -> str:
    # Delegates to KmpMultisample.auto_file_name — the SAME writer a brand-new multisample uses — so a
    # renamed one always agrees with a freshly created one (including the underscore padding).
    return os.path.splitext(KmpMultisample.auto_file_name(name, mno1))[0]


def cascade_top_keys(zones: List[KmpZone], idx: int, delta: int) -> None:
    """Raising a zone's Top Key past the next zone's pushes every zone above it up by the same amount,
    preserving each pushed zone's width until it runs out of room at 127. Lowering never cascades."""
    for j in range(idx + 1, len(zones)):
        zones[j].top_key = min(127, zones[j].top_key + delta)


# Observable attributes -> initial value.
_OBSERVABLE_DEFAULTS = {
    "status_text": "Ready",
    "has_zone_selected": False, "zone_filename": "", "zone_original_key": 0, "zone_top_key": 0,
    "zone_is_skipped": False,
    "has_sample_loaded": False, "sample_name": "", "sample_rate": 0, "sample_frame_count": 0,
    "sample_is_header_only": False, "sample_is_linked_stub": False, "sample_link_target_file": "",
    "sample_loop_enabled": False, "sample_start": 0, "loop_start": 0, "loop_end": 0,
    "sample_reverse_enabled": False, "sample_12db_boost_enabled": False, "sample_loop_tune": 0,
    "sample_waveform": None,
    "can_undo": False, "can_redo": False, "selection_start_frame": 0, "selection_end_frame": 0,
    "is_playing": False, "is_paused": False, "playback_matches_selection": True,
    "use_zero_crossing": False, "loop_lock_enabled": False,
    "has_stereo_pair": False, "is_primary_left_channel": False, "partner_sample_waveform": None,
    "split_both_active": False, "split_lr": False, "is_move_tool_active": False,
    "partner_sample_start": 0, "partner_loop_start": 0, "partner_loop_end": 0, "partner_loop_enabled": False,
}


class ModelCore:
    def __init__(self, settings=None, save_settings: Optional[Callable[[object], None]] = None):
        object.__setattr__(self, "_listeners", [])
        for k, v in _OBSERVABLE_DEFAULTS.items():
            object.__setattr__(self, k, v)
        self.settings = settings
        self._save_settings = save_settings
        cap_mb = getattr(settings, "sample_undo_byte_cap_mb", 256) if settings is not None else 256
        self.roots: List[SampleTreeNode] = []

        self._collection: Optional[KscCollection] = None
        self._collection_path: Optional[str] = None

        # selected-zone detail state
        self._selected_node: Optional[SampleTreeNode] = None
        self._selected_zone: Optional[KmpZone] = None
        self._selected_kmp_path: Optional[str] = None
        self._selected_sample: Optional[KsfSample] = None
        self._selected_sample_path: Optional[str] = None
        self._cached_link_resolution: Optional[Tuple[KsfSample, Optional[link_resolver.LinkResolution]]] = None
        self.current_multisample_zones: Optional[List[KmpZone]] = None

        # undo
        self._sample_undo = SampleEditUndo(cap_mb * 1024 * 1024)
        self._partner_undo = SampleEditUndo(cap_mb * 1024 * 1024)
        self._zone_undo = SampleZoneUndo()
        self._zone_undo_scope: Optional[List[KmpZone]] = None
        self._undo_domains: List[str] = []
        self._redo_domains: List[str] = []

        # stereo partner
        self._partner_sample: Optional[KsfSample] = None
        self._partner_sample_path: Optional[str] = None
        self._partner_zone: Optional[KmpZone] = None
        self._partner_kmp_path: Optional[str] = None

        # playback
        self._playback = SamplePlayback()
        if settings is not None:
            self._playback.output_device_id = getattr(settings, "sample_editor_output_device_id", "") or ""
        self._cursor_frame = -1
        self._move_tool_padding: Dict[int, int] = {}      # id(KsfSample) -> frames of Move-tool padding still trimmable

        # pending-edit registries (the LIVE edited objects) + remote map
        self._zone_dirty_field = False
        self._sample_dirty_field = False
        self._dirty_samples: "PathDict[KsfSample]" = PathDict()
        self._dirty_multisamples: "PathDict[KmpMultisample]" = PathDict()
        self._remote_map: Dict[str, str] = {}
        self._last_rebuild_skipped: List[str] = []
        self.last_renamed_multisample_path: Optional[str] = None

    # ── observable plumbing ─────────────────────────────────────────────────────────────────

    def add_listener(self, cb: Callable[[str], None]) -> None:
        self._listeners.append(cb)

    def remove_listener(self, cb: Callable[[str], None]) -> None:
        if cb in self._listeners:
            self._listeners.remove(cb)

    def _emit(self, name: str) -> None:
        for cb in list(self._listeners):
            try:
                cb(name)
            except Exception:
                log.exception("sample editor listener failed for %s", name)

    def __setattr__(self, name, value):
        if name in _OBSERVABLE_DEFAULTS:
            old = self.__dict__.get(name)
            same = (old is value) if isinstance(value, np.ndarray) or isinstance(old, np.ndarray) else old == value
            object.__setattr__(self, name, value)
            if not same:
                self._emit(name)
        else:
            object.__setattr__(self, name, value)

    def _tree_refreshed(self) -> None:
        self._emit("tree")

    # ── small derived state ────────────────────────────────────────────────────────────────

    @property
    def selected_zone(self) -> Optional[KmpZone]:
        return self._selected_zone

    @property
    def selected_sample(self) -> Optional[KsfSample]:
        return self._selected_sample

    @property
    def partner_sample(self) -> Optional[KsfSample]:
        return self._partner_sample

    @property
    def active_collection_path(self) -> Optional[str]:
        return self._collection_path

    @property
    def has_active_collection(self) -> bool:
        return self._collection is not None and self._collection_path is not None

    @property
    def should_mirror_to_partner(self) -> bool:
        """Combine mode (and Split with both channels active) mirrors every edit onto the stereo partner."""
        return (self.has_stereo_pair and (not self.split_lr or self.split_both_active)
                and self._partner_sample is not None and not self._partner_sample.is_header_only)

    @property
    def partner_zone_ref(self) -> Optional[Tuple[KmpZone, str]]:
        if self._partner_zone is not None and self._partner_kmp_path is not None:
            return self._partner_zone, self._partner_kmp_path
        return None

    # Fixed L-on-top / R-on-bottom regardless of which side is "primary".
    @property
    def left_sample_waveform(self):
        return None if not self.has_stereo_pair else (self.sample_waveform if self.is_primary_left_channel else self.partner_sample_waveform)

    @property
    def right_sample_waveform(self):
        return None if not self.has_stereo_pair else (self.partner_sample_waveform if self.is_primary_left_channel else self.sample_waveform)

    @property
    def left_sample_start_frame(self): return self.sample_start if self.is_primary_left_channel else self.partner_sample_start
    @property
    def right_sample_start_frame(self): return self.partner_sample_start if self.is_primary_left_channel else self.sample_start
    @property
    def left_loop_start_frame(self): return self.loop_start if self.is_primary_left_channel else self.partner_loop_start
    @property
    def right_loop_start_frame(self): return self.partner_loop_start if self.is_primary_left_channel else self.loop_start
    @property
    def left_loop_end_frame(self): return self.loop_end if self.is_primary_left_channel else self.partner_loop_end
    @property
    def right_loop_end_frame(self): return self.partner_loop_end if self.is_primary_left_channel else self.loop_end
    @property
    def left_loop_enabled(self): return self.sample_loop_enabled if self.is_primary_left_channel else self.partner_loop_enabled
    @property
    def right_loop_enabled(self): return self.partner_loop_enabled if self.is_primary_left_channel else self.sample_loop_enabled

    def _refresh_partner_markers(self) -> None:
        p = self._partner_sample
        if p is not None:
            self.partner_sample_start, self.partner_loop_start = int(p.sample_start), int(p.loop_start)
            self.partner_loop_end, self.partner_loop_enabled = int(p.loop_end), p.is_loop_enabled
        else:
            self.partner_sample_start = self.partner_loop_start = self.partner_loop_end = 0
            self.partner_loop_enabled = False

    # ── dirty tracking ─────────────────────────────────────────────────────────────────────
    # `zone_dirty` / `sample_dirty` are properties so every edit that sets them ALSO enrols what it just
    # edited in the pending-save registries. SelectNode resets the plain fields on navigation (a stale
    # "unsaved" badge on what you're looking at is confusing); the registries SURVIVE it, which is what
    # makes "edited zone A, navigated to zone B" safe.

    @property
    def _zone_dirty(self) -> bool:
        return self._zone_dirty_field

    @_zone_dirty.setter
    def _zone_dirty(self, value: bool) -> None:
        self._zone_dirty_field = value
        if value:
            self._register_dirty_multisample()

    @property
    def _sample_dirty(self) -> bool:
        return self._sample_dirty_field

    @_sample_dirty.setter
    def _sample_dirty(self, value: bool) -> None:
        self._sample_dirty_field = value
        if value:
            self._register_dirty_sample()

    @property
    def has_unsaved_changes(self) -> bool:
        """EXACT (not best-effort): keyed on what is actually pending, so saving multisample B can never
        silently cover an unsaved multisample A."""
        return len(self._dirty_samples) > 0 or len(self._dirty_multisamples) > 0

    def try_get_pending_sample_info(self, ksf_path: str) -> Optional[Tuple[str, str]]:
        s = self._dirty_samples.get(ksf_path)
        return (s.name, s.suffix) if s is not None else None

    def _register_dirty_sample(self) -> None:
        if self._selected_sample is not None and self._selected_sample_path is not None:
            self._dirty_samples[self._selected_sample_path] = self._selected_sample
        if self.should_mirror_to_partner and self._partner_sample is not None and self._partner_sample_path is not None:
            self._dirty_samples[self._partner_sample_path] = self._partner_sample

    def _register_dirty_partner_sample(self) -> None:
        """apply_channel_move's partner-only branch mutates the partner ALONE with mirroring off, which
        _register_dirty_sample's own mirror gate would enrol nothing for."""
        if self._partner_sample is not None and self._partner_sample_path is not None:
            self._dirty_samples[self._partner_sample_path] = self._partner_sample

    def _register_dirty_multisample(self, m: Optional[KmpMultisample] = None, path: Optional[str] = None) -> None:
        if m is not None and path is not None:           # explicit: the stereo sibling the mirroring writes to
            self._dirty_multisamples[path] = m
            return
        m, path = self._resolve_context_multisample()
        if m is not None and path is not None:
            self._dirty_multisamples[path] = m

    def _resolve_context_multisample(self) -> Tuple[Optional[KmpMultisample], Optional[str]]:
        """Whichever multisample the current selection is about: the selected multisample node, or the one
        owning the selected zone."""
        if self._selected_node is not None and self._selected_node.multisample_ref is not None:
            return self._selected_node.multisample_ref
        if self._selected_zone is None:
            return None, None
        m, p = find_multisample_and_path_containing(self.roots, self._selected_zone)
        return (m, p) if m is not None else (find_multisample_containing(self.roots, self._selected_zone), self._selected_kmp_path)

    @property
    def current_multisample_name(self) -> Optional[str]:
        m, _ = self._resolve_context_multisample()
        return None if m is None else f"{m.name}{m.suffix}"

    @property
    def current_multisample_bare_name(self) -> Optional[str]:
        """Bare (no suffix): what the Rename dialog pre-fills — pre-filling the suffixed form let a rename bake
        '-L' into Name, which then showed doubled ('Foo-L-L')."""
        m, _ = self._resolve_context_multisample()
        return None if m is None else m.name

    @property
    def current_sample_bare_name(self) -> Optional[str]:
        return None if self._selected_sample is None else self._selected_sample.name

    @property
    def selected_multisample_label(self) -> Optional[str]:
        return self.current_multisample_name

    # ── Opening ────────────────────────────────────────────────────────────────────────────

    def open_collection(self, path: str) -> None:
        if is_user_bank(path):
            self.status_text = (f"'{os.path.basename(path)}' is a _UserBank.KSC - a live shortcut to the instrument's SSD "
                                "library content, not real sample data. Nothing to edit here; open the actual .KSC instead.")
            return
        try:
            with open(path, "rb") as f:
                collection = KscCollection.open(f.read())
            collection.path = path
            self._collection = collection
            self._collection_path = path
            self._rebuild_tree_from_collection(path, collection)
            self._add_recent_file(path)
            n = len(collection.entries)
            self.status_text = (f"Loaded '{os.path.basename(path)}' ({n} entr{'y' if n == 1 else 'ies'})."
                                + (f" WARNING: {len(self._last_rebuild_skipped)} of them couldn't be loaded: "
                                   f"{'; '.join(self._last_rebuild_skipped)}" if self._last_rebuild_skipped else ""))
        except Exception as ex:
            log.error("Sample Editor: collection load '%s' failed: %s", path, ex, exc_info=True)
            self.status_text = f"Failed to load '{os.path.basename(path)}': {ex}"

    def open_multisample_direct(self, path: str) -> None:
        try:
            with open(path, "rb") as f:
                m = KmpMultisample.open(f.read())
            if m is None:
                self.status_text = f"'{os.path.basename(path)}' isn't a recognizable .KMP file."
                return
            m.path = path
            self._collection = None
            self._collection_path = None
            self.roots.clear()
            self.roots.append(self._build_multisample_node(m, path))
            self._tree_refreshed()
            self._add_recent_file(path)
            self.status_text = f"Loaded '{os.path.basename(path)}' directly ({len(m.zones)} zone(s))."
        except Exception as ex:
            log.error("Sample Editor: multisample load '%s' failed: %s", path, ex, exc_info=True)
            self.status_text = f"Failed to load '{os.path.basename(path)}': {ex}"

    def _rebuild_tree_from_collection(self, ksc_path: str, collection: KscCollection) -> None:
        """Rebuilds/replaces just the ONE root for `ksc_path`, leaving every other open collection's root
        untouched; an existing root is replaced in place at the same index with its expansion carried over."""
        existing = next((i for i, r in enumerate(self.roots)
                         if r.collection_ref is not None
                         and os.path.normcase(r.collection_ref[1]) == os.path.normcase(ksc_path)), -1)
        old_root = self.roots[existing] if existing >= 0 else None

        # Every node is rebuilt as a brand-new object (each .KMP is re-opened), so a selection pointing INTO
        # the collection being rebuilt is stale — drop it, or a later Save would act on the pre-rebuild copy.
        # Only when the selection was in THIS collection: editing A must not blow away the selection in B.
        if old_root is not None and is_descendant(old_root, self._selected_node):
            self.select_node(None)

        expanded = set() if old_root is None else {
            os.path.normcase(c.multisample_ref[1]) for c in old_root.children
            if c.is_expanded and c.multisample_ref is not None}
        root_was_expanded = old_root.is_expanded if old_root is not None else True

        root = SampleTreeNode.for_collection(os.path.basename(ksc_path), collection, ksc_path)
        root.is_expanded = root_was_expanded
        self._last_rebuild_skipped = []
        kmp_dir = ksc_mod.content_dir_for(ksc_path)
        for entry in collection.entries:
            if not entry.upper().endswith(".KMP"):
                continue
            kmp_path = os.path.join(kmp_dir, entry)
            if not os.path.exists(kmp_path):
                if not is_ignorable_placeholder_kmp(entry):
                    log.warning("Sample Editor: skipping missing multisample '%s'", kmp_path)
                    self._last_rebuild_skipped.append(f"{entry} (not found on disk)")
                continue
            try:
                # A multisample with pending zone edits keeps its LIVE edited object: a rebuild is triggered
                # by unrelated operations, and re-reading would silently drop another multisample's unsaved
                # zone edits while leaving them registered as pending. (Reverting clears the registry first.)
                m = self._dirty_multisamples.get(kmp_path)
                if m is None:
                    with open(kmp_path, "rb") as f:
                        m = KmpMultisample.open(f.read())
                    if m is not None:
                        m.path = kmp_path
                if m is None:
                    if not is_ignorable_placeholder_kmp(entry):
                        self._last_rebuild_skipped.append(f"{entry} (not a recognizable .KMP)")
                    continue
                node = self._build_multisample_node(m, kmp_path)
                node.is_expanded = os.path.normcase(kmp_path) in expanded
                root.children.append(node)
            except Exception as ex:
                log.warning("Sample Editor: skipping unreadable multisample '%s': %s", kmp_path, ex)
                if not is_ignorable_placeholder_kmp(entry):
                    self._last_rebuild_skipped.append(f"{entry} ({ex})")

        if existing >= 0:
            self.roots[existing] = root
        else:
            self.roots.append(root)
        self._tree_refreshed()

    def _build_multisample_node(self, m: KmpMultisample, path: str) -> SampleTreeNode:
        node = SampleTreeNode.for_multisample(multisample_node_label(m, path), m, path)
        self._sync_multisample_node_children(node)
        return node

    @staticmethod
    def _sync_multisample_node_children(node: SampleTreeNode) -> None:
        """Rebuild one multisample node's children in place from its CURRENT zone list, REUSING each
        surviving zone's existing node (matched by KmpZone identity) — a node survives as long as its zone
        does, so selection/staleness guards that compare node identity keep working across undo/redo."""
        m, path = node.multisample_ref
        existing = {id(c.zone_ref[0]): c for c in node.children if c.zone_ref is not None}
        rebuilt: List[SampleTreeNode] = []
        for z in m.zones:
            n = existing.get(id(z))
            if n is not None:
                n.label = zone_node_label(z)
                rebuilt.append(n)
            else:
                rebuilt.append(SampleTreeNode.for_zone(zone_node_label(z), z, path))
        node.children[:] = rebuilt

    # ── Unload / Revert ────────────────────────────────────────────────────────────────────

    def find_owning_collection_path(self, node: Optional[SampleTreeNode]) -> Optional[str]:
        if node is None:
            return None
        root = next((r for r in self.roots if is_descendant(r, node)), None)
        return root.collection_ref[1] if root is not None and root.collection_ref else None

    def unload_collection(self, ksc_path: str) -> None:
        """Removes an open collection from the tree (session-only; nothing on disk is touched)."""
        root = next((r for r in self.roots if r.collection_ref is not None
                     and os.path.normcase(r.collection_ref[1]) == os.path.normcase(ksc_path)), None)
        if root is None:
            self.status_text = "That collection isn't open."
            return
        if is_descendant(root, self._selected_node):
            self.select_node(None)
        # Pending edits under a collection that's no longer open have nowhere to be saved from; leaving
        # them registered would keep the close guard warning about it (and let a later Save write them out).
        self._discard_pending_edits_under(ksc_path)
        self.roots.remove(root)
        if self._collection_path is not None and os.path.normcase(self._collection_path) == os.path.normcase(ksc_path):
            self._collection = None
            self._collection_path = None
        self._tree_refreshed()
        self.status_text = f"Unloaded '{os.path.basename(ksc_path)}' (files on disk are untouched - re-open it any time)."

    def revert_active_collection_changes(self) -> None:
        """Discards every unsaved edit under ONE open collection by re-reading its .KSC and every .KMP."""
        if self._collection_path is None:
            self.status_text = "No collection is open to revert."
            return
        path = self._collection_path
        try:
            with open(path, "rb") as f:
                collection = KscCollection.open(f.read())
            collection.path = path
            self._collection = collection
            self._collection_path = path
            # Discarding is what makes this a revert now that pending edits outlive navigation —
            # re-reading the tree alone would leave them registered and the next Save would write them back.
            self._discard_pending_edits_under(path)
            self._rebuild_tree_from_collection(path, collection)
            self.status_text = (f"Reverted '{os.path.basename(path)}' - unsaved changes discarded."
                                + (f" WARNING: {len(self._last_rebuild_skipped)} of them couldn't be loaded: "
                                   f"{'; '.join(self._last_rebuild_skipped)}" if self._last_rebuild_skipped else ""))
        except Exception as ex:
            log.error("Sample Editor: revert '%s' failed: %s", path, ex, exc_info=True)
            self.status_text = f"Revert failed: {ex}"

    def revert_all_changes(self) -> None:
        """Closes every open collection and resets ALL session state — the literal 'begin again'."""
        self.select_node(None)
        self.roots.clear()
        self._collection = None
        self._collection_path = None
        self._remote_map.clear()
        self._sample_undo = SampleEditUndo(self._sample_undo.byte_cap)
        self._partner_undo = SampleEditUndo(self._partner_undo.byte_cap)
        self._zone_undo = SampleZoneUndo()
        self._zone_undo_scope = None
        self._undo_domains.clear()
        self._redo_domains.clear()
        self.can_undo = False
        self.can_redo = False
        self._discard_pending_edits_under(None)
        self._zone_dirty_field = False
        self._sample_dirty_field = False
        self._tree_refreshed()
        self.status_text = "All collections closed - starting fresh."

    def _discard_pending_edits_under(self, ksc_path: Optional[str]) -> None:
        if ksc_path is None:
            self._dirty_samples.clear()
            self._dirty_multisamples.clear()
            return
        content_dir = ksc_mod.content_dir_for(ksc_path)
        for k in [k for k in self._dirty_samples.keys() if is_under(k, content_dir)]:
            self._dirty_samples.remove(k)
        for k in [k for k in self._dirty_multisamples.keys() if is_under(k, content_dir)]:
            self._dirty_multisamples.remove(k)

    def _save_multisample_now(self, m: KmpMultisample, path: str) -> None:
        """Writing a multisample straight to disk must also retire its pending-save registration, or the
        registry keeps a stale claim and the next rebuild resurrects the pre-save state."""
        m.save(path)
        self._dirty_multisamples.remove(path)

    def _save_collection_with_user_bank(self, collection: KscCollection, path: str) -> None:
        """Every local write of a collection's .KSC keeps its _UserBank.KSC sibling in sync."""
        collection.save(path)
        collection.save_user_bank()

    # ── Selection ──────────────────────────────────────────────────────────────────────────

    def dispose(self) -> None:
        self._playback.stop()
        self._playback.dispose()

    def all_multisample_nodes(self) -> List[SampleTreeNode]:
        return [n for n in enumerate_nodes(self.roots) if n.multisample_ref is not None]

    def select_node(self, node: Optional[SampleTreeNode]) -> None:
        self._playback.stop()
        self.is_playing = False

        self._selected_node = node
        self._selected_zone = node.zone_ref[0] if node is not None and node.zone_ref else None
        self._selected_kmp_path = node.zone_ref[1] if node is not None and node.zone_ref else None
        self._selected_sample = None
        self._selected_sample_path = None
        self._zone_dirty_field = False        # plain fields, NOT the setters: navigation must not enrol anything
        self._sample_dirty_field = False

        # "The active collection" for collection-level operations tracks wherever the user is actually
        # looking, not just whichever was opened most recently.
        if node is not None:
            owning = next((r for r in self.roots if is_descendant(r, node)), None)
            if owning is not None and owning.collection_ref is not None:
                self._collection, self._collection_path = owning.collection_ref

        if node is not None and node.multisample_ref is not None:
            self.current_multisample_zones = node.multisample_ref[0].zones
        elif self._selected_zone is not None:
            ms = find_multisample_containing(self.roots, self._selected_zone)
            self.current_multisample_zones = ms.zones if ms is not None else None
        else:
            self.current_multisample_zones = None

        # Undo history is scoped to the loaded sample: switching zones starts a fresh history rather than
        # letting Undo reach into an unrelated sample's edits.
        self._sample_undo = SampleEditUndo(self._sample_undo.byte_cap)
        self._partner_undo = SampleEditUndo(self._partner_undo.byte_cap)
        self._cursor_frame = -1
        # Drop only the Sample-domain entries (that stack always resets) — NOT a blanket clear, which would
        # also wipe Zone-domain entries for a multisample whose zone history survives this navigation.
        self._undo_domains[:] = [d for d in self._undo_domains if d != DOMAIN_SAMPLE]
        self._redo_domains[:] = [d for d in self._redo_domains if d != DOMAIN_SAMPLE]
        # Zone-list undo is scoped to the MULTISAMPLE, not the zone selection: clicking between zones of the
        # same multisample (e.g. to inspect a boundary drag) must NOT wipe that drag's history.
        if self.current_multisample_zones is not self._zone_undo_scope:
            self._zone_undo = SampleZoneUndo()
            self._zone_undo_scope = self.current_multisample_zones
            self._undo_domains[:] = [d for d in self._undo_domains if d != DOMAIN_ZONE]
            self._redo_domains[:] = [d for d in self._redo_domains if d != DOMAIN_ZONE]
        self.can_undo = len(self._undo_domains) > 0
        self.can_redo = len(self._redo_domains) > 0
        self.selection_start_frame = 0
        self.selection_end_frame = 0

        self._partner_sample = None
        self._partner_sample_path = None
        self._partner_zone = None
        self._partner_kmp_path = None
        self.has_stereo_pair = False
        self.partner_sample_waveform = None
        self._refresh_partner_markers()

        self.has_zone_selected = self._selected_zone is not None
        if self._selected_zone is not None:
            z = self._selected_zone
            self.zone_filename, self.zone_original_key = z.filename, z.original_key
            self.zone_top_key, self.zone_is_skipped = z.top_key, z.is_skipped

        self.has_sample_loaded = False
        self.sample_waveform = None
        zone = self._selected_zone
        if zone is not None and not zone.is_skipped and self._selected_kmp_path is not None:
            ksf_path = zone.ksf_path(self._selected_kmp_path)
            try:
                pending = self._dirty_samples.get(ksf_path)
                if pending is not None:
                    # An unsaved edit wins over what's on disk. Set the plain field: it's already enrolled.
                    self._selected_sample = pending
                    self._selected_sample_path = ksf_path
                    self._sample_dirty_field = True
                    self._load_sample_detail_state(pending, reload_waveform=True)
                    self._resolve_stereo_partner(zone, self._selected_kmp_path)
                elif os.path.exists(ksf_path):
                    with open(ksf_path, "rb") as f:
                        s = KsfSample.open(f.read())
                    if s is not None:
                        s.path = ksf_path
                        self._selected_sample = s
                        self._selected_sample_path = ksf_path
                        self._load_sample_detail_state(s, reload_waveform=True)
                        self._resolve_stereo_partner(zone, self._selected_kmp_path)
                    else:
                        self.status_text = f"'{os.path.basename(ksf_path)}' isn't a recognizable .KSF file."
                else:
                    self.status_text = f"Referenced sample '{os.path.basename(ksf_path)}' not found on disk."
            except Exception as ex:
                log.error("Sample Editor: sample load '%s' failed: %s", ksf_path, ex, exc_info=True)
                self.status_text = f"Failed to load '{os.path.basename(ksf_path)}': {ex}"

    def _resolve_stereo_partner(self, zone: KmpZone, kmp_path: str) -> None:
        """Doc §2.2: a stereo instrument is two complete multisamples (same Name, opposite -L/-R Suffix,
        adjacent MNO1), zones matched by key range. Best-effort: any failure leaves has_stereo_pair False —
        the primary sample is already loaded fine."""
        if self._collection is None:
            return
        owning = (self._selected_node.multisample_ref[0] if self._selected_node is not None
                  and self._selected_node.multisample_ref is not None
                  else find_multisample_containing(self.roots, zone))
        if owning is None or owning.suffix not in ("-L", "-R"):
            return
        # Prefer the LIVE in-tree sibling over a disk read: otherwise an unsaved key-range edit on either
        # half makes the exact match below fail against the stale on-disk copy and the pair silently
        # drops to a mono view.
        sibling, sibling_path = self._resolve_stereo_sibling(owning, kmp_path)
        if sibling is None or sibling_path is None:
            return

        # Exact (OriginalKey, TopKey) match is the right correspondence when the halves' keymaps agree, but
        # real hand-edited content routinely splits the channels at slightly different points and is still a
        # legitimate pair — so fall back to the SAME INDEX (the rule _resolve_sibling_zones_for uses for
        # mirroring, so "which zone is this zone's partner" is answered one way for editing and display).
        match = next((z for z in sibling.zones if not z.is_skipped
                      and z.original_key == zone.original_key and z.top_key == zone.top_key), None)
        if match is None:
            idx = zone_index(owning.zones, zone)
            if 0 <= idx < len(sibling.zones) and not sibling.zones[idx].is_skipped:
                match = sibling.zones[idx]
        if match is None:
            return

        partner_ksf_path = match.ksf_path(sibling_path)
        pending = self._dirty_samples.get(partner_ksf_path)
        if pending is not None:                       # pending-edit-wins, same rule as the primary
            ps = pending
        else:
            if not os.path.exists(partner_ksf_path):
                return
            try:
                with open(partner_ksf_path, "rb") as f:
                    ps = KsfSample.open(f.read())
                if ps is None:
                    return
                ps.path = partner_ksf_path
            except Exception as ex:
                log.warning("Sample Editor: stereo partner load '%s' failed: %s", partner_ksf_path, ex)
                return
        self._partner_sample, self._partner_sample_path = ps, partner_ksf_path
        self._partner_zone, self._partner_kmp_path = match, sibling_path
        self.has_stereo_pair = True
        self.is_primary_left_channel = owning.suffix == "-L"
        playable = link_resolver.resolve_playable(ps, sibling_path)
        self.partner_sample_waveform = None if playable.is_header_only else playable.samples()
        self._refresh_partner_markers()

    def _find_live_stereo_sibling(self, m: KmpMultisample, kmp_path: str) -> Tuple[Optional[KmpMultisample], Optional[str]]:
        """The sibling's LIVE in-tree object, NOT a fresh disk read: mirroring onto a copy the tree doesn't
        hold makes the change invisible in the UI and then discards it when the tree's own instance is saved.
        Same matching rule as the disk variant (same Name, opposite Suffix, adjacent Mno1, same folder)."""
        if m.suffix not in ("-L", "-R"):
            return None, None
        want = "-R" if m.suffix == "-L" else "-L"
        kmp_dir = os.path.normcase(os.path.dirname(kmp_path))
        for node in enumerate_nodes(self.roots):
            if node.multisample_ref is None:
                continue
            c, p = node.multisample_ref
            if os.path.normcase(p) == os.path.normcase(kmp_path):
                continue
            if os.path.normcase(os.path.dirname(p)) != kmp_dir:
                continue
            adjacent = c.mno1 == m.mno1 + 1 or (m.mno1 > 0 and c.mno1 == m.mno1 - 1)
            if c.name == m.name and c.suffix == want and adjacent:
                return c, p
        return None, None

    def _resolve_stereo_sibling(self, m: KmpMultisample, kmp_path: str) -> Tuple[Optional[KmpMultisample], Optional[str]]:
        sibling, path = self._find_live_stereo_sibling(m, kmp_path)
        if sibling is not None and path is not None:
            return sibling, path
        return find_stereo_sibling_on_disk(self._collection, m, kmp_path) if self._collection is not None else (None, None)

    def _resolve_sibling_zones_for(self, primary_zones: List[KmpZone]
                                   ) -> Tuple[Optional[List[KmpZone]], Optional[KmpMultisample], Optional[str]]:
        """The stereo sibling's own zone list, for the key-range edits that must mirror onto it. Nothing
        unless the pair is well-formed (same zone count): a pair already out of parity is left alone."""
        m, path = self._resolve_context_multisample()
        if m is None or path is None or m.zones is not primary_zones:
            return None, None, None
        sibling, sib_path = self._find_live_stereo_sibling(m, path)
        if sibling is None or sib_path is None or len(sibling.zones) != len(primary_zones):
            return None, None, None
        return sibling.zones, sibling, sib_path

    def _load_sample_detail_state(self, s: KsfSample, reload_waveform: bool) -> None:
        """reload_waveform: whether to re-decode the waveform from s.samples(). It decodes a BRAND-NEW array
        every call (millions of iterations for a long sample), and a fresh array is never identical to the old
        one — which would also defeat the waveform control's own geometry cache (keyed on array identity).
        Only genuinely PCM-mutating callers pass True; field-only setters leave the array untouched."""
        self.has_sample_loaded = True
        self.sample_name = s.name + s.suffix
        self.sample_rate = int(s.sample_rate)
        self.sample_is_header_only = s.is_header_only
        self.sample_loop_enabled = s.is_loop_enabled
        self.sample_start, self.loop_start, self.loop_end = int(s.sample_start), int(s.loop_start), int(s.loop_end)
        self.sample_reverse_enabled = s.is_reversed
        self.sample_12db_boost_enabled = s.is_12db_boost_enabled
        self.sample_loop_tune = s.loop_tune

        # Doc §3.2: resolve a header-only zone's SMF1 link to real PCM. `s` itself (and every field above)
        # stays the STUB's own; this only changes what is DISPLAYED. The directory scan is cached by stub
        # identity because the field-only setters call this on every keystroke/drag frame.
        link = None
        if s.is_header_only:
            cached = self._cached_link_resolution
            if cached is not None and cached[0] is s:
                link = cached[1]
            else:
                link = link_resolver.resolve(s, self._selected_kmp_path) if self._selected_kmp_path is not None else None
                self._cached_link_resolution = (s, link)
        self.sample_is_linked_stub = link is not None
        self.sample_link_target_file = s.stub_target_filename or ""
        playable = link_resolver.build_playable_view(s, link.sample) if link is not None else s

        self.sample_frame_count = playable.frame_count
        if reload_waveform:
            self.sample_waveform = None if playable.is_header_only else playable.samples()
        # A selection past the (possibly now-shorter, post-edit) frame count is invalid — clamp it.
        self.selection_start_frame = max(0, min(self.selection_start_frame, playable.frame_count))
        self.selection_end_frame = max(self.selection_start_frame, min(self.selection_end_frame, playable.frame_count))

    # ── Rename ─────────────────────────────────────────────────────────────────────────────

    def _move_multisample_files_if_needed(self, m: KmpMultisample, old_path: str) -> str:
        """Moves `m`'s .KMP + zone-content folder to match its CURRENT Name/Mno1-derived filename — immediate
        (not deferred to Save) because a rename-time move fails loudly with nothing half-written, whereas
        deferring would make every Save reconcile 'the path this is keyed under' against 'the path its name
        implies'. FILE CONTENTS still defer: this moves what is already on disk (never m.save(new), which
        would flush the pending Name edit early). In-keymap .KSF files are NEVER renamed (MS<Mno1><idx>-keyed);
        only the FOLDER moves so zone.ksf_path keeps resolving. Returns the new path, or the SAME path if no
        move was needed or a collision blocked it (status set, nothing touched)."""
        d = os.path.dirname(old_path)
        new_base = compute_kmp_base_name(m.name, m.mno1)
        if not new_base:
            return old_path
        new_path = os.path.join(d, new_base + ".KMP")
        if os.path.normcase(new_path) == os.path.normcase(old_path):
            # The computed filename didn't change (the new Name sanitizes to the same basename) but Name may
            # have — still register dirty, or the rebuild re-reads the KMP and silently reverts the edit.
            self._register_dirty_multisample(m, old_path)
            return old_path

        old_folder = os.path.join(d, os.path.splitext(os.path.basename(old_path))[0])
        new_folder = os.path.join(d, new_base)
        if os.path.exists(new_path) or os.path.isdir(new_folder):
            self.status_text = f"Can't rename to '{new_base}' - a multisample already uses that filename in this collection."
            return old_path

        if os.path.exists(old_path):
            shutil.move(old_path, new_path)
        else:
            m.save(new_path)         # defensive: every selectable multisample has a saved .KMP
        if os.path.isdir(old_folder):
            shutil.move(old_folder, new_folder)

        self._rekey_pending_edits(old_folder, new_folder)
        self._dirty_multisamples.remove(old_path)
        self._register_dirty_multisample(m, new_path)
        m.path = new_path

        if self._collection is not None and self._collection_path is not None:
            old_name = os.path.basename(old_path).lower()
            for i, e in enumerate(self._collection.entries):
                if e.lower() == old_name:
                    self._collection.entries[i] = os.path.basename(new_path)
                    break
            # Immediate, not deferred: leaving the .KSC stale would make it name a .KMP that no longer
            # exists on disk (the move above already happened) until the next Save Changes.
            self._save_collection_with_user_bank(self._collection, self._collection_path)
        return new_path

    def _rekey_pending_edits(self, old_folder: str, new_folder: str) -> None:
        """Re-key pending edits (and remote-map entries) that lived under a folder that just moved."""
        old_n = os.path.normcase(os.path.normpath(old_folder))
        for registry in (self._dirty_samples, self._dirty_multisamples):
            for key, value in registry.items():
                kn = os.path.normcase(os.path.normpath(key))
                if kn.startswith(old_n + os.sep):
                    registry.remove(key)
                    registry[os.path.join(new_folder, key[len(old_folder) + 1:])] = value
        for key in list(self._remote_map.keys()):
            kn = os.path.normcase(os.path.normpath(key))
            if kn.startswith(old_n + os.sep):
                self._remote_map[os.path.join(new_folder, key[len(old_folder) + 1:])] = self._remote_map.pop(key)

    def rename_selected_multisample(self, new_name: str) -> None:
        """Renames the in-context multisample's Name (the -L/-R Suffix is left alone — editing it would
        silently break stereo pairing, which matches by exact Suffix) AND its .KMP file/folder. Mirrored onto a
        resolved stereo sibling, since sibling matching requires an EXACT Name match. BOTH halves' collisions
        are checked and BOTH moved before the tree rebuilds, so a blocked sibling move can never leave a pair
        straddling old/new locations."""
        self.last_renamed_multisample_path = None
        new_name = new_name.strip()
        if not new_name:
            return
        m, path = self._resolve_context_multisample()
        if m is None or path is None:
            self.status_text = "Select a multisample (or one of its zones) first."
            return
        if m.name == new_name:
            return

        sibling, sib_path = self._find_live_stereo_sibling(m, path)

        # Collision-check BOTH halves against their OWN target names before moving either.
        new_base = compute_kmp_base_name(new_name, m.mno1)
        d = os.path.dirname(path)
        new_target = os.path.join(d, new_base + ".KMP")
        if os.path.normcase(new_target) != os.path.normcase(path) and (
                os.path.exists(new_target) or os.path.isdir(os.path.join(d, new_base))):
            self.status_text = f"Can't rename to '{new_name}' - '{new_base}.KMP' already exists in this collection."
            return
        if sibling is not None and sib_path is not None:
            sb = compute_kmp_base_name(new_name, sibling.mno1)
            sd = os.path.dirname(sib_path)
            st = os.path.join(sd, sb + ".KMP")
            if os.path.normcase(st) != os.path.normcase(sib_path) and (
                    os.path.exists(st) or os.path.isdir(os.path.join(sd, sb))):
                self.status_text = f"Can't rename to '{new_name}' - '{sb}.KMP' (stereo partner) already exists in this collection."
                return

        m.name = new_name
        new_path = self._move_multisample_files_if_needed(m, path)
        new_sib_path = None
        if sibling is not None and sib_path is not None:
            sibling.name = new_name
            new_sib_path = self._move_multisample_files_if_needed(sibling, sib_path)

        if self._collection_path is not None and self._collection is not None:
            self._rebuild_tree_from_collection(self._collection_path, self._collection)
            self.last_renamed_multisample_path = new_path
        else:
            self._refresh_multisample_node_label(m, new_path)
            if sibling is not None and new_sib_path is not None:
                self._refresh_multisample_node_label(sibling, new_sib_path)

        self.status_text = (f"Renamed multisample to '{new_name}{m.suffix}'"
                            + (" (mirrored to stereo partner)" if sibling is not None else "")
                            + " - filename/folder updated, Name change not yet saved.")

    def _refresh_multisample_node_label(self, m: KmpMultisample, path: str) -> None:
        for node in self.all_multisample_nodes():
            if node.multisample_ref[0] is m:
                node.label = multisample_node_label(m, path)
        self._tree_refreshed()

    def rename_selected_sample(self, new_name: str) -> None:
        new_name = new_name.strip()
        if not new_name or self._selected_sample is None:
            return
        if self._selected_sample.name == new_name:
            return
        self._selected_sample.name = new_name
        if self.should_mirror_to_partner and self._partner_sample is not None:
            self._partner_sample.name = new_name
        self._sample_dirty = True
        # Must include the suffix, matching _load_sample_detail_state's "Name+Suffix" form.
        self.sample_name = self._selected_sample.name + self._selected_sample.suffix
        self.status_text = (f"Renamed sample to '{new_name}{self._selected_sample.suffix}'"
                            + (" (mirrored to stereo partner)" if self.should_mirror_to_partner and self._partner_sample is not None else "")
                            + " - not yet saved.")

    # ── Recent files / report ──────────────────────────────────────────────────────────────

    def get_recent_files(self) -> List[str]:
        return list(getattr(self.settings, "sample_recent_files", []) or [])

    def clear_recent_files(self) -> None:
        if self.settings is not None:
            self.settings.sample_recent_files = []
            if self._save_settings:
                self._save_settings(self.settings)

    def _add_recent_file(self, path: str) -> None:
        if self.settings is None:
            return
        recent = [p for p in self.settings.sample_recent_files if p != path]
        recent.insert(0, path)
        self.settings.sample_recent_files = recent[:RECENT_FILES_MAX]
        if self._save_settings:
            self._save_settings(self.settings)

    def build_normalization_report(self):
        if self._collection is None or self._collection_path is None:
            return []
        return build_normalization_report(self._collection, self._collection_path)
