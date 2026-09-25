"""SampleEditorWindow — browse, pull, edit, and push real Kronos sample
content (.KSC/.KMP/.KSF) over FTP.

This is a first, deliberately-scoped slice of C#'s much larger
SampleEditorWindow (2500+ lines, waveform editing, multisample zone
splitting, DSP effects, normalization reports — see PROJECT_STRUCTURE.md /
session notes for what's deferred). What this DOES do is real and
hardware-verified: parse/write the actual .KSC/.KMP/.KSF format (Data/
ksf_sample.py, kmp_multisample.py, ksc_collection.py — every real fixture in
KronosScreenRemote/SampleFixtures/ round-trips through it byte-identical),
and pull/push it over FTP using the app's real connection (Core/sample_ftp.py).

Editable fields are deliberately limited to what's safe without a waveform
view: display name, loop start/end, and the one-shot/reverse/+12dB-boost
flags. A header-only sample (no PCM — a legitimate stub OR silent data loss,
indistinguishable by inspection per kronosology doc §3.3) is read-only and
is never re-uploaded — see Core/sample_ftp.py's push_closure `only_ksf_paths`
parameter, used here to push only samples this session actually edited.
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
from typing import Dict, List, Optional, Set

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPushButton, QSpinBox, QSplitter,
    QStatusBar, QToolBar, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
    QCheckBox, QProgressDialog,
)

import Core.sample_ftp as sample_ftp
import Core.sample_link_resolver as sample_link_resolver
import Data.ksc_collection as ksc_mod
from Data.ksc_collection import KscCollection
from Data.kmp_multisample import KmpMultisample, KmpZone, find_stereo_sibling
from Data.ksf_sample import KsfSample
import Models.storage as storage
import Utils.theme as T

log = logging.getLogger(__name__)

# Port of C#'s SampleImportBuilder.MaxZonesPerMultisample.
_MAX_ZONES_PER_MULTISAMPLE = 128

# C#'s KronosNameMaxLength (SampleEditorWindow.xaml.cs) — a UI-only cap. Real
# hardware truncates a longer name gracefully on its own; this exists only so
# the tool's own rename dialog doesn't silently truncate without the user
# seeing what they typed. NEVER enforce this in the Data/ layer.
_KRONOS_NAME_MAX_LENGTH = 22

# The real Kronos's own sentinel filename for a zone with no assigned sample
# (KmpZone.is_skipped checks a 12-char prefix match, so this — truncated to
# 12 bytes on write, like every other 12-byte filename field — round-trips
# correctly either way).
_SKIPPED_SAMPLE_FILENAME = "SKIPPEDSAMPLE"


def _workspace_root() -> str:
    root = storage.data_dir() / "SampleEditorWorkspace"
    root.mkdir(parents=True, exist_ok=True)
    return str(root)


def _prompt_kronos_name(parent, title: str, label: str, current: str) -> Optional[str]:
    """22-char-capped rename/create prompt — port of C#'s PromptDialog(...,
    maxLength: KronosNameMaxLength). The cap lives here (a UI widget), never
    in the Data/ layer — see kronosology doc guidance repeated throughout
    this file's new zone-management code."""
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    layout = QVBoxLayout(dlg)
    layout.addWidget(QLabel(label))
    edit = QLineEdit(current)
    edit.setMaxLength(_KRONOS_NAME_MAX_LENGTH)
    edit.selectAll()
    layout.addWidget(edit)
    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    layout.addWidget(buttons)
    edit.setFocus()
    if dlg.exec() != QDialog.Accepted:
        return None
    new_name = edit.text().strip()
    return new_name or None


class _RemoteKscBrowserDialog(QDialog):
    """Minimal remote FTP tree browser scoped to picking a .KSC file.

    Not the full dual-pane File Manager (Tools/file_manager.py) — this only
    needs drill-down navigation and a single-file pick, so it's a much
    smaller, purpose-built tree rather than reusing that window's much larger
    surface (drag-drop, cut/copy/paste, rename, delete — none of which apply
    here)."""

    def __init__(self, ftp, parent=None):
        super().__init__(parent)
        self._ftp = ftp
        self.selected_path: Optional[str] = None
        self.setWindowTitle("Load Sample Collection from Kronos")
        self.resize(480, 520)

        layout = QVBoxLayout(self)
        info = QLabel("Select a .KSC file on the Kronos's SSD.")
        info.setStyleSheet(f"color: {T.TEXT_DIM};")
        layout.addWidget(info)

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.itemExpanded.connect(self._on_expand)
        self._tree.itemDoubleClicked.connect(self._on_double_click)
        layout.addWidget(self._tree, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._ok_btn = QPushButton("Load")
        self._ok_btn.setEnabled(False)
        self._ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(self._ok_btn)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

        self._tree.itemSelectionChanged.connect(self._on_selection_changed)
        self._populate_dir(None, "/")

    def _populate_dir(self, parent_item: Optional[QTreeWidgetItem], path: str):
        try:
            entries = self._ftp.list_dir(path)
        except Exception as e:
            log.warning("sample browser: list_dir('%s') failed: %s", path, e)
            return
        entries.sort(key=lambda e: (not e.is_directory, e.name.lower()))
        for entry in entries:
            if entry.is_directory:
                item = QTreeWidgetItem([f"\U0001F4C1 {entry.name}"])
                item.setData(0, Qt.UserRole, ("dir", entry.full_path))
                item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
                if parent_item is None:
                    self._tree.addTopLevelItem(item)
                else:
                    parent_item.addChild(item)
            elif entry.name.upper().endswith(".KSC"):
                item = QTreeWidgetItem([entry.name])
                item.setData(0, Qt.UserRole, ("ksc", entry.full_path))
                if parent_item is None:
                    self._tree.addTopLevelItem(item)
                else:
                    parent_item.addChild(item)

    def _on_expand(self, item: QTreeWidgetItem):
        if item.childCount() > 0:
            return
        kind, path = item.data(0, Qt.UserRole)
        if kind == "dir":
            self._populate_dir(item, path)

    def _on_selection_changed(self):
        items = self._tree.selectedItems()
        if not items:
            self._ok_btn.setEnabled(False)
            return
        kind, path = items[0].data(0, Qt.UserRole)
        self._ok_btn.setEnabled(kind == "ksc")
        if kind == "ksc":
            self.selected_path = path

    def _on_double_click(self, item: QTreeWidgetItem, _col: int):
        kind, path = item.data(0, Qt.UserRole)
        if kind == "ksc":
            self.selected_path = path
            self.accept()


class SampleEditorWindow(QMainWindow):
    _pull_done = Signal(object, object, list, object)   # local_path, remote_map, failures, error
    _push_done = Signal(list, object)                    # failures, error
    _progress = Signal(str)

    def __init__(self, host: str, ftp_port: int, user: str, password: str, parent=None):
        super().__init__(parent)
        self._host = host
        self._ftp_port = ftp_port
        self._user = user
        self._pass = password

        self._collection: Optional[KscCollection] = None
        self._local_ksc_path: Optional[str] = None
        self._remote_dest_dir: Optional[str] = None
        self._dirty_ksf_paths: Set[str] = set()
        self._kmp_cache: Dict[str, KmpMultisample] = {}   # kmp_local_path -> parsed
        self._current_ksf: Optional[KsfSample] = None
        self._current_ksf_path: Optional[str] = None
        self._loading_fields = False   # suppresses dirty-marking while populating the form

        self.setWindowTitle("Sample Editor — Kronos")
        self.resize(760, 540)
        self._setup_ui()

        self._pull_done.connect(self._on_pull_done)
        self._push_done.connect(self._on_push_done)
        self._progress.connect(self._on_progress)

    # ── UI construction ──────────────────────────────────────────────────────

    def _setup_ui(self):
        toolbar = QToolBar("Sample Editor")
        self.addToolBar(toolbar)
        self._act_load = toolbar.addAction("Load from Kronos…")
        self._act_load.triggered.connect(self._on_load_from_kronos)
        self._act_save = toolbar.addAction("Save Changes")
        self._act_save.setEnabled(False)
        self._act_save.triggered.connect(self._on_save_changes)
        self._act_push = toolbar.addAction("Push to Kronos")
        self._act_push.setEnabled(False)
        self._act_push.triggered.connect(self._on_push_to_kronos)

        toolbar.addSeparator()
        self._act_add_zone = toolbar.addAction("Add Zone")
        self._act_add_zone.triggered.connect(self._on_add_zone)
        self._act_delete_zone = toolbar.addAction("Delete Zone")
        self._act_delete_zone.triggered.connect(self._on_delete_zone)
        self._act_undo_delete_zone = toolbar.addAction("Undo Delete Zone")
        self._act_undo_delete_zone.setEnabled(False)
        self._act_undo_delete_zone.triggered.connect(self._on_undo_delete_zone)
        self._act_remove_sample = toolbar.addAction("Remove Sample")
        self._act_remove_sample.triggered.connect(self._on_remove_sample)
        self._act_rename_sample = toolbar.addAction("Rename Sample…")
        self._act_rename_sample.triggered.connect(self._on_rename_sample)
        self._act_rename_multisample = toolbar.addAction("Rename Multisample…")
        self._act_rename_multisample.triggered.connect(self._on_rename_multisample)
        for a in (self._act_add_zone, self._act_delete_zone, self._act_remove_sample,
                  self._act_rename_sample, self._act_rename_multisample):
            a.setEnabled(False)

        # Single-slot undo for Delete Zone only (matches C#'s own scoping:
        # Remove Sample and every other zone-content edit here is NOT
        # undoable either — see _on_delete_zone's docstring for why a full
        # Ctrl+Z/redo stack, which is what C#'s own DeleteZoneCompletely
        # actually offers, isn't ported here).
        self._last_deleted_zone = None   # type: Optional[dict]

        splitter = QSplitter(Qt.Horizontal)
        self.setCentralWidget(splitter)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Multisample / Zone"])
        self._tree.itemSelectionChanged.connect(self._on_tree_selection_changed)
        splitter.addWidget(self._tree)

        panel = QWidget()
        panel_layout = QVBoxLayout(panel)

        self._header_warning = QLabel(self._GENERIC_HEADER_ONLY_WARNING)
        self._header_warning.setStyleSheet(f"color: {T.WARN}; font-weight: bold;")
        self._header_warning.setWordWrap(True)
        self._header_warning.setVisible(False)
        panel_layout.addWidget(self._header_warning)

        group = QGroupBox("Sample")
        form = QFormLayout(group)

        self._name_edit = QLineEdit()
        self._name_edit.editingFinished.connect(self._mark_dirty_from_field)
        form.addRow("Name:", self._name_edit)

        self._info_label = QLabel("—")
        self._info_label.setStyleSheet(f"color: {T.TEXT_DIM};")
        form.addRow("Rate / Length:", self._info_label)

        self._loop_start_spin = QSpinBox()
        self._loop_start_spin.setRange(0, 0)
        self._loop_start_spin.valueChanged.connect(self._mark_dirty_from_field)
        form.addRow("Loop Start:", self._loop_start_spin)

        self._loop_end_spin = QSpinBox()
        self._loop_end_spin.setRange(0, 0)
        self._loop_end_spin.valueChanged.connect(self._mark_dirty_from_field)
        form.addRow("Loop End:", self._loop_end_spin)

        self._loop_enabled_check = QCheckBox("Loop enabled (off = one-shot)")
        self._loop_enabled_check.toggled.connect(self._mark_dirty_from_field)
        form.addRow(self._loop_enabled_check)

        self._reverse_check = QCheckBox("Reverse playback")
        self._reverse_check.toggled.connect(self._mark_dirty_from_field)
        form.addRow(self._reverse_check)

        self._boost_check = QCheckBox("+12dB gain boost")
        self._boost_check.toggled.connect(self._mark_dirty_from_field)
        form.addRow(self._boost_check)

        panel_layout.addWidget(group)
        panel_layout.addStretch()
        group.setEnabled(False)
        self._field_group = group

        splitter.addWidget(panel)
        splitter.setSizes([300, 460])

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage(
            "Load a collection from the Kronos to begin — Tools ▸ Sample Editor…")

    # ── Load from Kronos ─────────────────────────────────────────────────────

    def _on_load_from_kronos(self):
        self._act_load.setEnabled(False)
        self.statusBar().showMessage("Connecting…")

        def bg():
            from Tools.file_manager import _FtpWorker
            ftp = _FtpWorker(self._host, self._ftp_port, self._user, self._pass)
            try:
                ftp.connect()
            except Exception as e:
                QTimer.singleShot(0, self, lambda: self._on_connect_failed(str(e)))
                return
            QTimer.singleShot(0, self, lambda: self._open_browser(ftp))

        threading.Thread(target=bg, daemon=True, name="SampleEditorConnect").start()

    def _on_connect_failed(self, message: str):
        self._act_load.setEnabled(True)
        self.statusBar().showMessage("Not connected")
        QMessageBox.warning(self, "Sample Editor", f"Could not connect: {message}")

    def _open_browser(self, ftp):
        self._act_load.setEnabled(True)
        dlg = _RemoteKscBrowserDialog(ftp, self)
        if dlg.exec() != QDialog.Accepted or not dlg.selected_path:
            return
        self._start_pull(ftp, dlg.selected_path)

    def _start_pull(self, ftp, remote_ksc_path: str):
        self._progress_dlg = QProgressDialog("Downloading…", None, 0, 0, self)
        self._progress_dlg.setWindowModality(Qt.WindowModal)
        self._progress_dlg.setMinimumDuration(0)
        self._progress_dlg.show()

        def bg():
            try:
                local_root = _workspace_root()
                local_path, remote_map, failures = sample_ftp.pull(
                    ftp, remote_ksc_path, local_root,
                    on_progress=lambda msg: self._progress.emit(msg))
                self._pull_done.emit(local_path, remote_map, failures, None)
            except Exception as e:
                log.exception("sample pull failed")
                self._pull_done.emit(None, None, [], str(e))

        threading.Thread(target=bg, daemon=True, name="SampleEditorPull").start()

    @Slot(str)
    def _on_progress(self, message: str):
        if getattr(self, "_progress_dlg", None):
            self._progress_dlg.setLabelText(message)

    @Slot(object, object, list, object)
    def _on_pull_done(self, local_path, remote_map, failures, error):
        if getattr(self, "_progress_dlg", None):
            self._progress_dlg.close()
            self._progress_dlg = None
        if error:
            QMessageBox.warning(self, "Sample Editor", f"Load failed: {error}")
            return

        with open(local_path, "rb") as f:
            self._collection = KscCollection.open(f.read())
        self._collection.path = local_path
        self._local_ksc_path = local_path
        remote_ksc_path = remote_map[local_path]
        self._remote_dest_dir = remote_ksc_path.rsplit("/", 1)[0] or "/"
        self._dirty_ksf_paths.clear()
        self._kmp_cache.clear()
        self._current_ksf = None
        self._current_ksf_path = None
        self._field_group.setEnabled(False)
        self._act_push.setEnabled(False)
        self._last_deleted_zone = None
        self._act_undo_delete_zone.setEnabled(False)

        self._rebuild_tree()
        self.statusBar().showMessage(
            f"Loaded {os.path.basename(local_path)} — "
            f"{len(self._collection.entries)} entr{'y' if len(self._collection.entries)==1 else 'ies'}"
            + (f", {len(failures)} file(s) failed" if failures else ""))
        if failures:
            QMessageBox.warning(
                self, "Sample Editor",
                "Some files could not be downloaded and are missing locally:\n\n"
                + "\n".join(failures[:20])
                + ("\n…" if len(failures) > 20 else ""))

    # ── Tree ─────────────────────────────────────────────────────────────────

    def _rebuild_tree(self):
        self._tree.clear()
        if not self._collection or not self._local_ksc_path:
            return
        content_dir = ksc_mod.content_dir_for(self._local_ksc_path)
        for entry in self._collection.entries:
            if not entry.upper().endswith(".KMP"):
                continue
            kmp_local_path = os.path.join(content_dir, entry)
            if not os.path.isfile(kmp_local_path):
                continue
            with open(kmp_local_path, "rb") as f:
                m = KmpMultisample.open(f.read())
            if m is None:
                continue
            self._kmp_cache[kmp_local_path] = m
            label = f"{m.name}{m.suffix} ({entry})"
            kmp_item = QTreeWidgetItem([label])
            kmp_item.setData(0, Qt.UserRole, ("kmp", kmp_local_path))
            for idx, zone in enumerate(m.zones):
                if zone.is_skipped:
                    zlabel = f"[{zone.original_key}-{zone.top_key}] (no sample)"
                else:
                    zlabel = f"[{zone.original_key}-{zone.top_key}] {zone.filename}"
                zone_item = QTreeWidgetItem([zlabel])
                # ("zone", kmp_local_path, zone_index) — NOT the resolved .ksf
                # path alone: every zone action (delete/remove/rename/link)
                # needs to locate this KmpZone inside its owning
                # KmpMultisample.zones list, which a bare path can't address.
                # A skipped zone gets the same addressable data (just no
                # underlying .ksf to load into the field panel) so it can
                # still be selected for Delete/Add-sample/Link.
                zone_item.setData(0, Qt.UserRole, ("zone", kmp_local_path, idx))
                kmp_item.addChild(zone_item)
            self._tree.addTopLevelItem(kmp_item)
        self._tree.expandAll()

    def _selected_zone_ctx(self):
        """(multisample, zone, kmp_local_path, zone_index) for the currently
        selected zone tree item, or None if the selection isn't a zone (or
        the index is stale after an edit — callers should treat that as "no
        selection" too, not raise)."""
        items = self._tree.selectedItems()
        if not items or items[0].data(0, Qt.UserRole) is None:
            return None
        data = items[0].data(0, Qt.UserRole)
        if data[0] != "zone":
            return None
        _, kmp_local_path, zone_index = data
        m = self._kmp_cache.get(kmp_local_path)
        if m is None or not (0 <= zone_index < len(m.zones)):
            return None
        return m, m.zones[zone_index], kmp_local_path, zone_index

    def _on_tree_selection_changed(self):
        self._current_ksf = None
        self._current_ksf_path = None
        try:
            ctx = self._selected_zone_ctx()
            if ctx is None:
                self._field_group.setEnabled(False)
                return
            m, zone, kmp_local_path, _idx = ctx
            if zone.is_skipped:
                # A placeholder zone with no assigned sample — nothing to
                # load into the field panel, but it must still be selectable
                # (Delete Zone, Add/Link Sample all need a selected zone
                # with no .ksf).
                self._field_group.setEnabled(False)
                self._header_warning.setVisible(False)
                return
            path = zone.ksf_path(kmp_local_path)
            if not os.path.isfile(path):
                QMessageBox.warning(self, "Sample Editor", f"Not found locally: {path}")
                return
            with open(path, "rb") as f:
                ksf = KsfSample.open(f.read())
            if ksf is None:
                QMessageBox.warning(self, "Sample Editor", f"Not a recognizable .KSF: {path}")
                return
            ksf.path = path
            self._current_ksf = ksf
            self._current_ksf_path = path
            self._populate_fields(ksf, kmp_local_path)
        finally:
            self._update_zone_actions_enabled()

    def _selected_multisample_ctx(self):
        """(multisample, kmp_local_path) for whatever's selected — a "kmp"
        node directly, or the owning multisample of a selected "zone" node.
        None if nothing's selected or the collection isn't loaded."""
        items = self._tree.selectedItems()
        if not items or items[0].data(0, Qt.UserRole) is None:
            return None
        data = items[0].data(0, Qt.UserRole)
        kmp_local_path = data[1]
        m = self._kmp_cache.get(kmp_local_path)
        if m is None:
            return None
        return m, kmp_local_path

    def _update_zone_actions_enabled(self):
        ms_ctx = self._selected_multisample_ctx()
        zone_ctx = self._selected_zone_ctx()
        self._act_rename_multisample.setEnabled(ms_ctx is not None)
        self._act_add_zone.setEnabled(
            ms_ctx is not None and len(ms_ctx[0].zones) < _MAX_ZONES_PER_MULTISAMPLE)
        self._act_delete_zone.setEnabled(zone_ctx is not None and len(zone_ctx[0].zones) > 1)
        self._act_remove_sample.setEnabled(zone_ctx is not None and not zone_ctx[1].is_skipped)
        self._act_rename_sample.setEnabled(self._current_ksf is not None)

    def _reselect_zone(self, kmp_local_path: str, zone_index: int):
        for i in range(self._tree.topLevelItemCount()):
            kmp_item = self._tree.topLevelItem(i)
            data = kmp_item.data(0, Qt.UserRole)
            if data is None or data[1] != kmp_local_path:
                continue
            if 0 <= zone_index < kmp_item.childCount():
                self._tree.setCurrentItem(kmp_item.child(zone_index))
            else:
                self._tree.setCurrentItem(kmp_item)
            return

    def _save_multisample(self, m: KmpMultisample, kmp_local_path: str) -> bool:
        try:
            m.save(kmp_local_path)
        except Exception as e:
            QMessageBox.warning(
                self, "Sample Editor",
                f"Could not save {os.path.basename(kmp_local_path)}: {e}")
            return False
        return True

    @staticmethod
    def _delete_file_quietly(path: str):
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError as e:
            log.warning("sample editor: couldn't delete '%s': %s", path, e)

    # ── Zone management: Add / Delete / Remove Sample ───────────────────────

    def _next_zone_key_range(self, m: KmpMultisample):
        """Default key range for a new zone: append above the current last
        zone's TopKey. C# derives this from user-configurable Settings >
        Sample Editor "Create Zone Preferences" (position/range/root-key);
        this port hardcodes the same shape C#'s own default uses (append
        above the top, roughly an octave wide) rather than also porting the
        preferences UI — see CLAUDE.md's "What's left". Returns
        (original_key, top_key), or None if the keyboard is already fully
        covered (last zone's top_key == 127 — nowhere left to append)."""
        if not m.zones:
            return 60, 127   # first zone: root at middle C, spans the whole keyboard
        last = m.zones[-1]
        if last.top_key >= 127:
            return None
        top = min(last.top_key + 12, 127)
        orig = min(last.top_key + 1, top)
        return orig, top

    def _on_add_zone(self):
        ctx = self._selected_multisample_ctx()
        if ctx is None:
            return
        m, kmp_local_path = ctx
        if len(m.zones) >= _MAX_ZONES_PER_MULTISAMPLE:
            QMessageBox.warning(
                self, "Sample Editor",
                f"This multisample already has the maximum {_MAX_ZONES_PER_MULTISAMPLE} zones.")
            return
        key_range = self._next_zone_key_range(m)
        if key_range is None:
            QMessageBox.warning(
                self, "Sample Editor",
                "The keyboard is already fully covered by this multisample's zones.")
            return
        orig_key, top_key = key_range

        m.zones.append(KmpZone(original_key=orig_key, top_key=top_key,
                                filename=_SKIPPED_SAMPLE_FILENAME))
        if not self._save_multisample(m, kmp_local_path):
            m.zones.pop()
            return

        # Best-effort mirror onto a stereo sibling that was in sync (same
        # zone count) before this add — matches C#'s AddPlaceholderZone.
        # Skipped (not an error) if the sibling is already out of sync.
        sibling = find_stereo_sibling(self._kmp_cache, m)
        if sibling is not None:
            sib_path, sib_m = sibling
            if len(sib_m.zones) == len(m.zones) - 1:
                sib_m.zones.append(KmpZone(original_key=orig_key, top_key=top_key,
                                            filename=_SKIPPED_SAMPLE_FILENAME))
                self._save_multisample(sib_m, sib_path)

        self._rebuild_tree()
        self._reselect_zone(kmp_local_path, len(m.zones) - 1)
        self.statusBar().showMessage(f"Added zone [{orig_key}-{top_key}] to {m.name}{m.suffix}")

    def _on_delete_zone(self):
        """Physically removes the zone entry — the underlying .ksf is left
        untouched on disk (orphaned, not destroyed; distinct from Remove
        Sample below, which deletes the file but keeps the zone). Single-
        slot undo only ("Undo Delete Zone"), not a full Ctrl+Z/redo stack:
        C#'s own DeleteZoneCompletely is undoable via live-object-identity
        tree-patching machinery this simpler port doesn't have (no undo
        stack exists anywhere else in this window either) — a one-shot "put
        the last deleted zone back" covers the actual safety need (an
        accidental click) without building that machinery."""
        ctx = self._selected_zone_ctx()
        if ctx is None:
            return
        m, zone, kmp_local_path, idx = ctx
        if len(m.zones) <= 1:
            QMessageBox.warning(
                self, "Sample Editor",
                "Can't delete the last zone — the Kronos itself never allows an empty keymap.")
            return

        sibling = find_stereo_sibling(self._kmp_cache, m)
        sib_path = sib_m = sib_zone = None
        if sibling is not None:
            sib_path, sib_m = sibling
            if idx < len(sib_m.zones):
                if len(sib_m.zones) <= 1:
                    QMessageBox.warning(
                        self, "Sample Editor",
                        "Can't delete this zone — it would leave its stereo partner "
                        f"({sib_m.name}{sib_m.suffix}) with no zones.")
                    return
                sib_zone = sib_m.zones[idx]

        r = QMessageBox.question(
            self, "Delete Zone",
            f"Delete zone [{zone.original_key}-{zone.top_key}]?\n\n"
            "The underlying sample is not deleted — only the zone entry itself. "
            "This can be undone once with Undo Delete Zone.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r != QMessageBox.Yes:
            return

        m.zones.pop(idx)
        if sib_m is not None and sib_zone is not None:
            sib_m.zones.pop(idx)
        if not self._save_multisample(m, kmp_local_path):
            m.zones.insert(idx, zone)
            return
        if sib_m is not None and sib_zone is not None:
            self._save_multisample(sib_m, sib_path)

        self._last_deleted_zone = {
            "kmp_local_path": kmp_local_path, "index": idx, "zone": zone,
            "sib_path": sib_path, "sib_index": idx, "sib_zone": sib_zone,
        }
        self._act_undo_delete_zone.setEnabled(True)
        self._rebuild_tree()
        self._reselect_zone(kmp_local_path, max(0, idx - 1))
        self.statusBar().showMessage("Zone deleted — Undo Delete Zone to restore it")

    def _on_undo_delete_zone(self):
        state = self._last_deleted_zone
        if state is None:
            return
        kmp_local_path = state["kmp_local_path"]
        m = self._kmp_cache.get(kmp_local_path)
        if m is not None:
            idx = min(state["index"], len(m.zones))
            m.zones.insert(idx, state["zone"])
            self._save_multisample(m, kmp_local_path)
        if state["sib_zone"] is not None:
            sib_m = self._kmp_cache.get(state["sib_path"])
            if sib_m is not None:
                sib_idx = min(state["sib_index"], len(sib_m.zones))
                sib_m.zones.insert(sib_idx, state["sib_zone"])
                self._save_multisample(sib_m, state["sib_path"])

        self._last_deleted_zone = None
        self._act_undo_delete_zone.setEnabled(False)
        self._rebuild_tree()
        if m is not None:
            self._reselect_zone(kmp_local_path, state["index"])
        self.statusBar().showMessage("Zone restored")

    def _on_remove_sample(self):
        """Deletes the assigned sample but keeps the zone/key-range — the
        zone becomes a placeholder (filename -> SKIPPEDSAMPLE), same state
        as one that's never had a sample assigned. Deletes the zone's own
        .ksf file from disk AND any bare "repository" .ksf entries in the
        collection matching the same (name, suffix): every real Kronos
        sample import writes both a zone copy and a separate un-referenced-
        samples repository copy, and leaving the repository copy behind
        would let the "deleted" sample keep reappearing via a Sample
        picker — this is the actual bug C#'s own version of this action
        fixed (776334a). NOT undoable, matching C#'s own explicit "this
        cannot be undone" confirmation — unlike Delete Zone above, there's
        no reasonable single-slot undo for a real file deletion."""
        ctx = self._selected_zone_ctx()
        if ctx is None:
            return
        m, zone, kmp_local_path, idx = ctx
        if zone.is_skipped:
            QMessageBox.information(self, "Sample Editor", "This zone has no sample to remove.")
            return

        ksf_path = zone.ksf_path(kmp_local_path)
        removed_name = removed_suffix = None
        if os.path.isfile(ksf_path):
            try:
                with open(ksf_path, "rb") as f:
                    existing = KsfSample.open(f.read())
                if existing is not None:
                    removed_name, removed_suffix = existing.name, existing.suffix
            except OSError:
                pass

        r = QMessageBox.question(
            self, "Remove Sample",
            f"Remove the sample assigned to zone [{zone.original_key}-{zone.top_key}]?\n\n"
            "This deletes its audio data from disk. This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r != QMessageBox.Yes:
            return

        self._delete_file_quietly(ksf_path)
        zone.filename = _SKIPPED_SAMPLE_FILENAME
        self._dirty_ksf_paths.discard(ksf_path)

        sibling = find_stereo_sibling(self._kmp_cache, m)
        if sibling is not None:
            sib_path, sib_m = sibling
            if idx < len(sib_m.zones) and not sib_m.zones[idx].is_skipped:
                sib_zone = sib_m.zones[idx]
                sib_ksf_path = sib_zone.ksf_path(sib_path)
                self._delete_file_quietly(sib_ksf_path)
                sib_zone.filename = _SKIPPED_SAMPLE_FILENAME
                self._dirty_ksf_paths.discard(sib_ksf_path)
                self._save_multisample(sib_m, sib_path)

        if removed_name is not None:
            self._delete_matching_repository_entries(removed_name, removed_suffix)

        self._save_multisample(m, kmp_local_path)
        if self._current_ksf_path == ksf_path:
            self._current_ksf = None
            self._current_ksf_path = None
        self._rebuild_tree()
        self._reselect_zone(kmp_local_path, idx)
        self.statusBar().showMessage("Sample removed")

    def _delete_matching_repository_entries(self, name: str, suffix: str):
        """Removes every BARE (non-multisample-zone) .ksf entry in the
        collection whose (Name, Suffix) matches — Suffix matched loosely
        (-L/-R treated equivalent, matching C#'s own
        DeleteMatchingRepositoryEntries). Deletes the file and drops the
        .KSC entry, then re-saves the .KSC. Imprecise by design (inherited
        from C#, not introduced by this port): a repository sample sharing
        a name with the one just removed is deleted too, even if unrelated
        — accepted upstream as a real, known tradeoff."""
        if not self._collection or not self._local_ksc_path:
            return
        content_dir = ksc_mod.content_dir_for(self._local_ksc_path)
        suffix_set = {"-L", "-R"} if suffix in ("-L", "-R") else {suffix}
        kept: List[str] = []
        changed = False
        for entry in self._collection.entries:
            if entry.upper().endswith(".KMP"):
                kept.append(entry)
                continue
            entry_path = os.path.join(content_dir, entry)
            matched = False
            if os.path.isfile(entry_path):
                try:
                    with open(entry_path, "rb") as f:
                        s = KsfSample.open(f.read())
                    if s is not None and s.name == name and s.suffix in suffix_set:
                        matched = True
                except OSError:
                    pass
            if matched:
                self._delete_file_quietly(entry_path)
                changed = True
            else:
                kept.append(entry)
        if changed:
            self._collection.entries = kept
            try:
                self._collection.save(self._local_ksc_path)
            except Exception as e:
                log.warning("sample editor: couldn't re-save .KSC after repository cleanup: %s", e)

    # ── Rename (sample / multisample) ────────────────────────────────────────

    def _on_rename_sample(self):
        """Renames the individual sample's own Name (KsfSample.name) —
        distinct from Rename Multisample below. Applied and saved
        immediately (unlike a plain field-panel edit, which is staged until
        Save Changes): there's no separate "stereo partner" panel in this
        simpler Python UI to keep in sync with a deferred edit the way C#'s
        VM does, so mirroring has to happen right away too."""
        if self._current_ksf is None or self._current_ksf_path is None:
            return
        new_name = _prompt_kronos_name(
            self, "Rename Sample", "Sample name:", self._current_ksf.name)
        if new_name is None or new_name == self._current_ksf.name:
            return
        self._current_ksf.name = new_name
        try:
            self._current_ksf.save(self._current_ksf_path)
        except Exception as e:
            QMessageBox.warning(self, "Sample Editor", f"Could not save: {e}")
            return
        self._dirty_ksf_paths.add(self._current_ksf_path)
        self._name_edit.setText(new_name)

        ctx = self._selected_zone_ctx()
        reselect = None
        if ctx is not None:
            m, _zone, kmp_local_path, idx = ctx
            reselect = (kmp_local_path, idx)
            sibling = find_stereo_sibling(self._kmp_cache, m)
            if sibling is not None:
                sib_path, sib_m = sibling
                if idx < len(sib_m.zones) and not sib_m.zones[idx].is_skipped:
                    sib_ksf_path = sib_m.zones[idx].ksf_path(sib_path)
                    if os.path.isfile(sib_ksf_path):
                        try:
                            with open(sib_ksf_path, "rb") as f:
                                sib_ksf = KsfSample.open(f.read())
                            if sib_ksf is not None:
                                sib_ksf.name = new_name
                                sib_ksf.save(sib_ksf_path)
                                self._dirty_ksf_paths.add(sib_ksf_path)
                        except Exception as e:
                            log.warning(
                                "sample editor: couldn't mirror rename to stereo partner: %s", e)

        self._act_push.setEnabled(True)
        self._rebuild_tree()
        if reselect is not None:
            self._reselect_zone(*reselect)
        self.statusBar().showMessage(f"Renamed sample to '{new_name}'")

    def _on_rename_multisample(self):
        """Renames the multisample and immediately moves its .kmp file +
        zone-content folder to match — unlike Rename Sample above, this is
        NOT deferred: a multisample's own filename is derived from its name
        (KmpMultisample.auto_file_name), so renaming without moving would
        leave the file's name stale immediately, matching C#'s own
        immediate-move design. In-keymap .ksf files are never renamed
        (they're MS<mno1><zoneidx>-keyed, not name-keyed) — only the
        containing folder moves, taking them along with it."""
        ctx = self._selected_multisample_ctx()
        if ctx is None:
            return
        m, kmp_local_path = ctx
        new_name = _prompt_kronos_name(self, "Rename Multisample", "Multisample name:", m.name)
        if new_name is None or new_name == m.name:
            return

        sibling = find_stereo_sibling(self._kmp_cache, m)
        moves = [(m, kmp_local_path)]
        if sibling is not None:
            moves.append((sibling[1], sibling[0]))

        # Compute every new path and check for collisions BEFORE moving
        # anything — never leave a stereo pair straddling old/new locations
        # on a refused move.
        content_dir = os.path.dirname(kmp_local_path)
        planned = []
        for mm, old_path in moves:
            new_kmp_name = KmpMultisample.auto_file_name(new_name, mm.mno1)
            new_kmp_path = os.path.join(content_dir, new_kmp_name)
            if new_kmp_path != old_path and os.path.exists(new_kmp_path):
                QMessageBox.warning(
                    self, "Sample Editor", f"Can't rename — '{new_kmp_name}' already exists.")
                return
            old_folder = os.path.splitext(old_path)[0]
            new_folder = os.path.splitext(new_kmp_path)[0]
            if new_folder != old_folder and os.path.exists(new_folder):
                QMessageBox.warning(
                    self, "Sample Editor",
                    f"Can't rename — a folder named '{os.path.basename(new_folder)}' "
                    "already exists.")
                return
            planned.append((mm, old_path, new_kmp_path, old_folder, new_folder))

        for mm, old_path, new_kmp_path, old_folder, new_folder in planned:
            try:
                if new_folder != old_folder and os.path.isdir(old_folder):
                    shutil.move(old_folder, new_folder)
                    remapped = set()
                    for p in self._dirty_ksf_paths:
                        if p.startswith(old_folder + os.sep):
                            remapped.add(new_folder + p[len(old_folder):])
                        else:
                            remapped.add(p)
                    self._dirty_ksf_paths = remapped
                old_entry = os.path.basename(old_path)
                new_entry = os.path.basename(new_kmp_path)
                if new_kmp_path != old_path and os.path.isfile(old_path):
                    os.remove(old_path)
                mm.name = new_name
                mm.path = new_kmp_path
                mm.save(new_kmp_path)
                if self._collection is not None:
                    self._collection.entries = [
                        new_entry if e == old_entry else e for e in self._collection.entries]
                del self._kmp_cache[old_path]
                self._kmp_cache[new_kmp_path] = mm
            except OSError as e:
                QMessageBox.warning(self, "Sample Editor", f"Rename failed partway through: {e}")
                self._rebuild_tree()
                return

        if self._collection is not None and self._local_ksc_path is not None:
            try:
                self._collection.save(self._local_ksc_path)
            except Exception as e:
                log.warning("sample editor: couldn't re-save .KSC after rename: %s", e)

        self._current_ksf = None
        self._current_ksf_path = None
        self._act_push.setEnabled(True)
        self._rebuild_tree()
        self._reselect_zone(m.path, -1)   # -1: always falls through to selecting the kmp node itself
        self.statusBar().showMessage(f"Renamed multisample to '{new_name}'")

    # ── Field panel ──────────────────────────────────────────────────────────

    _GENERIC_HEADER_ONLY_WARNING = (
        "This sample has no audio data (header-only). It may be a legitimate\n"
        "link to another sample, or data loss — there is no way to tell from the\n"
        "file alone. It cannot be edited or pushed by this tool.")

    def _describe_header_only(self, ksf: KsfSample, kmp_local_path: Optional[str]) -> str:
        """Resolves ksf's SMF1 link (Core/sample_link_resolver.py, read-only
        — this app doesn't write links yet) to tell "deliberate link" apart
        from "data loss" whenever it can, matching C#'s
        SampleIsLinkedStub/SampleLinkTargetFile warning text. Falls back to
        the generic ambiguous warning when there's no SMF1 target, or it
        doesn't resolve to anything playable locally."""
        if kmp_local_path is not None:
            resolution = sample_link_resolver.resolve(ksf, kmp_local_path)
            if resolution is not None:
                verified = "" if resolution.sno1_verified else " (best guess — SNO1 didn't match)"
                return (f"Linked sample — plays '{os.path.basename(resolution.target_path)}'"
                        f"{verified}. It cannot be edited or pushed by this tool.")
        return self._GENERIC_HEADER_ONLY_WARNING

    def _populate_fields(self, ksf: KsfSample, kmp_local_path: Optional[str] = None):
        self._loading_fields = True
        try:
            self._header_warning.setVisible(ksf.is_header_only)
            self._field_group.setEnabled(not ksf.is_header_only)
            if ksf.is_header_only:
                self._header_warning.setText(self._describe_header_only(ksf, kmp_local_path))

            self._name_edit.setText(ksf.name)
            duration_s = ksf.frame_count / ksf.sample_rate if ksf.sample_rate else 0.0
            self._info_label.setText(
                f"{ksf.sample_rate} Hz, {ksf.frame_count:,} frames ({duration_s:.2f}s), "
                f"{ksf.bits}-bit")

            max_frame = max(0, ksf.frame_count - 1)
            self._loop_start_spin.setRange(0, max_frame)
            self._loop_end_spin.setRange(0, max_frame)
            self._loop_start_spin.setValue(min(ksf.loop_start, max_frame))
            self._loop_end_spin.setValue(min(ksf.loop_end, max_frame))

            self._loop_enabled_check.setChecked(ksf.is_loop_enabled)
            self._reverse_check.setChecked(ksf.is_reversed)
            self._boost_check.setChecked(ksf.is_12db_boost_enabled)
        finally:
            self._loading_fields = False

    def _mark_dirty_from_field(self, *_args):
        if self._loading_fields or self._current_ksf is None:
            return
        self._act_save.setEnabled(True)

    def _apply_fields_to_current(self):
        """Copy the form's current values onto self._current_ksf — does not
        touch disk (see _on_save_changes)."""
        ksf = self._current_ksf
        if ksf is None or ksf.is_header_only:
            return
        ksf.name = self._name_edit.text().strip() or ksf.name
        ksf.loop_start = self._loop_start_spin.value()
        ksf.loop_end = self._loop_end_spin.value()
        # One-shot bit is the INVERSE of "loop enabled" — see FLAG_ONE_SHOT.
        ksf.flags = (ksf.flags & ~0x80) if self._loop_enabled_check.isChecked() else (ksf.flags | 0x80)
        ksf.is_reversed = self._reverse_check.isChecked()
        ksf.is_12db_boost_enabled = self._boost_check.isChecked()

    def _on_save_changes(self):
        if self._current_ksf is None or self._current_ksf_path is None:
            return
        self._apply_fields_to_current()
        try:
            self._current_ksf.save(self._current_ksf_path)
        except Exception as e:
            QMessageBox.warning(self, "Sample Editor", f"Could not save locally: {e}")
            return
        self._dirty_ksf_paths.add(self._current_ksf_path)
        self._act_save.setEnabled(False)
        self._act_push.setEnabled(True)
        self.statusBar().showMessage(
            f"Saved locally — {len(self._dirty_ksf_paths)} sample(s) changed, not yet pushed")

    # ── Push to Kronos ───────────────────────────────────────────────────────

    def _on_push_to_kronos(self):
        if not self._dirty_ksf_paths or not self._collection or not self._local_ksc_path:
            return
        r = QMessageBox.question(
            self, "Push to Kronos",
            f"Push {len(self._dirty_ksf_paths)} changed sample(s) back to the Kronos "
            f"at {self._remote_dest_dir}?\n\n"
            "This overwrites the matching files on the Kronos's SSD.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r != QMessageBox.Yes:
            return

        self._progress_dlg = QProgressDialog("Uploading…", None, 0, 0, self)
        self._progress_dlg.setWindowModality(Qt.WindowModal)
        self._progress_dlg.setMinimumDuration(0)
        self._progress_dlg.show()

        local_ksc_path = self._local_ksc_path
        collection = self._collection
        remote_dest_dir = self._remote_dest_dir
        only_paths = set(self._dirty_ksf_paths)

        def bg():
            from Tools.file_manager import _FtpWorker
            try:
                ftp = _FtpWorker(self._host, self._ftp_port, self._user, self._pass)
                ftp.connect()
                failures = sample_ftp.push_closure(
                    ftp, local_ksc_path, collection, remote_dest_dir,
                    on_progress=lambda msg: self._progress.emit(msg),
                    only_ksf_paths=only_paths)
                self._push_done.emit(failures, None)
            except Exception as e:
                log.exception("sample push failed")
                self._push_done.emit([], str(e))

        threading.Thread(target=bg, daemon=True, name="SampleEditorPush").start()

    @Slot(list, object)
    def _on_push_done(self, failures: List[str], error):
        if getattr(self, "_progress_dlg", None):
            self._progress_dlg.close()
            self._progress_dlg = None
        if error:
            QMessageBox.warning(self, "Sample Editor", f"Push failed: {error}")
            return
        if failures:
            QMessageBox.warning(
                self, "Sample Editor",
                "Some files did not push successfully:\n\n" + "\n".join(failures[:20]))
        else:
            self._dirty_ksf_paths.clear()
            self._act_push.setEnabled(False)
            self.statusBar().showMessage("Pushed to Kronos successfully")
