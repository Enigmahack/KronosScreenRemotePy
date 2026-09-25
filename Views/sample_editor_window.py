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
import threading
from typing import Dict, List, Optional, Set

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPushButton, QSpinBox, QSplitter, QStatusBar,
    QToolBar, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget, QCheckBox,
    QProgressDialog,
)

import Core.sample_ftp as sample_ftp
import Data.ksc_collection as ksc_mod
from Data.ksc_collection import KscCollection
from Data.kmp_multisample import KmpMultisample
from Data.ksf_sample import KsfSample
import Models.storage as storage
import Utils.theme as T

log = logging.getLogger(__name__)


def _workspace_root() -> str:
    root = storage.data_dir() / "SampleEditorWorkspace"
    root.mkdir(parents=True, exist_ok=True)
    return str(root)


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

        splitter = QSplitter(Qt.Horizontal)
        self.setCentralWidget(splitter)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Multisample / Zone"])
        self._tree.itemSelectionChanged.connect(self._on_tree_selection_changed)
        splitter.addWidget(self._tree)

        panel = QWidget()
        panel_layout = QVBoxLayout(panel)

        self._header_warning = QLabel(
            "This sample has no audio data (header-only). It may be a legitimate\n"
            "link to another sample, or data loss — there is no way to tell from the\n"
            "file alone. It cannot be edited or pushed by this tool.")
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
            for zone in m.zones:
                if zone.is_skipped:
                    zlabel = f"[{zone.original_key}-{zone.top_key}] (skipped)"
                    zone_item = QTreeWidgetItem([zlabel])
                    zone_item.setDisabled(True)
                else:
                    zlabel = f"[{zone.original_key}-{zone.top_key}] {zone.filename}"
                    zone_item = QTreeWidgetItem([zlabel])
                    zone_item.setData(0, Qt.UserRole, ("zone", zone.ksf_path(kmp_local_path)))
                kmp_item.addChild(zone_item)
            self._tree.addTopLevelItem(kmp_item)
        self._tree.expandAll()

    def _on_tree_selection_changed(self):
        items = self._tree.selectedItems()
        if not items or items[0].data(0, Qt.UserRole) is None:
            self._current_ksf = None
            self._current_ksf_path = None
            self._field_group.setEnabled(False)
            return
        kind, path = items[0].data(0, Qt.UserRole)
        if kind != "zone":
            self._field_group.setEnabled(False)
            return
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
        self._populate_fields(ksf)

    # ── Field panel ──────────────────────────────────────────────────────────

    def _populate_fields(self, ksf: KsfSample):
        self._loading_fields = True
        try:
            self._header_warning.setVisible(ksf.is_header_only)
            self._field_group.setEnabled(not ksf.is_header_only)

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
