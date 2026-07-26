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
this subsystem's established porting-gap discipline):
  * Placement is BUTTON-driven, not drag-and-drop. PaneInteraction.cs's drag gestures,
    same-bank multi-select ranges, and cut/copy/paste clipboard semantics are not ported —
    each tree supports single-item selection and a small toolbar per pane instead. The
    task's own brief treats "drag-OR-button-driven" as an acceptable substitute.
  * Merge -> Local placement is single-item only. The C# "auto-fill sequentially" batch
    drop (ResolveSequentialFill) is not ported; placing N merge entries takes N button
    clicks. plan_batch_move exists in librarian_model.py for a future batch-placement pass.
  * Local -> Merge staging ("Move to Merge Window" from the Local pane, to relocate an
    object elsewhere via the merge cache) is not implemented — only Local<->Local direct
    swap (via plan_move) and PCG/Merge -> Local are, matching the task's explicit list of
    three placement routes.
  * No live "Object Dependencies" cross-reference panel and no persistent, replay-driven
    "History" ListBox reading OpLog.replay(). Both are folded into one plain activity log
    (QPlainTextEdit) fed by this window's own actions. OpLog is still written to on every
    local mutation (Place/Move/Discard/Delete) — only the READ-side history viewer is cut.
  * Dirty/conflicted/pending-delete visualization is plain QTreeWidgetItem foreground
    color (theme.py tokens), not the XAML's layered Border/DataTrigger Background scheme,
    and there is no separate green/red "dependency completeness" dot — that signal is
    folded into the Properties dialog's own read-out instead of a tree-row glyph.
  * Rename is supported for Local Library entries only (LocalIndexEntry.display_name,
    logged as an OpLog "Rename" op — the exact op_kind local_library_store.py's own
    self-test already exercises). Category/Sub-Category numeric fields (PropertiesDialog's
    ForProgramOrCombi) and the full Set-List slot editor (ForSetList) are not ported —
    this local index has no fields for either; they would need a body-level codec this
    task's scope doesn't cover.
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

import os
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit,
    QPushButton, QSpinBox, QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

import dependency_scanner as depscan
import kronos_sysex as ksx
from changeset_sync import ChangesetPlan, SyncResult, commit_changes, sync_library
from librarian_model import LibraryCatalog, ObjLoc, WriteOp, plan_move
from librarian_sysex import OBJ_COMBI, OBJ_PROGRAM, OBJ_SET_LIST, OBJ_VERSION, ObjectDump
from library_pull_pipeline import EDITABLE_BANKS, GetBankObjects, GetLiveDigest, SLOT_COUNT
from local_library_store import BlobStore, LocalIndexEntry, LocalLibraryIndex, OpLog
from merge_cache import MergeCache, MergeEntry
from pcg_file import PcgFile, PcgObjectEntry, open_pcg, wire_body_from_pcg_entry
from session_dependency_clipboard import SessionDependencyClipboard, SessionDependencyEntry
from setlist_data import MAX_COUNT
from sysex_service import SysExService
import theme as T

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
    """Port of PropertiesDialog's Program/Combi/Set-List "real estate" this
    subsystem's model layer actually exposes: name, bank/number, dirty/
    conflicted/pending-delete. Category/Sub-Category and the Set-List slot
    editor are cut (see module docstring)."""

    def __init__(self, heading: str, name: str, editable_name: bool,
                 location: str, flag_lines: List[str], extra_lines: List[str],
                 parent=None):
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

        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(2000)
        self._log_view.setStyleSheet(f"QPlainTextEdit {{ background: {T.INSET}; color: {T.TEXT}; "
                                     f"border: 1px solid {T.BORDER}; font-family: {T.FONT_MONO}; "
                                     f"font-size: 12px; }}")
        root.addWidget(self._log_view, stretch=0)
        self._log_view.setFixedHeight(140)

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
        v.addLayout(row)
        self._tree_local = QTreeWidget()
        self._tree_local.setHeaderHidden(True)
        self._tree_local.itemDoubleClicked.connect(lambda *_: self._show_local_properties())
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
        v.addLayout(row)
        self._tree_merge = QTreeWidget()
        self._tree_merge.setHeaderHidden(True)
        self._tree_merge.itemDoubleClicked.connect(lambda *_: self._show_merge_properties())
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
        v.addLayout(row2)
        self._pcg_status_label = QLabel("No PCG loaded")
        self._pcg_status_label.setStyleSheet(f"color: {T.TEXT_DIM};")
        v.addWidget(self._pcg_status_label)
        self._tree_pcg = QTreeWidget()
        self._tree_pcg.setHeaderHidden(True)
        self._tree_pcg.itemDoubleClicked.connect(lambda *_: self._show_pcg_properties())
        v.addWidget(self._tree_pcg)
        return box

    # ── Logging ──────────────────────────────────────────────────────────────

    def _log(self, text: str) -> None:
        self._log_view.appendPlainText(text)

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
        dlg = _PropertiesDialog(f"Properties — {loc.label()}", entry.display_name,
                                editable_name=True, location=f"Location: {loc.label()}",
                                flag_lines=flags, extra_lines=[], parent=self)
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
        src_entry = self._index.get(obj_type, bank, number)
        if src_entry is None:
            return
        src = ObjLoc(obj_type, bank, number)

        dlg = _DestinationDialog(obj_type, f"Swap {src.label()} with…", self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        dst_bank, dst_number = dlg.selected()
        dst = ObjLoc(obj_type, dst_bank, dst_number)
        if src == dst:
            self._log("Source and destination are the same location.")
            return
        dst_entry = self._index.get(obj_type, dst_bank, dst_number)
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
        src_dump = ObjectDump(obj_type, bank, number, src_entry.version, src_body)
        dst_dump = ObjectDump(obj_type, dst_bank, dst_number, dst_entry.version, dst_body)
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
        dlg = _PropertiesDialog(f"Merge entry — {entry.display_name or entry.content_hash[:8]}",
                                entry.display_name, editable_name=False,
                                location=f"Type: {_ROOT_LABEL.get(entry.obj_type, str(entry.obj_type))}",
                                flag_lines=[], extra_lines=extra, parent=self)
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
        payload = self._leaf_payload(self._tree_merge)
        if payload is None or payload[0] != "merge":
            return
        self._merge.remove(payload[1])
        self._log("Removed item from the Merge Window.")
        self._refresh_merge_tree()

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
        address = (obj_type, bank, number)
        added, gaps = self._merge.pull_recursive(address, self._pcg_resolve_content, self._resolve_refs,
                                                 source=self._pcg_source_label or "PCG")
        self._log(f"Pulled into Merge Window: {len(added)} new item(s), {len(gaps)} unresolved "
                 f"reference(s) (gaps reconcile automatically if pulled from elsewhere later).")
        for addr, kind in gaps:
            self._log(f"  gap: obj {addr[0]:02X} bank {addr[1]:02X} idx {addr[2]} ({kind})")
        self._refresh_merge_tree()

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
        dlg = _PropertiesDialog(f"PCG object — {loc.label()}", e.name, editable_name=False,
                                location=f"Location: {loc.label()}", flag_lines=[], extra_lines=extra,
                                parent=self)
        dlg.exec()

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
                    progress=lambda m: self._progress.emit(m))
            else:
                pull_result = None
                plan, result = commit_changes(
                    self._index, self._blobs, self._clipboard,
                    get_live_digest=self._get_live_digest, resolver=self._local_resolver,
                    write_to_hardware=self._write_to_hardware)
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
                     f"{result.deleted} local-only delete(s), {result.failed} failed.")
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
