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
    _run_sequential_fill) — nothing is duplicated. STILL cut: PaneInteraction.cs's Cut/
    Copy/Paste clipboard (Ctrl+X/C/V) and F2-to-rename keyboard shortcuts, and any custom
    drag-ghost/insertion-indicator visuals beyond Qt's own default drag cursor. Multi-select
    (Ctrl/Shift-click) on the Merge and PCG trees comes for free from Qt's
    ExtendedSelection mode; the Local pane stays single-selection (its own actions —
    Swap/Erase/Properties — are all inherently single-item).
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
  * Rename is still supported for Local Library entries only (LocalIndexEntry.display_name,
    logged as an OpLog "Rename" op). Category/Sub-Category (PropertiesDialog's
    ForProgramOrCombi) and the Set-List slot list (ForSetList) are now SURFACED — read-only —
    via the newly-landed object_body.py (parse_program_body/parse_combi_body/
    parse_setlist_slot) against the selected item's own body bytes, for all three panes
    (Local/Merge/PCG). STILL cut: editing them back into the body. object_body.py's own
    module docstring is explicit that it ports only ProgramBody.cs/CombiBody.cs/
    SetListBody.cs's READ side (no Write*/mutator methods exist there yet to port) — adding
    a body-level codec that can safely round-trip a category nibble or a slot's name/color/
    comments back into the wire bytes is real, separate work this UI-layer pass doesn't
    take on, so a correct read-only display beats a half-wired editable one, per the task's
    own guidance.
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
  * Whole-bank HD-1<->EXi reformat: _get_live_bank_type (new) plugs into
    changeset_sync.build_changeset's get_live_bank_type parameter the same way
    _get_live_digest/_get_bank_objects/_write_to_hardware already adapt SysExService for
    the rest of this pipeline — it derives a bank's live format from an actual Object Dump
    reply (slot 0's body length against pcg_file.WIRE_SIZE_EXI), since SysExService has no
    dedicated "query bank format" call. pending_bank_type_change/write_bank_type_change are
    NOT wired (always None) — staging an intentional bank-type conversion is, by
    ChangesetBuilder.cs's own design (see changeset_sync.py's module docstring), a distinct
    UI flow this task's brief explicitly calls out of scope. The practical effect: a real
    HD-1/EXi mismatch at Sync/Commit time now surfaces as a clear
    "REFUSE: ... is currently formatted as ..." line in the existing warnings log instead of
    either silently corrupting a bank or failing with no explanation — which is exactly the
    "REFUSE case surfaces a clear message" requirement this bullet closes. Reformatting
    itself remains a future, separate feature.
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

from PySide6.QtCore import QByteArray, QMimeData, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QDrag
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QSpinBox, QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

import dependency_scanner as depscan
import kronos_sysex as ksx
import object_body
from changeset_sync import ChangesetPlan, SyncResult, commit_changes, sync_library
from librarian_model import (
    BatchPlacement, LibraryCatalog, ObjLoc, SequentialFillItem, WriteOp,
    plan_batch_move, plan_move, resolve_sequential_fill,
)
from librarian_sysex import OBJ_COMBI, OBJ_PROGRAM, OBJ_SET_LIST, OBJ_VERSION, ObjectDump
from library_pull_pipeline import EDITABLE_BANKS, GetBankObjects, GetLiveDigest, SLOT_COUNT
from local_library_store import BlobStore, LocalIndexEntry, LocalLibraryIndex, OpLog
from merge_cache import MergeCache, MergeEntry
from pcg_file import PcgFile, PcgObjectEntry, WIRE_SIZE_EXI, open_pcg, wire_body_from_pcg_entry
from session_dependency_clipboard import SessionDependencyClipboard, SessionDependencyEntry
from setlist_data import MAX_COUNT
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


def _parse_key(key: str) -> Tuple[int, int, int]:
    a, b, c = key.split(":")
    return int(a), int(b), int(c)


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
    dirty/conflicted/pending-delete, plus (now) a read-only Category/Sub-Category read-out
    for Programs/Combis and a read-only Set-List slot list — both sourced from object_body.py
    against the item's own body bytes (see module docstring for why editing them back into
    the body is still cut). `extra_lines` renders as plain read-only labels (short,
    fixed-count facts); `list_rows`, if given, renders in a small scrollable QListWidget
    below them (same "long list" treatment _UnresolvedDependenciesDialog already uses) —
    used for the Set-List slot summary, which can run to dozens of rows."""

    def __init__(self, heading: str, name: str, editable_name: bool,
                 location: str, flag_lines: List[str], extra_lines: List[str],
                 list_rows: Optional[List[str]] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(heading)
        self.setStyleSheet(f"QDialog {{ background-color: {T.BG}; color: {T.TEXT}; }}")
        self.new_name: Optional[str] = None

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

        if list_rows:
            lst = QListWidget()
            lst.setStyleSheet(f"QListWidget {{ background: {T.INSET}; color: {T.TEXT}; "
                              f"border: 1px solid {T.BORDER}; }}")
            for row in list_rows:
                lst.addItem(QListWidgetItem(row))
            lst.setMaximumHeight(220)
            v.addWidget(lst)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        v.addWidget(btns)

    def _on_accept(self) -> None:
        self.new_name = self._name_edit.text().strip()
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
        for e in sorted(entries, key=lambda x: (not x.is_directory, x.name.lower())):
            item = QListWidgetItem(("[dir] " if e.is_directory else "") + e.name)
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
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Librarian Shell — Local Library / Merge / PCG")
        self.resize(1280, 820)
        self.setStyleSheet(f"QDialog {{ background-color: {T.BG}; color: {T.TEXT}; }}")

        self._host = host
        self._service = service
        self._ftp_port = ftp_port
        self._ftp_username = ftp_username
        self._ftp_password = ftp_password

        self._index = LocalLibraryIndex()
        self._index.load()
        self._blobs = BlobStore()
        self._oplog = OpLog()
        self._merge = MergeCache()
        self._clipboard = SessionDependencyClipboard()

        self._pcg: Optional[PcgFile] = None
        self._pcg_source_label = ""
        self._pcg_by_addr: Dict[Tuple[int, int, int], PcgObjectEntry] = {}

        self._busy = False

        self._build_ui()
        self._sync_done.connect(self._on_sync_done)
        self._merge_pull_done.connect(self._on_merge_pull_done)
        self._progress.connect(self._log)

        self._load_history()
        self._refresh_local_tree()
        self._refresh_merge_tree()
        self._refresh_pcg_tree()
        self._refresh_enable()

    # ── UI construction ──────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)

        sync_row = QHBoxLayout()
        self._btn_sync = QPushButton("Sync Library")
        self._btn_sync.setToolTip("Pull the whole library (lazy digest-diff), then push "
                                  "every pending local change.")
        self._btn_sync.clicked.connect(self._start_sync)
        sync_row.addWidget(self._btn_sync)
        self._btn_commit = QPushButton("Commit Changes")
        self._btn_commit.setToolTip("Push every pending local change now, without pulling first.")
        self._btn_commit.clicked.connect(self._start_commit)
        sync_row.addWidget(self._btn_commit)
        self._status_label = QLabel("")
        self._status_label.setStyleSheet(f"color: {T.ACCENT};")
        sync_row.addWidget(self._status_label, stretch=1)
        root.addLayout(sync_row)

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
        root.addLayout(close_row)

    def _build_local_pane(self) -> QWidget:
        box = QGroupBox("Local Library")
        v = QVBoxLayout(box)
        row = QHBoxLayout()
        btn_props = QPushButton("Properties…")
        btn_props.clicked.connect(lambda: self._show_local_properties())
        row.addWidget(btn_props)
        btn_swap = QPushButton("Swap With…")
        btn_swap.clicked.connect(self._swap_local_selected)
        row.addWidget(btn_swap)
        btn_erase = QPushButton("Erase")
        btn_erase.clicked.connect(self._erase_selected_local)
        row.addWidget(btn_erase)
        btn_clear = QPushButton("Clear Changes")
        btn_clear.clicked.connect(self._clear_changes)
        row.addWidget(btn_clear)
        btn_stage = QPushButton("Stage for Batch…")
        btn_stage.setToolTip("Copy this item (and its dependencies) into the Merge Window, "
                             "to rearrange and place it somewhere else — confirmed from "
                             "MergePaneViewModel.cs's PullFromLocal.")
        btn_stage.clicked.connect(self._stage_local_selected_to_merge)
        row.addWidget(btn_stage)
        v.addLayout(row)
        self._tree_local = _PaneTreeWidget("local", accepts_drop=True)
        self._tree_local.setHeaderHidden(True)
        self._tree_local.itemDoubleClicked.connect(lambda *_: self._show_local_properties())
        self._tree_local.itemSelectionChanged.connect(
            lambda: self._update_object_dependencies("local", self._tree_local))
        self._tree_local.itemsDropped.connect(self._on_local_dropped)
        v.addWidget(self._tree_local)
        return box

    def _build_merge_pane(self) -> QWidget:
        box = QGroupBox("Merge Window")
        v = QVBoxLayout(box)
        row = QHBoxLayout()
        btn_place = QPushButton("Place into Local…")
        btn_place.clicked.connect(self._place_merge_selected)
        row.addWidget(btn_place)
        btn_remove = QPushButton("Remove")
        btn_remove.clicked.connect(self._remove_merge_selected)
        row.addWidget(btn_remove)
        btn_clear = QPushButton("Clear Merge")
        btn_clear.clicked.connect(self._clear_merge)
        row.addWidget(btn_clear)
        btn_fill = QPushButton("Fill Sequentially…")
        btn_fill.setToolTip("Select two or more staged items of the same type, then pick a "
                            "starting Local Library slot — placed consecutively from there "
                            "(librarian_model.resolve_sequential_fill).")
        btn_fill.clicked.connect(self._fill_sequentially_merge)
        row.addWidget(btn_fill)
        v.addLayout(row)
        self._tree_merge = _PaneTreeWidget("merge", accepts_drop=True)
        self._tree_merge.setHeaderHidden(True)
        self._tree_merge.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._tree_merge.itemDoubleClicked.connect(lambda *_: self._show_merge_properties())
        self._tree_merge.itemSelectionChanged.connect(
            lambda: self._update_object_dependencies("merge", self._tree_merge))
        self._tree_merge.itemsDropped.connect(self._on_merge_dropped)
        v.addWidget(self._tree_merge)
        return box

    def _build_pcg_pane(self) -> QWidget:
        box = QGroupBox("Loaded PCG File")
        v = QVBoxLayout(box)
        row = QHBoxLayout()
        btn_open = QPushButton("From Computer…")
        btn_open.clicked.connect(self._open_pcg_from_computer)
        row.addWidget(btn_open)
        self._btn_pull_kronos = QPushButton("From Kronos…")
        self._btn_pull_kronos.clicked.connect(self._open_pcg_from_kronos)
        row.addWidget(self._btn_pull_kronos)
        v.addLayout(row)
        row2 = QHBoxLayout()
        btn_pull_merge = QPushButton("Pull into Merge Window")
        btn_pull_merge.clicked.connect(self._pull_pcg_selected_into_merge)
        row2.addWidget(btn_pull_merge)
        btn_fill = QPushButton("Fill Sequentially…")
        btn_fill.setToolTip("Select two or more PCG items of the same type, then pick a "
                            "starting Local Library slot — placed consecutively from there.")
        btn_fill.clicked.connect(self._fill_sequentially_pcg)
        row2.addWidget(btn_fill)
        v.addLayout(row2)
        self._pcg_status_label = QLabel("No PCG loaded")
        self._pcg_status_label.setStyleSheet(f"color: {T.TEXT_DIM};")
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

    def _style_local_item(self, item: QTreeWidgetItem, entry: LocalIndexEntry) -> None:
        if entry.pending_delete:
            item.setForeground(0, QBrush(QColor(T.TEXT_IDLE)))
        elif entry.conflicted:
            item.setForeground(0, QBrush(QColor(T.ERROR_TEXT)))
        elif entry.is_dirty:
            item.setForeground(0, QBrush(QColor(T.OK_TEXT)))

    def _refresh_local_tree(self) -> None:
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
            for bank in sorted(by_bank):
                items = by_bank[bank]
                if obj_type == OBJ_SET_LIST:
                    parent = root
                else:
                    parent = QTreeWidgetItem([_bank_label(obj_type, bank)])
                    # Tagged (distinct from a leaf's "local" tag) so a drag drop onto a whole
                    # bank — no specific slot — can still resolve a destination bank for
                    # _find_first_free_slot (see _handle_merge_to_local_drop).
                    parent.setData(0, Qt.ItemDataRole.UserRole, ("local_bank", obj_type, bank))
                    root.addChild(parent)
                for number, key, entry in items:
                    suffix = ""
                    if entry.is_dirty and obj_type in (OBJ_COMBI, OBJ_SET_LIST):
                        body = self._blobs.get(entry.current_hash)
                        if body is not None:
                            ok = depscan.has_all_dependencies(self._local_resolver, obj_type, body)
                            suffix = "  ✓" if ok else "  ✗ missing deps"
                    label = f"{entry.display_name or '(unnamed)'}  {number:03d}{suffix}"
                    leaf = QTreeWidgetItem([label])
                    leaf.setData(0, Qt.ItemDataRole.UserRole, ("local", obj_type, bank, number))
                    self._style_local_item(leaf, entry)
                    parent.addChild(leaf)
            if root.text(0) in expanded:
                root.setExpanded(True)

    def _refresh_merge_tree(self) -> None:
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
                shared = "  [shared]" if e.referenced_by else ""
                gap = "  [!]" if e.has_unresolved_dependencies else ""
                label = f"{e.display_name or e.content_hash[:8]}{shared}{gap}"
                leaf = QTreeWidgetItem([label])
                leaf.setData(0, Qt.ItemDataRole.UserRole, ("merge", e.content_hash))
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
        tree.expandAll()
        if self._pcg is None:
            self._pcg_status_label.setText("No PCG loaded")
        else:
            n_rejected = len(self._pcg.rejected_banks)
            extra = f"  ({n_rejected} rejected bank(s))" if n_rejected else ""
            self._pcg_status_label.setText(f"{self._pcg_source_label} — {len(objects)} object(s){extra}")

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
            rows.append(f"{slot.number:03d}  {slot.name}   [{type_label}  bank={slot.bank} "
                       f"idx={slot.index}  color={slot.color}  hold={slot.hold_time}  "
                       f"vol={slot.volume}]" + (f"  — {slot.comments}" if slot.comments else ""))
        return rows

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

    def _find_first_free_slot(self, obj_type: int, bank: int) -> int:
        limit = MAX_COUNT if obj_type == OBJ_SET_LIST else 128
        for i in range(limit):
            if self._index.get(obj_type, bank, i) is None:
                return i
        return 0

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
        extra_lines, list_rows = self._object_body_readout(obj_type, self._blobs.get(entry.current_hash))
        dlg = _PropertiesDialog(f"Properties — {loc.label()}", entry.display_name,
                                editable_name=True, location=f"Location: {loc.label()}",
                                flag_lines=flags, extra_lines=extra_lines, list_rows=list_rows,
                                parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.new_name and dlg.new_name != entry.display_name:
            old_name = entry.display_name
            entry.display_name = dlg.new_name
            entry.modified_utc = _now_iso()
            self._oplog.append({
                "id": str(uuid.uuid4()), "timestamp_utc": _now_iso(), "op_kind": "Rename",
                "targets": [{"obj_type": obj_type, "bank": bank, "number": number,
                            "result_hash": entry.current_hash}],
                "description": f"Renamed {loc.label()} '{old_name}' -> '{dlg.new_name}'",
                "sync_batch_id": None, "synced_at_utc": None,
            })
            self._index.save()
            self._log(f"Renamed {loc.label()} to '{dlg.new_name}'.")
            self._refresh_local_tree()

    def _erase_selected_local(self) -> None:
        payload = self._leaf_payload(self._tree_local)
        if payload is None or payload[0] != "local":
            return
        _, obj_type, bank, number = payload
        entry = self._index.get(obj_type, bank, number)
        if entry is None:
            return
        loc = ObjLoc(obj_type, bank, number)
        if QMessageBox.question(self, "Erase", f"Erase {loc.label()} locally? "
                               "Hardware is unaffected until Sync/Commit.") != QMessageBox.StandardButton.Yes:
            return
        from blank_template_store import BlankTemplateStore
        blank_store = BlankTemplateStore(self._blobs)
        ok = blank_store.erase(self._index, obj_type, bank, number, is_exi=entry.is_exi)
        if not ok:
            QMessageBox.warning(self, "Erase", "No blank template has been captured yet for this "
                                "object kind on this installation — erase is unavailable until one is.")
            return
        entry_after = self._index.get(obj_type, bank, number)
        self._oplog.append({
            "id": str(uuid.uuid4()), "timestamp_utc": _now_iso(), "op_kind": "Delete",
            "targets": [{"obj_type": obj_type, "bank": bank, "number": number,
                        "result_hash": entry_after.current_hash if entry_after else ""}],
            "description": f"Erased {loc.label()}", "sync_batch_id": None, "synced_at_utc": None,
        })
        self._index.save()
        self._log(f"Erased {loc.label()} (staged locally, pending Sync/Commit).")
        self._refresh_local_tree()

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

    def _swap_local_selected(self) -> None:
        payload = self._leaf_payload(self._tree_local)
        if payload is None or payload[0] != "local":
            self._log("Select exactly one Local Library item to swap.")
            return
        _, obj_type, bank, number = payload
        if self._index.get(obj_type, bank, number) is None:
            return
        src = ObjLoc(obj_type, bank, number)

        dlg = _DestinationDialog(obj_type, f"Swap {src.label()} with…", self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        dst_bank, dst_number = dlg.selected()
        self._swap_local_at(src, ObjLoc(obj_type, dst_bank, dst_number))

    def _swap_local_at(self, src: ObjLoc, dst: ObjLoc) -> None:
        """The actual Local<->Local swap — factored out of _swap_local_selected so the
        Local pane's drag-drop handler (_handle_local_to_local_drop) can call the exact
        same logic with a drop-computed destination instead of a dialog-chosen one."""
        src_entry = self._index.get(src.obj_type, src.bank, src.number)
        if src_entry is None:
            self._log(f"Swap aborted: no local content at {src.label()}.")
            return
        if src == dst:
            self._log("Source and destination are the same location.")
            return
        dst_entry = self._index.get(dst.obj_type, dst.bank, dst.number)
        if dst_entry is None:
            QMessageBox.information(self, "Swap", f"{dst.label()} has no local content yet — "
                                    "place something there first (e.g. from the Merge Window) "
                                    "before swapping.")
            return
        src_body = self._blobs.get(src_entry.current_hash)
        dst_body = self._blobs.get(dst_entry.current_hash)
        if src_body is None or dst_body is None:
            self._log("Swap aborted: missing blob content for source or destination.")
            return

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
            return

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
        added, gaps = self._merge.pull_recursive(
            (obj_type, bank, number), self._local_resolve_content, self._resolve_refs,
            source=MergeCache.LOCAL_SOURCE_LABEL)
        self._log(f"Staged into Merge Window: {len(added)} new item(s), {len(gaps)} unresolved "
                 f"reference(s).")
        for addr, kind in gaps:
            self._log(f"  gap: obj {addr[0]:02X} bank {addr[1]:02X} idx {addr[2]} ({kind})")
        self._refresh_merge_tree()

    def _clear_changes(self) -> None:
        targets = []
        for key, entry in self._index.entries.items():
            if entry.is_dirty or entry.pending_delete or entry.conflicted:
                obj_type, bank, number = _parse_key(key)
                entry.current_hash = entry.baseline_hash
                entry.pending_delete = False
                entry.conflicted = False
                entry.modified_utc = _now_iso()
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

    def _place_merge_selected(self) -> None:
        payload = self._leaf_payload(self._tree_merge)
        if payload is None or payload[0] != "merge":
            self._log("Select exactly one Merge Window item to place.")
            return
        entry = self._merge.try_get(payload[1])
        if entry is None:
            return

        dlg = _DestinationDialog(entry.obj_type, f"Place {entry.display_name or entry.content_hash[:8]} at…", self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        dst_bank, dst_number = dlg.selected()
        self._place_merge_entry_at(entry, dst_bank, dst_number)

    def _place_merge_entry_at(self, entry: MergeEntry, dst_bank: int, dst_number: int) -> None:
        """The actual Merge->Local single-item placement — factored out of
        _place_merge_selected so the Merge pane's drag-drop handler
        (_handle_merge_to_local_drop) can call the exact same logic with a drop-computed
        destination instead of a dialog-chosen one."""
        dst = ObjLoc(entry.obj_type, dst_bank, dst_number)

        missing = []
        if entry.obj_type in (OBJ_COMBI, OBJ_SET_LIST):
            missing = depscan.scan(self._local_resolver, entry.obj_type, entry.body)
        if missing:
            rows = [f"{m.ref.label()}  ({m.ref_kind})" for m in missing]
            confirm = _UnresolvedDependenciesDialog(
                f"{len(missing)} reference(s) in this object are not present locally.",
                rows, allow_continue=True, parent=self)
            if confirm.exec() != QDialog.DialogCode.Accepted:
                self._log("Placement cancelled (unresolved dependencies).")
                return

        existing = self._index.get(entry.obj_type, dst_bank, dst_number)
        now = _now_iso()
        new_entry = _advance_entry_after_write(existing, entry.content_hash, entry.display_name,
                                               entry.version, now)
        new_entry.has_resolved_dependencies = not missing
        self._index.set_entry(entry.obj_type, dst_bank, dst_number, new_entry)
        self._oplog.append({
            "id": str(uuid.uuid4()), "timestamp_utc": now, "op_kind": "Place",
            "targets": [{"obj_type": entry.obj_type, "bank": dst_bank, "number": dst_number,
                        "result_hash": entry.content_hash}],
            "description": f"Placed '{entry.display_name}' at {dst.label()}",
            "sync_batch_id": None, "synced_at_utc": None,
        })
        self._index.save()
        self._merge.mark_placed(entry.content_hash, (entry.obj_type, dst_bank, dst_number))
        self._merge.remove(entry.content_hash)

        for m in missing:
            self._clipboard.add(SessionDependencyEntry(
                missing_ref=m.ref, ref_kind=m.ref_kind, site=m.site,
                required_by=dst, expected_content_hash=None))

        self._log(f"Placed '{entry.display_name}' at {dst.label()} "
                 f"(staged locally, pending Sync/Commit).")
        self._refresh_local_tree()
        self._refresh_merge_tree()

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

    def _seq_item_from_merge(self, entry: MergeEntry) -> SequentialFillItem:
        addr = entry.origins[0].address if entry.origins else (entry.obj_type, 0, 0)
        origin = ObjLoc(entry.obj_type, addr[1], addr[2])
        dump = ObjectDump(entry.obj_type, origin.bank, origin.number, entry.version, entry.body)
        label = entry.display_name or entry.content_hash[:8]
        return SequentialFillItem(origin, dump, label)

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

    def _run_sequential_fill(self, seq_items: List[SequentialFillItem], obj_type: int,
                             dest_bank: int, start_slot: int,
                             hash_by_label: Optional[Dict[str, str]] = None) -> None:
        """Port of resolve_sequential_fill() -> plan_batch_move(), the pipeline behind both
        the "Fill Sequentially…" toolbar buttons and a multi-item Merge->Local drag. Shared
        so neither path duplicates the other's placement logic. `hash_by_label` maps a
        placed item's label back to its Merge content hash (empty/None for PCG sources,
        which have nothing to remove from a cache)."""
        hash_by_label = hash_by_label or {}
        placed, pending = resolve_sequential_fill(seq_items, obj_type, dest_bank, start_slot,
                                                   bank_type_of=self._local_bank_type_of)
        if not placed:
            self._log("Fill Sequentially: nothing placeable.")
            for it, reason in pending:
                self._log(f"  pending: {it.describe()} — {reason}")
            return

        catalog = self._build_local_catalog()
        occupants = self._dest_occupants_for(placed)
        plan = plan_batch_move(catalog, obj_type, placed, occupants, divert_displaced=False,
                               bank_type_of=self._local_bank_type_of)
        for line in plan.preview:
            self._log("  " + line)
        for w in plan.warnings:
            self._log("  ! " + w)
        if plan.is_refusable:
            self._log("Fill Sequentially refused (see warnings above).")
            return

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
            self._index.set_entry(w.obj, w.bank, w.index, new_entry)
            targets.append({"obj_type": w.obj, "bank": w.bank, "number": w.index, "result_hash": new_hash})

        self._oplog.append({
            "id": str(uuid.uuid4()), "timestamp_utc": now, "op_kind": "Place",
            "targets": targets,
            "description": f"Filled {len(placed)} item(s) sequentially starting at "
                           f"{ObjLoc(obj_type, dest_bank, start_slot).label()}",
            "sync_batch_id": None, "synced_at_utc": None,
        })
        self._index.save()

        for p in placed:
            h = hash_by_label.get(p.label)
            if h is not None:
                self._merge.mark_placed(h, (p.dst.obj_type, p.dst.bank, p.dst.number))
                self._merge.remove(h)

        self._log(f"Fill Sequentially: placed {len(placed)} item(s), {len(pending)} left pending.")
        for it, reason in pending:
            self._log(f"  pending: {it.describe()} — {reason}")
        self._refresh_local_tree()
        self._refresh_merge_tree()

    def _fill_sequentially_merge(self) -> None:
        payloads = [p for p in self._selected_payloads(self._tree_merge) if p[0] == "merge"]
        entries = [e for e in (self._merge.try_get(p[1]) for p in payloads) if e is not None]
        if not entries:
            self._log("Select one or more Merge Window items to fill sequentially.")
            return
        obj_type = entries[0].obj_type
        if any(e.obj_type != obj_type for e in entries):
            self._log("Fill Sequentially: all selected items must be the same object type.")
            return
        dlg = _DestinationDialog(obj_type, "Fill Sequentially starting at…", self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        dest_bank, start_slot = dlg.selected()
        seq_items = [self._seq_item_from_merge(e) for e in entries]
        hash_by_label = {it.label: e.content_hash for it, e in zip(seq_items, entries)}
        self._run_sequential_fill(seq_items, obj_type, dest_bank, start_slot, hash_by_label)

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
        path, _ = QFileDialog.getOpenFileName(self, "Open PCG File", "", "Kronos PCG (*.PCG *.pcg)")
        if not path:
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
        self._refresh_pcg_tree()

    def _pcg_resolve_content(self, obj_type: int, bank: int, number: int) -> Optional[bytes]:
        e = self._pcg_by_addr.get((obj_type, bank, number))
        if e is None:
            return None
        return wire_body_from_pcg_entry(obj_type, e)

    @staticmethod
    def _resolve_refs(obj_type: int, body: bytes) -> List[Tuple[int, int, int]]:
        return [(r.ref.obj_type, r.ref.bank, r.ref.number) for r in depscan.walk_object_references(obj_type, body)]

    def _pull_pcg_selected_into_merge(self) -> None:
        payload = self._leaf_payload(self._tree_pcg)
        if payload is None or payload[0] != "pcg":
            self._log("Select exactly one PCG item to pull into the Merge Window.")
            return
        _, obj_type, bank, number = payload
        self._pull_pcg_address_into_merge((obj_type, bank, number))

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
            seq_items = [self._seq_item_from_merge(e) for e in entries]
            hash_by_label = {it.label: e.content_hash for it, e in zip(seq_items, entries)}
            self._run_sequential_fill(seq_items, obj_type, dst_bank, dst_number, hash_by_label)

    def _on_merge_dropped(self, source_pane: str, items: List[tuple],
                         target_item: Optional[QTreeWidgetItem]) -> None:
        if source_pane != "pcg":
            self._log("The Merge Window only accepts drops from the PCG pane.")
            return
        for it in items:
            if it and it[0] == "pcg":
                self._pull_pcg_address_into_merge((it[1], it[2], it[3]))

    # ── Sync / Commit ────────────────────────────────────────────────────────

    def _require_connected(self) -> bool:
        if self._service is None or not self._service.can_dump:
            QMessageBox.warning(self, "Librarian Shell", "Not connected / MIDI monitoring off — "
                                "Sync/Commit need a live Kronos connection.")
            return False
        return True

    def _show_sync_gate_dialog_if_blocked(self) -> bool:
        """Returns True if it's safe to proceed (clipboard is clear); shows the
        blocking notice and returns False otherwise."""
        pending = self._clipboard.pending
        if not pending:
            return True
        grouped: Dict[Tuple[ObjLoc, Optional[str]], int] = {}
        for e in pending:
            k = (e.missing_ref, e.expected_content_hash)
            grouped[k] = grouped.get(k, 0) + 1
        rows = [f"{loc.label()}  (needed by {count} placement(s))" for (loc, _h), count in grouped.items()]
        dlg = _UnresolvedDependenciesDialog(
            f"{len(pending)} dependency(ies) are still pending in the session clipboard. "
            "Place them locally (from the Merge Window or a PCG) before Sync/Commit.",
            rows, allow_continue=False, parent=self)
        dlg.exec()
        return False

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._refresh_enable()

    def _refresh_enable(self) -> None:
        connected = self._service is not None and self._service.can_dump
        self._btn_sync.setEnabled(connected and not self._busy)
        self._btn_commit.setEnabled(connected and not self._busy)
        self._btn_pull_kronos.setEnabled(not self._busy)

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
        build_changeset's own "non-blocking when unverifiable" contract. See module
        docstring for why pending_bank_type_change/write_bank_type_change stay unwired
        (no staged-conversion UI in this pass) — this adapter's only job is making the
        REFUSE-on-mismatch path (Step 3.5b) surface a real, not-guessed answer instead of
        never firing at all."""
        d = self._service.dump_object_parsed(OBJ_PROGRAM, bank, 0, no_response_ms=3000)
        if d is None:
            return None
        return len(d.body) == WIRE_SIZE_EXI

    def _start_sync(self) -> None:
        if self._busy or not self._require_connected():
            return
        if not self._show_sync_gate_dialog_if_blocked():
            return
        self._set_busy(True)
        self._status_label.setText("Syncing…")
        threading.Thread(target=self._sync_worker, args=(True,), daemon=True, name="LibShellSync").start()

    def _start_commit(self) -> None:
        if self._busy or not self._require_connected():
            return
        if not self._show_sync_gate_dialog_if_blocked():
            return
        self._set_busy(True)
        self._status_label.setText("Committing…")
        threading.Thread(target=self._sync_worker, args=(False,), daemon=True, name="LibShellCommit").start()

    def _sync_worker(self, full_sync: bool) -> None:
        try:
            if full_sync:
                pull_result, plan, result = sync_library(
                    self._index, self._blobs, self._clipboard,
                    get_live_digest=self._get_live_digest, get_bank_objects=self._get_bank_objects,
                    resolver=self._local_resolver, write_to_hardware=self._write_to_hardware,
                    progress=lambda m: self._progress.emit(m),
                    get_live_bank_type=self._get_live_bank_type)
            else:
                pull_result = None
                plan, result = commit_changes(
                    self._index, self._blobs, self._clipboard,
                    get_live_digest=self._get_live_digest, resolver=self._local_resolver,
                    write_to_hardware=self._write_to_hardware,
                    get_live_bank_type=self._get_live_bank_type)
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
