r"""
Librarian Shell — three-pane Local Library / Merge Window / PCG shell.

Port of Views/LibrarianShellWindow.xaml(.cs) + ViewModels/LibrarianShellViewModel.cs/
LocalLibraryPaneViewModel.cs/MergePaneViewModel.cs/PcgPaneViewModel.cs. This is the UI
layer over a model layer that already exists and is fully committed: local_library_store
(BlobStore/LocalLibraryIndex/OpLog), merge_cache (MergeCache/pull_recursive), pcg_file
(open_pcg), dependency_scanner (scan/has_all_dependencies), librarian_model (plan_move/
plan_batch_move), session_dependency_clipboard, blank_template_store, and changeset_sync
(build_changeset/execute_changeset/commit_changes/sync_library). This module wires them
together into one window; it adds no new persistence format and no new hardware protocol.

Pane layout (confirmed from the XAML, not guessed): Grid.Column 0 = Local Library,
Grid.Column 2 = Merge Window, Grid.Column 4 = Loaded PCG File — left-to-right
Local | Merge | PCG, which is what the QSplitter below reproduces.

Placement flow (confirmed from the XAML/code-behind comments, not guessed): the PCG pane
is drag-SOURCE only, never a drop target (module docstring's "strictly read-only"
requirement) — content always lands in the Merge Window first ("Move to Merge Window"),
never directly in Local. Local<->Local and Merge->Local are the only paths that write
into LocalLibraryIndex directly.

Deliberate simplifications vs the C# source (documented here, not silently dropped, per
this subsystem's established porting-gap discipline). This list was revised in a follow-up
pass that closed several gaps the first pass above used to leave open — see each bullet for
what changed and what is STILL cut:
  * Drag-and-drop is now real (a `_PaneTreeWidget` QTreeWidget subclass wires
    setDragEnabled/AcceptDrops/startDrag/dropEvent, encoding the dragged leaf payloads as
    JSON over a custom mime type), for all three routes: PCG->Merge, Merge->Local, and
    Local<->Local. Every drop calls the SAME placement methods the toolbar buttons call
    (_place_merge_entry_at / _swap_local_at / _pull_pcg_address_into_merge /
    _run_sequential_fill) — nothing is duplicated. STILL cut: F2-to-rename (Properties…
    already covers renaming), and any custom drag-ghost/insertion-indicator visuals beyond
    Qt's own default drag cursor. Multi-select (Ctrl/Shift-click) comes for free from Qt's
    ExtendedSelection mode on all three trees now (the Local pane switched from single- to
    ExtendedSelection to support multi-item Copy below) — Swap/Erase/Properties/Stage-for-
    Batch stay inherently single-item (each already refuses via `_leaf_payload`'s own "exactly
    one selected" guard if more than one Local item is selected, unchanged).
  * PaneInteraction.cs's Cut/Copy/Paste clipboard is now wired: `batch_clipboard.BatchClipboard`
    backs Ctrl+X/Ctrl+C/Ctrl+V (active while the Local tree has focus — see keyPressEvent)
    plus a Local-pane right-click context menu with the same three actions. Cut refuses (with
    a message, not a silent no-op) if more than one item is selected — the clipboard itself
    enforces the cap (see batch_clipboard.py's own module docstring for why: a swap has no way
    to vacate more than one source slot). Paste always goes through the SAME `_DestinationDialog`
    bank+number picker every other placement action in this file already uses, rather than
    porting PasteIntoSlot/PasteIntoBank's "target whatever node is right-clicked" distinction
    — a deliberate simplification (one paste UX, not two) that still reaches every real
    destination a bank+number picker can address. Cut's paste feeds `paste_swap_target()`'s
    result into the EXISTING `_swap_local_at`/plan_move path; Copy's paste feeds
    `paste_targets()`'s result into the EXISTING `_apply_batch_placements`/plan_batch_move path
    (factored out of `_run_sequential_fill`, which now also uses it) — neither placement
    codepath is duplicated for the clipboard.
  * Merge -> Local (and PCG -> Local, via Merge staging) placement is no longer single-item
    only: a "Fill Sequentially…" toolbar button on both the Merge and PCG panes, plus
    dragging a MULTI-selection onto the Local pane, both call librarian_model.py's
    resolve_sequential_fill() then plan_batch_move() — the same two-stage pipeline the C#
    source uses for its own sequential auto-fill drop. Dragging a SINGLE item still means
    "place exactly here" (matches PaneInteraction's own single-vs-multi drag distinction).
    STILL cut: the persisted BatchClipboard cut/paste history and any UI surface for
    plan.displaced (a bumped destination occupant is only ever CHECK-warned in the log, via
    divert_displaced=False, never diverted to a recoverable clipboard slot).
  * Local -> Merge staging ("Move to Merge Window" from the Local pane) is now implemented
    as a "Stage for Batch…" button — confirmed from MergePaneViewModel.cs's own PullFromLocal
    doc comment ("stage a Local Library object, transitively, back into the Merge Window, so
    it can be rearranged and pushed to a different destination") to be the SAME operation as
    PullFromPcg, just against a different source. No new merge_cache.py API was needed:
    MergeCache.pull_recursive() already takes resolve_content/resolve_refs as plain
    parameters (not hardcoded to PCG), and MergeCache.LOCAL_SOURCE_LABEL already existed for
    exactly this origin label — composing pull_recursive against a small local-index-backed
    resolve_content closes this gap with zero changes to merge_cache.py itself.
  * History and Object Dependencies are now two separate panels (matching
    LibrarianShellWindow.xaml's own Grid.Row 3, confirmed two side-by-side GroupBoxes, not
    one combined log): History is a QListWidget seeded at window-open from OpLog.replay()
    (so it survives closing/reopening the Librarian, unlike the old combined QPlainTextEdit)
    and then grows with one line per subsequent local mutation, in the SAME plain-English
    style _log() always used — this is a simpler read-side than the XAML's own
    Description/Timestamp/IsSynced-bound ListBox template (OpLog's synced_at_utc is never
    actually written by anything in this subsystem yet, in Python OR C#'s
    RecordPushSuccesses-equivalent wiring here, so a real "— synced" marker has nothing to
    key off; tracked as a pre-existing gap, not introduced by this pass). Object Dependencies
    is a second QListWidget, populated on selection change in ANY of the three trees, walking
    the selected Combi/Set List's OWN outgoing references RECURSIVELY (Combi timbre refs
    nest into that Combi's own Program refs, one level; a selected Program contributes
    nothing, matching the XAML's own tooltip: "every Program/Combi the currently selected
    Combi(s) or Set List(s) reference, including nested dependencies") — each row marked
    [OK]/[MISSING] against the resolver appropriate to that pane (Local index, the Merge
    cache's own already-resolved ref_sites graph, or the loaded PCG plus a Local fallback).
  * Dirty/conflicted/pending-delete visualization is plain QTreeWidgetItem foreground
    color (theme.py tokens), not the XAML's layered Border/DataTrigger Background scheme,
    and there is no separate green/red "dependency completeness" dot — that signal is
    folded into the Properties dialog's own read-out instead of a tree-row glyph.
  * Category/Sub-Category (PropertiesDialog's ForProgramOrCombi) and the Set-List slot list
    (ForSetList) are now EDITABLE for Local Library entries, backed by object_body.py's now-
    landed write_program_name/write_program_category/write_combi_name/write_combi_category/
    write_setlist_name/write_setlist_slot_name/write_setlist_slot_color/
    write_setlist_slot_comments. Confirmed Local-pane-only from the C# source itself, not
    guessed: LibrarianShellWindow.xaml only ever wires MouseDoubleClick/"Properties…" to
    `TV_Local` — TV_Merge and TV_Pcg have no PropertiesDialog call site at all — so Merge/PCG
    keep the READ-ONLY-only treatment this dialog already had for them (extra_lines/list_rows,
    unchanged); PropertiesDialog.xaml.cs itself has no "is this editable" flag of its own, it's
    simply never constructed for those two panes. Saving folds every changed field (name,
    category/sub-category, and/or one Set-List slot's name/color/comments) into the entry's
    body bytes then advances current_hash (baseline_hash untouched, so it's correctly dirty —
    the SAME `_advance_entry_after_write` pattern every other local write in this file already
    uses) and appends one "PropertyEdit" OpLog entry — the exact op_kind LocalEditOps.cs's own
    EditProperties/EditSetListSlot use (confirmed from source: NOT the separate, standalone
    "Rename" op_kind LocalEditOps.Rename uses for an unrelated quick-rename gesture this
    Properties dialog was never wired to). This also fixes a gap the PRE-this-pass rename path
    had: it only ever touched LocalIndexEntry.display_name (a UI-cache field), never the body's
    own name bytes — so a plain rename used to silently desync the display name from what
    Sync/Commit would actually push. Folding rename into the same combined body-write path
    this pass adds for Category/slots closes that for free. A Set-List rename and a slot edit
    in the same dialog Accept are still two independent body writes (matching
    LocalEditOps.EditProperties/EditSetListSlot being two separate calls in the C# source, the
    second reading whatever the first just wrote), not one combined write like Program/Combi's
    name+category. Set-List slot color is a swatch+name QComboBox backed by setlist_colors.py
    (port of Tools/SetListColors.cs — Default/Charcoal/Brick/… with real RGB values), matching
    PropertiesDialog.xaml.cs's ForSetList exactly.
  * The "unresolved dependencies" dialog is reused for two distinct moments, matching the
    two real call sites in the C# source: (a) a blocking, OK-only notice at Sync/Commit
    time (build_changeset's own gate already refuses while the clipboard is non-empty —
    this dialog just surfaces that up front instead of making the user click Sync to find
    out), and (b) a Continue/Cancel confirmation at Merge->Local placement time, when the
    object being placed references something not yet present locally (accepting adds a
    SessionDependencyEntry per missing reference, which is what makes (a) eventually fire).
  * write_to_hardware's wire signature (changeset_sync.WriteToHardware) does not carry a
    version byte through to the hardware writer, so this module falls back to
    librarian_sysex.OBJ_VERSION's per-type default when pushing — the same fallback
    librarian_sysex.py's own module docstring already documents as acceptable ("Program
    version depends on HD-1 vs EXi; both are 5 today"). Fixing this would mean changing
    changeset_sync's already-committed public signature, out of scope for a UI-layer task.
  * Whole-bank HD-1<->EXi reformat: _get_live_bank_type plugs into changeset_sync.
    build_changeset's get_live_bank_type parameter the same way _get_live_digest/
    _get_bank_objects/_write_to_hardware already adapt SysExService for the rest of this
    pipeline — it derives a bank's live format from an actual Object Dump reply (slot 0's
    body length against pcg_file.WIRE_SIZE_EXI), since SysExService has no dedicated "query
    bank format" call. A real HD-1/EXi mismatch with nothing staged still surfaces as a clear
    "REFUSE: ... is currently formatted as ..." line in the warnings log, same as before.
    pending_bank_type_change/write_bank_type_change ARE now wired: a "Stage Bank
    Conversion…"/"Unstage Bank Conversion" pair on a Program bank node's right-click menu
    calls LocalLibraryIndex.set_pending_bank_type_change/clear_pending_bank_type_change
    directly (behind a confirmation dialog spelling out the func-0x7C whole-bank
    erase/reformat), `index.get_pending_bank_type_change` is passed straight through as
    build_changeset's `pending_bank_type_change` callable (the exact shape that method's own
    docstring says it exists for), and `_write_bank_type_change` (new) is the
    WriteBankTypeChange adapter — it sends a REAL func-0x7C Change Program Bank Type: neither
    a wire-format builder nor a SysExService method for 0x7C existed ANYWHERE in this codebase
    before this pass (grepped first, confirmed empty), so both were added
    (librarian_sysex.change_program_bank_type_request + SysExService.change_program_bank_type),
    with the wire bytes (`F0 42 3g 68 7C bank type F7`, func-0x24 Reply) confirmed against the
    C# source's own KronosSysEx.cs.BuildChangeProgramBankType rather than guessed. On a fully
    successful reformat (SyncResult.reformatted == the number of banks staged this push),
    _on_sync_done clears the staged intent for each of those banks — mirroring
    SyncPipeline.cs's own post-success `ClearPendingBankTypeChange` loop (see
    local_library_store.py's bank_type_pending docstring: this class deliberately never clears
    it automatically, that's the caller's job). Deliberate simplification vs the C# source:
    the ONLY way the real app ever stages a bank-type change is as a side effect of a specific
    whole-Program-bank Merge->Local placement flow (LibrarianShellViewModel.
    PlaceMergeBankWithTypeChange, which also replaces every existing local Program in that
    bank and places a Merge-Window group into it in one step) — there is no standalone
    "just stage a conversion" command in the C# UI at all. Building that full combo flow is a
    separate, larger feature; this pass's standalone Stage/Unstage action (matching the task's
    own explicit "e.g. right-click on a Program bank node" ask) reaches the same underlying
    changeset_sync/hardware plumbing without it.
  * The remote PCG file picker (RemoteFilePickerDialog port) navigates over FTP
    synchronously (blocking the dialog during each directory listing) rather than on a
    background thread — reuses file_manager._FtpWorker directly, no new FTP client. This
    trades a brief UI freeze per navigation for a much smaller dialog; Sync/Commit/Merge-
    pull (the operations that can take a long time) DO run on background threads.

Threading: Sync Library, Commit Changes, and PCG-pull-into-Merge run on a daemon worker
thread, matching librarian_window.py's own QThread-free `threading.Thread` + Qt Signal
pattern; sysex_service.SysExService's own docstrings require its blocking calls run off
the GUI thread. Purely-local operations (Erase, Rename, Local<->Local swap, Clear
Changes, opening a local .pcg file) are cheap disk/CPU work and run synchronously.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import QByteArray, QMimeData, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QDrag, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QGraphicsOpacityEffect, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMenu, QMessageBox, QProgressBar, QPushButton, QSpinBox, QSplitter,
    QStackedLayout, QStyle, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from batch_clipboard import BatchClipboard, ClipboardMode
from blank_template_store import BlankTemplateStore
import dependency_scanner as depscan
import erase_body
import kronos_sysex as ksx
import object_body
from changeset_sync import ChangesetPlan, SyncResult, commit_changes, sync_library
from librarian_model import (
    BatchPlacement, LibraryCatalog, ObjLoc, SequentialFillItem, WriteOp,
    _READONLY_PROGRAM_BANKS, plan_batch_move, plan_move, resolve_sequential_fill,
)
from librarian_sysex import (
    OBJ_COMBI, OBJ_PROGRAM, OBJ_SET_LIST, OBJ_VERSION, ObjectDump,
    obj_bank_to_func33, set_combi_timbre_ref, set_setlist_slot_ref,
)
from library_pull_pipeline import EDITABLE_BANKS, GetBankObjects, GetLiveDigest, SLOT_COUNT
from local_library_store import BlobStore, LocalIndexEntry, LocalLibraryIndex, OpLog
from merge_cache import MergeCache, MergeEntry, MergeRefSite
from pcg_file import PcgFile, PcgObjectEntry, WIRE_SIZE_EXI, open_pcg, wire_body_from_pcg_entry
from session_dependency_clipboard import SessionDependencyClipboard, SessionDependencyEntry
from setlist_colors import ALL_COLORS as SETLIST_COLORS, get_by_index_or_default as setlist_color
from setlist_data import MAX_COUNT
import storage
from sysex_service import SysExService
import theme as T

# Custom mime type for the three panes' real (not simulated) drag-and-drop — carries the
# SAME leaf-payload tuples each tree already stores in Qt.ItemDataRole.UserRole (see
# _PaneTreeWidget below), so a drop just decodes back into the identical shape
# _leaf_payload()/_selected_payloads() already understand.
_DND_MIME = "application/x-kronos-librarian-payload"

_ROOT_LABEL = {OBJ_PROGRAM: "Programs", OBJ_COMBI: "Combis", OBJ_SET_LIST: "Set Lists"}
_ROOT_ORDER = (OBJ_PROGRAM, OBJ_COMBI, OBJ_SET_LIST)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()



def _current_hash_or_empty(window, loc: "ObjLoc") -> str:
    entry = window._index.get(loc.obj_type, loc.bank, loc.number)
    return entry.current_hash if entry is not None else ""

def _parse_key(key: str) -> Tuple[int, int, int]:
    a, b, c = key.split(":")
    return int(a), int(b), int(c)


def _legend_text(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color: {T.TEXT_DIM}; font-size: 10px;")
    return lbl


def _bank_label(obj_type: int, bank: int) -> str:
    if obj_type == OBJ_PROGRAM:
        return ksx.program_label(bank)
    if obj_type == OBJ_COMBI:
        return ksx.combi_label(bank)
    return ""


# ── Pure grouping logic (no Qt — self-tested below) ──────────────────────────


def _group_local(entries: Dict[str, LocalIndexEntry]
                  ) -> Dict[int, Dict[int, List[Tuple[int, str, LocalIndexEntry]]]]:
    """{obj_type: {bank: [(number, key, entry), ...]}}, sorted by number within
    each bank. Always has all three obj_type keys present (Local pane keeps all
    three type roots even when empty, matching ObjectTreeScaffold's
    keepEmptyRoots=True for the Local pane)."""
    groups: Dict[int, Dict[int, List[Tuple[int, str, LocalIndexEntry]]]] = {
        OBJ_PROGRAM: {}, OBJ_COMBI: {}, OBJ_SET_LIST: {}}
    for key, entry in entries.items():
        obj_type, bank, number = _parse_key(key)
        groups.setdefault(obj_type, {}).setdefault(bank, []).append((number, key, entry))
    for by_bank in groups.values():
        for lst in by_bank.values():
            lst.sort(key=lambda t: t[0])
    return groups


def _group_pcg(objects: List[PcgObjectEntry]) -> Dict[int, Dict[int, List[PcgObjectEntry]]]:
    """{obj_type: {obj_bank: [PcgObjectEntry, ...]}}, sorted by index. Set List
    entries (bank=None) group under obj_bank 0, matching the flat "no inner bank
    node" convention ObjectTreeScaffold.Rebuild uses for Set Lists."""
    groups: Dict[int, Dict[int, List[PcgObjectEntry]]] = {}
    for e in objects:
        b = e.bank.obj_bank if e.bank is not None else 0
        groups.setdefault(e.obj_type, {}).setdefault(b, []).append(e)
    for by_bank in groups.values():
        for lst in by_bank.values():
            lst.sort(key=lambda e: e.index)
    return groups


def _group_merge(entries: List[MergeEntry]) -> Dict[int, List[MergeEntry]]:
    """{obj_type: [MergeEntry, ...]} — bag-based, so no bank grouping (MergeCache
    has no address space until placement; see merge_cache.py's own docstring)."""
    groups: Dict[int, List[MergeEntry]] = {OBJ_PROGRAM: [], OBJ_COMBI: [], OBJ_SET_LIST: []}
    for e in entries:
        groups.setdefault(e.obj_type, []).append(e)
    for lst in groups.values():
        lst.sort(key=lambda e: (e.display_name or "", e.content_hash))
    return groups


def _advance_entry_after_write(existing: Optional[LocalIndexEntry], new_hash: str,
                                display_name: str, version: int, now: str) -> LocalIndexEntry:
    """Shared write-application logic for both Merge->Local placement and a
    Local<->Local swap's local-side apply: baseline_hash is preserved (so the
    slot becomes/stays dirty against whatever hardware baseline it already had,
    or NO_BASELINE_SENTINEL for a genuinely new local-only slot) while
    current_hash advances to the new content. Never touches hardware — pushing
    is Sync/Commit's job."""
    baseline = existing.baseline_hash if existing is not None else LocalLibraryIndex.NO_BASELINE_SENTINEL
    return LocalIndexEntry(
        version=existing.version if existing is not None else version,
        baseline_hash=baseline, current_hash=new_hash, display_name=display_name,
        created_utc=existing.created_utc if existing is not None else now,
        modified_utc=now, conflicted=False,
        has_resolved_dependencies=existing.has_resolved_dependencies if existing is not None else True,
        is_exi=existing.is_exi if existing is not None else True,
        pending_delete=False,
    )


# ── Small dialogs ─────────────────────────────────────────────────────────────


class _PropertiesDialog(QDialog):
    """Port of PropertiesDialog's Program/Combi/Set-List "real estate": name, bank/number,
    dirty/conflicted/pending-delete, plus Category/Sub-Category (Program/Combi) and a
    Set-List slot list, both sourced from object_body.py against the item's own body bytes.

    Two distinct modes, matching what the caller passes (mirrors ForProgramOrCombi's fixed
    two ints vs ForSetList's slot browser in the C# source):
      * `category_choice=(category, sub_category)` — EDITABLE (Local pane only; see module
        docstring for why Merge/PCG never pass this): two QSpinBoxes, raw ints, no name table
        (object_body.py's own precedent — no such table exists in the documented format).
      * `setlist_slots=[SetListSlotInfo, ...]` — EDITABLE (Local pane only): a selectable
        QListWidget of non-empty slots; selecting one populates Name/Color/Comments fields
        (color: swatch+name QComboBox backed by setlist_colors.py) enabled only
        while a slot is selected, mirroring PropertiesDialog.xaml.cs's own OnSlotSelected/
        SetSlotFieldsEnabled. `edited_slot` captures whichever slot was selected at Accept
        time (all three fields, even if the user didn't touch them — matches the C# source's
        own OnOk, which always re-sends the text boxes' current values for `_selectedSlotNumber`
        rather than diffing against the original).

    Read-only fallback (Merge/PCG, and any call that omits the two params above): `extra_lines`
    renders as plain read-only labels; `list_rows`, if given, renders as a plain (non-
    selectable-for-editing) QListWidget below them — the Set-List slot summary's original
    read-only shape, still used for Merge/PCG."""

    _MIN_CATEGORY, _MAX_CATEGORY = 0, 0x11
    _MIN_SUB_CATEGORY, _MAX_SUB_CATEGORY = 0, 7

    def __init__(self, heading: str, name: str, editable_name: bool,
                 location: str, flag_lines: List[str], extra_lines: List[str],
                 list_rows: Optional[List[str]] = None,
                 category_choice: Optional[Tuple[int, int]] = None,
                 setlist_slots: Optional[List["object_body.SetListSlotInfo"]] = None,
                 category_names=None, obj_type: int = 0,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle(heading)
        self.setStyleSheet(f"QDialog {{ background-color: {T.BG}; color: {T.TEXT}; }}")
        self.new_name: Optional[str] = None
        self.new_category: Optional[Tuple[int, int]] = None
        self.edited_slot: Optional[Tuple[int, str, int, str]] = None

        v = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(QLabel("Name:"))
        self._name_edit = QLineEdit(name)
        self._name_edit.setEnabled(editable_name)
        row.addWidget(self._name_edit)
        v.addLayout(row)

        if location:
            loc_lbl = QLabel(location)
            loc_lbl.setStyleSheet(f"color: {T.TEXT_DIM};")
            v.addWidget(loc_lbl)

        for line in flag_lines:
            lbl = QLabel(line)
            lbl.setStyleSheet(f"color: {T.TEXT_DIM};")
            v.addWidget(lbl)

        for line in extra_lines:
            lbl = QLabel(line)
            lbl.setWordWrap(True)
            lbl.setStyleSheet(f"color: {T.TEXT_IDLE}; font-size: {T.FS_SMALL}px;")
            v.addWidget(lbl)

        self._cat_spin: Optional[QSpinBox] = None
        self._subcat_spin: Optional[QSpinBox] = None
        self._cat_combo: Optional[QComboBox] = None
        self._subcat_combo: Optional[QComboBox] = None
        if category_choice is not None:
            cat, sub = category_choice
            cat_row = QHBoxLayout()
            cat_row.addWidget(QLabel("Category:"))
            # Real names from the Global object (GlobalBody.ReadCategoryNames) when a
            # synced decode is available — plain numeric spinboxes otherwise. Both write
            # back through the same _collect_category() below (mirrors PropertiesDialog
            # labelling its dropdown with names when it has them, numeric otherwise).
            if category_names is not None and category_names.program:
                self._cat_combo = QComboBox()
                self._cat_combo.addItems(
                    category_names.category_label(obj_type, i) for i in range(0x12))
                self._cat_combo.setCurrentIndex(max(self._MIN_CATEGORY, min(cat, self._MAX_CATEGORY)))
                cat_row.addWidget(self._cat_combo)
                cat_row.addWidget(QLabel("Sub-Category:"))
                self._subcat_combo = QComboBox()
                self._subcat_combo.addItems(
                    category_names.sub_category_label(obj_type, cat, s) for s in range(8))
                self._subcat_combo.setCurrentIndex(max(self._MIN_SUB_CATEGORY, min(sub, self._MAX_SUB_CATEGORY)))
                cat_row.addWidget(self._subcat_combo)
            else:
                self._cat_spin = QSpinBox()
                self._cat_spin.setRange(self._MIN_CATEGORY, self._MAX_CATEGORY)
                self._cat_spin.setValue(max(self._MIN_CATEGORY, min(cat, self._MAX_CATEGORY)))
                cat_row.addWidget(self._cat_spin)
                cat_row.addWidget(QLabel("Sub-Category:"))
                self._subcat_spin = QSpinBox()
                self._subcat_spin.setRange(self._MIN_SUB_CATEGORY, self._MAX_SUB_CATEGORY)
                self._subcat_spin.setValue(max(self._MIN_SUB_CATEGORY, min(sub, self._MAX_SUB_CATEGORY)))
                cat_row.addWidget(self._subcat_spin)
            v.addLayout(cat_row)

        if list_rows:
            lst = QListWidget()
            lst.setStyleSheet(f"QListWidget {{ background: {T.INSET}; color: {T.TEXT}; "
                              f"border: 1px solid {T.BORDER}; }}")
            for row in list_rows:
                lst.addItem(QListWidgetItem(row))
            lst.setMaximumHeight(220)
            v.addWidget(lst)

        self._slot_list: Optional[QListWidget] = None
        self._slot_numbers: List[int] = []
        self._selected_slot_number: Optional[int] = None
        self._setlist_slots: List["object_body.SetListSlotInfo"] = setlist_slots or []
        if setlist_slots:
            self._slot_list = QListWidget()
            self._slot_list.setStyleSheet(f"QListWidget {{ background: {T.INSET}; color: {T.TEXT}; "
                                          f"border: 1px solid {T.BORDER}; }}")
            for slot in setlist_slots:
                if slot.is_empty:
                    continue
                self._slot_numbers.append(slot.number)
                self._slot_list.addItem(QListWidgetItem(f"{slot.number:03d}  {slot.name}"))
            self._slot_list.setMaximumHeight(160)
            v.addWidget(self._slot_list)

            slot_row = QHBoxLayout()
            slot_row.addWidget(QLabel("Slot name:"))
            self._slot_name_edit = QLineEdit()
            self._slot_name_edit.setEnabled(False)
            slot_row.addWidget(self._slot_name_edit)
            slot_row.addWidget(QLabel("Color:"))
            self._slot_color_combo = QComboBox()
            for c in SETLIST_COLORS:
                pix = QPixmap(28, 12)
                pix.fill(QColor(c.hex))
                self._slot_color_combo.addItem(QIcon(pix), c.display_name)
            self._slot_color_combo.setEnabled(False)
            slot_row.addWidget(self._slot_color_combo)
            v.addLayout(slot_row)
            v.addWidget(QLabel("Slot comments:"))
            self._slot_comments_edit = QLineEdit()
            self._slot_comments_edit.setEnabled(False)
            v.addWidget(self._slot_comments_edit)

            self._slot_list.itemSelectionChanged.connect(self._on_slot_selected)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        v.addWidget(btns)

    def _on_slot_selected(self) -> None:
        row = self._slot_list.currentRow()
        if row < 0 or row >= len(self._slot_numbers):
            self._selected_slot_number = None
            self._slot_name_edit.setEnabled(False)
            self._slot_color_combo.setEnabled(False)
            self._slot_comments_edit.setEnabled(False)
            return
        number = self._slot_numbers[row]
        slot = next((s for s in self._setlist_slots if s.number == number), None)
        if slot is None:
            return
        self._selected_slot_number = number
        self._slot_name_edit.setText(slot.name)
        self._slot_color_combo.setCurrentIndex(setlist_color(slot.color).index)
        self._slot_comments_edit.setText(slot.comments)
        self._slot_name_edit.setEnabled(True)
        self._slot_color_combo.setEnabled(True)
        self._slot_comments_edit.setEnabled(True)

    def _on_accept(self) -> None:
        self.new_name = self._name_edit.text().strip()
        if self._cat_combo is not None and self._subcat_combo is not None:
            self.new_category = (self._cat_combo.currentIndex(), self._subcat_combo.currentIndex())
        elif self._cat_spin is not None and self._subcat_spin is not None:
            self.new_category = (self._cat_spin.value(), self._subcat_spin.value())
        if self._slot_list is not None and self._selected_slot_number is not None:
            self.edited_slot = (self._selected_slot_number, self._slot_name_edit.text(),
                               self._slot_color_combo.currentIndex(), self._slot_comments_edit.text())
        self.accept()


class _UnresolvedDependenciesDialog(QDialog):
    """Port of UnresolvedDependenciesDialog, reused at two call sites — see
    module docstring. `allow_continue=False` renders an OK-only blocking
    notice (Sync/Commit gate); `allow_continue=True` renders a Continue-Anyway
    / Cancel confirmation (placement-time gate)."""

    def __init__(self, heading: str, rows: List[str], allow_continue: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Unresolved Dependencies")
        self.setStyleSheet(f"QDialog {{ background-color: {T.BG}; color: {T.TEXT}; }}")
        v = QVBoxLayout(self)
        head = QLabel(heading)
        head.setWordWrap(True)
        v.addWidget(head)
        lst = QListWidget()
        lst.setStyleSheet(f"QListWidget {{ background: {T.INSET}; color: {T.TEXT}; "
                          f"border: 1px solid {T.BORDER}; }}")
        for r in rows:
            lst.addItem(QListWidgetItem(r))
        lst.setMaximumHeight(220)
        v.addWidget(lst)

        if allow_continue:
            btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            btns.button(QDialogButtonBox.StandardButton.Ok).setText("Continue Anyway")
        else:
            btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        v.addWidget(btns)


class _DestinationDialog(QDialog):
    """Bank + number picker for Merge->Local placement and a Local<->Local
    swap's destination. Set List has no bank concept (matches ObjLoc/BankId
    convention throughout this subsystem)."""

    def __init__(self, obj_type: int, heading: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(heading)
        self.setStyleSheet(f"QDialog {{ background-color: {T.BG}; color: {T.TEXT}; }}")
        v = QVBoxLayout(self)
        v.addWidget(QLabel(heading))
        row = QHBoxLayout()
        self._bank_combo = QComboBox()
        if obj_type != OBJ_SET_LIST:
            label_fn = ksx.program_label if obj_type == OBJ_PROGRAM else ksx.combi_label
            for b in EDITABLE_BANKS[obj_type]:
                self._bank_combo.addItem(label_fn(b), b)
        else:
            self._bank_combo.addItem("Set Lists", 0)
            self._bank_combo.setEnabled(False)
        row.addWidget(QLabel("Bank:"))
        row.addWidget(self._bank_combo)
        row.addWidget(QLabel("Number:"))
        self._number_spin = QSpinBox()
        self._number_spin.setRange(0, (MAX_COUNT - 1) if obj_type == OBJ_SET_LIST else 127)
        row.addWidget(self._number_spin)
        v.addLayout(row)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        v.addWidget(btns)

    def selected(self) -> Tuple[int, int]:
        return int(self._bank_combo.currentData()), self._number_spin.value()


class _RemoteFilePickerDialog(QDialog):
    """Port of RemoteFilePickerDialog — reuses file_manager._FtpWorker (no new
    FTP client). Navigation is synchronous/blocking (see module docstring)."""

    def __init__(self, ftp_worker, start_path: str = "/", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Pull PCG from Kronos")
        self.resize(560, 420)
        self.setStyleSheet(f"QDialog {{ background-color: {T.BG}; color: {T.TEXT}; }}")
        self._ftp = ftp_worker
        self._path = start_path
        self.selected_remote_path: Optional[str] = None

        v = QVBoxLayout(self)
        self._path_label = QLabel(self._path)
        self._path_label.setStyleSheet(f"color: {T.ACCENT};")
        v.addWidget(self._path_label)

        self._list = QListWidget()
        self._list.setStyleSheet(f"QListWidget {{ background: {T.INSET}; color: {T.TEXT}; "
                                 f"border: 1px solid {T.BORDER}; }}")
        self._list.itemDoubleClicked.connect(self._on_double_click)
        v.addWidget(self._list, stretch=1)

        row = QHBoxLayout()
        up_btn = QPushButton("Up")
        up_btn.clicked.connect(self._go_up)
        row.addWidget(up_btn)
        row.addStretch(1)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Open | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._on_open)
        btns.rejected.connect(self.reject)
        row.addWidget(btns)
        v.addLayout(row)

        self._refresh()

    def _refresh(self) -> None:
        self._path_label.setText(self._path)
        self._list.clear()
        try:
            entries = self._ftp.list_dir(self._path)
        except Exception as e:  # pragma: no cover - defensive
            QMessageBox.warning(self, "FTP", f"Could not list {self._path}: {e}")
            return
        dir_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon)
        for e in sorted(entries, key=lambda x: (not x.is_directory, x.name.lower())):
            item = QListWidgetItem(e.name)
            if e.is_directory:
                item.setIcon(dir_icon)
            item.setData(Qt.ItemDataRole.UserRole, e)
            self._list.addItem(item)

    def _go_up(self) -> None:
        clean = self._path.rstrip("/")
        idx = clean.rfind("/")
        self._path = "/" if idx <= 0 else clean[:idx]
        self._refresh()

    def _on_double_click(self, item: QListWidgetItem) -> None:
        entry = item.data(Qt.ItemDataRole.UserRole)
        if entry is not None and entry.is_directory:
            self._path = entry.full_path
            self._refresh()

    def _on_open(self) -> None:
        item = self._list.currentItem()
        entry = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if entry is None or entry.is_directory:
            QMessageBox.information(self, "Pull PCG", "Select a .pcg file first.")
            return
        self.selected_remote_path = entry.full_path
        self.accept()


class _PaneTreeWidget(QTreeWidget):
    """QTreeWidget subclass adding REAL drag-and-drop to a Librarian pane, on top of (not
    instead of) that pane's existing toolbar buttons — every drop this class recognizes is
    handled by LibrarianShellWindow calling the exact same placement methods the buttons
    call (see itemsDropped below). `pane` tags every leaf's UserRole payload as the drag
    SOURCE identity ("local"/"merge"/"pcg" — the same string already stored as payload[0]);
    `accepts_drop=False` makes a tree drag-SOURCE-only, which is how the PCG pane enforces
    "strictly read-only" (module docstring) at the widget level, not just by convention.

    Multi-select (Ctrl/Shift-click) comes for free from Qt's own ExtendedSelection mode —
    set by the caller, not here, since only Merge/PCG need it (Local's own actions are all
    single-item)."""

    itemsDropped = Signal(str, list, object)   # source_pane, [payload tuples], target QTreeWidgetItem|None

    def __init__(self, pane: str, accepts_drop: bool, parent=None):
        super().__init__(parent)
        self._pane = pane
        self.setDragEnabled(True)
        self.setAcceptDrops(accepts_drop)
        self.viewport().setAcceptDrops(accepts_drop)
        self.setDropIndicatorShown(accepts_drop)

    def leaf_payloads(self) -> List[tuple]:
        """Every currently-selected item's UserRole payload (skips bank/root nodes, which
        carry either no payload or a distinct "_bank"-tagged one, not this pane's own leaf
        tag)."""
        out: List[tuple] = []
        for item in self.selectedItems():
            data = item.data(0, Qt.ItemDataRole.UserRole)
            if data is not None:
                out.append(tuple(data))
        return out

    def startDrag(self, supportedActions) -> None:  # noqa: N802 - Qt override
        payloads = self.leaf_payloads()
        if not payloads:
            return
        mime = QMimeData()
        mime.setData(_DND_MIME, QByteArray(
            json.dumps({"pane": self._pane, "items": payloads}).encode("utf-8")))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.mimeData().hasFormat(_DND_MIME):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.mimeData().hasFormat(_DND_MIME):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt override
        mime = event.mimeData()
        if not mime.hasFormat(_DND_MIME):
            event.ignore()
            return
        try:
            payload = json.loads(bytes(mime.data(_DND_MIME)).decode("utf-8"))
            items = [tuple(it) for it in payload.get("items", [])]
            source_pane = payload.get("pane", "")
        except Exception:  # pragma: no cover - defensive
            event.ignore()
            return
        try:
            pos = event.position().toPoint()
        except AttributeError:  # pragma: no cover - older PySide6/Qt5-style event
            pos = event.pos()
        target_item = self.itemAt(pos)
        event.acceptProposedAction()
        self.itemsDropped.emit(source_pane, items, target_item)


# ── Main window ────────────────────────────────────────────────────────────────


class LibrarianShellWindow(QDialog):
    _sync_done = Signal(object, object, object, str)     # PullResult|None, ChangesetPlan|None, SyncResult|None, status
    _merge_pull_done = Signal(int, int, str)              # added, gaps, status
    _progress = Signal(str)

    def __init__(self, host: str, service: Optional[SysExService],
                 ftp_port: int = 21, ftp_username: str = "", ftp_password: str = "",
                 parent=None, settings=None):
        super().__init__(parent)
        self.setWindowTitle("Librarian Shell — Local Library / Merge / PCG")
        self.resize(1280, 800)
        self.setMinimumSize(900, 560)   # matches LibrarianShellWindow.xaml's MinWidth/MinHeight
        self.setStyleSheet(f"QDialog {{ background-color: {T.BG}; color: {T.TEXT}; }}")

        self._host = host
        self._service = service
        self._ftp_port = ftp_port
        self._ftp_username = ftp_username
        self._ftp_password = ftp_password
        if settings is None:
            from app_settings import AppSettings
            settings = AppSettings()
        self._settings = settings

        self._index = LocalLibraryIndex()
        self._index.load()
        self._blobs = BlobStore()
        self._blank_templates = BlankTemplateStore(self._blobs)
        self._oplog = OpLog()
        from merge_cache import MergeCacheBehavior
        behavior = (MergeCacheBehavior.TEMPORARY_MEMORY
                    if getattr(self._settings, 'merge_behavior', 'local_storage') == 'temporary_memory'
                    else MergeCacheBehavior.LOCAL_STORAGE)
        self._merge = MergeCache(behavior=behavior)
        self._clipboard = SessionDependencyClipboard()
        self._batch_clip = BatchClipboard()

        self._pcg: Optional[PcgFile] = None
        self._pcg_source_label = ""
        self._pcg_by_addr: Dict[Tuple[int, int, int], PcgObjectEntry] = {}

        self._busy = False
        self._auto_fill_active = False
        self._auto_fill_scope: Optional[object] = None
        # Set for the duration of one Auto-Fill chunk (see _auto_fill_tick): each placed
        # item's own _place_merge_entry_at would otherwise trigger a full Local+Merge tree
        # rebuild (icon/dependency recompute for every row) PER ITEM, making an N-item
        # sweep do O(N) full rebuilds of an ever-larger dirty tree - one rebuild per TICK
        # (a handful of items) is what the C# source gets for free from binding to
        # ObservableCollections incrementally instead of rebuilding a view from scratch.
        self._suppress_tree_refresh = False
        self._auto_fill_timer = QTimer(self)
        self._auto_fill_timer.setInterval(30)
        self._auto_fill_timer.timeout.connect(self._auto_fill_tick)

        # Linear undo over every LOCAL (pre-Commit) edit made in this window — port of
        # Core/LocalLibrary/LibrarianUndo.cs (librarian_undo.py). Every mutating action
        # below wraps itself in one scope, so one user gesture is one Ctrl+Z. The index
        # and merge cache raise their observer hooks (slot_mutating_cb / mutating_cb) so
        # captures land automatically no matter how deep inside a placement an edit is.
        from librarian_undo import LibrarianUndoRecorder
        self._undo = LibrarianUndoRecorder(
            self._index,
            snapshot_merge=self._merge_snapshot,
            restore_merge=self._merge_restore,
            read_session_deps=lambda: self._clipboard.pending,
            restore_session_deps=self._restore_session_deps,
            pending_bank_type_change=self._index.get_pending_bank_type_change,
            set_pending_bank_type_change=self._set_pending_bank_type_change,
        )
        self._undo.on_changed = self._on_undo_stack_changed
        self._index.slot_mutating_cb = self._undo.on_slot_mutating
        self._merge.mutating_cb = self._undo.on_merge_mutating

        # Category names (GlobalBody.ReadCategoryNames) — seeded from the per-host
        # disk cache at construction, falling back to plain numeric labels, then
        # refreshed live from the Global object in the background (mirrors
        # LibrarianShellViewModel's WarmCategoryNamesAsync: a Kronos that can't be
        # reached, or a reply too short to decode, leaves whatever labels are
        # already in place).
        from global_body import CategoryNames
        cached = storage.load_category_names(host) if host else None
        self._category_names = (CategoryNames.from_dict(cached)
                                if cached is not None else None) or CategoryNames.numeric()
        if host and service is not None and service.can_dump:
            threading.Thread(target=self._warm_category_names, args=(host,),
                             daemon=True, name="CategoryNamesWarm").start()

        self._build_ui()
        self._sync_done.connect(self._on_sync_done)
        self._merge_pull_done.connect(self._on_merge_pull_done)
        self._progress.connect(self._log)
        self._progress.connect(self._status_label.setText)

        self._load_history()
        self._refresh_local_tree()
        self._refresh_merge_tree()
        self._refresh_pcg_tree()
        self._refresh_enable()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override
        """Ctrl+Z (undo, window-wide) plus Ctrl+X/C/V for the Local pane's Cut/Copy/
        Paste, Delete/F2 for the Local pane's toggle-delete/rename (active only while the
        Local tree has focus - Merge/PCG have no clipboard of their own), matching
        main_window.py's own keyPressEvent-based (not QShortcut) accelerator convention
        elsewhere in this app."""
        mods = event.modifiers()
        if mods & Qt.KeyboardModifier.ControlModifier:
            key = event.key()
            if key == Qt.Key.Key_Z:
                self._do_undo()
                return
            if self._tree_local.hasFocus():
                if key == Qt.Key.Key_X:
                    self._cut_local_selected()
                    return
                if key == Qt.Key.Key_C:
                    self._copy_local_selected()
                    return
                if key == Qt.Key.Key_V:
                    self._paste_local_selected()
                    return
        if self._tree_local.hasFocus():
            key = event.key()
            if key == Qt.Key.Key_Delete:
                self._toggle_delete_local_selected()
                return
            if key == Qt.Key.Key_F2:
                self._rename_local_selected()
                return
        super().keyPressEvent(event)

    # ── UI construction ──────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)

        sync_row = QHBoxLayout()
        self._btn_sync = QPushButton("Sync Library")
        self._btn_sync.setToolTip("Pull the whole library (lazy digest-diff, or Force Full Sync below), then push "
                                  "every pending local change.")
        self._btn_sync.clicked.connect(self._start_sync)
        sync_row.addWidget(self._btn_sync)
        self._chk_force_full = QCheckBox("Force Full Sync")
        self._chk_force_full.setToolTip("Instead of syncing changes, forces a complete sync for all "
                                        "programs/combis/set lists on the Kronos.")
        sync_row.addWidget(self._chk_force_full)
        self._btn_undo = QPushButton("Undo")
        self._btn_undo.setToolTip("Nothing to undo")
        self._btn_undo.setEnabled(False)
        self._btn_undo.clicked.connect(self._do_undo)
        sync_row.addWidget(self._btn_undo)
        sync_row.addStretch(1)
        root.addLayout(sync_row)

        # Overall live status line (req 7) - between the Sync row and the three panes,
        # fed by the same _progress signal that drives the History panel, so Sync/Commit/
        # pull progress and error messages are visible here as they happen.
        self._status_label = QLabel("")
        self._status_label.setStyleSheet(f"color: {T.ACCENT}; font-size: 11px;")
        self._status_label.setWordWrap(True)
        root.addWidget(self._status_label)

        panes = QSplitter(Qt.Orientation.Horizontal)
        panes.addWidget(self._build_local_pane())
        panes.addWidget(self._build_merge_pane())
        panes.addWidget(self._build_pcg_pane())
        panes.setSizes([420, 420, 420])
        root.addWidget(panes, stretch=1)

        history_row = QHBoxLayout()
        history_row.addWidget(self._build_history_pane(), stretch=1)
        history_row.addWidget(self._build_dependencies_pane(), stretch=1)
        root.addLayout(history_row)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.reject)
        close_row.addWidget(btn_close)
        self._btn_commit = QPushButton("Commit Changes")
        self._btn_commit.setToolTip("Validate and push every pending local change now, "
                                    "without pulling first.")
        self._btn_commit.setStyleSheet(
            f"QPushButton {{ background-color: #6E3535; color: {T.TEXT}; }} "
            f"QPushButton:hover {{ background-color: #7E3F3F; }} "
            f"QPushButton:disabled {{ background-color: {T.INSET}; color: {T.TEXT_DIM}; }}")
        self._btn_commit.clicked.connect(self._start_commit)
        close_row.addWidget(self._btn_commit)
        root.addLayout(close_row)

    def _build_local_pane(self) -> QWidget:
        box = QGroupBox("Local Library")
        v = QVBoxLayout(box)
        row = QHBoxLayout()
        btn_cut = QPushButton("Cut")
        btn_cut.setToolTip("Cut the selected item(s) (Ctrl+X)")
        btn_cut.clicked.connect(self._cut_local_selected)
        row.addWidget(btn_cut)
        btn_copy = QPushButton("Copy")
        btn_copy.setToolTip("Copy the selected item(s) (Ctrl+C)")
        btn_copy.clicked.connect(self._copy_local_selected)
        row.addWidget(btn_copy)
        self._btn_paste = QPushButton("Paste")
        self._btn_paste.setToolTip("Paste onto the selected slot (Ctrl+V)")
        self._btn_paste.clicked.connect(self._paste_local_selected)
        row.addWidget(self._btn_paste)
        btn_rename = QPushButton("Rename...")
        btn_rename.setToolTip("Rename the selected item (F2)")
        btn_rename.clicked.connect(self._rename_local_selected)
        row.addWidget(btn_rename)
        self._btn_delete = QPushButton("Delete")
        self._btn_delete.setToolTip("Removes from your local library only - hardware is "
                                    "unaffected until Sync/Commit; Pull restores it. (Del)")
        self._btn_delete.clicked.connect(self._toggle_delete_local_selected)
        row.addWidget(self._btn_delete)
        btn_clear = QPushButton("Clear Changes")
        btn_clear.setToolTip("Reverts every pending local edit back to baseline and un-marks "
                             "every pending deletion - a fresh Pull would show the same thing. "
                             "Does not touch hardware.")
        btn_clear.clicked.connect(self._clear_changes)
        row.addWidget(btn_clear)
        v.addLayout(row)
        self._local_status_label = QLabel("")
        self._local_status_label.setStyleSheet(f"color: {T.ACCENT}; font-size: 11px;")
        self._local_status_label.setWordWrap(True)
        v.addWidget(self._local_status_label)
        self._tree_local = _PaneTreeWidget("local", accepts_drop=True)
        self._tree_local.setHeaderHidden(True)
        # ExtendedSelection (not the single-selection every other pane action here still
        # assumes) so Copy can take more than one item — see module docstring. Swap/Erase/
        # Properties/Stage-for-Batch are unaffected: `_leaf_payload` already refuses (not
        # silently misbehaves) whenever more than one item is selected.
        self._tree_local.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._tree_local.itemDoubleClicked.connect(lambda *_: self._show_local_properties())
        self._tree_local.itemSelectionChanged.connect(
            lambda: self._update_object_dependencies("local", self._tree_local))
        self._tree_local.itemsDropped.connect(self._on_local_dropped)
        self._tree_local.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree_local.customContextMenuRequested.connect(self._show_local_context_menu)
        v.addWidget(self._tree_local)
        # Empty-state hint shown in the tree's place (port of
        # LocalLibraryPaneViewModel.ShowEmptyHint) when the library holds nothing yet
        # — a fresh install, or the exe run from a folder with no library beside it.
        # No bare type-root headers appear until the first Sync populates the library.
        self._empty_hint = QLabel("Your local library is empty.\nClick \"Sync Library\" to "
                                  "pull it from your Kronos.")
        self._empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_hint.setWordWrap(True)
        self._empty_hint.setStyleSheet(f"color: {T.ACCENT}; font-style: italic;")
        self._empty_hint.hide()
        v.addWidget(self._empty_hint)
        # Legend (req 6) - matches LibrarianShellWindow.xaml's Local Library legend wording:
        # Legend (req 6) - matches LibrarianShellWindow.xaml's Local Library legend wording:
        # dot 1 = edit state, dot 2 = dependency state; swatches for conflict / pending-delete
        # / read-only row tints.
        legend = QWidget()
        legend_lay = QVBoxLayout(legend)
        legend_lay.setContentsMargins(0, 4, 0, 0)
        legend_lay.setSpacing(2)
        row1 = QHBoxLayout()
        row1.setSpacing(3)
        d1 = QLabel("●")
        d1.setStyleSheet(f"color: {T.ERROR_TEXT}; font-size: 9px;")
        row1.addWidget(d1)
        row1.addWidget(_legend_text("1st: edited locally"))
        d2a = QLabel("●")
        d2a.setStyleSheet(f"color: {T.OK_TEXT}; font-size: 9px;")
        row1.addWidget(d2a)
        row1.addWidget(_legend_text("2nd: dependencies OK"))
        d2b = QLabel("●")
        d2b.setStyleSheet(f"color: {T.ERROR_TEXT}; font-size: 9px;")
        row1.addWidget(d2b)
        row1.addWidget(_legend_text("2nd: dependencies missing"))
        row1.addStretch(1)
        legend_lay.addLayout(row1)
        row2 = QHBoxLayout()
        row2.setSpacing(3)
        sw1 = QLabel()
        sw1.setFixedSize(11, 11)
        sw1.setStyleSheet(f"background-color: {T.WARN}; border: 1px solid {T.TEXT_DIM};")
        row2.addWidget(sw1)
        row2.addWidget(_legend_text("conflicted"))
        sw2 = QLabel()
        sw2.setFixedSize(11, 11)
        sw2.setStyleSheet(f"background-color: {T.INSET}; border: 1px solid {T.TEXT_DIM};")
        row2.addWidget(sw2)
        row2.addWidget(_legend_text("marked for deletion"))
        aa = QLabel("Aa")
        aa.setStyleSheet(f"color: {T.TEXT_DIM}; font-family: {T.FONT_MONO}; font-size: 10px;")
        row2.addWidget(aa)
        row2.addWidget(_legend_text("read-only (GM/g)"))
        row2.addStretch(1)
        legend_lay.addLayout(row2)
        v.addWidget(legend)
        return box

    def _build_merge_pane(self) -> QWidget:
        # Toolbar matches LibrarianShellWindow.xaml's Merge Window GroupBox exactly:
        # Auto-Fill to Library (with an indeterminate progress bar overlaid while it runs,
        # req 10a), Force Overwrite checkbox + Clear Merge on the right. Placing one specific
        # item and removing a staged item are drag-drop / right-click-Remove in C# (MI_
        # RemoveFromMerge), not toolbar buttons — same here now: dragging a Merge leaf
        # onto the Local pane still calls _place_merge_entry_at, multi-select drag still
        # calls _run_sequential_fill, and Remove moved to the tree's context menu below.
        box = QGroupBox("Merge Window")
        v = QVBoxLayout(box)
        row = QHBoxLayout()
        self._btn_auto_fill = QPushButton()
        self._btn_auto_fill.setToolTip(
            "Place everything staged here into the next free slots of its own type in "
            "Local Library - Programs first, then Combis, then Set Lists, so each one's "
            "dependencies are already placed and its references point at where they "
            "actually landed. Nothing is sent to the Kronos: this only stages, exactly "
            "like dragging items across yourself. Review the result, then Commit Changes "
            "to push.")
        self._btn_auto_fill.clicked.connect(self._auto_fill_to_library)
        # Matches LibrarianShellWindow.xaml's Auto-Fill button exactly: a single Grid
        # cell with the indeterminate ProgressBar (dimmed) BEHIND the centered label,
        # not a two-row stack above it - QStackedLayout in StackAll mode is the Qt
        # equivalent of WPF's same-cell overlay (both children get the button's full
        # geometry; the label is raised so it paints on top).
        self._auto_fill_progress = QProgressBar()
        self._auto_fill_progress.setRange(0, 0)   # indeterminate
        self._auto_fill_progress.setTextVisible(False)
        self._auto_fill_progress.setStyleSheet(
            "QProgressBar { border: none; background: transparent; } "
            f"QProgressBar::chunk {{ background-color: {T.ACCENT_DEEP}; }}")
        # StackAll keeps BOTH children technically "shown" at all times (that's the whole
        # point of the mode - it manages their visibility itself and would fight a plain
        # .hide()/.show() on the progress bar), so idle-vs-filling is toggled by OPACITY
        # instead: 0 at idle (fully transparent, indeterminate animation just not visible),
        # 0.35 while filling - matching the XAML ProgressBar's own Opacity="0.35".
        self._auto_fill_progress_effect = QGraphicsOpacityEffect(self._auto_fill_progress)
        self._auto_fill_progress_effect.setOpacity(0.0)
        self._auto_fill_progress.setGraphicsEffect(self._auto_fill_progress_effect)
        self._auto_fill_label = QLabel("Auto-Fill to Library")
        self._auto_fill_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        fill_stack_lay = QStackedLayout()
        fill_stack_lay.setStackingMode(QStackedLayout.StackingMode.StackAll)
        fill_stack_lay.setContentsMargins(0, 0, 0, 0)
        fill_stack_lay.addWidget(self._auto_fill_progress)
        fill_stack_lay.addWidget(self._auto_fill_label)
        self._auto_fill_label.raise_()
        self._btn_auto_fill.setLayout(fill_stack_lay)
        # C# source: Padding="10 2" MinWidth="128". The button has no setText() of its
        # own (the label inside the stack renders the caption instead), so QPushButton's
        # own sizeHint() - computed from its EMPTY text - stays tiny regardless of the
        # child layout's real content, and the row's QHBoxLayout would otherwise squeeze
        # it down to that tiny size. An explicit size is needed; 170x28 comfortably fits
        # "Auto-Fill to Library" at the app's real font (Segoe UI 9pt measures ~99px for
        # that string - verified against a real Qt "windows" platform session, not the
        # offscreen QPA plugin, which fails to resolve Segoe UI at all and reports wildly
        # inflated fallback-font metrics).
        self._btn_auto_fill.setFixedSize(170, 28)
        row.addWidget(self._btn_auto_fill)
        row.addStretch(1)
        self._chk_force_overwrite = QCheckBox("Force Overwrite")
        self._chk_force_overwrite.setToolTip(
            "Placing onto a slot still referenced by another Combi/Set List normally refuses, "
            "to avoid silently breaking that reference. Check this to overwrite it anyway - "
            "the referrer(s) will then resolve to the NEW object instead of the old one. "
            "The old occupant is still diverted to the session clipboard, never lost outright.")
        row.addWidget(self._chk_force_overwrite)
        btn_clear = QPushButton("Clear Merge")
        btn_clear.setToolTip("Abandons everything staged here, whether or not any of it "
                             "has been placed into Local Library yet.")
        btn_clear.clicked.connect(self._clear_merge)
        row.addWidget(btn_clear)
        v.addLayout(row)
        self._merge_status_label = QLabel("")
        self._merge_status_label.setStyleSheet(f"color: {T.ACCENT}; font-size: 11px;")
        self._merge_status_label.setWordWrap(True)
        v.addWidget(self._merge_status_label)
        self._tree_merge = _PaneTreeWidget("merge", accepts_drop=True)
        self._tree_merge.setHeaderHidden(True)
        self._tree_merge.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._tree_merge.itemDoubleClicked.connect(lambda *_: self._show_merge_properties())
        self._tree_merge.itemSelectionChanged.connect(
            lambda: self._update_object_dependencies("merge", self._tree_merge))
        self._tree_merge.itemsDropped.connect(self._on_merge_dropped)
        self._tree_merge.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree_merge.customContextMenuRequested.connect(self._show_merge_context_menu)
        v.addWidget(self._tree_merge)
        return box

    def _build_pcg_pane(self) -> QWidget:
        box = QGroupBox("Loaded PCG File")
        v = QVBoxLayout(box)
        row = QHBoxLayout()
        lbl = QLabel("Load PCG...")
        lbl.setStyleSheet(f"color: {T.TEXT_DIM};")
        row.addWidget(lbl)
        btn_open = QPushButton("From Computer")
        btn_open.setToolTip("Open a .pcg file from this computer")
        btn_open.clicked.connect(self._open_pcg_from_computer)
        row.addWidget(btn_open)
        self._btn_pull_kronos = QPushButton("From Kronos")
        self._btn_pull_kronos.setToolTip("Pull a .pcg file from the Kronos over FTP")
        self._btn_pull_kronos.clicked.connect(self._open_pcg_from_kronos)
        row.addWidget(self._btn_pull_kronos)
        row.addStretch(1)
        v.addLayout(row)
        self._pcg_status_label = QLabel("No PCG loaded")
        self._pcg_status_label.setStyleSheet(f"color: {T.ACCENT}; font-size: 11px;")
        self._pcg_status_label.setWordWrap(True)
        v.addWidget(self._pcg_status_label)
        self._tree_pcg = _PaneTreeWidget("pcg", accepts_drop=False)
        self._tree_pcg.setHeaderHidden(True)
        self._tree_pcg.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._tree_pcg.itemDoubleClicked.connect(lambda *_: self._show_pcg_properties())
        self._tree_pcg.itemSelectionChanged.connect(
            lambda: self._update_object_dependencies("pcg", self._tree_pcg))
        v.addWidget(self._tree_pcg)
        return box

    def _build_history_pane(self) -> QWidget:
        box = QGroupBox("History")
        box.setFixedHeight(180)
        v = QVBoxLayout(box)
        btn_clear = QPushButton("Clear History")
        btn_clear.clicked.connect(self._clear_history)
        v.addWidget(btn_clear, alignment=Qt.AlignmentFlag.AlignLeft)
        self._history_list = QListWidget()
        self._history_list.setStyleSheet(f"QListWidget {{ background: {T.INSET}; color: {T.TEXT}; "
                                         f"border: 1px solid {T.BORDER}; font-size: 11px; }}")
        v.addWidget(self._history_list)
        return box

    def _build_dependencies_pane(self) -> QWidget:
        box = QGroupBox("Object Dependencies")
        box.setToolTip("Every Program/Combi the currently selected Combi(s) or Set List(s) "
                       "reference, including nested dependencies.")
        box.setFixedHeight(180)
        v = QVBoxLayout(box)
        self._deps_list = QListWidget()
        self._deps_list.setStyleSheet(f"QListWidget {{ background: {T.INSET}; color: {T.TEXT}; "
                                      f"border: 1px solid {T.BORDER}; font-size: 11px; }}")
        v.addWidget(self._deps_list)
        return box

    # ── Logging / History / Object Dependencies ──────────────────────────────

    def _log(self, text: str) -> None:
        """Appends one session-only note to the History panel — used for everything that
        ISN'T itself a discrete local mutation (Sync/Commit progress, pull/placement
        summaries, plan preview/warning lines). NOT written to OpLog, so — unlike the rows
        _load_history() seeds from OpLog.replay() at window-open — a note logged here won't
        reappear after closing and reopening the Librarian. Dimmed so it visually reads as
        ephemeral next to the permanent, OpLog-backed history (see module docstring)."""
        item = QListWidgetItem(text)
        item.setForeground(QBrush(QColor(T.TEXT_DIM)))
        self._history_list.addItem(item)
        self._history_list.scrollToBottom()

    def _load_history(self) -> None:
        """Seeds the History panel from OpLog.replay() at window-open — the persisted,
        authoritative audit trail (see module docstring for why this isn't a live re-render
        of the file on every mutation: this window is the only writer to its own OpLog
        instance during a session, so appending one line per _oplog.append() call, the same
        way _log() already does, stays in sync without re-reading the file)."""
        for op in self._oplog.replay():
            desc = op.get("description") or op.get("op_kind", "")
            ts = op.get("timestamp_utc", "")
            self._history_list.addItem(QListWidgetItem(f"{desc}  [{ts}]"))
        self._history_list.scrollToBottom()

    def _clear_history(self) -> None:
        """Port of the XAML's "Clear History" button — permanently deletes the on-disk
        audit log. OpLog (local_library_store.py) has no delete/truncate method of its own
        (by design: it's meant to be append-only, write-ahead), so this reaches into its
        public `root` attribute to remove oplog.jsonl directly rather than adding a mutator
        method to that module for a UI-only action. Does not touch the local library index,
        pending edits, or hardware — matches the XAML tooltip exactly."""
        if QMessageBox.question(self, "Clear History", "Permanently delete the local audit "
                               "log? This does not affect your local library, pending "
                               "edits, or hardware.") != QMessageBox.StandardButton.Yes:
            return
        try:
            path = self._oplog.root / "oplog.jsonl"
            if path.exists():
                path.unlink()
        except OSError as e:
            QMessageBox.warning(self, "Clear History", f"Could not delete the audit log: {e}")
            return
        self._history_list.clear()
        self._log("History cleared.")

    def _update_object_dependencies(self, pane: str, tree: QTreeWidget) -> None:
        self._deps_list.clear()
        payloads = [p for p in self._selected_payloads(tree) if p[0] == pane]
        for row in self._compute_dependency_rows(pane, payloads):
            self._deps_list.addItem(QListWidgetItem(row))

    @staticmethod
    def _selected_payloads(tree: QTreeWidget) -> List[tuple]:
        out: List[tuple] = []
        for item in tree.selectedItems():
            data = item.data(0, Qt.ItemDataRole.UserRole)
            if data is not None:
                out.append(tuple(data))
        return out

    def _compute_dependency_rows(self, pane: str, payloads: List[tuple]) -> List[str]:
        rows: List[str] = []
        for p in payloads:
            if pane == "local":
                _, obj_type, bank, number = p
                if obj_type not in (OBJ_COMBI, OBJ_SET_LIST):
                    continue
                entry = self._index.get(obj_type, bank, number)
                if entry is None:
                    continue
                body = self._blobs.get(entry.current_hash)
                if body is None:
                    continue
                rows.append(f"── {ObjLoc(obj_type, bank, number).label()} ──")
                rows.extend(self._walk_local_deps(obj_type, body, 1, set()))
            elif pane == "merge":
                entry = self._merge.try_get(p[1])
                if entry is None or entry.obj_type not in (OBJ_COMBI, OBJ_SET_LIST):
                    continue
                rows.append(f"── {entry.display_name or entry.content_hash[:8]} ──")
                rows.extend(self._walk_merge_deps(entry, 1, set()))
            elif pane == "pcg":
                _, obj_type, bank, number = p
                if obj_type not in (OBJ_COMBI, OBJ_SET_LIST):
                    continue
                e = self._pcg_by_addr.get((obj_type, bank, number))
                if e is None:
                    continue
                body = self._pcg_resolve_content(obj_type, bank, number)
                if body is None:
                    continue
                rows.append(f"── {ObjLoc(obj_type, bank, number).label()} ──")
                rows.extend(self._walk_pcg_deps(obj_type, body, 1, set()))
        return rows

    def _walk_local_deps(self, obj_type: int, body: bytes, depth: int,
                         visited: set) -> List[str]:
        indent = "  " * depth
        rows: List[str] = []
        for ref in depscan.walk_object_references(obj_type, body):
            present = self._local_resolver(ref.ref.obj_type, ref.ref.bank, ref.ref.number)
            rows.append(f"{indent}{ref.ref_kind}: {ref.ref.label()}  "
                       f"[{'OK' if present else 'MISSING'}]")
            key = (ref.ref.obj_type, ref.ref.bank, ref.ref.number)
            if present and ref.ref.obj_type == OBJ_COMBI and key not in visited:
                visited.add(key)
                sub_entry = self._index.get(*key)
                sub_body = self._blobs.get(sub_entry.current_hash) if sub_entry else None
                if sub_body is not None:
                    rows.extend(self._walk_local_deps(OBJ_COMBI, sub_body, depth + 1, visited))
        return rows

    def _walk_merge_deps(self, entry: MergeEntry, depth: int, visited: set) -> List[str]:
        indent = "  " * depth
        rows: List[str] = []
        for site in entry.ref_sites:
            loc = ObjLoc(*site.target_address)
            if site.resolved_content_hash is None:
                rows.append(f"{indent}{loc.label()}  [MISSING]")
                continue
            dep = self._merge.try_get(site.resolved_content_hash)
            name = dep.display_name if dep is not None else loc.label()
            rows.append(f"{indent}{loc.label()}: {name}  [OK]")
            if dep is not None and dep.obj_type == OBJ_COMBI and dep.content_hash not in visited:
                visited.add(dep.content_hash)
                rows.extend(self._walk_merge_deps(dep, depth + 1, visited))
        return rows

    def _walk_pcg_deps(self, obj_type: int, body: bytes, depth: int, visited: set) -> List[str]:
        indent = "  " * depth
        rows: List[str] = []
        for ref in depscan.walk_object_references(obj_type, body):
            key = (ref.ref.obj_type, ref.ref.bank, ref.ref.number)
            in_pcg = key in self._pcg_by_addr
            present_locally = self._local_resolver(*key)
            marker = "in PCG" if in_pcg else ("in Local" if present_locally else "MISSING")
            rows.append(f"{indent}{ref.ref_kind}: {ref.ref.label()}  [{marker}]")
            if in_pcg and ref.ref.obj_type == OBJ_COMBI and key not in visited:
                visited.add(key)
                sub_body = self._pcg_resolve_content(*key)
                if sub_body is not None:
                    rows.extend(self._walk_pcg_deps(OBJ_COMBI, sub_body, depth + 1, visited))
        return rows

    # ── Tree population (Qt glue over the pure _group_* helpers) ────────────

    def _local_leaf_icon(self, obj_type: int, entry: LocalIndexEntry) -> Optional[QIcon]:
        """Port of the LocalNodeTemplate's two-dot scheme (req 6): dot 1 (red) = edited
        locally / conflicted; dot 2 (green=OK / red=missing) = dependency completeness for
        a dirty Combi/Set List. Painted as a small DecorationRole pixmap so both dots can
        carry their own color (a plain QTreeWidgetItem has one foreground per row)."""
        dot1 = False
        dot2: Optional[bool] = None   # None = no dot; True = green; False = red
        if entry.is_dirty or entry.conflicted:
            dot1 = True
        if entry.is_dirty and obj_type in (OBJ_COMBI, OBJ_SET_LIST):
            body = self._blobs.get(entry.current_hash)
            if body is not None:
                dot2 = depscan.has_all_dependencies(self._local_resolver, obj_type, body)
        if not dot1 and dot2 is None:
            return None
        pm = QPixmap(16, 10)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        if dot1:
            p.setBrush(QColor(T.ERROR_TEXT))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(2, 2, 6, 6)
        if dot2 is not None:
            p.setBrush(QColor(T.OK_TEXT if dot2 else T.ERROR_TEXT))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(9, 2, 6, 6)
        p.end()
        return QIcon(pm)

    def _style_local_item(self, item: QTreeWidgetItem, entry: LocalIndexEntry) -> None:
        if entry.pending_delete:
            item.setForeground(0, QBrush(QColor(T.TEXT_IDLE)))
            item.setBackground(0, QBrush(QColor("#3A3A3A")))   # PendingDeleteHighlightBrush
        elif entry.conflicted:
            item.setBackground(0, QBrush(QColor("#C08A20")))   # ConflictHighlightBrush (amber)
        # Read-only rows are styled at their own construction site (grey foreground).
    def _refresh_local_tree(self) -> None:
        if self._suppress_tree_refresh:
            return
        tree = self._tree_local
        expanded = {tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())
                    if tree.topLevelItem(i).isExpanded()}
        tree.clear()
        self._deps_list.clear()
        groups = _group_local(self._index.entries)
        for obj_type in _ROOT_ORDER:
            root = QTreeWidgetItem([_ROOT_LABEL[obj_type]])
            tree.addTopLevelItem(root)
            by_bank = groups.get(obj_type, {})
            # Read-only factory (GM/g) Program banks are browsable rows fed by the shared
            # name sweep, never writable — port of ObjectTypeRegistry.ReadOnlyBanks + the
            # pane's ReadOnlyBankNames name source. Bodies are never pulled for these; the
            # rows are purely browse/select (any action on them is filtered out by the
            # cache lookup, which has no entries for them).
            if obj_type == OBJ_PROGRAM and self._service is not None:
                for ro_bank in range(0x10, 0x1B):
                    names = self._service.cached_bank_names(1, ro_bank)
                    if not names:
                        continue
                    ro_label = f"{_bank_label(obj_type, ro_bank)}  (read-only)"
                    ro_parent = QTreeWidgetItem([ro_label])
                    for num in sorted(names):
                        leaf = QTreeWidgetItem([f"{names[num]}  {num:03d}"])
                        leaf.setData(0, Qt.ItemDataRole.UserRole,
                                     ("local", obj_type, ro_bank, num))
                        leaf.setForeground(0, QBrush(QColor(T.TEXT_IDLE)))
                        ro_parent.addChild(leaf)
                    root.addChild(ro_parent)
            for bank in sorted(by_bank):
                items = by_bank[bank]
                if obj_type == OBJ_SET_LIST:
                    parent = root
                else:
                    bank_label = _bank_label(obj_type, bank)
                    if obj_type == OBJ_PROGRAM:
                        pending = self._index.get_pending_bank_type_change(bank)
                        if pending is not None:
                            bank_label += f"  [staged -> {'EXi' if pending else 'HD-1'}]"
                    parent = QTreeWidgetItem([bank_label])
                    # Tagged (distinct from a leaf's "local" tag) so a drag drop onto a whole
                    # bank — no specific slot — can still resolve a destination bank for
                    # _find_first_free_slot (see _handle_merge_to_local_drop).
                    parent.setData(0, Qt.ItemDataRole.UserRole, ("local_bank", obj_type, bank))
                    root.addChild(parent)
                for number, key, entry in items:
                    label = f"{entry.display_name or '(unnamed)'}  {number:03d}"
                    leaf = QTreeWidgetItem([label])
                    leaf.setData(0, Qt.ItemDataRole.UserRole, ("local", obj_type, bank, number))
                    icon = self._local_leaf_icon(obj_type, entry)
                    if icon is not None:
                        leaf.setIcon(0, icon)
                    self._style_local_item(leaf, entry)
                    parent.addChild(leaf)
            if root.text(0) in expanded:
                root.setExpanded(True)
        # Empty-state hint: the tree shows only once the library actually holds
        # something — an empty library shows the Sync hint in its place instead, so
        # the bare type-root headers (Programs/Combis/Set Lists) never appear until
        # the first Sync populates them (mirrors LocalPane.ShowTree/ShowEmptyHint).
        is_empty = len(self._index.entries) == 0
        self._tree_local.setVisible(not is_empty)
        self._empty_hint.setVisible(is_empty)

    def _refresh_merge_tree(self) -> None:
        if self._suppress_tree_refresh:
            return
        tree = self._tree_merge
        tree.clear()
        self._deps_list.clear()
        groups = _group_merge(self._merge.entries)
        for obj_type in _ROOT_ORDER:
            entries = groups.get(obj_type, [])
            if not entries:
                continue
            root = QTreeWidgetItem([_ROOT_LABEL[obj_type]])
            tree.addTopLevelItem(root)
            for e in entries:
                # Port of MergePaneViewModel.MakeNode: name + origin summary (PCG
                # filename, or "N source(s)"), never a raw content hash. "Shared"
                # is a bullet+tooltip in C#, not inline text, and only counts
                # referrers still actually present in the bag (a stale
                # referenced_by entry from an already-placed/removed item must
                # not read as shared).
                name = e.display_name or "(unnamed)"
                origin_summary = (e.origins[0].source if len(e.origins) == 1
                                   else f"{len(e.origins)} source(s)")
                gap = "  [!]" if e.has_unresolved_dependencies else ""
                label = f"{name}  [{origin_summary}]{gap}"
                leaf = QTreeWidgetItem([label])
                leaf.setData(0, Qt.ItemDataRole.UserRole, ("merge", e.content_hash))
                current_referrers = sum(1 for h in e.referenced_by if self._merge.try_get(h) is not None)
                if current_referrers > 1:
                    leaf.setForeground(0, QBrush(QColor("#D4C020")))
                    leaf.setToolTip(0, "Shared by multiple Combis/Songs")
                if e.has_unresolved_dependencies:
                    leaf.setForeground(0, QBrush(QColor(T.ERROR_TEXT)))
                root.addChild(leaf)
        tree.expandAll()

    def _refresh_pcg_tree(self) -> None:
        tree = self._tree_pcg
        tree.clear()
        self._deps_list.clear()
        objects = self._pcg.objects if self._pcg is not None else []
        self._pcg_by_addr = {}
        groups = _group_pcg(objects)
        for obj_type in _ROOT_ORDER:
            by_bank = groups.get(obj_type, {})
            if not by_bank:
                continue
            root = QTreeWidgetItem([_ROOT_LABEL[obj_type]])
            tree.addTopLevelItem(root)
            for bank in sorted(by_bank):
                items = by_bank[bank]
                if obj_type == OBJ_SET_LIST:
                    parent = root
                else:
                    bank_label = items[0].bank.label if items[0].bank is not None else _bank_label(obj_type, bank)
                    parent = QTreeWidgetItem([bank_label])
                    root.addChild(parent)
                for e in items:
                    self._pcg_by_addr[(obj_type, bank, e.index)] = e
                    label = f"{e.name or '(unnamed)'}  {e.index:03d}"
                    leaf = QTreeWidgetItem([label])
                    leaf.setData(0, Qt.ItemDataRole.UserRole, ("pcg", obj_type, bank, e.index))
                    parent.addChild(leaf)
        # Bank-group nodes start collapsed — matches C#'s ObjectTreeNode.IsExpanded
        # default (false); a fresh rebuild otherwise re-collapses everything anyway.
        if self._pcg is None:
            self._pcg_status_label.setText("No PCG loaded")
        else:
            n_rejected = len(self._pcg.rejected_banks)
            extra = f" ({n_rejected} rejected bank(s))" if n_rejected else ""
            self._pcg_status_label.setText(
                f"{self._pcg_source_label} - {len(objects)} object(s){extra}")

    # ── Selection helpers ────────────────────────────────────────────────────

    @staticmethod
    def _leaf_payload(tree: QTreeWidget) -> Optional[tuple]:
        items = tree.selectedItems()
        if len(items) != 1:
            return None
        return items[0].data(0, Qt.ItemDataRole.UserRole)

    def _local_resolver(self, obj_type: int, bank: int, number: int) -> bool:
        return self._index.get(obj_type, bank, number) is not None

    _SETLIST_SLOT_TYPE_LABEL = {0: "Combi", 1: "Program", 2: "Song"}

    def _object_body_readout(self, obj_type: int, body: Optional[bytes]
                             ) -> Tuple[List[str], Optional[List[str]]]:
        """Wires object_body.py's parse_program_body/parse_combi_body/parse_setlist_slot
        into the Properties dialog's existing (extra_lines, list_rows) shapes — Category/
        Sub-Category as a plain fact line (Program/Combi), or the Set List's own slot
        summary as a scrollable list (Set List). Read-only (see module docstring for why);
        returns ([], None) for a missing body or an unsupported obj_type (Set-List has no
        Category concept of its own)."""
        if body is None:
            return [], None
        if obj_type == OBJ_PROGRAM:
            info = object_body.parse_program_body(body)
            return [f"Category: {info.category}   Sub-Category: {info.sub_category}"], None
        if obj_type == OBJ_COMBI:
            info = object_body.parse_combi_body(body)
            return [f"Category: {info.category}   Sub-Category: {info.sub_category}"], None
        if obj_type == OBJ_SET_LIST:
            return [], self._setlist_slot_rows(body)
        return [], None

    def _setlist_slot_rows(self, body: bytes) -> List[str]:
        rows: List[str] = []
        for i in range(object_body.SLOT_COUNT):
            slot = object_body.parse_setlist_slot(body, i)
            if slot is None:
                break
            if slot.is_empty:
                continue
            type_label = self._SETLIST_SLOT_TYPE_LABEL.get(slot.type, str(slot.type))
            color_name = setlist_color(slot.color).display_name
            rows.append(f"{slot.number:03d}  {slot.name}   [{type_label}  bank={slot.bank} "
                       f"idx={slot.index}  color={color_name}  hold={slot.hold_time}  "
                       f"vol={slot.volume}]" + (f"  — {slot.comments}" if slot.comments else ""))
        return rows

    def _object_body_editable_fields(self, obj_type: int, body: Optional[bytes]
                                     ) -> Tuple[Optional[Tuple[int, int]],
                                               Optional[List["object_body.SetListSlotInfo"]]]:
        """Local-pane-only editable counterpart to _object_body_readout — confirmed Local-only
        from PropertiesDialog.xaml.cs's own call sites (see module docstring: Merge/PCG never
        construct this dialog at all in the C# source, so they keep the read-only
        extra_lines/list_rows shape unchanged). Returns (category_choice, setlist_slots) for
        _PropertiesDialog's matching constructor params; (None, None) for a missing body."""
        if body is None:
            return None, None
        if obj_type == OBJ_PROGRAM:
            info = object_body.parse_program_body(body)
            return (info.category, info.sub_category), None
        if obj_type == OBJ_COMBI:
            info = object_body.parse_combi_body(body)
            return (info.category, info.sub_category), None
        if obj_type == OBJ_SET_LIST:
            slots: List[object_body.SetListSlotInfo] = []
            for i in range(object_body.SLOT_COUNT):
                slot = object_body.parse_setlist_slot(body, i)
                if slot is None:
                    break
                slots.append(slot)
            return None, slots
        return None, None

    def _local_bank_type_of(self, bank: int) -> Optional[bool]:
        """Program HD-1/EXi lookup for librarian_model.py's bank_type_of convention
        (True=EXi/False=HD-1/None=unverifiable) — derived from whatever's already locally
        indexed in that bank (LocalIndexEntry.is_exi), since this UI layer has no live
        per-bank format query of its own beyond _get_live_bank_type's single-slot dump
        (which is Sync/Commit-time only, not meant to be polled for planning UI)."""
        for key, entry in self._index.entries.items():
            obj_type, b, _ = _parse_key(key)
            if obj_type == OBJ_PROGRAM and b == bank:
                return entry.is_exi
        return None

    def _first_free_slot_in_bank(self, obj_type: int, bank: int) -> Optional[int]:
        """First slot with no real content — a slot holding only an INIT/blank
        placeholder counts as free (mirrors LocalLibraryCache.HasContent:
        Exists && !IsInitSlot). C#'s own comment on the previous behaviour:
        "a real Kronos ships INT Combi banks I-E/I-F/I-G as init placeholders,
        which HasContent correctly reads as free". None when every slot in
        this bank holds real content."""
        limit = MAX_COUNT if obj_type == OBJ_SET_LIST else 128
        for i in range(limit):
            entry = self._index.get(obj_type, bank, i)
            if entry is None:
                return i
            body = self._blobs.get(entry.current_hash)
            if body is None:
                continue
            if not self._is_init_body(obj_type, body):
                continue
            return i
        return None

    def _find_first_free_slot(self, obj_type: int, bank: int) -> int:
        """Same-bank free-slot lookup for a chosen drop/fill destination. Returns 0
        when every slot holds real content (caller then refuses/dialogues, never
        silently overwrites slot 0)."""
        found = self._first_free_slot_in_bank(obj_type, bank)
        return found if found is not None else 0

    def _find_first_free_slot_any_bank(self, obj_type: int) -> Optional[Tuple[int, int]]:
        """Global counterpart for Auto-Fill to Library: try each EDITABLE_BANKS bank of
        this type in order (port of AutoFillFromMergeAsync's slot-hunting), returning
        the first bank+slot with no real content anywhere. None if the whole type is full."""
        for bank in EDITABLE_BANKS.get(obj_type, []):
            slot = self._first_free_slot_in_bank(obj_type, bank)
            if slot is not None:
                return (bank, slot)
        return None

    def _is_init_body(self, obj_type: int, body: bytes) -> bool:
        """InitObjects.IsInit routed through object_body.py — name signal for
        Program, name-or-all-defaults for Combi, aggregate for Set List."""
        from object_body import (
            program_body_is_init, combi_body_is_init, setlist_body_is_init)
        if obj_type == OBJ_PROGRAM:
            return program_body_is_init(body)
        if obj_type == OBJ_COMBI:
            return combi_body_is_init(body)
        if obj_type == OBJ_SET_LIST:
            return setlist_body_is_init(body)
        return False

    def _local_resolve_content(self, obj_type: int, bank: int, number: int) -> Optional[bytes]:
        entry = self._index.get(obj_type, bank, number)
        if entry is None:
            return None
        return self._blobs.get(entry.current_hash)

    # ── Local pane actions ───────────────────────────────────────────────────

    def _show_local_properties(self) -> None:
        payload = self._leaf_payload(self._tree_local)
        if payload is None or payload[0] != "local":
            return
        _, obj_type, bank, number = payload
        entry = self._index.get(obj_type, bank, number)
        if entry is None:
            return
        loc = ObjLoc(obj_type, bank, number)
        flags = [f"Dirty: {entry.is_dirty}", f"Conflicted: {entry.conflicted}",
                 f"Pending delete: {entry.pending_delete}"]
        body = self._blobs.get(entry.current_hash)
        category_choice, setlist_slots = self._object_body_editable_fields(obj_type, body)
        dlg = _PropertiesDialog(f"Properties — {loc.label()}", entry.display_name,
                                editable_name=True, location=f"Location: {loc.label()}",
                                flag_lines=flags, extra_lines=[],
                                category_choice=category_choice, setlist_slots=setlist_slots,
                                category_names=self._category_names, obj_type=obj_type,
                                parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._apply_local_properties_edit(loc, obj_type, entry.display_name, dlg)

    def _write_local_body_edit(self, loc: ObjLoc, new_body: bytes, description: str) -> None:
        """Shared "apply one body mutation to a Local Library entry" step every Properties-
        dialog edit funnels through — advances current_hash (baseline_hash untouched, so it's
        correctly dirty; the SAME _advance_entry_after_write pattern every other local write
        in this file already uses), re-derives display_name from the new body's own name bytes
        (matches LocalLibraryCache.RecordEdit's ExtractDisplayName — the display name is
        always a read of the body, never independently settable), and logs one "PropertyEdit"
        OpLog entry (LocalEditOps.EditProperties/EditSetListSlot's own op_kind)."""
        entry = self._index.get(loc.obj_type, loc.bank, loc.number)
        if entry is None:
            return
        new_hash = self._blobs.put(new_body)
        display_name = ksx._ascii_trim(new_body, 0, 24)
        now = _now_iso()
        new_entry = _advance_entry_after_write(entry, new_hash, display_name, entry.version, now)
        self._index.set_entry(loc.obj_type, loc.bank, loc.number, new_entry)
        self._oplog.append({
            "id": str(uuid.uuid4()), "timestamp_utc": now, "op_kind": "PropertyEdit",
            "targets": [{"obj_type": loc.obj_type, "bank": loc.bank, "number": loc.number,
                        "result_hash": new_hash}],
            "description": f"Edited {loc.label()}: {description}",
            "sync_batch_id": None, "synced_at_utc": None,
        })
        self._index.save()
        self._log(f"Edited {loc.label()}: {description}")

    def _apply_local_properties_edit(self, loc: ObjLoc, obj_type: int, current_name: str,
                                     dlg: "_PropertiesDialog") -> None:
        """Applies whichever of Name/Category-Sub-Category (Program/Combi) or Set-List-slot
        Name/Color/Comments the user actually changed in the Properties dialog. Program/Combi
        combine name+category into ONE write (LocalEditOps.EditProperties's own single-call
        semantics — a single `changes` list, single RecordEdit). Set List is two INDEPENDENT
        writes if both a rename and a slot edit happened — matching EditProperties and
        EditSetListSlot being two separate LocalEditOps calls in the C# source, the second
        reading whatever body the first one just wrote (see module docstring). One undo
        scope per dialog Accept (one user gesture = one Ctrl+Z)."""
        scope = self._undo.begin(f"Property edit on {loc.label()}")
        if obj_type == OBJ_SET_LIST:
            if dlg.new_name and dlg.new_name != current_name:
                entry = self._index.get(loc.obj_type, loc.bank, loc.number)
                body = self._blobs.get(entry.current_hash) if entry is not None else None
                if body is not None:
                    new_body = object_body.write_setlist_name(body, dlg.new_name)
                    self._write_local_body_edit(loc, new_body, f'name to "{dlg.new_name}"')
            if dlg.edited_slot is not None:
                slot_number, slot_name, slot_color, slot_comments = dlg.edited_slot
                entry = self._index.get(loc.obj_type, loc.bank, loc.number)
                body = self._blobs.get(entry.current_hash) if entry is not None else None
                if body is not None:
                    new_body = object_body.write_setlist_slot_name(body, slot_number, slot_name)
                    new_body = object_body.write_setlist_slot_color(new_body, slot_number, slot_color)
                    new_body = object_body.write_setlist_slot_comments(new_body, slot_number, slot_comments)
                    self._write_local_body_edit(
                        loc, new_body,
                        f'slot {slot_number} name to "{slot_name}", slot {slot_number} color '
                        f"to {slot_color}, slot {slot_number} comments")
            self._refresh_local_tree()
            if scope is not None:
                scope.dispose()
            return

        entry = self._index.get(loc.obj_type, loc.bank, loc.number)
        body = self._blobs.get(entry.current_hash) if entry is not None else None
        if body is None:
            if scope is not None:
                scope.dispose()
            return
        new_body = body
        changes: List[str] = []
        if dlg.new_name and dlg.new_name != current_name:
            writer = (object_body.write_program_name if obj_type == OBJ_PROGRAM
                     else object_body.write_combi_name)
            new_body = writer(new_body, dlg.new_name)
            changes.append(f'name to "{dlg.new_name}"')
        if dlg.new_category is not None:
            cat, sub = dlg.new_category
            reader = (object_body.parse_program_body if obj_type == OBJ_PROGRAM
                     else object_body.parse_combi_body)
            original = reader(body)
            if cat != original.category or sub != original.sub_category:
                writer = (object_body.write_program_category if obj_type == OBJ_PROGRAM
                         else object_body.write_combi_category)
                new_body = writer(new_body, cat, sub)
                changes.append(f"category to {cat}/{sub}")
        if not changes:
            if scope is not None:
                scope.dispose()
            return
        self._write_local_body_edit(loc, new_body, ", ".join(changes))
        self._refresh_local_tree()
        if scope is not None:
            scope.dispose()

    def _build_local_catalog(self) -> LibraryCatalog:
        cat = LibraryCatalog()
        for key, entry in self._index.entries.items():
            obj_type, bank, number = _parse_key(key)
            if obj_type not in (OBJ_COMBI, OBJ_SET_LIST):
                continue
            body = self._blobs.get(entry.current_hash)
            if body is None:
                continue
            dump = ObjectDump(obj_type, bank, number, entry.version, body)
            if obj_type == OBJ_COMBI:
                cat.add_combi(dump)
            else:
                cat.add_setlist(dump)
        return cat

    def _swap_local_at(self, src: ObjLoc, dst: ObjLoc) -> bool:
        """The actual Local<->Local swap — factored out so the
        Local pane's drag-drop handler (_handle_local_to_local_drop) AND the Cut clipboard's
        paste (_paste_local_cut) can call the exact same logic with a computed destination
        instead of a dialog-chosen one. Returns True iff the swap actually wrote — the Cut
        clipboard needs this to know whether it's safe to clear itself (matches
        PasteSingle/FinishPaste's own "only clear on confirmed success" semantics)."""
        src_entry = self._index.get(src.obj_type, src.bank, src.number)
        if src_entry is None:
            self._log(f"Swap aborted: no local content at {src.label()}.")
            return False
        if src == dst:
            self._log("Source and destination are the same location.")
            return False
        dst_entry = self._index.get(dst.obj_type, dst.bank, dst.number)
        if dst_entry is None:
            QMessageBox.information(self, "Swap", f"{dst.label()} has no local content yet — "
                                    "place something there first (e.g. from the Merge Window) "
                                    "before swapping.")
            return False
        scope = self._undo.begin(f"Swap {src.label()} ↔ {dst.label()}")
        src_body = self._blobs.get(src_entry.current_hash)
        dst_body = self._blobs.get(dst_entry.current_hash)
        if src_body is None or dst_body is None:
            self._log("Swap aborted: missing blob content for source or destination.")
            return False

        catalog = self._build_local_catalog()
        src_dump = ObjectDump(src.obj_type, src.bank, src.number, src_entry.version, src_body)
        dst_dump = ObjectDump(dst.obj_type, dst.bank, dst.number, dst_entry.version, dst_body)
        plan = plan_move(catalog, src, src_dump, dst, dst_dump)
        for line in plan.preview:
            self._log("  " + line)
        for w in plan.warnings:
            self._log("  ! " + w)
        if plan.is_refusable:
            self._log("Swap refused (see warnings above).")
            return False

        now = _now_iso()
        targets = []
        for w in plan.writes:
            key = LocalLibraryIndex.key(w.obj, w.bank, w.index)
            existing = self._index.entries.get(key)
            new_hash = self._blobs.put(w.body)
            name = ksx._ascii_trim(w.body, 0, 24)
            new_entry = _advance_entry_after_write(existing, new_hash, name, w.version, now)
            self._index.set_entry(w.obj, w.bank, w.index, new_entry)
            targets.append({"obj_type": w.obj, "bank": w.bank, "number": w.index, "result_hash": new_hash})
        self._oplog.append({
            "id": str(uuid.uuid4()), "timestamp_utc": now, "op_kind": "Move",
            "targets": targets, "description": f"Swapped {src.label()} <-> {dst.label()}",
            "sync_batch_id": None, "synced_at_utc": None,
        })
        self._index.save()
        self._log(f"Swapped {src.label()} <-> {dst.label()} (staged locally, pending Sync/Commit).")
        self._refresh_local_tree()
        if scope is not None:
            scope.dispose()
        return True

    def _stage_local_selected_to_merge(self) -> None:
        """Requirement 2 / MergePaneViewModel.cs's PullFromLocal — stage a Local Library
        object (and its dependencies, transitively) back into the Merge Window so it can be
        rearranged and pushed to a different destination. Composes MergeCache's existing
        pull_recursive against a LOCAL resolve_content/source instead of a PCG one — no new
        merge_cache.py API needed (see module docstring)."""
        payload = self._leaf_payload(self._tree_local)
        if payload is None or payload[0] != "local":
            self._log("Select exactly one Local Library item to stage for a batch placement.")
            return
        _, obj_type, bank, number = payload
        scope = self._undo.begin(f"Pulled {ObjLoc(obj_type, bank, number).label()} into Merge Window")
        added, gaps = self._merge.pull_recursive(
            (obj_type, bank, number), self._local_resolve_content, self._resolve_refs,
            source=MergeCache.LOCAL_SOURCE_LABEL)
        self._log(f"Staged into Merge Window: {len(added)} new item(s), {len(gaps)} unresolved "
                 f"reference(s).")
        for addr, kind in gaps:
            self._log(f"  gap: obj {addr[0]:02X} bank {addr[1]:02X} idx {addr[2]} ({kind})")
        self._refresh_merge_tree()
        if scope is not None:
            scope.dispose()

    def _clear_changes(self) -> None:
        scope = self._undo.begin("Clear pending changes")
        targets = []
        for key, entry in self._index.entries.items():
            if entry.is_dirty or entry.pending_delete or entry.conflicted:
                obj_type, bank, number = _parse_key(key)
                # Reinstall through set_entry so the undo recorder's slot_mutating_cb
                # captures the pre-state (one Ctrl+Z restores every reverted slot).
                restored = LocalIndexEntry(
                    version=entry.version, baseline_hash=entry.baseline_hash,
                    current_hash=entry.baseline_hash, display_name=entry.display_name,
                    created_utc=entry.created_utc, modified_utc=_now_iso(),
                    conflicted=False, has_resolved_dependencies=entry.has_resolved_dependencies,
                    is_exi=entry.is_exi, pending_delete=False)
                self._index.set_entry(obj_type, bank, number, restored)
                targets.append({"obj_type": obj_type, "bank": bank, "number": number,
                                "result_hash": entry.baseline_hash})
        if not targets:
            self._log("Nothing to clear.")
            return
        self._oplog.append({
            "id": str(uuid.uuid4()), "timestamp_utc": _now_iso(), "op_kind": "Discard",
            "targets": targets, "description": f"Reverted {len(targets)} pending change(s)",
            "sync_batch_id": None, "synced_at_utc": None,
        })
        self._index.save()
        self._log(f"Cleared {len(targets)} pending change(s).")
        self._refresh_local_tree()
        if scope is not None:
            scope.dispose()

    # ── Local pane: Cut / Copy / Paste (batch_clipboard.BatchClipboard) ─────────

    def _selected_local_object_locs(self) -> List[ObjLoc]:
        out: List[ObjLoc] = []
        for p in self._tree_local.leaf_payloads():
            if p and p[0] == "local":
                _, obj_type, bank, number = p
                out.append(ObjLoc(obj_type, bank, number))
        return out

    def _local_dump_of(self, loc: ObjLoc) -> Optional[ObjectDump]:
        """BatchClipboard's DumpOf callable — the existence-only staleness re-check
        cut()/paste_swap_target()/paste_targets() all use (see batch_clipboard.py's own
        module docstring: no content-hash gate, just "does this still resolve locally")."""
        entry = self._index.get(loc.obj_type, loc.bank, loc.number)
        if entry is None:
            return None
        body = self._blobs.get(entry.current_hash)
        if body is None:
            return None
        return ObjectDump(loc.obj_type, loc.bank, loc.number, entry.version, body)

    def _cut_local_selected(self) -> None:
        locs = self._selected_local_object_locs()
        if len(locs) > 1:
            QMessageBox.information(self, "Cut", "Cut only works on exactly one item at a "
                                    "time — select a single Local Library entry, or use Copy "
                                    "for multiple.")
        ok, msg = self._batch_clip.cut(locs, self._local_dump_of)
        self._log(msg if ok else f"Cut: {msg}")

    def _copy_local_selected(self) -> None:
        locs = self._selected_local_object_locs()
        ok, msg = self._batch_clip.copy(locs, self._local_dump_of)
        self._log(msg if ok else f"Copy: {msg}")

    def _rename_local_selected(self) -> None:
        """Rename the single selected Local Library entry (F2 / toolbar / context menu) -
        port of LocalLibraryPaneViewModel.Rename + LocalEditOps.Rename. Writes the new
        name into the body's own name bytes via object_body.py (the same body-write path
        Properties uses), so the display name never desyncs from what Sync/Commit pushes."""
        payload = self._leaf_payload(self._tree_local)
        if payload is None or payload[0] != "local":
            self._local_status_label.setText("Select exactly one Local Library item to rename.")
            return
        _, obj_type, bank, number = payload
        entry = self._index.get(obj_type, bank, number)
        if entry is None:
            return
        loc = ObjLoc(obj_type, bank, number)
        from PySide6.QtWidgets import QInputDialog
        new_name, ok = QInputDialog.getText(self, "Rename", f"Rename {loc.label()}:",
                                            text=entry.display_name or "")
        if not ok or not new_name.strip() or new_name.strip() == (entry.display_name or ""):
            return
        body = self._blobs.get(entry.current_hash)
        if body is None:
            return
        writer = (object_body.write_program_name if obj_type == OBJ_PROGRAM
                  else object_body.write_combi_name if obj_type == OBJ_COMBI
                  else object_body.write_setlist_name)
        new_body = writer(body, new_name.strip())
        self._write_local_body_edit(loc, new_body, f'name to "{new_name.strip()}"')
        self._local_status_label.setText(
            f"Renamed {loc.label()} to \"{new_name.strip()}\"")
        self._refresh_local_tree()

    def _delete_dependency_warning(self, locs: List[ObjLoc]) -> bool:
        """Port of ConfirmDeleteDependency: warn before deleting something other Combis/
        Set Lists depend on, listing up to 8 referrers. Returns True to proceed."""
        dependents: List[str] = []
        catalog = self._build_local_catalog()
        for loc in locs:
            for ref in catalog.referrers_of(loc):
                dependents.append(
                    f"  - {loc.label()} - used by {ObjLoc(ref.ref_obj, ref.ref_bank, ref.ref_index).label()}")
        if not dependents:
            return True
        lines = dependents[:8]
        if len(dependents) > 8:
            lines.append(f"  ... and {len(dependents) - 8} more")
        box = QMessageBox(self)
        box.setWindowTitle("Delete a dependency?")
        box.setText("This object is used by other Combis/Set Lists. Deleting it may leave "
                    "their references unresolved.\n\n" + "\n".join(lines))
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    def _toggle_delete_local_selected(self) -> None:
        """Port of LocalLibraryPaneViewModel.ToggleDelete/ToggleDeleteMany - "Delete" marks
        the selected item(s) pending-delete (fading in place); calling again on an already-
        pending item Restores it. Local-only: hardware is untouched until Sync/Commit.
        Each mutation goes through index.set_entry so the undo recorder's slot_mutating_cb
        captures the pre-state (one Ctrl+Z restores the edit AND the flag together)."""
        payloads = [p for p in self._selected_payloads(self._tree_local) if p[0] == "local"]
        if not payloads:
            self._local_status_label.setText("Select one or more Local Library items to delete/restore.")
            return
        # Skip read-only GM/g rows (browse-only, never writable).
        editable = [p for p in payloads
                    if not (p[1] == OBJ_PROGRAM and p[2] in _READONLY_PROGRAM_BANKS)]
        if not editable:
            self._local_status_label.setText("Read-only bank - cannot delete.")
            return
        locs = [ObjLoc(p[1], p[2], p[3]) for p in editable]
        restoring = all(self._index.get(l.obj_type, l.bank, l.number) is not None
                        and self._index.get(l.obj_type, l.bank, l.number).pending_delete
                        for l in locs)
        if not restoring and not self._delete_dependency_warning(locs):
            return
        count_noun = f"{len(locs)} item(s)" if len(locs) > 1 else locs[0].label()
        scope = self._undo.begin(("Restore " if restoring else "Delete ") + count_noun)
        ok = 0
        now = _now_iso()
        for loc in locs:
            entry = self._index.get(loc.obj_type, loc.bank, loc.number)
            if entry is None:
                continue
            if not restoring:
                # Abandon any pending edit first (Discard semantics), then stage a real
                # blank body as current_hash — the instrument's own captured blank
                # template if one exists, else erase_body's derived reset-to-INIT/
                # empty-Set-List fallback. Matches C#'s ChangesetBuilder preference
                # order ("BlankTemplates.EnsureAsync ?? EraseBody.Build") — the Kronos
                # protocol has no delete opcode, so a slot must be overwritten with
                # something recognizably blank, never left as its original content.
                blank_body = self._blank_templates.blank_body_for(loc.obj_type, entry.is_exi)
                if blank_body is None:
                    original_body = self._blobs.get(entry.baseline_hash)
                    if original_body is not None:
                        blank_body = erase_body.build(loc.obj_type, original_body)
                blank_hash = (self._blobs.put(blank_body) if blank_body is not None
                             else entry.baseline_hash)
                entry = LocalIndexEntry(
                    version=entry.version, baseline_hash=entry.baseline_hash,
                    current_hash=blank_hash, display_name=entry.display_name,
                    created_utc=entry.created_utc, modified_utc=now,
                    conflicted=False, has_resolved_dependencies=entry.has_resolved_dependencies,
                    is_exi=entry.is_exi, pending_delete=True)
            else:
                # Restore: undo the blank-body staging above, back to the real content.
                entry = LocalIndexEntry(
                    version=entry.version, baseline_hash=entry.baseline_hash,
                    current_hash=entry.baseline_hash, display_name=entry.display_name,
                    created_utc=entry.created_utc, modified_utc=now,
                    conflicted=entry.conflicted,
                    has_resolved_dependencies=entry.has_resolved_dependencies,
                    is_exi=entry.is_exi, pending_delete=False)
            self._index.set_entry(loc.obj_type, loc.bank, loc.number, entry)
            ok += 1
        if ok:
            self._oplog.append({
                "id": str(uuid.uuid4()), "timestamp_utc": now,
                "op_kind": "Delete",
                "targets": [{"obj_type": l.obj_type, "bank": l.bank, "number": l.number,
                             "result_hash": _current_hash_or_empty(self, l)} for l in locs],
                "description": (f"Marked {len(locs)} item(s) for deletion" if not restoring
                                else f"Restored {len(locs)} item(s)"),
                "sync_batch_id": None, "synced_at_utc": None,
            })
            self._index.save()
            self._local_status_label.setText(
                f"Marked {len(locs)} item(s) for deletion (pending Sync/Commit)." if not restoring
                else f"Restored {len(locs)} item(s).")
        self._refresh_local_tree()
        if scope is not None:
            scope.dispose()

    def _paste_local_selected(self) -> None:
        """Paste always prompts the same `_DestinationDialog` bank+number picker every other
        placement action in this file already uses — a deliberate simplification vs
        PasteIntoSlot/PasteIntoBank's "target whatever tree node is right-clicked" split (see
        module docstring): one paste UX, still able to reach any real destination."""
        if self._batch_clip.is_empty:
            self._log("Nothing to paste — Cut or Copy something first.")
            return
        obj_type = self._batch_clip.entries[0].loc.obj_type
        dlg = _DestinationDialog(obj_type, "Paste at…", self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        dest_bank, dest_number = dlg.selected()
        if self._batch_clip.mode == ClipboardMode.CUT:
            self._paste_local_cut(ObjLoc(obj_type, dest_bank, dest_number))
        else:
            self._paste_local_copy(obj_type, dest_bank, dest_number)

    def _paste_local_cut(self, dest: ObjLoc) -> None:
        src, err = self._batch_clip.paste_swap_target(dest, self._local_dump_of)
        if src is None:
            self._log(f"Paste: {err}")
            return
        # paste_swap_target() never clears the clipboard itself (mirrors FinishPaste, which
        # only clears on a confirmed-successful apply) — clear it here only if _swap_local_at
        # actually wrote something.
        if self._swap_local_at(src, dest):
            self._batch_clip.clear()

    def _paste_local_copy(self, obj_type: int, dest_bank: int, start_slot: int) -> None:
        placed, pending = self._batch_clip.paste_targets(
            obj_type, dest_bank, start_slot, self._local_dump_of,
            bank_type_of=self._local_bank_type_of)
        if not placed:
            self._log("Paste: nothing placeable.")
            for entry, reason in pending:
                self._log(f"  pending: {entry.loc.label()} — {reason}")
            return
        ok = self._apply_batch_placements(
            placed, obj_type, "Place",
            f"Pasted {len(placed)} item(s) starting at "
            f"{ObjLoc(obj_type, dest_bank, start_slot).label()}")
        if not ok:
            self._log("Paste refused (see warnings above).")
            return
        self._log(f"Pasted {len(placed)} item(s); {len(pending)} left pending.")
        for entry, reason in pending:
            self._log(f"  pending: {entry.loc.label()} — {reason}")

    def _show_local_context_menu(self, local_pos) -> None:
        menu = QMenu(self)
        item = self._tree_local.itemAt(local_pos)
        payload = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        is_leaf = payload is not None and payload[0] == "local"
        is_bank = payload is not None and payload[0] == "local_bank"

        a_cut = menu.addAction("Cut", self._cut_local_selected)
        a_copy = menu.addAction("Copy", self._copy_local_selected)
        a_paste = menu.addAction("Paste", self._paste_local_selected)
        a_paste.setEnabled(not self._batch_clip.is_empty)
        menu.addSeparator()
        a_move = menu.addAction("Move to Merge Window", self._stage_local_selected_to_merge)
        a_move.setToolTip("Stage this (and its dependencies) in the Merge Window to rearrange "
                          "and push it somewhere else.")
        if is_leaf and payload[1] in (OBJ_COMBI, OBJ_SET_LIST):
            a_scan = menu.addAction("Scan PCG for dependencies...", self._scan_dependencies_for_selected)
            a_scan.setToolTip("Pick a .pcg file and stage whatever it holds of this object's "
                              "missing dependencies into the Merge Window.")
        menu.addSeparator()
        a_rename = menu.addAction("Rename...", self._rename_local_selected)
        a_props = menu.addAction("Properties...", self._show_local_properties)
        menu.addSeparator()
        a_delete = menu.addAction("Delete", self._toggle_delete_local_selected)
        # Restore label when every selected item is already pending-delete (C# parity).
        selected = self._selected_payloads(self._tree_local)
        if selected and all(
                (p[0] == "local"
                 and self._index.get(p[1], p[2], p[3]) is not None
                 and self._index.get(p[1], p[2], p[3]).pending_delete)
                for p in selected):
            a_delete.setText("Restore")

        if is_bank:
            # Bank nodes: Cut/Copy/Delete/Move act on every leaf inside (SelectedLocs
            # expansion in C#); Rename/Properties are leaf-only.
            a_rename.setEnabled(False)
            a_props.setEnabled(False)
        if not is_leaf and not is_bank:
            # Type-root header or empty area - no object actions.
            a_cut.setEnabled(False)
            a_copy.setEnabled(False)
            a_rename.setEnabled(False)
            a_delete.setEnabled(False)

        if is_bank and payload[1] == OBJ_PROGRAM:
            bank = payload[2]
            menu.addSeparator()
            if self._index.get_pending_bank_type_change(bank) is None:
                menu.addAction("Stage Bank Conversion...", lambda: self._stage_bank_type_change(bank))
            else:
                menu.addAction("Unstage Bank Conversion", lambda: self._unstage_bank_type_change(bank))
        menu.exec(self._tree_local.viewport().mapToGlobal(local_pos))

    def _show_merge_context_menu(self, local_pos) -> None:
        """Port of MergeNodeTemplate's context menu (MI_RemoveFromMerge) — Remove is a
        right-click action on the Merge tree in C#, not a toolbar button."""
        item = self._tree_merge.itemAt(local_pos)
        if item is None:
            return
        menu = QMenu(self)
        menu.addAction("Remove", self._remove_merge_selected)
        menu.exec(self._tree_merge.viewport().mapToGlobal(local_pos))

    # ── Local pane: staged whole-bank HD-1/EXi conversion (requirement 3) ────────
    # A standalone Stage/Unstage action, not the C# source's own "side effect of a specific
    # Merge->Local whole-Program-bank placement" trigger — see module docstring for why.

    def _scan_dependencies_for_selected(self) -> None:
        """Port of ScanPcgForDependencies: pick a .pcg and stage whatever it holds of the
        selected Combi/Set List's missing dependencies into the Merge Window."""
        payload = self._leaf_payload(self._tree_local)
        if payload is None or payload[0] != "local" or payload[1] == OBJ_PROGRAM:
            return
        _, obj_type, bank, number = payload
        entry = self._index.get(obj_type, bank, number)
        if entry is None:
            return
        body = self._blobs.get(entry.current_hash)
        if body is None:
            return
        missing = [r for r in depscan.walk_object_references(obj_type, body)
                   if not self._local_resolver(r.ref.obj_type, r.ref.bank, r.ref.number)]
        if not missing:
            self._local_status_label.setText("Nothing missing - every dependency already resolves.")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Scan a PCG for missing dependencies", "", "Korg PCG Files (*.pcg *.PCG)")
        if not path:
            return
        try:
            data = open(path, "rb").read()
        except OSError as e:
            QMessageBox.warning(self, "Scan PCG", f"Could not read {path}: {e}")
            return
        pcg = open_pcg(data)
        if pcg is None:
            QMessageBox.warning(self, "Scan PCG", f"{os.path.basename(path)} is not a "
                                "recognizable Kronos .pcg file.")
            return
        wanted = {(r.ref.obj_type, r.ref.bank, r.ref.number) for r in missing}
        found = 0
        for e in pcg.objects:
            addr = (e.obj_type, e.bank.obj_bank if e.bank is not None else 0, e.index)
            if addr in wanted:
                self._pull_pcg_address_into_merge(addr)
                found += 1
        if found:
            self._local_status_label.setText(
                f"Found {found} of {len(missing)} missing dependency(ies) in "
                f"{os.path.basename(path)} - staged in the Merge Window, ready to place.")
        else:
            self._local_status_label.setText(
                f"{os.path.basename(path)} has none of the {len(missing)} missing "
                "dependency(ies) - try another PCG.")

    def _stage_bank_type_change(self, bank: int) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("Stage Bank Conversion")
        box.setText(
            f"Stage {ksx.program_label(bank)} for a whole-bank HD-1/EXi format conversion?\n\n"
            "This queues a func-0x7C Change Program Bank Type for the next Sync/Commit. On "
            "the instrument, 0x7C REFORMATS AND ERASES the ENTIRE bank — every Program slot "
            "in it, not just the ones you've edited locally — right before this push's "
            "Program writes for that bank land. There is no undo once it executes on "
            "hardware.")
        btn_exi = box.addButton("Convert to EXi", QMessageBox.ButtonRole.AcceptRole)
        btn_hd1 = box.addButton("Convert to HD-1", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked is btn_exi:
            to_exi = True
        elif clicked is btn_hd1:
            to_exi = False
        else:
            return
        self._index.set_pending_bank_type_change(bank, to_exi)
        self._index.save()
        self._log(f"Staged {ksx.program_label(bank)} for conversion to "
                 f"{'EXi' if to_exi else 'HD-1'} (func 0x7C reformats + erases the whole "
                 "bank at next Sync/Commit).")
        self._refresh_local_tree()

    def _unstage_bank_type_change(self, bank: int) -> None:
        self._index.clear_pending_bank_type_change(bank)
        self._index.save()
        self._log(f"Unstaged the pending bank conversion for {ksx.program_label(bank)}.")
        self._refresh_local_tree()

    # ── Merge pane actions ───────────────────────────────────────────────────

    def _show_merge_properties(self) -> None:
        payload = self._leaf_payload(self._tree_merge)
        if payload is None or payload[0] != "merge":
            return
        entry = self._merge.try_get(payload[1])
        if entry is None:
            return
        loc = ObjLoc(entry.obj_type, 0, 0)
        extra = [f"Content hash: {entry.content_hash}",
                 f"Referenced by {len(entry.referenced_by)} other staged item(s)"
                 if entry.referenced_by else "Not referenced by anything else staged.",
                 "Unresolved dependencies." if entry.has_unresolved_dependencies else "All dependencies resolved."]
        obj_extra, list_rows = self._object_body_readout(entry.obj_type, entry.body)
        dlg = _PropertiesDialog(f"Merge entry — {entry.display_name or entry.content_hash[:8]}",
                                entry.display_name, editable_name=False,
                                location=f"Type: {_ROOT_LABEL.get(entry.obj_type, str(entry.obj_type))}",
                                flag_lines=[], extra_lines=extra + obj_extra, list_rows=list_rows,
                                parent=self)
        dlg.exec()

    def _place_merge_entry_at(self, entry: MergeEntry, dst_bank: int, dst_number: int) -> None:
        """The actual Merge->Local single-item placement — reachable from the Merge
        pane's drag-drop handler (_handle_merge_to_local_drop, a drop-computed
        destination) and from Auto-Fill (_auto_fill_to_library, a scanned free slot).
        No per-item unresolved-dependency prompt (req 9): matches the C# source, whose
        placement paths never gate on missing dependencies at placement time — anything
        unresolved is tracked to the session clipboard and only surfaces at Sync/Commit."""
        dst = ObjLoc(entry.obj_type, dst_bank, dst_number)
        scope = self._undo.begin(f"Place {entry.display_name or entry.content_hash[:8]} at {dst.label()}")

        # Duplicate-content guard (port of LibrarianShellViewModel.FindExistingLocalCopy):
        # if this entry's OWN content already sits somewhere in Local Library byte-identical,
        # reuse that copy instead of writing a second one — unless the entry was itself staged
        # from Local (nothing to dedup against), or the per-type preserve-duplication policy is
        # on (Settings > Librarian; also mirrored as quick toggles in the Merge Window toolbar
        # in the C# source). Programs dedup by default, Combis copy as-is.
        was_staged_from_local = any(o.source == MergeCache.LOCAL_SOURCE_LABEL for o in entry.origins)
        preserve = (entry.obj_type == OBJ_PROGRAM and self._settings.merge_preserve_duplicate_programs) or \
                   (entry.obj_type == OBJ_COMBI and self._settings.merge_preserve_duplicate_combis)
        if not was_staged_from_local and not preserve:
            existing_loc = self._index.find_by_content_hash(entry.obj_type, entry.content_hash)
            # A Combi's raw content holds the SOURCE (PCG) addresses for its timbres, so the
            # raw hash almost never matches a previously-placed copy — that copy was repointed
            # at where its Programs actually landed before being written. Comparing the
            # RESOLVED body (references repointed at local reality, exactly as placement itself
            # would write it) is what makes "scan duplicates and reuse" actually fire for the
            # re-copied-PCG case (port of LibrarianShellViewModel.FindExistingLocalCopy).
            if existing_loc is None and entry.obj_type == OBJ_COMBI and entry.ref_sites:
                resolved_probe, _ = self._merge.resolve_references_for_placement(entry, self._local_lookup)
                resolved_probe_hash = BlobStore.compute_hash(resolved_probe)
                if resolved_probe_hash != entry.content_hash:
                    existing_loc = self._index.find_by_content_hash(entry.obj_type, resolved_probe_hash)
            if existing_loc is not None and existing_loc != (dst_bank, dst_number):
                self._merge.mark_placed(entry.content_hash, (entry.obj_type, *existing_loc))
                self._merge.remove(entry.content_hash)
                self._log(f"Reused existing content already at "
                         f"{ObjLoc(entry.obj_type, *existing_loc).label()} "
                         f"(no duplicate written).")
                self._refresh_local_tree()
                self._refresh_merge_tree()
                if scope is not None:
                    scope.dispose()
                return

        # Patch whatever of this entry's OWN dependency references resolve — either because
        # the dependency was ALSO placed via Merge this session (_placed_addresses), or
        # because it already exists ANYWHERE in Local Library (self._local_lookup, by
        # content) — the many-to-one dedup payoff, generalized beyond just this-session Merge
        # placements. Anything still unresolved is tracked below for a later retry. Port of
        # LibrarianShellViewModel.PlaceFromMerge's MergePane.ResolveReferencesForPlacement call.
        write_body, unresolved = self._merge.resolve_references_for_placement(entry, self._local_lookup)
        # No per-placement unresolved-dependency dialog (req 9): matches the C# source, whose
        # PlaceFromMerge/PlaceMergeGroupSequentially never prompt at placement time - anything
        # still unresolved is tracked to the session clipboard below and only surfaces as the
        # blocking notice at Sync/Commit, by which point a later pull may have resolved it.
        # Orphan gate for the single-item path (port of PlaceFromMerge's ForceOverwrite
        # wiring into LocalEditOps.PlaceObject -> BatchPlace -> PlanBatchMove): overwriting
        # a slot still referenced by another local Combi/Set List refuses unless Force
        # Overwrite is checked (the referrer(s) then resolve to the NEW object instead).
        existing = self._index.get(entry.obj_type, dst_bank, dst_number)
        if existing is not None and existing.current_hash != entry.content_hash:
            occupied_body = self._blobs.get(existing.current_hash)
            if occupied_body is not None and not self._is_init_body(entry.obj_type, occupied_body):
                catalog = self._build_local_catalog()
                displaced_refs = catalog.referrers_of(dst)
                if displaced_refs:
                    if not self._chk_force_overwrite.isChecked():
                        self._log(f"REFUSE: {dst.label()} is referenced by "
                                  f"{len(displaced_refs)} object(s) and would be overwritten "
                                  "- check Force Overwrite to place it anyway.")
                        if scope is not None:
                            scope.dispose()
                        return
                    self._log(f"CHECK: {dst.label()} was referenced by {len(displaced_refs)} "
                              "object(s) - Force Overwrite placed it anyway, so those "
                              "referrer(s) now resolve to the NEW object instead of the old one.")

        write_hash = self._blobs.put(write_body)
        now = _now_iso()
        new_entry = _advance_entry_after_write(existing, write_hash, entry.display_name,
                                               entry.version, now)
        new_entry.has_resolved_dependencies = not unresolved
        self._index.set_entry(entry.obj_type, dst_bank, dst_number, new_entry)
        self._oplog.append({
            "id": str(uuid.uuid4()), "timestamp_utc": now, "op_kind": "Place",
            "targets": [{"obj_type": entry.obj_type, "bank": dst_bank, "number": dst_number,
                        "result_hash": write_hash}],
            "description": f"Placed '{entry.display_name}' at {dst.label()}",
            "sync_batch_id": None, "synced_at_utc": None,
        })
        self._index.save()
        self._merge.mark_placed(entry.content_hash, (entry.obj_type, dst_bank, dst_number))
        self._merge.remove(entry.content_hash)

        for s in unresolved:
            self._clipboard.add(SessionDependencyEntry(
                missing_ref=ObjLoc(*s.target_address), ref_kind=s.ref_kind, site=s.site,
                required_by=dst, expected_content_hash=s.resolved_content_hash))

        self._log(f"Placed '{entry.display_name}' at {dst.label()} "
                 f"(staged locally, pending Sync/Commit).")
        self._refresh_local_tree()
        self._refresh_merge_tree()
        if scope is not None:
            scope.dispose()

    def _auto_fill_to_library(self) -> None:
        """Port of AutoFillToLibraryAsync/AutoFillFromMergeAsync: places every item
        currently staged in the Merge Window into the next free Local Library slot of
        its own type - Programs first, then Combis, then Set Lists (_ROOT_ORDER), so
        a Combi/Set List's dependencies are already placed by the time it's Auto-
        Filled and its references resolve to where they actually landed. Reuses
        _place_merge_entry_at per item, which already runs resolve_references_for_
        placement, the duplicate-content dedup guard, and its own (no-op-when-nested)
        undo scope.

        Runs as a chunked QTimer state machine instead of one blocking loop (req 10 /
        10a): each tick places a few items then returns to the event loop, so the UI
        stays responsive and the button's indeterminate progress bar (req 10a) actually
        paints and animates instead of the whole sweep landing as one frozen block -
        the Qt equivalent of the C# source's Dispatcher.Yield(Background) pump."""
        total_staged = len(self._merge.entries)
        if total_staged == 0:
            self._log("Auto-Fill: nothing staged in the Merge Window.")
            return
        if self._busy or getattr(self, "_auto_fill_active", False):
            return   # matches CanAutoFill() => !IsBusy && !IsAutoFilling in the C# source
        self._auto_fill_active = True
        self._auto_fill_progress_effect.setOpacity(0.35)
        self._auto_fill_label.setText("Filling...")
        self._refresh_enable()
        scope = self._undo.begin(f"Auto-Fill {total_staged} item(s) to Local Library")
        self._auto_fill_scope = scope
        self._auto_fill_placed = 0
        self._auto_fill_skipped = 0
        self._auto_fill_queue: List[MergeEntry] = []
        for obj_type in _ROOT_ORDER:
            # A snapshot per type - _place_merge_entry_at mutates self._merge as it goes
            # (removes on write, or on a dedup-reuse), so this list must not be a live view.
            self._auto_fill_queue.extend(
                e for e in self._merge.entries if e.obj_type == obj_type)
        self._auto_fill_total = len(self._auto_fill_queue)
        self._auto_fill_timer.start()
        self._merge_status_label.setText(
            f"Auto-Filling: 0/{self._auto_fill_total} placed...")

    def _auto_fill_tick(self) -> None:
        """One chunk of the Auto-Fill sweep - places up to `_AUTO_FILL_CHUNK` items per
        tick, updates the status/progress, and stops when the queue is empty.

        Local/Merge tree rebuilds are suppressed for the duration of the chunk (see
        `_suppress_tree_refresh`'s docstring at its declaration) and done ONCE at the
        end of the tick instead of once per placed item - one rebuild per TICK
        (ceil(total/chunk) for the whole sweep) instead of one per placed item.
        Verified empirically: a 10-item sweep at chunk=4 does 3 real rebuilds, not 10
        (see the scratch-rooted test harness this fix was checked against)."""
        chunk = getattr(self, "_AUTO_FILL_CHUNK", 4)
        self._suppress_tree_refresh = True
        try:
            for _ in range(chunk):
                if not self._auto_fill_queue:
                    break
                entry = self._auto_fill_queue.pop(0)
                if self._merge.try_get(entry.content_hash) is None:
                    continue   # already resolved as a side effect of an earlier item this sweep
                dst = self._find_first_free_slot_any_bank(entry.obj_type)
                if dst is None:
                    self._log(f"Auto-Fill: no free {_ROOT_LABEL.get(entry.obj_type, str(entry.obj_type))} "
                              f"slot for '{entry.display_name or entry.content_hash[:8]}'.")
                    self._auto_fill_skipped += 1
                    continue
                self._place_merge_entry_at(entry, dst[0], dst[1])
                # Outcome, not intent: there's no cancel path in Auto-Fill, but count from
                # what actually happened rather than assuming the call succeeded.
                if self._merge.try_get(entry.content_hash) is None:
                    self._auto_fill_placed += 1
                else:
                    self._auto_fill_skipped += 1
                self._merge_status_label.setText(
                    f"Auto-Filling: {self._auto_fill_placed}/{self._auto_fill_total} placed...")
        finally:
            self._suppress_tree_refresh = False
        self._refresh_local_tree()
        self._refresh_merge_tree()
        if not self._auto_fill_queue:
            self._auto_fill_finish()

    def _auto_fill_finish(self) -> None:
        self._auto_fill_timer.stop()
        self._auto_fill_active = False
        self._auto_fill_progress_effect.setOpacity(0.0)
        self._auto_fill_label.setText("Auto-Fill to Library")
        self._refresh_enable()
        scope = self._auto_fill_scope
        self._auto_fill_scope = None
        placed = self._auto_fill_placed
        skipped = self._auto_fill_skipped
        self._merge_status_label.setText(
            f"Auto-Fill: placed {placed} item(s)"
            + (f", {skipped} skipped (see History above)." if skipped else "."))
        self._log(f"Auto-Fill: placed {placed} item(s)"
                  + (f", {skipped} skipped (see History above)." if skipped else "."))
        if scope is not None:
            scope.dispose()

    def _remove_merge_selected(self) -> None:
        payloads = [p for p in self._selected_payloads(self._tree_merge) if p[0] == "merge"]
        if not payloads:
            self._log("Select one or more Merge Window items to remove.")
            return
        removed = sum(1 for p in payloads if self._merge.remove(p[1]))
        self._log("Removed item from the Merge Window." if removed == 1
                 else f"Removed {removed} item(s) from the Merge Window.")
        self._refresh_merge_tree()

    # ── Sequential fill (requirement 5) + Merge/PCG drag-drop shared core ────

    def _seq_item_from_merge(self, entry: MergeEntry) -> Tuple[SequentialFillItem, List[MergeRefSite]]:
        """Same reference-repointing as the single-item _place_merge_entry_at path
        (resolve_references_for_placement) — a multi-select Fill Sequentially/drag batch
        must not write a Combi/Set List's raw PCG-source-pointing body just because it
        went through a different placement pipeline than a single drag."""
        body, unresolved = self._merge.resolve_references_for_placement(entry, self._local_lookup)
        addr = entry.origins[0].address if entry.origins else (entry.obj_type, 0, 0)
        origin = ObjLoc(entry.obj_type, addr[1], addr[2])
        dump = ObjectDump(entry.obj_type, origin.bank, origin.number, entry.version, body)
        label = entry.display_name or entry.content_hash[:8]
        return SequentialFillItem(origin, dump, label), unresolved

    def _seq_item_from_pcg(self, obj_type: int, bank: int, index: int) -> Optional[SequentialFillItem]:
        e = self._pcg_by_addr.get((obj_type, bank, index))
        if e is None:
            return None
        body = self._pcg_resolve_content(obj_type, bank, index)
        if body is None:
            return None
        origin = ObjLoc(obj_type, bank, index)
        dump = ObjectDump(obj_type, bank, index, OBJ_VERSION.get(obj_type, 0), body)
        label = e.name or origin.label()
        return SequentialFillItem(origin, dump, label)

    def _dest_occupants_for(self, placements: List[BatchPlacement]) -> Dict[ObjLoc, ObjectDump]:
        out: Dict[ObjLoc, ObjectDump] = {}
        for p in placements:
            entry = self._index.get(p.dst.obj_type, p.dst.bank, p.dst.number)
            if entry is None:
                continue
            body = self._blobs.get(entry.current_hash)
            if body is None:
                continue
            out[p.dst] = ObjectDump(p.dst.obj_type, p.dst.bank, p.dst.number, entry.version, body)
        return out

    def _apply_batch_placements(self, placed: List[BatchPlacement], obj_type: int,
                                oplog_kind: str, description: str,
                                hash_by_label: Optional[Dict[str, str]] = None,
                                unresolved_by_label: Optional[Dict[str, List[MergeRefSite]]] = None,
                                force_overwrite: bool = False) -> bool:
        """The shared "write a resolved batch of BatchPlacements into the Local Library index
        + OpLog" core — factored out of _run_sequential_fill so the Copy clipboard's paste
        (_paste_local_copy, whose `placed` already came out of batch_clipboard.paste_targets'
        own resolve_sequential_fill call) can apply the exact same plan_batch_move logic
        without re-deriving the placements a second time. Returns False (nothing applied) if
        plan_batch_move refuses the whole batch; the caller decides what to log about that.
        `force_overwrite` is the Merge Window's Force Overwrite checkbox (req 3)."""
        hash_by_label = hash_by_label or {}
        unresolved_by_label = unresolved_by_label or {}
        scope = self._undo.begin(description)
        catalog = self._build_local_catalog()
        occupants = self._dest_occupants_for(placed)
        plan = plan_batch_move(catalog, obj_type, placed, occupants, divert_displaced=False,
                               bank_type_of=self._local_bank_type_of,
                               force_overwrite=force_overwrite)
        for line in plan.preview:
            self._log("  " + line)
        for w in plan.warnings:
            self._log("  ! " + w)
        if plan.is_refusable:
            if scope is not None:
                scope.dispose()
            return False

        now = _now_iso()
        label_by_dst = {p.dst: p.label for p in placed}
        targets = []
        for w in plan.writes:
            key = LocalLibraryIndex.key(w.obj, w.bank, w.index)
            existing = self._index.entries.get(key)
            new_hash = self._blobs.put(w.body)
            dst_loc = ObjLoc(w.obj, w.bank, w.index)
            name = label_by_dst.get(dst_loc) or ksx._ascii_trim(w.body, 0, 24)
            new_entry = _advance_entry_after_write(existing, new_hash, name, w.version, now)
            label = label_by_dst.get(dst_loc)
            unresolved = unresolved_by_label.get(label) if label is not None else None
            if unresolved is not None:
                new_entry.has_resolved_dependencies = not unresolved
                for s in unresolved:
                    self._clipboard.add(SessionDependencyEntry(
                        missing_ref=ObjLoc(*s.target_address), ref_kind=s.ref_kind, site=s.site,
                        required_by=dst_loc, expected_content_hash=s.resolved_content_hash))
            self._index.set_entry(w.obj, w.bank, w.index, new_entry)
            targets.append({"obj_type": w.obj, "bank": w.bank, "number": w.index, "result_hash": new_hash})

        self._oplog.append({
            "id": str(uuid.uuid4()), "timestamp_utc": now, "op_kind": oplog_kind,
            "targets": targets, "description": description,
            "sync_batch_id": None, "synced_at_utc": None,
        })
        self._index.save()

        for p in placed:
            h = hash_by_label.get(p.label)
            if h is not None:
                self._merge.mark_placed(h, (p.dst.obj_type, p.dst.bank, p.dst.number))
                self._merge.remove(h)

        self._refresh_local_tree()
        self._refresh_merge_tree()
        if scope is not None:
            scope.dispose()
        return True

    def _run_sequential_fill(self, seq_items: List[SequentialFillItem], obj_type: int,
                             dest_bank: int, start_slot: int,
                             hash_by_label: Optional[Dict[str, str]] = None,
                             unresolved_by_label: Optional[Dict[str, List[MergeRefSite]]] = None,
                             force_overwrite: bool = False) -> None:
        """Port of resolve_sequential_fill() -> plan_batch_move(), the pipeline behind both
        the "Fill Sequentially…" toolbar buttons and a multi-item Merge->Local drag. Shared
        so neither path duplicates the other's placement logic. `hash_by_label` maps a
        placed item's label back to its Merge content hash (empty/None for PCG sources,
        which have nothing to remove from a cache)."""
        # Auto-fill skips slots that hold real content (INIT placeholders count as free —
        # port of BatchMoveModel.ResolveSequentialFill's slotAvailable filter fed by
        # LocalEditOps.AvailableSlotsFrom). Scattered free slots are the norm once init
        # detection exists, so a plain contiguous walk would overwrite real patches.
        def _slot_available(n: int) -> bool:
            entry = self._index.get(obj_type, dest_bank, n)
            if entry is None:
                return True
            body = self._blobs.get(entry.current_hash)
            return body is not None and self._is_init_body(obj_type, body)

        placed, pending = resolve_sequential_fill(seq_items, obj_type, dest_bank, start_slot,
                                                   bank_type_of=self._local_bank_type_of,
                                                   slot_available=_slot_available)
        if not placed:
            self._log("Fill Sequentially: nothing placeable.")
            for it, reason in pending:
                self._log(f"  pending: {it.describe()} — {reason}")
            return

        ok = self._apply_batch_placements(
            placed, obj_type, "Place",
            f"Filled {len(placed)} item(s) sequentially starting at "
            f"{ObjLoc(obj_type, dest_bank, start_slot).label()}",
            hash_by_label, unresolved_by_label, force_overwrite=force_overwrite)
        if not ok:
            self._log("Fill Sequentially refused (see warnings above).")
            return

        self._log(f"Fill Sequentially: placed {len(placed)} item(s), {len(pending)} left pending.")
        for it, reason in pending:
            self._log(f"  pending: {it.describe()} — {reason}")

    def _clear_merge(self) -> None:
        if not self._merge.entries:
            return
        if QMessageBox.question(self, "Clear Merge", "Abandon everything staged in the Merge "
                               "Window?") != QMessageBox.StandardButton.Yes:
            return
        self._merge.clear()
        self._log("Cleared the Merge Window.")
        self._refresh_merge_tree()

    # ── PCG pane actions ─────────────────────────────────────────────────────

    def _open_pcg_from_computer(self) -> None:
        # Only .pcg files are offered/selectable (req 8): the Kronos PCG format is the sole
        # format this pane understands, so attempting anything else isn't an option.
        path, _ = QFileDialog.getOpenFileName(self, "Open PCG File", "", "Kronos PCG (*.pcg)")
        if not path:
            return
        if not path.lower().endswith(".pcg"):
            QMessageBox.information(self, "Open PCG", "Only .pcg files can be loaded here.")
            return
        try:
            data = open(path, "rb").read()
        except OSError as e:
            QMessageBox.warning(self, "Open PCG", f"Could not read {path}: {e}")
            return
        pcg = open_pcg(data)
        if pcg is None:
            QMessageBox.warning(self, "Open PCG", f"{os.path.basename(path)} is not a recognizable "
                                "Kronos .pcg file.")
            return
        self._pcg = pcg
        self._pcg_source_label = os.path.basename(path)
        self._log(f"Loaded {self._pcg_source_label}: {len(pcg.objects)} object(s), "
                 f"{len(pcg.rejected_banks)} rejected bank(s).")
        self._pcg_status_label.setText(
            f"{self._pcg_source_label} - {len(pcg.objects)} object(s)"
            + (f" ({len(pcg.rejected_banks)} rejected bank(s))" if pcg.rejected_banks else ""))
        self._refresh_pcg_tree()

    def _open_pcg_from_kronos(self) -> None:
        if not self._ftp_username:
            QMessageBox.information(self, "Pull PCG", "Set up FTP credentials (File Manager or "
                                    "Settings) before pulling a PCG file from the Kronos.")
            return
        from file_manager import _FtpWorker
        worker = _FtpWorker(self._host, self._ftp_port, self._ftp_username, self._ftp_password)
        try:
            worker.connect()
        except Exception as e:
            QMessageBox.warning(self, "Pull PCG", f"FTP connect failed: {e}")
            return
        try:
            picker = _RemoteFilePickerDialog(worker, "/", self)
            if picker.exec() != QDialog.DialogCode.Accepted or not picker.selected_remote_path:
                return
            remote_path = picker.selected_remote_path
            local_path = os.path.join(tempfile.gettempdir(), os.path.basename(remote_path))
            try:
                worker.download(remote_path, local_path)
            except Exception as e:
                QMessageBox.warning(self, "Pull PCG", f"Download failed: {e}")
                return
        finally:
            worker.disconnect()

        try:
            data = open(local_path, "rb").read()
        except OSError as e:
            QMessageBox.warning(self, "Pull PCG", f"Could not read downloaded file: {e}")
            return
        pcg = open_pcg(data)
        if pcg is None:
            QMessageBox.warning(self, "Pull PCG", f"{os.path.basename(remote_path)} is not a "
                                "recognizable Kronos .pcg file.")
            return
        self._pcg = pcg
        self._pcg_source_label = f"Kronos:{remote_path}"
        self._log(f"Pulled {self._pcg_source_label}: {len(pcg.objects)} object(s), "
                 f"{len(pcg.rejected_banks)} rejected bank(s).")
        self._pcg_status_label.setText(
            f"{self._pcg_source_label} - {len(pcg.objects)} object(s)"
            + (f" ({len(pcg.rejected_banks)} rejected bank(s))" if pcg.rejected_banks else ""))
        self._refresh_pcg_tree()

    def _pcg_resolve_content(self, obj_type: int, bank: int, number: int) -> Optional[bytes]:
        e = self._pcg_by_addr.get((obj_type, bank, number))
        if e is None:
            return None
        return wire_body_from_pcg_entry(obj_type, e)

    @staticmethod
    def _resolve_refs(obj_type: int, body: bytes) -> List[Tuple[str, int, Tuple[int, int, int]]]:
        """Feeds MergeCache.pull_recursive's resolve_refs param. walk_resolvable_
        references (not walk_object_references): a GM/g reference always resolves
        on the instrument and must never become a RefSite the Merge Window has to
        satisfy — see dependency_scanner.is_always_available."""
        return [(r.ref_kind, r.site, (r.ref.obj_type, r.ref.bank, r.ref.number))
                for r in depscan.walk_resolvable_references(obj_type, body)]

    def _local_lookup(self, obj_type: int, content_hash: str) -> Optional[Tuple[int, int, int]]:
        """Port of LibrarianShellViewModel.LocalLookup — search Local Library as a
        WHOLE, by content identity, for resolve_references_for_placement."""
        found = self._index.find_by_content_hash(obj_type, content_hash)   # (bank, number) or None
        return None if found is None else (obj_type, found[0], found[1])

    def _pull_pcg_selected_into_merge(self) -> None:
        payload = self._leaf_payload(self._tree_pcg)
        if payload is None or payload[0] != "pcg":
            self._log("Select exactly one PCG item to pull into the Merge Window.")
            return
        _, obj_type, bank, number = payload
        scope = self._undo.begin(
            f"Pulled {ObjLoc(obj_type, bank, number).label()} into Merge Window")
        self._pull_pcg_address_into_merge((obj_type, bank, number))
        if scope is not None:
            scope.dispose()

    def _pull_pcg_address_into_merge(self, address: Tuple[int, int, int]) -> None:
        """The actual PCG->Merge pull — factored out of _pull_pcg_selected_into_merge so
        the PCG pane's drag-drop handler (a PCG leaf dropped onto the Merge tree,
        _on_merge_dropped) can call the exact same logic for each dragged item."""
        added, gaps = self._merge.pull_recursive(address, self._pcg_resolve_content, self._resolve_refs,
                                                 source=self._pcg_source_label or "PCG")
        self._log(f"Pulled into Merge Window: {len(added)} new item(s), {len(gaps)} unresolved "
                 f"reference(s) (gaps reconcile automatically if pulled from elsewhere later).")
        for addr, kind in gaps:
            self._log(f"  gap: obj {addr[0]:02X} bank {addr[1]:02X} idx {addr[2]} ({kind})")
        self._refresh_merge_tree()

    def _fill_sequentially_pcg(self) -> None:
        payloads = [p for p in self._selected_payloads(self._tree_pcg) if p[0] == "pcg"]
        if not payloads:
            self._log("Select one or more PCG items to fill sequentially.")
            return
        obj_type = payloads[0][1]
        if any(p[1] != obj_type for p in payloads):
            self._log("Fill Sequentially: all selected items must be the same object type.")
            return
        seq_items = [it for it in (self._seq_item_from_pcg(p[1], p[2], p[3]) for p in payloads)
                    if it is not None]
        if not seq_items:
            self._log("Fill Sequentially: nothing resolvable from the selected PCG items.")
            return
        dlg = _DestinationDialog(obj_type, "Fill Sequentially starting at…", self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        dest_bank, start_slot = dlg.selected()
        self._run_sequential_fill(seq_items, obj_type, dest_bank, start_slot)

    def _show_pcg_properties(self) -> None:
        payload = self._leaf_payload(self._tree_pcg)
        if payload is None or payload[0] != "pcg":
            return
        _, obj_type, bank, number = payload
        e = self._pcg_by_addr.get((obj_type, bank, number))
        if e is None:
            return
        loc = ObjLoc(obj_type, bank, number)
        extra = [f"is_exi: {e.is_exi}"] if obj_type == OBJ_PROGRAM else []
        body = self._pcg_resolve_content(obj_type, bank, number)
        obj_extra, list_rows = self._object_body_readout(obj_type, body)
        dlg = _PropertiesDialog(f"PCG object — {loc.label()}", e.name, editable_name=False,
                                location=f"Location: {loc.label()}", flag_lines=[],
                                extra_lines=extra + obj_extra, list_rows=list_rows, parent=self)
        dlg.exec()

    # ── Drag-and-drop routing (real gestures, same placement methods as the buttons) ──

    def _on_local_dropped(self, source_pane: str, items: List[tuple],
                          target_item: Optional[QTreeWidgetItem]) -> None:
        if source_pane == "merge":
            self._handle_merge_to_local_drop(items, target_item)
        elif source_pane == "local":
            self._handle_local_to_local_drop(items, target_item)
        else:
            self._log("The Local Library pane only accepts drops from the Merge Window or itself.")

    def _handle_local_to_local_drop(self, items: List[tuple],
                                    target_item: Optional[QTreeWidgetItem]) -> None:
        locals_only = [it for it in items if it and it[0] == "local"]
        if len(locals_only) != 1:
            self._log("Drag exactly one Local Library item onto another to swap them.")
            return
        _, obj_type, bank, number = locals_only[0]
        target_payload = target_item.data(0, Qt.ItemDataRole.UserRole) if target_item is not None else None
        if target_payload is None or target_payload[0] != "local":
            self._log("Drop directly onto another Local Library slot to swap.")
            return
        _, t_obj_type, t_bank, t_number = target_payload
        self._swap_local_at(ObjLoc(obj_type, bank, number), ObjLoc(t_obj_type, t_bank, t_number))

    def _handle_merge_to_local_drop(self, items: List[tuple],
                                    target_item: Optional[QTreeWidgetItem]) -> None:
        hashes = [it[1] for it in items if it and it[0] == "merge"]
        entries = [e for e in (self._merge.try_get(h) for h in hashes) if e is not None]
        if not entries:
            self._log("Drag one or more Merge Window items onto the Local Library pane to place them.")
            return
        obj_type = entries[0].obj_type
        if any(e.obj_type != obj_type for e in entries):
            self._log("Drop refused: all dragged items must be the same object type.")
            return

        target_payload = target_item.data(0, Qt.ItemDataRole.UserRole) if target_item is not None else None
        if target_payload is not None and target_payload[0] == "local":
            _, t_obj_type, t_bank, t_number = target_payload
            if t_obj_type != obj_type:
                self._log("Drop refused: target slot is a different object type.")
                return
            dst_bank, dst_number = t_bank, t_number
        elif target_payload is not None and target_payload[0] == "local_bank":
            _, t_obj_type, t_bank = target_payload
            if t_obj_type != obj_type:
                self._log("Drop refused: target bank is a different object type.")
                return
            dst_bank = t_bank
            dst_number = self._find_first_free_slot(obj_type, t_bank)
        else:
            self._log("Drop onto a specific Local Library slot or bank.")
            return

        if len(entries) == 1:
            # Single item: "place exactly here" (PaneInteraction.cs's own single-vs-multi
            # drag distinction — see module docstring).
            self._place_merge_entry_at(entries[0], dst_bank, dst_number)
        else:
            # Multi-select: auto-fill sequentially starting at the drop target.
            seq_pairs = [self._seq_item_from_merge(e) for e in entries]
            seq_items = [p[0] for p in seq_pairs]
            hash_by_label = {it.label: e.content_hash for it, e in zip(seq_items, entries)}
            unresolved_by_label = {it.label: unresolved for it, unresolved in seq_pairs if unresolved}
            self._run_sequential_fill(seq_items, obj_type, dst_bank, dst_number,
                                      hash_by_label, unresolved_by_label,
                                      force_overwrite=self._chk_force_overwrite.isChecked())

    def _on_merge_dropped(self, source_pane: str, items: List[tuple],
                         target_item: Optional[QTreeWidgetItem]) -> None:
        if source_pane != "pcg":
            self._log("The Merge Window only accepts drops from the PCG pane.")
            return
        scope = self._undo.begin(f"Pulled {len(items)} item(s) into Merge Window")
        for it in items:
            if it and it[0] == "pcg":
                self._pull_pcg_address_into_merge((it[1], it[2], it[3]))
        if scope is not None:
            scope.dispose()

    # ── Sync / Commit ────────────────────────────────────────────────────────

    def _require_connected(self) -> bool:
        if self._service is None or not self._service.can_dump:
            QMessageBox.warning(self, "Librarian Shell", "Not connected / MIDI monitoring off — "
                                "Sync/Commit need a live Kronos connection.")
            return False
        return True

    def _repoint_session_dependency(self, entry: SessionDependencyEntry,
                                    new_bank: int, new_number: int) -> bool:
        """Repatch entry.required_by's own stored reference (a Combi timbre or
        Set List slot) to point at (new_bank, new_number) instead of the
        original missing address. The broader half of ResolvePendingDependencies:
        the expected content landed at a DIFFERENT local address than first
        placed at (e.g. Auto-Fill's free-slot scan didn't pick the same slot
        twice)."""
        referrer = self._index.get(entry.required_by.obj_type, entry.required_by.bank,
                                   entry.required_by.number)
        if referrer is None:
            return False
        body = self._blobs.get(referrer.current_hash)
        if body is None:
            return False
        type_for_func33 = 1 if entry.missing_ref.obj_type == OBJ_PROGRAM else 0
        func33_bank = obj_bank_to_func33(type_for_func33, new_bank)
        if func33_bank < 0:
            return False
        mutable = bytearray(body)
        if entry.ref_kind == "combi_timbre":
            set_combi_timbre_ref(mutable, entry.site, func33_bank=func33_bank, number=new_number)
        elif entry.ref_kind == "setlist_slot":
            set_setlist_slot_ref(mutable, entry.site, func33_bank=func33_bank, index=new_number)
        else:
            return False
        new_loc = ObjLoc(entry.missing_ref.obj_type, new_bank, new_number)
        self._write_local_body_edit(entry.required_by, bytes(mutable),
                                    f"repointed {entry.ref_kind} to {new_loc.label()}")
        return True

    def _retry_resolve_pending_dependencies(self) -> None:
        """Port of ResolvePendingDependencies: before hard-blocking Sync/Commit,
        re-check every pending session dependency against Local Library's
        CURRENT state — a dependency the user (or Auto-Fill) placed after it was
        first flagged no longer needs to block anything. Two cases, matching
        SessionDependencyClipboard's own split: (1) something now sits at the
        exact original missing address -> clipboard.resolve(); (2) the expected
        content landed at a different local address -> repoint the referrer's
        own reference there, then clipboard.remove() just that entry."""
        for entry in self._clipboard.pending:
            local = self._index.get(entry.missing_ref.obj_type, entry.missing_ref.bank,
                                    entry.missing_ref.number)
            if local is not None and not local.pending_delete and (
                    entry.expected_content_hash is None
                    or local.current_hash == entry.expected_content_hash):
                self._clipboard.resolve(entry.missing_ref)
                continue
            if entry.expected_content_hash is not None:
                found = self._index.find_by_content_hash(entry.missing_ref.obj_type,
                                                          entry.expected_content_hash)
                if found is not None and found != (entry.missing_ref.bank, entry.missing_ref.number):
                    new_bank, new_number = found
                    if self._repoint_session_dependency(entry, new_bank, new_number):
                        self._clipboard.remove(entry)

    def _show_sync_gate_dialog_if_blocked(self) -> bool:
        """Returns True if it's safe to proceed. Retries every pending session
        dependency against Local Library's current state first (see
        _retry_resolve_pending_dependencies); only what's STILL unresolved after
        that gets a genuine Continue-Anyway/Cancel choice, matching C#'s
        PrepareForPushAsync (retry, then a real choice — never a permanent,
        un-overridable block)."""
        self._retry_resolve_pending_dependencies()
        pending = self._clipboard.pending
        if not pending:
            self._refresh_local_tree()
            return True
        grouped: Dict[Tuple[ObjLoc, Optional[str]], int] = {}
        for e in pending:
            k = (e.missing_ref, e.expected_content_hash)
            grouped[k] = grouped.get(k, 0) + 1
        rows = [f"{loc.label()}  (needed by {count} placement(s))" for (loc, _h), count in grouped.items()]
        dlg = _UnresolvedDependenciesDialog(
            f"{len(pending)} dependency(ies) are still pending in the session clipboard. "
            "Place them locally (from the Merge Window or a PCG), or continue anyway and "
            "leave those references dangling.",
            rows, allow_continue=True, parent=self)
        proceed = dlg.exec() == QDialog.DialogCode.Accepted
        if proceed:
            self._refresh_local_tree()
        return proceed

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._refresh_enable()
        self._on_undo_stack_changed()   # Undo is gated on !IsBusy in the C# source too

    def _refresh_enable(self) -> None:
        connected = self._service is not None and self._service.can_dump
        auto_filling = getattr(self, "_auto_fill_active", False)
        self._btn_sync.setEnabled(connected and not self._busy and not auto_filling)
        self._btn_commit.setEnabled(connected and not self._busy and not auto_filling)
        self._btn_pull_kronos.setEnabled(not self._busy and not auto_filling)
        self._btn_paste.setEnabled(not self._batch_clip.is_empty and not self._busy)
        # Single owner of this button's enabled state (matches CanAutoFill() => !IsBusy
        # && !IsAutoFilling in the C# source) - _auto_fill_to_library/_auto_fill_finish
        # call _refresh_enable() rather than setEnabled() directly themselves.
        self._btn_auto_fill.setEnabled(not self._busy and not auto_filling)

    # ── Undo (Ctrl+Z / the toolbar's Undo button) ────────────────────────────
    # One step per user gesture, over LOCAL state only. See librarian_undo.py for
    # the capture model and what's deliberately outside undo's reach (the displaced-
    # occupant safety clipboard, anything already pushed to hardware, Clear History).

    def _merge_snapshot(self) -> Optional[dict]:
        """Full serialized snapshot of the Merge Window's staged contents, taken
        lazily ONLY when an action actually mutates the merge cache — a rename must
        not pay for copying every staged body."""
        try:
            return self._merge.snapshot_to_dict()
        except Exception:
            return None

    def _merge_restore(self, snapshot: Optional[dict]) -> None:
        """Put the Merge Window back exactly as captured (mirrors
        MergePane.Restore). A restore is itself a merge mutation, but it runs under
        the recorder's _restoring flag so it never becomes a new undoable step."""
        if snapshot is None:
            return
        try:
            self._merge.restore_from_dict(snapshot)
            self._refresh_merge_tree()
        except Exception:
            pass

    def _restore_session_deps(self, entries) -> None:
        self._clipboard.clear()
        for e in entries:
            self._clipboard.add(e)

    def _set_pending_bank_type_change(self, bank: int, prior: Optional[bool]) -> None:
        if prior is None:
            self._index.clear_pending_bank_type_change(bank)
        else:
            self._index.set_pending_bank_type_change(bank, prior)
        self._index.save()

    def _on_undo_stack_changed(self) -> None:
        if self._undo.can_undo:
            self._btn_undo.setText(f"Undo: {self._undo.top_description or ''}")
            self._btn_undo.setToolTip(self._undo.top_description or "")
        else:
            self._btn_undo.setText("Undo")
            self._btn_undo.setToolTip("Nothing to undo")
        self._btn_undo.setEnabled(self._undo.can_undo and not self._busy)
        self._btn_paste.setEnabled(not self._batch_clip.is_empty and not self._busy)

    def _do_undo(self) -> None:
        if self._busy:
            return
        desc = self._undo.undo()
        if desc is None:
            self._log("Nothing to undo.")
            return
        self._refresh_local_tree()
        self._log(f"Undid: {desc}")
        self._refresh_enable()

    def _warm_category_names(self, host: str) -> None:
        """Fetch the Global object (obj 0x03, bank 0, index 0) once per window
        and decode its category-name block, persisting per host. Fire-and-forget:
        failure leaves whatever labels are already in place (the numeric fallback
        is already showing), never surfaces to the user."""
        try:
            dump = self._service.dump_object_parsed(0x03, 0, 0, no_response_ms=8000)
            if dump is None:
                return
            from global_body import read_category_names
            names = read_category_names(dump.body)
            if names is None:
                return
            self._category_names = names
            storage.save_category_names(host, names.to_dict())
        except Exception as e:  # pragma: no cover - defensive
            print(f"[librarian] category-name warm-up failed: {e}")

    def _get_live_digest(self, bank_key: str) -> Optional[str]:
        obj_type, bank = (int(x) for x in bank_key.split(":"))
        d = self._service.bank_digest(obj_type, bank)
        return d.hex() if d is not None else None

    def _get_bank_objects(self, obj_type: int, bank: int) -> Dict[int, Tuple[int, bytes]]:
        slot_count = SLOT_COUNT.get(obj_type, 128)
        out: Dict[int, Tuple[int, bytes]] = {}
        for number in range(slot_count):
            d = self._service.dump_object_parsed(obj_type, bank, number,
                                                 no_response_ms=6000 if obj_type == OBJ_SET_LIST else 3000)
            if d is not None:
                out[number] = (d.version, d.body)
        return out

    def _write_to_hardware(self, obj_type: int, bank: int, number: int, body: bytes) -> bool:
        """Port of ApplyMoveAsync's per-object safety wrapping around a Sync/Commit
        write: staleness re-check right before the write (catches a front-panel
        edit landing in the narrow window after changeset_sync's earlier, coarser
        bank-level pre-scan), then a pre-image backup to a .syx file so a bad push
        is recoverable. Both are best-effort around the write itself: a failed
        backup logs and proceeds (nothing to protect if the dump comes back
        empty), but a stale bank digest hard-aborts the write, matching
        ApplyMoveAsync's own abort-before-any-Store behavior."""
        loc = ObjLoc(obj_type, bank, number)
        bank_key = LocalLibraryIndex.bank_key(obj_type, bank)
        baseline = self._index.bank_digest_baseline.get(bank_key)
        if baseline is not None:
            fresh_hex = self._get_live_digest(bank_key)
            if fresh_hex is not None and fresh_hex != baseline:
                self._log(f"ABORT: {loc.label()}'s bank changed on hardware since the "
                         "last pre-scan (edited at the panel?) — write skipped to avoid "
                         "clobbering a concurrent edit.")
                return False

        pre_image = self._service.dump_object_parsed(obj_type, bank, number)
        if pre_image is not None:
            try:
                stamp = _now_iso().replace(":", "").replace("-", "").replace(".", "")
                backup_path = str(storage.backup_dir() / f"{stamp}_sync_{loc.label()}.syx"
                                  .replace(" ", "_").replace(":", ""))
                self._service.backup_objects(
                    [WriteOp(obj_type, bank, number, pre_image.version, pre_image.body)],
                    backup_path)
            except Exception as e:  # pragma: no cover - defensive, backup is best-effort
                self._log(f"CHECK: pre-write backup of {loc.label()} failed ({e}) — proceeding anyway.")

        version = OBJ_VERSION.get(obj_type, 0)
        op = WriteOp(obj_type, bank, number, version, body)
        rc = self._service.write_object(op)
        if rc != 0:
            return False
        rc2 = self._service.store_bank(obj_type, bank)
        return rc2 == 0

    def _get_live_bank_type(self, bank: int) -> Optional[bool]:
        """changeset_sync.build_changeset's get_live_bank_type adapter — same "adapt
        SysExService for this pipeline" role _get_live_digest/_get_bank_objects/
        _write_to_hardware already play. SysExService has no dedicated bank-format query
        (no 0x7C read side), so this derives the live format from an actual Object Dump:
        slot 0's reply body length is deterministically WIRE_SIZE_EXI or WIRE_SIZE_HD1
        (pcg_file.py's own convention, the same one library_pull_pipeline/plan_batch_move
        already rely on). None (unverifiable) if the dump fails or times out — matches
        build_changeset's own "non-blocking when unverifiable" contract."""
        d = self._service.dump_object_parsed(OBJ_PROGRAM, bank, 0, no_response_ms=3000)
        if d is None:
            return None
        return len(d.body) == WIRE_SIZE_EXI

    def _write_bank_type_change(self, bank: int, to_exi: bool) -> bool:
        """changeset_sync.build_changeset's WriteBankTypeChange adapter — the func-0x7C
        counterpart to _write_to_hardware/_get_live_bank_type (same "adapt SysExService for
        this pipeline" role). Sends a REAL func-0x7C Change Program Bank Type; neither a wire-
        format builder nor a SysExService method for 0x7C existed anywhere in this codebase
        before this pass (grepped first, confirmed empty) — both were added
        (librarian_sysex.change_program_bank_type_request + SysExService.
        change_program_bank_type), with the wire bytes confirmed against the C# source's own
        KronosSysEx.cs.BuildChangeProgramBankType rather than guessed. Only relays the Reply
        code as a bool — execute_changeset already owns the all-or-nothing "one rejected
        reformat aborts the whole push" semantics (see changeset_sync.py's own module
        docstring), not this adapter."""
        return self._service.change_program_bank_type(bank, to_exi) == 0

    def _start_sync(self) -> None:
        if self._busy or not self._require_connected():
            return
        if not self._show_sync_gate_dialog_if_blocked():
            return
        self._set_busy(True)
        self._status_label.setText("Syncing...")
        full_sync = self._chk_force_full.isChecked()
        threading.Thread(target=self._sync_worker, args=(full_sync,), daemon=True,
                         name="LibShellSync").start()

    def _start_commit(self) -> None:
        if self._busy or not self._require_connected():
            return
        if not self._show_sync_gate_dialog_if_blocked():
            return
        self._set_busy(True)
        self._status_label.setText("Committing...")
        threading.Thread(target=self._sync_worker, args=(False,), daemon=True,
                         name="LibShellCommit").start()

    def _sync_worker(self, full_sync: bool) -> None:
        try:
            if full_sync:
                pull_result, plan, result = sync_library(
                    self._index, self._blobs, self._clipboard,
                    get_live_digest=self._get_live_digest, get_bank_objects=self._get_bank_objects,
                    resolver=self._local_resolver, write_to_hardware=self._write_to_hardware,
                    progress=lambda m: self._progress.emit(m),
                    full=full_sync,
                    get_live_bank_type=self._get_live_bank_type,
                    pending_bank_type_change=self._index.get_pending_bank_type_change,
                    write_bank_type_change=self._write_bank_type_change)
                # EDITABLE_BANKS (what sync_library pulls bodies for) excludes the GM/g
                # read-only Program banks — their names come from SysExService's own
                # cache instead (fed by passive dump-traffic capture, or this explicit
                # sweep), which is what the Local pane's read-only browse rows read via
                # cached_bank_names. This used to be a separate "Sync Program/Combi
                # Names" Tools-menu action; folded in here since that menu item (and its
                # redundant "Sync All") had no C# equivalent and were removed as such —
                # but the GM/g name sweep itself is still needed, just triggered from
                # the one Sync action that remains.
                if self._service is not None and self._service.can_dump:
                    self._service.sync_names(
                        progress=lambda done, total, names: self._progress.emit(
                            f"Bulk-dumping Program names {done}/{total}..."))
            else:
                pull_result = None
                plan, result = commit_changes(
                    self._index, self._blobs, self._clipboard,
                    get_live_digest=self._get_live_digest, resolver=self._local_resolver,
                    write_to_hardware=self._write_to_hardware,
                    get_live_bank_type=self._get_live_bank_type,
                    pending_bank_type_change=self._index.get_pending_bank_type_change,
                    write_bank_type_change=self._write_bank_type_change)
            self._index.save()
        except Exception as e:  # pragma: no cover - defensive
            self._sync_done.emit(None, None, None, f"Sync/Commit crashed: {e}")
            return
        status = "DONE" if not plan.is_refusable else "REFUSED"
        self._sync_done.emit(pull_result, plan, result, status)

    def _on_sync_done(self, pull_result, plan: Optional[ChangesetPlan], result: Optional[SyncResult],
                       status: str) -> None:
        self._set_busy(False)
        self._status_label.setText(status)
        if plan is None:
            self._log(status)
            return
        # A successful Sync/Commit wrote local state to hardware — the undo stack can't
        # roll a hardware write back, so it's cleared (mirrors LibrarianShellViewModel
        # clearing the stack after a push).
        if result is not None and result.written + result.erased > 0 and not result.failed:
            self._undo.clear()
            self._on_undo_stack_changed()
        if pull_result is not None:
            self._log(f"Pull: {pull_result.banks_checked} bank(s) checked, "
                     f"{pull_result.objects_fetched} object(s) fetched, "
                     f"{pull_result.conflicts} new conflict(s).")
        for w in plan.warnings:
            self._log("  ! " + w)
        if result is not None:
            self._log(f"Push: {result.written} written, {result.erased} erased, "
                     f"{result.deleted} local-only delete(s), {result.failed} failed, "
                     f"{result.reformatted} bank(s) reformatted.")
            # Committed whole-bank type changes are now realized on hardware — clear the
            # staged intent so a later, unrelated push to the same bank doesn't re-issue the
            # (erasing) func 0x7C. Mirrors SyncPipeline.cs's own post-success
            # ClearPendingBankTypeChange loop, confirmed from source: it only runs after the
            # WHOLE push succeeds, never on a partial/aborted one — result.reformatted equals
            # len(plan.bank_type_changes) exactly when every queued reformat in THIS push
            # actually succeeded (execute_changeset's all-or-nothing reformat gate: a single
            # rejected reformat aborts before any more are attempted).
            if plan.bank_type_changes and result.reformatted == len(plan.bank_type_changes):
                for bank, _ in plan.bank_type_changes:
                    self._index.clear_pending_bank_type_change(bank)
                self._index.save()
        self._refresh_local_tree()

    # ── Merge pull (kept synchronous — local blob store only, no hardware) ──

    def _on_merge_pull_done(self, added: int, gaps: int, status: str) -> None:
        self._log(status)
        self._refresh_merge_tree()


# ── Self-test (python librarian_shell_window.py) — pure grouping logic only,
#    no QApplication/Qt widget construction (see module docstring's Qt smoke
#    test note — that part is exercised separately, off-module, with a live
#    QT_QPA_PLATFORM=offscreen QApplication). ──────────────────────────────


def _selftest() -> None:
    import sys

    fails: List[str] = []

    def check(name: str, cond: bool) -> None:
        if not cond:
            fails.append(name)

    now = _now_iso()

    # ── _group_local: all three obj_type roots present, sorted, empty-safe ──
    entries = {
        LocalLibraryIndex.key(OBJ_PROGRAM, 0x00, 5): LocalIndexEntry(
            baseline_hash="a", current_hash="a", display_name="P5", created_utc=now, modified_utc=now),
        LocalLibraryIndex.key(OBJ_PROGRAM, 0x00, 2): LocalIndexEntry(
            baseline_hash="b", current_hash="c", display_name="P2", created_utc=now, modified_utc=now),
        LocalLibraryIndex.key(OBJ_COMBI, 0x40, 0): LocalIndexEntry(
            baseline_hash="d", current_hash="d", display_name="C0", created_utc=now, modified_utc=now),
    }
    groups = _group_local(entries)
    check("group-local-all-roots", set(groups.keys()) == {OBJ_PROGRAM, OBJ_COMBI, OBJ_SET_LIST})
    check("group-local-setlist-empty", groups[OBJ_SET_LIST] == {})
    prog_bank0 = groups[OBJ_PROGRAM][0x00]
    check("group-local-sorted-by-number", [t[0] for t in prog_bank0] == [2, 5])
    check("group-local-dirty-flag", prog_bank0[0][2].is_dirty and not prog_bank0[1][2].is_dirty)
    check("group-local-combi-present", groups[OBJ_COMBI][0x40][0][1] == LocalLibraryIndex.key(OBJ_COMBI, 0x40, 0))

    # ── _group_pcg: groups by (obj_type, obj_bank), sorted by index, Set List bank=None -> 0 ──
    from kronos_sysex import BankId

    pcg_objs = [
        PcgObjectEntry(OBJ_PROGRAM, BankId(1, "I-A", 0x00, 3), 3, b"x" * 4960, "PROG3", is_exi=True),
        PcgObjectEntry(OBJ_PROGRAM, BankId(1, "I-A", 0x00, 1), 1, b"x" * 4960, "PROG1", is_exi=True),
        PcgObjectEntry(OBJ_SET_LIST, None, 7, b"x" * 69416, "SETLIST7", is_exi=False),
    ]
    pgroups = _group_pcg(pcg_objs)
    check("group-pcg-program-sorted", [e.index for e in pgroups[OBJ_PROGRAM][0x00]] == [1, 3])
    check("group-pcg-setlist-bank0", 0 in pgroups[OBJ_SET_LIST] and pgroups[OBJ_SET_LIST][0][0].index == 7)
    check("group-pcg-no-combi-key", OBJ_COMBI not in pgroups)

    # ── _group_merge: groups by obj_type, sorted by (display_name, content_hash) ──
    e1 = MergeEntry(content_hash="hash1", obj_type=OBJ_PROGRAM, body=b"1", display_name="Bravo")
    e2 = MergeEntry(content_hash="hash2", obj_type=OBJ_PROGRAM, body=b"2", display_name="Alpha")
    e3 = MergeEntry(content_hash="hash3", obj_type=OBJ_COMBI, body=b"3", display_name="")
    mgroups = _group_merge([e1, e2, e3])
    check("group-merge-program-sorted", [e.display_name for e in mgroups[OBJ_PROGRAM]] == ["Alpha", "Bravo"])
    check("group-merge-combi-present", len(mgroups[OBJ_COMBI]) == 1)
    check("group-merge-setlist-empty", mgroups[OBJ_SET_LIST] == [])

    # ── _advance_entry_after_write: baseline preserved (or NO_BASELINE for a new slot) ──
    existing = LocalIndexEntry(version=2, baseline_hash="base1", current_hash="base1",
                               display_name="Old", created_utc="t0", modified_utc="t0",
                               has_resolved_dependencies=False, is_exi=False)
    advanced = _advance_entry_after_write(existing, "new1", "New Name", 9, now)
    check("advance-preserves-baseline", advanced.baseline_hash == "base1")
    check("advance-updates-current", advanced.current_hash == "new1")
    check("advance-now-dirty", advanced.is_dirty)
    check("advance-preserves-version", advanced.version == 2)
    check("advance-preserves-created", advanced.created_utc == "t0")
    check("advance-preserves-is-exi", advanced.is_exi is False)
    check("advance-clears-pending-delete", advanced.pending_delete is False)

    fresh = _advance_entry_after_write(None, "new2", "Fresh", 5, now)
    check("advance-fresh-no-baseline", fresh.baseline_hash == LocalLibraryIndex.NO_BASELINE_SENTINEL)
    check("advance-fresh-dirty", fresh.is_dirty)
    check("advance-fresh-version", fresh.version == 5)

    # ── _bank_label ──
    check("bank-label-program", _bank_label(OBJ_PROGRAM, 0x00) == ksx.program_label(0x00))
    check("bank-label-setlist-empty", _bank_label(OBJ_SET_LIST, 0) == "")

    if fails:
        print("FAIL:", ", ".join(fails))
        sys.exit(1)
    print("librarian_shell_window self-test: OK")


if __name__ == "__main__":
    _selftest()
