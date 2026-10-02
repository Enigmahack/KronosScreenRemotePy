"""The Sample Editor's FTP seam — port of Views/KronosRemoteSampleSource.cs + SampleRemoteBrowserDialog.

The model (Core/sample_editor_model/io.py) calls a duck-typed source with three blocking methods; this implements
them against the real Kronos over `Tools.file_manager._FtpWorker`. Each blocking call runs its network work on a
worker thread under a modal progress dialog that keeps the event loop spinning, so the GUI never freezes and the
model still sees a plain synchronous call.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import QEventLoop, Qt, QTimer
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QMessageBox, QProgressDialog, QPushButton, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout)

import Core.sample_ftp as sample_ftp
import Utils.theme as T
from Core.sample_editor_model.io import RemotePullResult, RemotePushResult
from Views.sample_editor_dialogs import confirm, prompt_text

log = logging.getLogger(__name__)

PUSH_TIMEOUT_NOTE = ("the Kronos stopped responding partway through - nothing on it is guaranteed complete, "
                     "check the file there before relying on it")


def run_blocking(parent, label: str, work: Callable[[Callable[[str], None]], object]):
    """Runs `work(on_progress)` on a thread under a modal progress dialog; returns (result, error)."""
    dlg = QProgressDialog(label, None, 0, 0, parent)
    dlg.setWindowModality(Qt.WindowModality.WindowModal)
    dlg.setMinimumDuration(0)
    dlg.setWindowTitle("Sample Editor")
    dlg.show()
    loop = QEventLoop()
    box: Dict[str, object] = {"result": None, "error": None, "msg": None}

    def bg() -> None:
        try:
            box["result"] = work(lambda m: box.__setitem__("msg", m))
        except Exception as ex:                                  # noqa: BLE001 — reported to the caller
            log.exception("sample remote operation failed")
            box["error"] = ex
        finally:
            loop.quit()

    t = threading.Thread(target=bg, daemon=True, name="SampleRemote")
    poll = QTimer()
    poll.setInterval(100)
    poll.timeout.connect(lambda: dlg.setLabelText(box["msg"]) if box["msg"] else None)
    poll.start()
    t.start()
    if t.is_alive():
        loop.exec()
    poll.stop()
    dlg.close()
    return box["result"], box["error"]


class RemoteBrowserDialog(QDialog):
    """FTP tree browser. mode 'file': pick one file with `extension`. mode 'folder': pick a destination folder.
    Includes the folder-management actions (New Folder / Rename / Delete / Refresh) behind the same top-level
    storage-volume guards the File Manager uses — those are enforced inside _FtpWorker too, not only here."""

    def __init__(self, ftp, mode: str = "file", extension: str = ".KSC", parent=None):
        super().__init__(parent)
        self._ftp = ftp
        self._mode = mode
        self._ext = extension.upper()
        self.selected_path: Optional[str] = None
        self.setWindowTitle("Push to Kronos" if mode == "folder" else
                            f"Load {'Multisample' if self._ext == '.KMP' else 'Sample Collection'} from Kronos")
        self.resize(480, 560)

        lay = QVBoxLayout(self)
        info = QLabel("Select the destination folder on the Kronos's SSD." if mode == "folder"
                      else f"Select a {self._ext} file on the Kronos's SSD.")
        info.setStyleSheet(f"color: {T.TEXT_DIM};")
        lay.addWidget(info)

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.itemExpanded.connect(self._on_expand)
        self._tree.itemDoubleClicked.connect(self._on_double_click)
        self._tree.itemSelectionChanged.connect(self._on_selection_changed)
        lay.addWidget(self._tree, 1)

        row = QHBoxLayout()
        self._new_btn = QPushButton("New Folder...")
        self._new_btn.clicked.connect(self._on_new_folder)
        self._rename_btn = QPushButton("Rename...")
        self._rename_btn.setEnabled(False)
        self._rename_btn.clicked.connect(self._on_rename)
        self._delete_btn = QPushButton("Delete")
        self._delete_btn.setEnabled(False)
        self._delete_btn.clicked.connect(self._on_delete)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._on_refresh)
        for b in (self._new_btn, self._rename_btn, self._delete_btn, refresh):
            row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)

        btns = QHBoxLayout()
        btns.addStretch()
        self._ok = QPushButton("Push Here" if mode == "folder" else "Load")
        self._ok.setEnabled(False)
        self._ok.clicked.connect(self.accept)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        btns.addWidget(self._ok)
        btns.addWidget(cancel)
        lay.addLayout(btns)
        self._populate(None, "/")

    # -- tree ------------------------------------------------------------------------------

    def _populate(self, parent_item: Optional[QTreeWidgetItem], path: str) -> None:
        try:
            entries = self._ftp.list_dir(path)
        except Exception as ex:
            log.warning("sample browser: list_dir('%s') failed: %s", path, ex)
            return
        entries.sort(key=lambda e: (not e.is_directory, e.name.lower()))
        for e in entries:
            if e.is_directory:
                item = QTreeWidgetItem([f"\U0001F4C1 {e.name}"])
                item.setData(0, Qt.ItemDataRole.UserRole, ("dir", e.full_path))
                item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
            elif self._mode == "file" and e.name.upper().endswith(self._ext):
                item = QTreeWidgetItem([e.name])
                item.setData(0, Qt.ItemDataRole.UserRole, ("file", e.full_path))
            else:
                continue
            (self._tree.addTopLevelItem if parent_item is None else parent_item.addChild)(item)

    def _on_expand(self, item: QTreeWidgetItem) -> None:
        kind, path = item.data(0, Qt.ItemDataRole.UserRole)
        if item.childCount() == 0 and kind == "dir":
            self._populate(item, path)

    def _on_selection_changed(self) -> None:
        items = self._tree.selectedItems()
        if not items:
            self._ok.setEnabled(False)
            self._rename_btn.setEnabled(False)
            self._delete_btn.setEnabled(False)
            return
        kind, path = items[0].data(0, Qt.ItemDataRole.UserRole)
        want = "dir" if self._mode == "folder" else "file"
        self._ok.setEnabled(kind == want)
        if kind == want:
            self.selected_path = path
        # Top-level storage volumes (SSD1/SSD2/...) can never be renamed or deleted.
        from Tools.file_manager import _is_top_level_ftp_path
        top = _is_top_level_ftp_path(path)
        self._rename_btn.setEnabled(not top)
        self._delete_btn.setEnabled(not top)

    def _on_double_click(self, item: QTreeWidgetItem, _col: int) -> None:
        kind, path = item.data(0, Qt.ItemDataRole.UserRole)
        if self._mode == "file" and kind == "file":
            self.selected_path = path
            self.accept()

    # -- folder management -----------------------------------------------------------------

    def _selected_dir(self) -> str:
        items = self._tree.selectedItems()
        if not items:
            return "/"
        kind, path = items[0].data(0, Qt.ItemDataRole.UserRole)
        return path if kind == "dir" else (path.rsplit("/", 1)[0] or "/")

    def _on_new_folder(self) -> None:
        name = prompt_text(self, "New Folder", "Folder name:", "NewFolder")
        if not name or not name.strip():
            return
        try:
            self._ftp.mkdir(f"{self._selected_dir().rstrip('/')}/{name.strip()}")
        except Exception as ex:
            QMessageBox.warning(self, "Sample Editor", f"Could not create folder: {ex}")
            return
        self._on_refresh()

    def _on_rename(self) -> None:
        items = self._tree.selectedItems()
        if not items:
            return
        _kind, path = items[0].data(0, Qt.ItemDataRole.UserRole)
        current = path.rsplit("/", 1)[-1]
        new = prompt_text(self, "Rename", "New name:", current)
        if not new or not new.strip() or new.strip() == current:
            return
        try:
            self._ftp.rename(path, f"{path.rsplit('/', 1)[0]}/{new.strip()}")
        except Exception as ex:
            QMessageBox.warning(self, "Sample Editor", f"Rename failed: {ex}")
            return
        self._on_refresh()

    def _on_delete(self) -> None:
        items = self._tree.selectedItems()
        if not items:
            return
        kind, path = items[0].data(0, Qt.ItemDataRole.UserRole)
        name = path.rsplit("/", 1)[-1]
        if not confirm(self, "Delete", f"Delete '{name}'{' and everything inside it' if kind == 'dir' else ''}?"):
            return
        try:
            (self._ftp.delete_dir if kind == "dir" else self._ftp.delete_file)(path)
        except Exception as ex:
            QMessageBox.warning(self, "Sample Editor", f"Delete failed: {ex}")
            return
        self._on_refresh()

    def _on_refresh(self) -> None:
        self._tree.clear()
        self.selected_path = None
        self._populate(None, "/")


class KronosRemoteSampleSource:
    """Production source for the model. A fresh instance per call is fine — it only captures the owner window and
    the connection settings, all of which can change between calls."""

    def __init__(self, owner, host: str, port: int, user: str, password: str):
        self._owner = owner
        self._host, self._port, self._user, self._pass = host, port, user, password

    def _connect(self):
        from Tools.file_manager import _FtpWorker
        ftp = _FtpWorker(self._host, self._port, self._user, self._pass)
        ftp.connect()
        return ftp

    def pick_and_pull(self, extension: str, local_root: str) -> RemotePullResult:
        ftp, err = run_blocking(self._owner, "Connecting...", lambda _p: self._connect())
        if err is not None:
            return RemotePullResult(None, f"Could not connect to the Kronos: {err}")
        try:
            dlg = RemoteBrowserDialog(ftp, "file", extension, self._owner)
            if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.selected_path:
                return RemotePullResult(None, "Load from Kronos cancelled.")
            remote = dlg.selected_path
            res, err = run_blocking(self._owner, "Downloading...",
                                    lambda prog: sample_ftp.pull(ftp, remote, local_root, on_progress=prog))
            if err is not None:
                return RemotePullResult(None, f"Load failed: {err}")
            local_path, remote_map, failures = res
            if failures:
                QMessageBox.warning(self._owner, "Sample Editor",
                                    "Some files could not be downloaded and are missing locally:\n\n"
                                    + "\n".join(failures[:20]) + ("\n..." if len(failures) > 20 else ""))
            return RemotePullResult(local_path, "", dict(remote_map))
        finally:
            try:
                ftp.disconnect()
            except Exception:
                pass

    def push(self, local_path: str, remote_path: str) -> RemotePushResult:
        def work(prog):
            ftp = self._connect()
            try:
                failures: List[str] = []
                sample_ftp._upload_one(ftp, local_path, remote_path, prog, failures)
                return failures
            finally:
                try:
                    ftp.disconnect()
                except Exception:
                    pass

        failures, err = run_blocking(self._owner, "Uploading...", work)
        name = os.path.basename(local_path)
        if err is not None:
            return RemotePushResult(f"Push failed: {err}")
        if failures:
            return RemotePushResult(f"Push failed: {failures[0]}")
        return RemotePushResult(f"Pushed '{name}' to the Kronos.")

    def pick_folder_and_push_collection(self, local_ksc_path: str, collection) -> RemotePushResult:
        ftp, err = run_blocking(self._owner, "Connecting...", lambda _p: self._connect())
        if err is not None:
            return RemotePushResult(f"Could not connect to the Kronos: {err}")
        try:
            dlg = RemoteBrowserDialog(ftp, "folder", "", self._owner)
            if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.selected_path:
                return RemotePushResult("Push to Kronos cancelled.")
            dest = dlg.selected_path
            failures, err = run_blocking(
                self._owner, "Uploading...",
                lambda prog: sample_ftp.push_closure(ftp, local_ksc_path, collection, dest, on_progress=prog))
            if err is not None:
                return RemotePushResult(f"Push failed: {err}")
            if failures:
                QMessageBox.warning(self._owner, "Sample Editor",
                                    "Some files did not push successfully:\n\n" + "\n".join(failures[:20]))
                return RemotePushResult(f"Push finished with {len(failures)} failure(s).")
            return RemotePushResult(f"Pushed '{os.path.basename(local_ksc_path)}' and its content to '{dest}' on the Kronos.")
        finally:
            try:
                ftp.disconnect()
            except Exception:
                pass
