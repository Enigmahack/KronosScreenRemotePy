"""The Sample Editor's FTP seam — port of Views/KronosRemoteSampleSource.cs + SampleRemoteBrowserDialog.

The model (Core/sample_editor_model/io.py) calls a duck-typed source with three blocking methods; this implements
them against the real Kronos over `Tools.file_manager._FtpWorker`. Connect, download and upload run on a worker
thread under a window-modal busy dialog whose nested event loop keeps the window painting, so the model still sees a
plain synchronous call. The folder browser runs its own FTP calls (listings, New Folder / Rename / Delete) on worker
threads too. Closing the browser never waits on the instrument: `release_when_idle` lets the browser's outstanding
FTP call (a recursive folder Delete can take long) finish on its own thread, then disconnects there and hands the
dialog back to the GUI thread for deletion.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import QEventLoop, QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QMessageBox, QProgressDialog, QPushButton, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout)

import Core.sample_ftp as sample_ftp
import Utils.theme as T
from Core.sample_editor_model.io import RemotePullResult, RemotePushResult
from Views.sample_editor_dialogs import confirm, prompt_text

log = logging.getLogger(__name__)

PUSH_TIMEOUT_NOTE = ("the instrument stopped responding partway through - nothing on it is guaranteed complete, "
                     "check the file there before relying on it")


class _LockedProgressDialog(QProgressDialog):
    """A busy dialog the user cannot dismiss: with no cancel button a plain QProgressDialog still hides on Escape
    or its title-bar close button, which un-blocks the window while the worker is still running."""

    def __init__(self, label: str, parent=None):
        super().__init__(label, None, 0, 0, parent)
        self._done = False

    def finish(self) -> None:
        self._done = True
        self.close()

    def reject(self) -> None:                                   # Escape
        if self._done:
            super().reject()

    def closeEvent(self, e) -> None:
        if self._done:
            super().closeEvent(e)
        else:
            e.ignore()


def run_blocking(parent, label: str, work: Callable[[Callable[[str], None]], object]):
    """Runs `work(on_progress)` on a thread under a modal progress dialog; returns (result, error).

    `work` must not touch Qt widgets or the Sample Editor model's observable state (their listeners repaint widgets);
    it gets plain inputs and returns a plain result for the caller to apply on the GUI thread."""
    dlg = _LockedProgressDialog(label, parent)
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
            log.exception("sample editor background operation failed")
            box["error"] = ex

    t = threading.Thread(target=bg, daemon=True, name="SampleRemote")
    poll = QTimer()
    poll.setInterval(30)

    def _tick() -> None:
        # The loop is quit from here, not from the worker: a quit() issued before exec() is dropped,
        # which hung the dialog forever when the work failed instantly.
        if box["msg"]:
            dlg.setLabelText(box["msg"])
        if not t.is_alive():
            loop.quit()

    poll.timeout.connect(_tick)
    poll.start()
    t.start()
    loop.exec()
    poll.stop()
    dlg.finish()
    dlg.deleteLater()
    return box["result"], box["error"]


class _Relay(QObject):
    """Carries a worker thread's (callback, result, error) back to the GUI thread (queued: the relay lives there)."""
    done = Signal(object, object, object)
    released = Signal()                 # release_when_idle finished: the dialog may be deleted (GUI thread)


# Dialogs whose FTP work is still finishing after they closed: kept referenced here (and released on the GUI thread)
# so a parentless dialog's wrapper is never the last reference dropped on a worker thread.
_PENDING_RELEASE: set = set()
# A new session must not start while an earlier recursive Delete is still running: it could delete files just pushed.
_STILL_FINISHING = "The instrument is still finishing the previous folder change - try again in a moment."


def release_when_idle(dlg: Optional["RemoteBrowserDialog"], ftp) -> None:
    """Non-blocking teardown after the browser closed: on a worker thread, wait for the dialog's outstanding FTP
    calls (no time limit - the instrument decides), then `ftp.disconnect()` (which takes _FtpWorker's lock, so it
    would also wait), then delete the dialog back on the GUI thread. The caller returns immediately."""
    threads = list(dlg._threads) if dlg is not None else []
    relay = dlg._relay if dlg is not None else None
    finished = dlg.release_done if dlg is not None else threading.Event()
    if dlg is not None:
        _PENDING_RELEASE.add(dlg)

    def bg() -> None:
        for t in threads:
            t.join()
        try:
            ftp.disconnect()
        except Exception:                                        # noqa: BLE001 - the session is over either way
            log.debug("sample browser: disconnect failed", exc_info=True)
        finished.set()
        if relay is not None:
            try:
                relay.released.emit()
            except RuntimeError:                                 # already deleted (application shutting down)
                pass

    threading.Thread(target=bg, daemon=True, name="SampleBrowserRelease").start()


class RemoteBrowserDialog(QDialog):
    """FTP tree browser. mode 'file': pick one file with `extension`. mode 'folder': pick a destination folder.
    Includes the folder-management actions (New Folder / Rename / Delete / Refresh) behind the same top-level
    storage-volume guards the File Manager uses — those are enforced inside _FtpWorker too, not only here.

    Every FTP call (listing, mkdir, rename, delete - a recursive folder delete can take a long time) runs on a worker
    thread; results come back through `_Relay`. A listing shows a 'Loading...' child meanwhile and is dropped if the
    tree was refreshed (or the dialog closed) before it arrived. A changing operation disables the tree and buttons
    until it finishes. _FtpWorker serializes its own calls, so overlapping listings simply queue."""

    def __init__(self, ftp, mode: str = "file", extension: str = ".KSC", parent=None):
        super().__init__(parent)
        self._ftp = ftp
        self._mode = mode
        self._ext = extension.upper()
        self.selected_path: Optional[str] = None
        self._generation = 0          # bumped by every refresh: a listing for an older tree is stale
        self._closed = False
        self._mutating = False
        self._threads: List[threading.Thread] = []
        self.closed_while_busy = False   # closed while a folder change was still running on the instrument
        self.release_done = threading.Event()     # set once release_when_idle has disconnected
        self._relay = _Relay(self)
        self._relay.done.connect(self._on_worker_done)
        self._relay.released.connect(self._on_released)
        self.setWindowTitle("Push to Instrument" if mode == "folder" else
                            f"Load {'Multisample' if self._ext == '.KMP' else 'Sample Collection'} from Instrument")
        self.resize(480, 560)

        lay = QVBoxLayout(self)
        info = QLabel("Select the destination folder on the instrument's SSD." if mode == "folder"
                      else f"Select a {self._ext} file on the instrument's SSD.")
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
        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(self._on_refresh)
        for b in (self._new_btn, self._rename_btn, self._delete_btn, self._refresh_btn):
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

    # -- worker plumbing -------------------------------------------------------------------

    def _run_async(self, fn: Callable[[], object], on_done: Callable[[object, Optional[Exception]], None]) -> None:
        relay = self._relay

        def bg() -> None:
            try:
                result, err = fn(), None
            except Exception as ex:                              # noqa: BLE001 - handed to on_done
                result, err = None, ex
            try:
                relay.done.emit(on_done, result, err)
            except RuntimeError:                                 # the dialog (and its relay) are already gone
                pass

        self._threads = [t for t in self._threads if t.is_alive()]
        t = threading.Thread(target=bg, daemon=True, name="SampleBrowserFtp")
        self._threads.append(t)
        t.start()

    def _on_worker_done(self, on_done, result, err) -> None:
        if not self._closed:
            on_done(result, err)

    def done(self, r: int) -> None:
        # Closing never waits for the worker: the dialog goes inert (late results are dropped by _on_worker_done)
        # and the owner hands the connection to release_when_idle.
        self._closed = True
        self.setEnabled(False)
        if self._mutating:
            self.closed_while_busy = True
            QApplication.restoreOverrideCursor()
            self._mutating = False
        super().done(r)

    def _on_released(self) -> None:
        _PENDING_RELEASE.discard(self)
        self.deleteLater()

    def wait_idle(self, timeout_s: float = 10.0) -> bool:
        """Joins outstanding FTP work (blocking; for tests and non-GUI callers). True when nothing is left."""
        deadline = time.monotonic() + timeout_s
        for t in self._threads:
            t.join(max(0.0, deadline - time.monotonic()))
        return not any(t.is_alive() for t in self._threads)

    def _set_mutating(self, on: bool) -> None:
        if on == self._mutating:
            return
        self._mutating = on
        for w in (self._tree, self._new_btn, self._refresh_btn):
            w.setEnabled(not on)
        if on:
            for w in (self._ok, self._rename_btn, self._delete_btn):
                w.setEnabled(False)
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        else:
            QApplication.restoreOverrideCursor()
            self._on_selection_changed()

    def _mutate(self, fn: Callable[[], object], failure: str) -> None:
        """A changing FTP call on the worker with the dialog locked; then report a failure, or refresh."""
        self._set_mutating(True)

        def finished(_result, err) -> None:
            self._set_mutating(False)
            if err is not None:
                QMessageBox.warning(self, "Sample Editor", f"{failure}: {err}")
                return
            self._on_refresh()

        self._run_async(fn, finished)

    # -- tree ------------------------------------------------------------------------------

    def _populate(self, parent_item: Optional[QTreeWidgetItem], path: str) -> None:
        placeholder = QTreeWidgetItem(["Loading..."])
        placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
        placeholder.setData(0, Qt.ItemDataRole.UserRole, ("loading", path))
        (self._tree.addTopLevelItem if parent_item is None else parent_item.addChild)(placeholder)
        generation = self._generation

        def listed(entries, err) -> None:
            if generation != self._generation:
                return                                           # the tree was rebuilt since: these items are gone
            parent = self._tree.invisibleRootItem() if parent_item is None else parent_item
            idx = parent.indexOfChild(placeholder)
            if idx >= 0:
                parent.takeChild(idx)
            if err is not None:
                log.warning("sample browser: list_dir('%s') failed: %s", path, err)
                return
            self._add_entries(parent_item, entries)

        ftp = self._ftp
        self._run_async(lambda: ftp.list_dir(path), listed)

    def _add_entries(self, parent_item: Optional[QTreeWidgetItem], entries) -> None:
        for e in sorted(entries, key=lambda e: (not e.is_directory, e.name.lower())):
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
        entry = self._selected_entry()
        if self._mutating or entry is None:
            self._ok.setEnabled(False)
            self._rename_btn.setEnabled(False)
            self._delete_btn.setEnabled(False)
            return
        kind, path = entry
        want ="dir" if self._mode == "folder" else "file"
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
        if self._mode == "file" and kind == "file" and not self._mutating:
            self.selected_path = path
            self.accept()

    # -- folder management -----------------------------------------------------------------

    def _selected_entry(self) -> Optional[Tuple[str, str]]:
        """(kind, path) of the selected dir/file row; None for no selection or a 'Loading...' row."""
        items = self._tree.selectedItems()
        data = items[0].data(0, Qt.ItemDataRole.UserRole) if items else None
        return (data[0], data[1]) if data and data[0] in ("dir", "file") else None

    def _selected_dir(self) -> str:
        entry = self._selected_entry()
        if entry is None:
            return "/"
        kind, path = entry
        return path if kind == "dir" else (path.rsplit("/", 1)[0] or "/")

    def _on_new_folder(self) -> None:
        name = prompt_text(self, "New Folder", "Folder name:", "NewFolder")
        if not name or not name.strip():
            return
        target, ftp = f"{self._selected_dir().rstrip('/')}/{name.strip()}", self._ftp
        self._mutate(lambda: ftp.mkdir(target), "Could not create folder")

    def _on_rename(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        _kind, path = entry
        current = path.rsplit("/", 1)[-1]
        new = prompt_text(self, "Rename", "New name:", current)
        if not new or not new.strip() or new.strip() == current:
            return
        new = new.strip()
        from Core.sample_ftp import unsafe_name_reason
        bad = unsafe_name_reason(new)
        target, ftp = f"{path.rsplit('/', 1)[0]}/{new}", self._ftp

        def _rename() -> None:
            if bad:
                raise OSError(f"'{new}' is not a valid name - {bad}")
            # Both instruments accept RNTO over an existing file, which would destroy it.
            if ftp.file_exists(target):
                raise OSError(f"'{new}' already exists in that folder")
            ftp.rename(path, target)
        self._mutate(_rename, "Rename failed")

    def _on_delete(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        kind, path = entry
        name = path.rsplit("/", 1)[-1]
        if not confirm(self, "Delete", f"Delete '{name}'{' and everything inside it' if kind == 'dir' else ''}?"):
            return
        op = self._ftp.delete_dir if kind == "dir" else self._ftp.delete_file
        self._mutate(lambda: op(path), "Delete failed")

    def _on_refresh(self) -> None:
        self._generation += 1
        self._tree.clear()
        self.selected_path = None
        self._populate(None, "/")


def _busy_note(dlg: RemoteBrowserDialog) -> str:
    return (" The folder change already started finishes on the instrument in the background."
            if dlg.closed_while_busy else "")


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
        if _PENDING_RELEASE:   # a closed browser's folder change is still finishing on the instrument
            return RemotePullResult(None, _STILL_FINISHING)
        ftp, err = run_blocking(self._owner, "Connecting...", lambda _p: self._connect())
        if err is not None:
            return RemotePullResult(None, f"Could not connect to the instrument: {err}")
        dlg = None
        try:
            dlg = RemoteBrowserDialog(ftp, "file", extension, self._owner)
            if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.selected_path:
                return RemotePullResult(None, "Load from Instrument cancelled." + _busy_note(dlg))
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
            release_when_idle(dlg, ftp)             # never blocks: its threads still use `ftp`

    def push(self, local_path: str, remote_path: str) -> RemotePushResult:
        if _PENDING_RELEASE:
            return RemotePushResult(_STILL_FINISHING)
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
        return RemotePushResult(f"Pushed '{name}' to the instrument.")

    def pick_folder_and_push_collection(self, local_ksc_path: str, collection) -> RemotePushResult:
        if _PENDING_RELEASE:
            return RemotePushResult(_STILL_FINISHING)
        ftp, err = run_blocking(self._owner, "Connecting...", lambda _p: self._connect())
        if err is not None:
            return RemotePushResult(f"Could not connect to the instrument: {err}")
        dlg = None
        try:
            dlg = RemoteBrowserDialog(ftp, "folder", "", self._owner)
            if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.selected_path:
                return RemotePushResult("Push to Instrument cancelled." + _busy_note(dlg))
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
            return RemotePushResult(f"Pushed '{os.path.basename(local_ksc_path)}' and its content to '{dest}' on the instrument.")
        finally:
            release_when_idle(dlg, ftp)             # never blocks: its threads still use `ftp`
