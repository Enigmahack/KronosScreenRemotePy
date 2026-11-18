"""
FileManagerWindow — dual-pane FTP file manager (local ↔ Kronos).

Mirrors the C# FileManagerWindow: left pane = local filesystem, right pane = Kronos
FTP.  Supports navigate, upload, download, rename, delete, new folder, and
cut/copy/paste via right-click context menu and keyboard shortcuts.
Drag-drop between and within panes for moving/transferring files.
"""
from __future__ import annotations

import ftplib
import logging
import math
import os
import pathlib
import re
import shutil
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

from PySide6.QtCore import QMimeData, QPointF, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QDrag, QIcon, QKeyEvent, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow,
    QMenu, QMessageBox, QProgressBar, QPushButton, QSplitter,
    QStatusBar, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

log = logging.getLogger(__name__)

# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class FileEntry:
    name: str
    full_path: str
    is_directory: bool
    size: int
    modified: Optional[datetime]
    read_only: bool = False
    is_parent: bool = False   # the ".." row; full_path is the parent folder

    @property
    def display_name(self) -> str:
        return f"\U0001F4C1 {self.name}" if self.is_directory else self.name

    @property
    def size_text_or_blank(self) -> str:
        return "" if self.is_parent else self.size_text

    @property
    def size_text(self) -> str:
        if self.is_directory:
            return "<DIR>"
        if self.size < 1024:
            return f"{self.size} B"
        if self.size < 1_048_576:
            return f"{self.size // 1024} KB"
        return f"{self.size // 1_048_576} MB"

    @property
    def date_text(self) -> str:
        if self.is_parent or not self.modified:
            return ""
        return self.modified.strftime("%Y-%m-%d %H:%M")


@dataclass
class ClipboardPayload:
    is_cut: bool
    from_remote: bool
    items: List[FileEntry]


class _ConflictRequest:
    """Cross-thread request/response envelope for a name-collision prompt.

    A background transfer thread emits this via _conflict_signal and blocks on
    `event`; the GUI-thread slot fills in the fields below and sets the event.
    """
    def __init__(self, name: str, suggested_name: str):
        self.name = name
        self.new_name = suggested_name
        self.action = "cancel"   # "rename" | "overwrite" | "skip" | "cancel"
        self.apply_all = False
        self.event = threading.Event()


class _ConflictDialog(QDialog):
    """Rename / Overwrite / Skip / Cancel prompt for a file-transfer collision."""

    def __init__(self, name: str, suggested_name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("File Already Exists")
        self.setStyleSheet(_STYLE)
        self.action = "cancel"

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"'{name}' already exists at the destination."))

        rename_row = QHBoxLayout()
        rename_row.addWidget(QLabel("New name:"))
        self._name_edit = QLineEdit(suggested_name)
        rename_row.addWidget(self._name_edit)
        layout.addLayout(rename_row)

        self._apply_all_cb = QCheckBox("Apply this choice to all remaining conflicts")
        layout.addWidget(self._apply_all_cb)

        btn_row = QHBoxLayout()
        for label, action in (("Rename", "rename"), ("Overwrite", "overwrite"),
                              ("Skip", "skip"), ("Cancel", "cancel")):
            b = QPushButton(label)
            b.clicked.connect(lambda checked=False, a=action: self._choose(a))
            btn_row.addWidget(b)
        layout.addLayout(btn_row)

    def _choose(self, action: str):
        self.action = action
        self.accept()

    @property
    def new_name(self) -> str:
        return self._name_edit.text().strip()

    @property
    def apply_all(self) -> bool:
        return self._apply_all_cb.isChecked()


# Sort helpers
_SORT_NAME = 0
_SORT_SIZE = 1
_SORT_DATE = 2


def _sort_entries(entries: List[FileEntry], col: int, ascending: bool) -> List[FileEntry]:
    """Sorted view with the ".." row, if present, always pinned first."""
    parent = [e for e in entries if e.is_parent]
    return parent + _sort_plain([e for e in entries if not e.is_parent], col, ascending)


def _sort_plain(entries: List[FileEntry], col: int, ascending: bool) -> List[FileEntry]:
    if col == _SORT_SIZE:
        key = lambda e: (0 if e.is_directory else 1, e.size)
    elif col == _SORT_DATE:
        key = lambda e: (0 if e.is_directory else 1, e.modified or datetime.min)
    else:
        key = lambda e: (0 if e.is_directory else 1, e.name.lower())
    return sorted(entries, key=key, reverse=not ascending)


# ── Stylesheet ────────────────────────────────────────────────────────────────

_STYLE = """
QMainWindow, QWidget { background: #1A1A1A; color: white; }
QSplitter::handle { background: #444444; }
QTreeWidget {
    background: #232323; color: white; border: 1px solid #444444;
    font-size: 12px;
}
QTreeWidget::item { padding: 3px 0; }
QTreeWidget::item:hover { background: #363636; }
QTreeWidget::item:selected { background: #1A4E6E; border: 1px solid #3A8FBF; }
QHeaderView::section {
    background: #2E2E2E; color: #CCCCCC; border: 1px solid #555555;
    padding: 4px 6px; font-weight: bold;
}
/* Aligned to the app-wide QPushButton tokens (theme.PANEL_ALT / BORDER_STRONG). */
QPushButton {
    background: #2A2A2A; color: #CCCCCC; border: 1px solid #444444;
    border-radius: 3px; padding: 4px 10px;
}
QPushButton:hover { background: #333333; border-color: #555555; }
QPushButton:disabled { color: #555555; border-color: #333333; }
QLineEdit {
    background: #1A1A1A; color: #CCCCCC; border: none; padding: 2px;
}
QComboBox {
    background: #3A3A3A; color: white; border: 1px solid #666666;
    padding: 3px 6px;
}
QComboBox::drop-down { border: none; }
QComboBox QAbstractItemView {
    background: #2E2E2E; color: white; border: 1px solid #555555;
    selection-background-color: #1A4E6E;
}
QProgressBar {
    background: #333333; border: 1px solid #555555; height: 12px;
    text-align: center; color: #CCC; font-size: 10px;
}
QProgressBar::chunk { background: #88AADD; }
QStatusBar { background: #1A1A1A; color: #CCCCCC; font-size: 11px; }
QStatusBar::item { border: none; }
QLabel { color: white; }
QMenu {
    background: #282828; color: white; border: 1px solid #555555; padding: 2px;
}
QMenu { icon-size: 16px; }
QMenu::item { padding: 6px 20px; }
QMenu::icon { padding-left: 6px; }
QMenu::item:selected { background: #1A4E6E; }
QMenu::item:disabled { color: #606060; }
QMenu::separator { height: 1px; background: #444444; margin: 3px 4px; }
"""

# ── FTP path safety (port of C#'s Networking/FtpPathSafety.cs) ────────────────
# The Kronos's OS/firmware expects a fixed name at each top-level FTP mount
# point (SSD1/SSD2/SSD3/...) — renaming one is a real risk of corrupting or
# bricking the unit's storage naming, not just a cosmetic mistake the user can
# undo by renaming it back (see the C# original's comment: its own Rename
# command was found capable of exactly this). _FtpWorker.rename() below is the
# ONE chokepoint every FTP rename/move in this app goes through — do not call
# ftplib's .rename directly anywhere else.

# Hardware-verified 2026-09-04 (see FtpPathSafety.cs): the Kronos's own
# filesystem refuses a path once its total length from the FTP root passes
# this many characters.
_MAX_REMOTE_PATH_LENGTH = 245


def _is_top_level_ftp_path(path: str) -> bool:
    """A path is "top-level" when it names a direct child of the FTP root (or the root itself), however it is
    spelled: "." and ".." segments and repeated slashes are resolved first, so "/SSD1/.." or "/SSD1/../X" can't
    slip past a purely textual check."""
    parts: List[str] = []
    for seg in path.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return len(parts) <= 1


class FtpTopLevelPathError(Exception):
    """Raised by _FtpWorker.rename/delete_file/delete_dir when the path is a
    top-level Kronos storage volume (SSD1/SSD2/SSD3/...)."""
    def __init__(self, path: str, action: str = "rename/move"):
        super().__init__(
            f"Refusing to {action} '{path}' — top-level instrument storage "
            "volumes (SSD1/SSD2/SSD3/...) can never be renamed, moved, or deleted.")


class FtpPathTooLongError(Exception):
    def __init__(self, path: str):
        super().__init__(
            f"That would make the remote path {len(path)} characters long — the instrument's "
            f"own filesystem refuses anything over {_MAX_REMOTE_PATH_LENGTH}. Try a shorter "
            "name, or a shallower destination folder.")


# ── FTP worker (runs blocking ftplib calls off the main thread) ───────────────

def _serialized(fn):
    """One ftplib.FTP connection cannot run two commands at once: parallel UI threads (a fast double-click
    on Up, Refresh during a listing) would desync the control channel. Every public method takes the lock."""
    import functools

    @functools.wraps(fn)
    def wrapper(self, *a, **kw):
        with self._lock:
            return fn(self, *a, **kw)
    return wrapper


class _FtpWorker:
    """Wraps a single ftplib.FTP connection; all methods are blocking (and serialized by an RLock)."""

    def __init__(self, host: str, port: int, user: str, password: str):
        self._host = host
        self._port = port
        self._user = user
        self._pass = password
        self._ftp: Optional[ftplib.FTP] = None
        self._lock = threading.RLock()

    @_serialized
    def connect(self):
        ftp = ftplib.FTP()
        ftp.connect(self._host, self._port, timeout=10)
        ftp.login(self._user, self._pass)
        ftp.set_pasv(True)
        self._ftp = ftp

    @_serialized
    def disconnect(self):
        if self._ftp:
            try:
                self._ftp.quit()
            except Exception:
                try:
                    self._ftp.close()
                except Exception:
                    pass
            self._ftp = None

    @_serialized
    def ensure_connected(self):
        if self._ftp is None:
            self.connect()
            return
        try:
            self._ftp.voidcmd("NOOP")
        except Exception:
            self.connect()

    @_serialized
    def list_dir(self, path: str, include_hidden: bool = False) -> List[FileEntry]:
        self.ensure_connected()
        if include_hidden:   # MLSD is refused by both instruments and would not list dotfiles reliably anyway
            return self._list_dir_fallback(path, include_hidden=True)
        entries: List[FileEntry] = []
        try:
            for name, facts in self._ftp.mlsd(path):
                if name in (".", ".."):
                    continue
                is_dir = facts.get("type", "").startswith("dir")
                size = int(facts.get("size", "0")) if not is_dir else 0
                mod_str = facts.get("modify", "")
                modified = None
                if mod_str:
                    try:
                        modified = datetime.strptime(mod_str[:14], "%Y%m%d%H%M%S")
                    except ValueError:
                        pass
                full = f"{path.rstrip('/')}/{name}"
                perm = facts.get("perm")
                # RFC 3659 perm facts: files carry 'w'/'a' when writable, directories 'c'/'m'/'p'. Only a
                # non-empty fact that lacks all of them counts as evidence.
                ro = bool(perm) and not any(c in perm for c in ("cmp" if is_dir else "wa"))
                entries.append(FileEntry(name, full, is_dir, size, modified, read_only=ro))
        except Exception:
            entries = self._list_dir_fallback(path)
        return entries

    @_serialized
    def _list_dir_fallback(self, path: str, include_hidden: bool = False) -> List[FileEntry]:
        """Parse LIST output when MLSD is not supported. `include_hidden` uses `LIST -a`: the instruments' plain LIST
        hides dotfiles, which matters wherever 'is something there?' decides a delete or an overwrite."""
        self.ensure_connected()
        lines: List[str] = []
        if include_hidden:
            self._ftp.dir("-a", path, lines.append)
        else:
            self._ftp.dir(path, lines.append)
        entries: List[FileEntry] = []
        for line in lines:
            parts = line.split(None, 8)
            if len(parts) < 9:
                continue
            name = parts[8]
            if name in (".", ".."):
                continue
            is_dir = line.startswith("d")
            size = int(parts[4]) if not is_dir else 0
            full = f"{path.rstrip('/')}/{name}"
            # Unix mode string: read-only only if nobody (owner/group/other) can write.
            ro = len(parts[0]) == 10 and "w" not in parts[0][1:]
            entries.append(FileEntry(name, full, is_dir, size, None, read_only=ro))
        return entries

    @_serialized
    def upload(self, local_path: str, remote_path: str):
        self.ensure_connected()
        if re.search(r"\.[0-9a-f]{8}\.part$", remote_path):   # the caller (sample push) already stages
            with open(local_path, "rb") as f:
                self._ftp.storbinary(f"STOR {remote_path}", f)
            return
        # Stage to a sibling and rename over the target, so a dropped connection mid-transfer can't
        # truncate a previously-valid remote file (both instruments accept RNTO over an existing file).
        part = f"{remote_path}.{uuid.uuid4().hex[:8]}.part"
        if len(part) > _MAX_REMOTE_PATH_LENGTH:
            raise FtpPathTooLongError(part)
        try:
            with open(local_path, "rb") as f:
                self._ftp.storbinary(f"STOR {part}", f)
            self.rename(part, remote_path)
        except BaseException:
            try:
                self._ftp.delete(part)
            except Exception:
                pass
            raise

    @_serialized
    def download(self, remote_path: str, local_path: str):
        self.ensure_connected()
        tmp = f"{local_path}.{uuid.uuid4().hex[:8]}.part"   # unique: never clobbers a user's own .part; never leave a truncated/partial file under the real name
        try:
            with open(tmp, "wb") as f:
                self._ftp.retrbinary(f"RETR {remote_path}", f.write)
            os.replace(tmp, local_path)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    @_serialized
    def retrieve(self, remote_path: str, on_chunk, blocksize: int = 65536):
        """Stream a remote file to a callback instead of to a local path.

        For callers that want the bytes in memory (the Librarian's PCG pull) or
        want per-chunk progress/cancellation: `on_chunk(bytes)` is invoked for
        each block as it arrives, and any exception it raises propagates out —
        which is how a cancel aborts a transfer mid-flight. Avoids inventing a
        temp file, and with it the "temp directory is not writable" failure mode,
        for data that is only going to be parsed and discarded."""
        self.ensure_connected()
        self._ftp.retrbinary(f"RETR {remote_path}", on_chunk, blocksize)

    @_serialized
    def delete_file(self, path: str):
        if _is_top_level_ftp_path(path):
            raise FtpTopLevelPathError(path, "delete")
        self.ensure_connected()
        self._ftp.delete(path)

    @_serialized
    def delete_dir(self, path: str):
        """Belt-and-suspenders on top of _on_remote_delete's UI-level check
        (C# only gates this at the UI level; this app additionally guards the
        actual FTP call, same as rename). Recursion-safe: only a literal
        top-level path (SSD1) trips this, never a subfolder within one
        (SSD1/subfolder has a second slash), so descending into a volume's
        own contents is unaffected."""
        if _is_top_level_ftp_path(path):
            raise FtpTopLevelPathError(path, "delete")
        self.ensure_connected()
        # list_dir, not MLSD: the Kronos' FTP server answers MLSD with "500 Unknown command".
        for entry in self.list_dir(path, include_hidden=True):   # a lone dotfile would make RMD fail (550)
            if entry.is_directory:
                self.delete_dir(entry.full_path)
            else:
                self._ftp.delete(entry.full_path)
        self._ftp.rmd(path)

    @_serialized
    def rename(self, old: str, new: str):
        """The ONE chokepoint every FTP rename/move goes through — see the
        module-level FTP-path-safety block above. Ported from C#'s
        FtpPathSafety.RenameGuardedAsync."""
        if _is_top_level_ftp_path(old) or _is_top_level_ftp_path(new):
            raise FtpTopLevelPathError(old, "rename/move")
        if len(new) > _MAX_REMOTE_PATH_LENGTH:
            raise FtpPathTooLongError(new)
        self.ensure_connected()
        self._ftp.rename(old, new)

    @_serialized
    def rmdir(self, path: str):
        """Remove ONE empty folder (fails if anything is left in it, unlike delete_dir)."""
        if _is_top_level_ftp_path(path):
            raise FtpTopLevelPathError(path, "delete")
        self.ensure_connected()
        self._ftp.rmd(path)

    @_serialized
    def mkdir(self, path: str):
        if len(path) > _MAX_REMOTE_PATH_LENGTH:
            raise FtpPathTooLongError(path)
        self.ensure_connected()
        self._ftp.mkd(path)

    @_serialized
    def file_exists(self, path: str) -> bool:
        """True when `path` is listed in its parent folder. Goes through list_dir, which falls back to LIST
        because the Kronos answers MLSD with 500 (an MLSD-only check was always False there, so the File
        Manager never saw a name collision and the sample push never saw the file it was replacing). A parent
        folder that doesn't exist (a 550) means the file doesn't either; a dropped connection or timeout is not
        "absent" and propagates."""
        self.ensure_connected()
        parent = _ftp_parent(path)
        name = path.rstrip("/").rsplit("/", 1)[-1]
        try:
            # Exact match: the instruments' volumes are case-sensitive ('a.txt' and 'A.TXT' coexist), and plain LIST
            # hides dotfiles, so list with -a or a hidden file would read as absent and be overwritten by RNTO.
            return any(e.name == name for e in self.list_dir(parent, include_hidden=True))
        except ftplib.error_perm as ex:
            # Only a definite "no such folder" means absent. A permission/other refusal says nothing about the
            # file, and answering "absent" here lets RNTO silently overwrite it.
            msg = str(ex).lower()
            if msg.startswith("550") and not any(w in msg for w in ("permission", "denied", "not allowed")):
                return False
            raise


def _safe_local_join(local_dir: str, name: str) -> Optional[str]:
    """Join a *server-supplied* filename onto a local directory, or None if the
    result would escape that directory.

    Names here come from the remote MLSD/LIST listing (and from the rename
    dialog), so they are not trustworthy: os.path.join discards its first
    argument entirely when the second is absolute, so a listing entry named
    "/etc/cron.d/x" would otherwise be written straight to that path, and
    "../../.bashrc" would climb out of the download folder.
    """
    base = os.path.basename(name.replace("\\", "/").rstrip("/"))
    if not base or base in (".", ".."):
        return None
    dest = os.path.join(local_dir, base)
    if os.path.commonpath((os.path.abspath(local_dir),
                           os.path.abspath(dest))) != os.path.abspath(local_dir):
        return None
    return dest


def _same_local_path(a: str, b: str) -> bool:
    """True when two local paths name the same item ("Foo" and "foo" are one folder on Windows)."""
    try:
        if os.path.exists(a) and os.path.exists(b) and os.path.samefile(a, b):
            return True
    except OSError:
        pass
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _local_is_inside(path: str, folder: str) -> bool:
    """True when `path` is `folder` itself or anywhere below it."""
    p = os.path.normcase(os.path.abspath(path))
    f = os.path.normcase(os.path.abspath(folder))
    return p == f or p.startswith(f.rstrip(os.sep) + os.sep)


def _temp_sibling(dest: str) -> str:
    return os.path.join(os.path.dirname(dest), f".{os.path.basename(dest)}.{uuid.uuid4().hex[:8]}.part")


def _put_local_file(src: str, dest: str, move: bool) -> None:
    """Copy/move one file onto `dest`, replacing whatever file is there without ever leaving `dest` half-written:
    a move within one volume is a single os.replace; otherwise the bytes go to a temp sibling first and are
    renamed over `dest` only once complete (the previous file survives any failure before that)."""
    if move:
        try:
            os.replace(src, dest)
            return
        except OSError:
            pass   # another volume (or a locked source): fall back to copy + delete
    tmp = _temp_sibling(dest)
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    if move:
        try:
            os.remove(src)
        except OSError as ex:
            raise OSError(f"copied, but the original couldn't be removed ({ex})") from ex


def _put_local_dir(src: str, dest: str, move: bool) -> None:
    """Copy/move a folder to a `dest` that does not exist yet. A copy is built under a temp sibling and renamed
    into place, so a failure part-way never leaves a half-copied folder under the real name."""
    if move:
        try:
            os.rename(src, dest)   # same volume: atomic, nothing is copied
            return
        except OSError:
            pass                   # another volume: stage a copy, then remove the original
    tmp = _temp_sibling(dest)
    try:
        shutil.copytree(src, tmp)
        os.rename(tmp, dest)   # refuses to replace an existing folder on both Windows and POSIX
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    if move:
        try:
            shutil.rmtree(src)
        except OSError as ex:
            raise OSError(f"copied, but the original folder couldn't be fully removed ({ex})") from ex


def _merge_local_dir(src: str, dest: str, move: bool, errors: List[str]) -> None:
    """Overwrite chosen for a folder: merge `src` into the existing `dest`, replacing same-named files (each one
    via _put_local_file) and recursing into same-named folders. A file/folder type clash is reported, never
    resolved by deleting either side. After a move the emptied source folders are removed (rmdir only, so
    nothing that failed to move is ever lost)."""
    for child in sorted(os.listdir(src)):
        s = os.path.join(src, child)
        d = os.path.join(dest, child)
        try:
            if os.path.isdir(s) and not os.path.islink(s):
                if os.path.isdir(d):
                    _merge_local_dir(s, d, move, errors)
                elif os.path.lexists(d):
                    errors.append(f"{d}: a file with that name is in the way of the folder")
                else:
                    _put_local_dir(s, d, move)
            else:
                if os.path.isdir(d):
                    errors.append(f"{d}: a folder with that name is in the way of the file")
                else:
                    _put_local_file(s, d, move)
        except Exception as ex:
            errors.append(f"{s}: {ex}")
    if move and not errors:
        try:
            os.rmdir(src)
        except OSError as ex:
            errors.append(f"{src}: moved its contents but couldn't remove it ({ex})")


def _ftp_parent(path: str) -> str:
    clean = path.rstrip("/")
    idx = clean.rfind("/")
    return "/" if idx <= 0 else clean[:idx]


def normalize_remote_folder(path: str) -> str:
    """'SSD/Samples/' -> '/SSD/Samples'; empty -> '/'."""
    return "/" + path.strip().replace("\\", "/").strip("/")


def _default_local_folder() -> str:
    desktop = pathlib.Path.home() / "Desktop"
    return str(desktop if desktop.is_dir() else pathlib.Path.home())


# ── Drag-drop constants and tree subclass ─────────────────────────────────────

_DRAG_MIME = "application/x-kronos-fileentries"


@dataclass
class DragPayload:
    from_remote: bool
    items: List[FileEntry]


class _DragTreeWidget(QTreeWidget):
    """QTreeWidget with custom drag-drop that avoids Qt's auto row mutation."""

    def __init__(self, is_remote: bool, window: "FileManagerWindow", parent=None):
        super().__init__(parent)
        self._is_remote = is_remote
        self._window = window

        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)

    # Qt's own press/move handling does the drag detection (so a row you have not clicked yet can be dragged
    # straight away, and Ctrl/Shift multi-selections survive the press); we only supply the drag itself.
    def startDrag(self, supportedActions):
        w = self._window
        items = w._selected_entries(self, self._entries())
        if not items:
            return
        if w._busy:
            w._set_status("Wait for the current transfer to finish before dragging.")
            return

        w._drag_payload = DragPayload(from_remote=self._is_remote, items=items)
        try:
            drag = QDrag(self)
            mime = QMimeData()
            mime.setData(_DRAG_MIME, b"1")
            drag.setMimeData(mime)
            drag.exec(Qt.DropAction.CopyAction | Qt.DropAction.MoveAction, Qt.DropAction.CopyAction)
        finally:
            w._drag_payload = None

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(_DRAG_MIME):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if not event.mimeData().hasFormat(_DRAG_MIME):
            event.ignore()
            return
        w = self._window
        payload = w._drag_payload
        if payload is None:
            event.ignore()
            return

        item = self.itemAt(event.position().toPoint())
        entry = w._entry_from_item(item, self._entries()) if item else None
        same_pane = self._is_remote == payload.from_remote
        if same_pane and entry and entry.is_directory:
            event.setDropAction(Qt.DropAction.MoveAction)
        elif same_pane:
            event.setDropAction(Qt.DropAction.MoveAction)
        else:
            event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()

    def dropEvent(self, event):
        if not event.mimeData().hasFormat(_DRAG_MIME):
            event.ignore()
            return
        w = self._window
        payload = w._drag_payload
        if payload is None or w._busy:
            event.ignore()
            return

        event.acceptProposedAction()

        item = self.itemAt(event.position().toPoint())
        entry = w._entry_from_item(item, self._entries()) if item else None
        same_pane = self._is_remote == payload.from_remote
        items = payload.items

        if same_pane:
            target_folder = entry if (entry and entry.is_directory) else None
            if target_folder and any(e.full_path == target_folder.full_path for e in items):
                return
            w._handle_same_pane_drop(self._is_remote, items, target_folder)
        else:
            target_folder = entry if (entry and entry.is_directory) else None
            w._handle_cross_pane_drop(payload.from_remote, self._is_remote, items, target_folder)

    def _entries(self) -> List[FileEntry]:
        w = self._window
        return w._remote_entries if self._is_remote else w._local_entries


# ── Toolbar icons ────────────────────────────────────────────────────────────

def _toolbar_icon(kind: str, color: str = "#E6E6E6", size: int = 16) -> QIcon:
    """Crisp white line icons drawn with QPainter (a font glyph like U+21BA is thin, tiny and missing from some
    fonts): 'up' = arrow rising out of a folder level, 'refresh' = circular arrow. Rendered at 2x for HiDPI."""
    ratio = 2
    pm = QPixmap(size * ratio, size * ratio)
    pm.setDevicePixelRatio(ratio)
    pm.fill(Qt.transparent)
    qp = QPainter(pm)
    qp.setRenderHint(QPainter.Antialiasing)
    qp.scale(size / 16, size / 16)   # every shape below is drawn on a 16x16 grid
    pen = QPen(QColor(color), 1.9, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    qp.setPen(pen)
    if kind == "up":
        qp.drawLine(QPointF(8, 14), QPointF(8, 3.5))                    # shaft
        qp.drawLine(QPointF(3.5, 7.5), QPointF(8, 3))                   # head
        qp.drawLine(QPointF(12.5, 7.5), QPointF(8, 3))
    elif kind in ("right", "left"):   # big transfer arrows that fill the whole icon box
        if kind == "left":
            qp.translate(16, 0)
            qp.scale(-1, 1)
        qp.setPen(QPen(QColor(color), 2.4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        qp.drawLine(QPointF(1.5, 8), QPointF(14.2, 8))                  # shaft
        qp.drawLine(QPointF(8.2, 2.2), QPointF(14.2, 8))                # head
        qp.drawLine(QPointF(8.2, 13.8), QPointF(14.2, 8))
    elif kind in ("open", "newfolder"):
        folder = QPainterPath()
        folder.moveTo(1.5, 4); folder.lineTo(6, 4); folder.lineTo(7.5, 5.8); folder.lineTo(14.5, 5.8)
        folder.lineTo(14.5, 13.2); folder.lineTo(1.5, 13.2); folder.closeSubpath()
        qp.drawPath(folder)
        if kind == "newfolder":
            qp.setPen(QPen(QColor("#8FD88F"), 2.0, Qt.SolidLine, Qt.RoundCap))
            qp.drawLine(QPointF(11.2, 7.6), QPointF(11.2, 11.8))
            qp.drawLine(QPointF(9.1, 9.7), QPointF(13.3, 9.7))
    elif kind == "star":
        pts = [QPointF(8 + (6.8 if i % 2 == 0 else 2.9) * math.sin(i * math.pi / 5),
                       8.6 - (6.8 if i % 2 == 0 else 2.9) * math.cos(i * math.pi / 5)) for i in range(10)]
        qp.drawPolygon(QPolygonF(pts))
    elif kind == "cut":
        qp.drawEllipse(QPointF(4.2, 12.2), 2.3, 2.3)
        qp.drawEllipse(QPointF(11.8, 12.2), 2.3, 2.3)
        qp.drawLine(QPointF(5.2, 10.2), QPointF(11.2, 1.8))
        qp.drawLine(QPointF(10.8, 10.2), QPointF(4.8, 1.8))
    elif kind == "copy":
        qp.drawRoundedRect(5.5, 1.5, 8.5, 9.5, 1.2, 1.2)
        qp.setBrush(QColor("#282828"))
        qp.drawRoundedRect(2, 5, 8.5, 9.5, 1.2, 1.2)
    elif kind == "paste":
        qp.drawRoundedRect(2.8, 3, 10.4, 11.5, 1.2, 1.2)
        qp.setBrush(QColor(color))
        qp.drawRoundedRect(5.8, 1.4, 4.4, 3.2, 0.8, 0.8)
        qp.drawLine(QPointF(5.3, 8.2), QPointF(10.7, 8.2))
        qp.drawLine(QPointF(5.3, 11), QPointF(10.7, 11))
    elif kind == "rename":
        pencil = QPainterPath()
        pencil.moveTo(2.5, 13.5); pencil.lineTo(3.4, 10); pencil.lineTo(11.4, 2); pencil.lineTo(14, 4.6)
        pencil.lineTo(6, 12.6); pencil.closeSubpath()
        qp.drawPath(pencil)
        qp.drawLine(QPointF(9.6, 3.8), QPointF(12.2, 6.4))
    elif kind == "delete":
        qp.setPen(QPen(QColor("#F29A9A"), 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        qp.drawLine(QPointF(2.8, 4.6), QPointF(13.2, 4.6))
        qp.drawPolyline(QPolygonF([QPointF(6.2, 4.4), QPointF(6.2, 2.6), QPointF(9.8, 2.6), QPointF(9.8, 4.4)]))
        qp.drawPolyline(QPolygonF([QPointF(4.4, 4.8), QPointF(5, 14), QPointF(11, 14), QPointF(11.6, 4.8)]))
        qp.drawLine(QPointF(7, 7.2), QPointF(7, 11.6))
        qp.drawLine(QPointF(9, 7.2), QPointF(9, 11.6))
    elif kind == "info":
        qp.drawEllipse(QPointF(8, 8), 6.3, 6.3)
        qp.drawLine(QPointF(8, 7.3), QPointF(8, 11.2))
        qp.setBrush(QColor(color))
        qp.drawEllipse(QPointF(8, 4.9), 0.7, 0.7)
    elif kind == "refresh":
        path = QPainterPath()
        path.arcMoveTo(2.5, 2.5, 11, 11, 60)
        path.arcTo(2.5, 2.5, 11, 11, 60, 270)                           # open circle, gap at upper right
        qp.drawPath(path)
        qp.setBrush(QColor(color))
        qp.drawPolygon(QPolygonF([QPointF(9.2, 0.8), QPointF(14.4, 2.6), QPointF(10.6, 6.0)]))   # arrowhead
    qp.end()
    return QIcon(pm)


# ── Drive names ──────────────────────────────────────────────────────────────

_DRIVE_TYPE_NAMES = {2: "Removable Disk", 3: "Local Disk", 4: "Network Drive", 5: "CD/DVD Drive", 6: "RAM Disk"}


def _windows_drive_labels(roots: List[str]) -> dict:
    """{'C:\\': 'Windows'}: the volume name, else the drive kind ('Local Disk'); a network drive with no volume
    name shows its UNC share. Windows only; a drive that is not ready is simply left as its letter."""
    import ctypes
    k32 = ctypes.windll.kernel32
    old_mode = k32.SetErrorMode(1)   # SEM_FAILCRITICALERRORS: no "insert a disk" popup for empty drives
    out = {}
    try:
        for root in roots:
            name = ctypes.create_unicode_buffer(261)
            ok = k32.GetVolumeInformationW(root, name, 261, None, None, None, None, 0)
            label = name.value if ok else ""
            kind = k32.GetDriveTypeW(root)
            if not label and kind == 4:
                buf = ctypes.create_unicode_buffer(1024)
                size = ctypes.c_ulong(1024)
                if ctypes.windll.mpr.WNetGetConnectionW(root[:2], buf, ctypes.byref(size)) == 0:
                    label = buf.value
            if not label and ok:
                label = _DRIVE_TYPE_NAMES.get(kind, "")
            out[root] = label
    finally:
        k32.SetErrorMode(old_mode)
    return out


_PSEUDO_FS = {"proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2", "securityfs", "debugfs",
              "tracefs", "pstore", "bpf", "autofs", "mqueue", "hugetlbfs", "configfs", "fusectl", "overlay",
              "squashfs", "efivarfs", "binfmt_misc", "ramfs", "nsfs", "rpc_pipefs", "fuse.gvfsd-fuse",
              "fuse.portal"}
_NETWORK_FS = {"nfs", "nfs4", "cifs", "smb3", "smbfs", "9p", "fuse.sshfs", "afs", "ceph"}
_USER_MOUNT_PREFIXES = ("/media/", "/mnt/", "/run/media/")   # where desktops and admins mount removable/extra disks


def _unescape_mount(s: str) -> str:
    """/proc/mounts writes space, tab, newline and backslash as octal escapes (\\040)."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def parse_linux_mounts(mounts_text: str, labels: dict) -> List[Tuple[str, str]]:
    """[(mount_point, name)] for '/' and every user-visible disk in /proc/self/mounts text. `labels` maps a device
    path (/dev/sdb1) to its volume label. Pseudo filesystems and system mounts (/boot, /snap, /sys ...) are left
    out; network mounts and anything under /media, /mnt or /run/media are kept. Pure: no I/O."""
    out: List[Tuple[str, str]] = []
    seen = set()
    for line in mounts_text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        dev, mnt, fstype = _unescape_mount(parts[0]), _unescape_mount(parts[1]), parts[2]
        if mnt in seen or fstype in _PSEUDO_FS:
            continue
        real_disk = dev.startswith("/dev/") or fstype in _NETWORK_FS
        if mnt == "/":
            pass
        elif not real_disk or not mnt.startswith(_USER_MOUNT_PREFIXES):
            continue
        seen.add(mnt)
        name = labels.get(dev) or ("root" if mnt == "/" else os.path.basename(mnt.rstrip("/")) or mnt)
        out.append((mnt, name))
    return out


def _linux_volume_labels() -> dict:
    """{'/dev/sdb1': 'BACKUP'} from /dev/disk/by-label (udev); {} when unavailable."""
    base = "/dev/disk/by-label"
    labels = {}
    try:
        for n in os.listdir(base):
            labels[os.path.realpath(os.path.join(base, n))] = _unescape_label(n)
    except OSError:
        pass
    return labels


def _unescape_label(n: str) -> str:
    return re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), n)   # udev: \x20 = space


def list_local_drives() -> List[Tuple[str, str]]:
    """[(root_path, combo_text)] for the local pane's drive selector, on every platform. No volume-name lookup that
    can stall on Windows (those arrive later via _windows_drive_labels)."""
    disk = "\U0001F4BD"
    home = str(pathlib.Path.home())
    items: List[Tuple[str, str]] = []
    if os.name == "nt":
        import ctypes
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for i in range(26):
            if bitmask & (1 << i):
                letter = chr(ord("A") + i)
                items.append((f"{letter}:\\", f"{disk} {letter}:"))
        return items
    items.append(("/", "/ (root)"))
    if sys.platform == "darwin":
        try:
            for n in sorted(os.listdir("/Volumes")):
                path = f"/Volumes/{n}"
                if os.path.realpath(path) != "/" and os.path.isdir(path):   # skip the 'Macintosh HD' -> / link
                    items.append((path, f"{disk} {n} ({path})"))
        except OSError:
            pass
    else:
        try:
            with open("/proc/self/mounts", encoding="utf-8", errors="replace") as f:
                mounts = parse_linux_mounts(f.read(), _linux_volume_labels())
        except OSError:
            mounts = []
        for mnt, name in mounts:
            if mnt != "/":
                items.append((mnt, f"{disk} {name} ({mnt})"))
    items.append((home, f"~ ({home})"))
    return items


def _is_under(path: str, root: str) -> bool:
    path, root = os.path.normcase(os.path.abspath(path)), os.path.normcase(root)
    stem = root.rstrip("/\\")
    return path == stem or path.startswith(stem + os.sep) or (stem == "" and path.startswith(os.sep))


# ── Properties ───────────────────────────────────────────────────────────────

def _fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} bytes"
    size, unit = float(n), "B"
    for unit in ("KB", "MB", "GB", "TB"):
        size /= 1024
        if size < 1024:
            break
    return f"{size:.2f} {unit} ({n:,} bytes)"


def _fmt_duration(sec: float) -> str:
    sec = int(max(sec, 0))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s" if m else f"{s}s"


def _local_children(path: str) -> List[FileEntry]:
    out: List[FileEntry] = []
    with os.scandir(path) as it:
        for c in it:
            try:
                if c.is_dir(follow_symlinks=False):
                    out.append(FileEntry(c.name, c.path, True, 0, None))
                elif c.is_file():
                    st = c.stat()
                    out.append(FileEntry(c.name, c.path, False, st.st_size, datetime.fromtimestamp(st.st_mtime)))
            except OSError:
                continue
    return out


def gather_properties(entries: List[FileEntry], listdir, cancel: threading.Event, publish) -> dict:
    """Walks `entries` (folders recursively through `listdir(path) -> [FileEntry]`) and totals what is inside.
    `publish(totals)` is called every few dozen items and once at the end (with done=True). Pure: no Qt."""
    t = {"files": 0, "folders": 0, "bytes": 0, "newest": None, "errors": 0, "done": False}
    seen = [0]

    def note(e: FileEntry):
        if e.modified and (t["newest"] is None or e.modified > t["newest"]):
            t["newest"] = e.modified
        seen[0] += 1
        if seen[0] % 25 == 0:
            publish(dict(t))

    def walk(path: str):
        if cancel.is_set():
            return
        try:
            kids = listdir(path)
        except Exception:
            t["errors"] += 1
            return
        for k in kids:
            if cancel.is_set():
                return
            if k.is_directory:
                t["folders"] += 1
                walk(k.full_path)
            else:
                t["files"] += 1
                t["bytes"] += k.size
            note(k)

    for e in entries:
        if e.is_directory:
            walk(e.full_path)
        else:
            t["files"] += 1
            t["bytes"] += e.size
            note(e)
    t["done"] = not cancel.is_set()
    publish(dict(t))
    return t


class _PropertiesDialog(QDialog):
    _update = Signal(object)

    def __init__(self, entries: List[FileEntry], is_remote: bool, listdir, parent=None):
        super().__init__(parent)
        self._entries = entries
        self._cancel = threading.Event()
        self.setWindowTitle("Properties")
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        lay.addLayout(form)
        self._rows = {}

        def row(key: str, text: str):
            lbl = QLabel(text)
            lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            lbl.setWordWrap(True)
            form.addRow(key + ":", lbl)
            self._rows[key] = lbl

        first = entries[0]
        multi = len(entries) > 1
        n_dirs = sum(1 for e in entries if e.is_directory)
        if multi:
            row("Name", f"{len(entries)} items ({len(entries) - n_dirs} files, {n_dirs} folders selected)")
            row("Type", "Multiple items")
            parents = {(_ftp_parent(e.full_path) if is_remote else os.path.dirname(e.full_path)) for e in entries}
            row("Location", parents.pop() if len(parents) == 1 else "(several folders)")
        else:
            row("Name", first.name)
            row("Type", "Folder" if first.is_directory else "File")
            row("Location", _ftp_parent(first.full_path) if is_remote else os.path.dirname(first.full_path))
            row("Full path", first.full_path)
        row("Where", "Instrument" if is_remote else "This PC")
        row("Size", "Calculating…" if (multi or first.is_directory) else _fmt_bytes(first.size))
        if multi or first.is_directory:
            row("Contains", "Calculating…")
        row("Modified", first.modified.strftime("%Y-%m-%d %H:%M:%S") if (not multi and first.modified) else
            ("Calculating…" if (multi or first.is_directory) else "unknown"))
        if not multi:
            row("Attributes", "Read-only" if first.read_only else "Normal")
        self._note = QLabel("")
        lay.addWidget(self._note)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        self._update.connect(self._apply)
        self._multi = multi
        if multi or first.is_directory:
            threading.Thread(target=lambda: gather_properties(entries, listdir, self._cancel, self._update.emit),
                             daemon=True, name="Properties").start()

    def _apply(self, t: dict):
        self._rows["Size"].setText(_fmt_bytes(t["bytes"]) + ("" if t["done"] else "  (counting…)"))
        self._rows["Contains"].setText(f"{t['files']:,} files, {t['folders']:,} folders"
                                       + ("" if t["done"] else "  (counting…)"))
        if t["newest"] is not None or t["done"]:
            label = "Newest file" if (self._multi or self._entries[0].is_directory) else "Modified"
            if "Modified" in self._rows and (self._multi or self._entries[0].is_directory):
                self._rows["Modified"].setText(
                    (t["newest"].strftime("%Y-%m-%d %H:%M:%S") + f" ({label.lower()})") if t["newest"] else "unknown")
        if t["errors"]:
            self._note.setText(f"{t['errors']} folder(s) could not be read; totals are partial.")

    def done(self, r):
        self._cancel.set()
        super().done(r)


# ── File manager window ──────────────────────────────────────────────────────

class FileManagerWindow(QMainWindow):
    _status_signal = Signal(str)
    _refresh_local_signal = Signal()
    _refresh_remote_signal = Signal(list)
    _busy_signal = Signal(bool, str)
    _progress_signal = Signal(int)
    _drive_labels_signal = Signal(object)   # {root: label}
    _count_signal = Signal(int, int, float)   # done, total, eta seconds (<0: unknown)
    _conflict_signal = Signal(object)   # _ConflictRequest, filled in on the GUI thread

    def __init__(self, host: str, ftp_port: int, user: str, password: str, parent=None, settings=None):
        super().__init__(parent)
        self._host = host
        self._ftp_port = ftp_port
        self._user = user
        self._pass = password
        # The live AppSettings (never a private copy): start folders are read from it and
        # "Set as Default Start Folder" writes to it.
        self._settings = settings

        self._ftp = _FtpWorker(host, ftp_port, user, password)
        self._remote_path = "/"
        self._local_path = _default_local_folder()
        start = settings.file_manager_default_local_folder if settings else ""
        if start and os.path.isdir(start):
            self._local_path = start

        self._remote_entries: List[FileEntry] = []
        self._local_entries: List[FileEntry] = []
        self._busy = False
        self._clipboard: Optional[ClipboardPayload] = None

        self._local_sort_col = _SORT_NAME
        self._local_sort_asc = True
        self._remote_sort_col = _SORT_NAME
        self._remote_sort_asc = True

        self._drag_payload: Optional[DragPayload] = None

        self._setup_ui()
        self._wire_signals()

        self._status_signal.connect(self._on_status)
        self._refresh_local_signal.connect(self._refresh_local)
        self._refresh_remote_signal.connect(self._on_remote_listing)
        self._busy_signal.connect(self._on_busy)
        self._progress_signal.connect(self._on_progress)
        self._count_signal.connect(self._on_count)
        self._drive_labels_signal.connect(self._on_drive_labels)
        self._conflict_signal.connect(self._on_conflict_request)

        QTimer.singleShot(0, self._initial_connect)

    # ── UI construction ───────────────────────────────────────────────────────

    def _setup_ui(self):
        self.setWindowTitle("File Manager — Instrument")
        self.resize(960, 600)
        self.setMinimumSize(640, 400)
        self.setStyleSheet(_STYLE)

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Navigation bars
        nav_row = QHBoxLayout()
        nav_row.setContentsMargins(0, 0, 0, 0)
        nav_row.setSpacing(0)

        # Local nav
        local_nav = QWidget()
        local_nav.setStyleSheet("background: #252525;")
        ln_lay = QHBoxLayout(local_nav)
        ln_lay.setContentsMargins(6, 4, 6, 4)
        ln_lay.setSpacing(4)
        self._drive_combo = QComboBox()
        self._drive_combo.setMinimumWidth(230)
        self._drive_combo.setFixedHeight(24)
        self._drive_combo.setToolTip("Switch to a different drive or volume on this computer")
        ln_lay.addWidget(self._drive_combo)
        self._btn_local_up = QPushButton()
        self._btn_local_up.setIcon(_toolbar_icon("up"))
        self._btn_local_up.setFixedSize(32, 26)
        self._btn_local_up.setToolTip("Go up one folder (Backspace)\nOr drop files here to move them up a folder")
        self._btn_local_up.setAcceptDrops(True)
        ln_lay.addWidget(self._btn_local_up)
        lbl_local = QLabel("Local: ")
        lbl_local.setStyleSheet("color: #88DD88; font-weight: bold;")
        ln_lay.addWidget(lbl_local)
        self._local_path_box = QLineEdit()
        self._local_path_box.setReadOnly(True)
        self._local_path_box.setToolTip("Current folder on this computer")
        ln_lay.addWidget(self._local_path_box, 1)
        nav_row.addWidget(local_nav, 1)

        # Remote nav
        remote_nav = QWidget()
        remote_nav.setStyleSheet("background: #252525;")
        rn_lay = QHBoxLayout(remote_nav)
        rn_lay.setContentsMargins(6, 4, 6, 4)
        rn_lay.setSpacing(4)
        self._btn_remote_up = QPushButton()
        self._btn_remote_up.setIcon(_toolbar_icon("up"))
        self._btn_remote_up.setFixedSize(32, 26)
        self._btn_remote_up.setToolTip("Go up one folder (Backspace)\nOr drop files here to move them up a folder")
        self._btn_remote_up.setAcceptDrops(True)
        rn_lay.addWidget(self._btn_remote_up)
        lbl_remote = QLabel("Instrument: ")
        lbl_remote.setStyleSheet("color: #88AADD; font-weight: bold;")
        rn_lay.addWidget(lbl_remote)
        self._remote_path_box = QLineEdit()
        self._remote_path_box.setReadOnly(True)
        self._remote_path_box.setToolTip("Current folder on the instrument")
        rn_lay.addWidget(self._remote_path_box, 1)
        nav_row.addWidget(remote_nav, 1)

        outer.addLayout(nav_row)

        # Splitter with two tree widgets
        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(4)

        self._local_tree = self._make_tree(False, "Name (Local)", "Size", "Modified")
        self._remote_tree = self._make_tree(True, "Name (Instrument)", "Size", "Modified")
        splitter.addWidget(self._local_tree)
        splitter.addWidget(self._remote_tree)
        splitter.setSizes([480, 480])

        outer.addWidget(splitter, 1)

        # Toolbar row
        tb_row = QHBoxLayout()
        tb_row.setContentsMargins(0, 0, 0, 0)
        tb_row.setSpacing(0)

        # Local toolbar
        local_tb = QWidget()
        local_tb.setStyleSheet("background: #252525; border-top: 1px solid #444;")
        lt_lay = QHBoxLayout(local_tb)
        lt_lay.setContentsMargins(6, 4, 6, 4)
        lt_lay.setSpacing(4)
        self._btn_upload = QPushButton("Send to Instrument")
        self._btn_upload.setIcon(_toolbar_icon("right"))
        self._btn_upload.setIconSize(QSize(16, 16))
        self._btn_upload.setMinimumWidth(self._btn_upload.sizeHint().width())
        self._btn_upload.setToolTip("Copy the selected files and folders to the instrument folder on the right")
        self._btn_local_new = QPushButton("New Folder")
        self._btn_local_new.setToolTip("Create a new folder in the current folder on this computer")
        self._btn_local_del = QPushButton("Delete")
        self._btn_local_del.setToolTip("Delete the selected files and folders from this computer (Del)")
        self._btn_local_ren = QPushButton("Rename")
        self._btn_local_ren.setToolTip("Rename the selected item on this computer (F2)")
        self._btn_local_ref = QPushButton()
        self._btn_local_ref.setIcon(_toolbar_icon("refresh"))
        self._btn_local_ref.setFixedSize(32, 26)
        self._btn_local_ref.setToolTip("Refresh this folder listing (F5)")
        for b in (self._btn_upload, self._btn_local_new, self._btn_local_del,
                  self._btn_local_ren, self._btn_local_ref):
            lt_lay.addWidget(b)
        lt_lay.addStretch()
        tb_row.addWidget(local_tb, 1)

        # Remote toolbar
        remote_tb = QWidget()
        remote_tb.setStyleSheet("background: #252525; border-top: 1px solid #444;")
        rt_lay = QHBoxLayout(remote_tb)
        rt_lay.setContentsMargins(6, 4, 6, 4)
        rt_lay.setSpacing(4)
        self._btn_download = QPushButton("Send to PC")
        self._btn_download.setIcon(_toolbar_icon("left"))
        self._btn_download.setIconSize(QSize(16, 16))
        self._btn_download.setMinimumWidth(self._btn_download.sizeHint().width())
        self._btn_download.setToolTip("Copy the selected files and folders to the folder on the left (this computer)")
        self._btn_remote_new = QPushButton("New Folder")
        self._btn_remote_new.setToolTip("Create a new folder in the current folder on the instrument")
        self._btn_remote_del = QPushButton("Delete")
        self._btn_remote_del.setToolTip("Delete the selected files and folders from the instrument (Del)")
        self._btn_remote_ren = QPushButton("Rename")
        self._btn_remote_ren.setToolTip("Rename the selected item on the instrument (F2)")
        self._btn_remote_ref = QPushButton()
        self._btn_remote_ref.setIcon(_toolbar_icon("refresh"))
        self._btn_remote_ref.setFixedSize(32, 26)
        self._btn_remote_ref.setToolTip("Refresh this folder listing (F5)")
        for b in (self._btn_download, self._btn_remote_new, self._btn_remote_del,
                  self._btn_remote_ren, self._btn_remote_ref):
            rt_lay.addWidget(b)
        rt_lay.addStretch()
        tb_row.addWidget(remote_tb, 1)

        outer.addLayout(tb_row)

        # Status bar
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_label = QLabel("Ready")
        self._status_label.setStyleSheet("color: #CCCCCC;")
        self._progress_bar = QProgressBar()
        self._progress_bar.setMinimumWidth(300)
        self._progress_bar.setFormat("%p%")
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setVisible(False)
        self._status_bar.addWidget(self._status_label, 1)
        self._status_bar.addPermanentWidget(self._progress_bar)

    def _make_tree(self, is_remote: bool, *headers: str) -> _DragTreeWidget:
        tree = _DragTreeWidget(is_remote, self)
        tree.setHeaderLabels(list(headers))
        tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        tree.setRootIsDecorated(False)
        tree.setAllColumnsShowFocus(True)
        tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        hdr = tree.header()
        hdr.setStretchLastSection(True)
        hdr.resizeSection(0, 240)
        hdr.resizeSection(1, 80)
        return tree

    # ── Signal wiring ─────────────────────────────────────────────────────────

    def _wire_signals(self):
        self._btn_local_up.clicked.connect(self._on_local_up)
        self._btn_remote_up.clicked.connect(self._on_remote_up)
        self._btn_upload.clicked.connect(self._on_upload)
        self._btn_download.clicked.connect(self._on_download)
        self._btn_local_new.clicked.connect(self._on_local_new_folder)
        self._btn_remote_new.clicked.connect(self._on_remote_new_folder)
        self._btn_local_del.clicked.connect(self._on_local_delete)
        self._btn_remote_del.clicked.connect(self._on_remote_delete)
        self._btn_local_ren.clicked.connect(self._on_local_rename)
        self._btn_remote_ren.clicked.connect(self._on_remote_rename)
        self._btn_local_ref.clicked.connect(self._refresh_local)
        self._btn_remote_ref.clicked.connect(self._refresh_remote)

        self._local_tree.itemDoubleClicked.connect(self._on_local_double_click)
        self._remote_tree.itemDoubleClicked.connect(self._on_remote_double_click)

        self._local_tree.customContextMenuRequested.connect(
            lambda pos: self._show_context_menu(self._local_tree, False, pos))
        self._remote_tree.customContextMenuRequested.connect(
            lambda pos: self._show_context_menu(self._remote_tree, True, pos))

        self._local_tree.header().sectionClicked.connect(self._on_local_header_click)
        self._remote_tree.header().sectionClicked.connect(self._on_remote_header_click)

        self._drive_combo.currentIndexChanged.connect(self._on_drive_changed)

        self._btn_local_up.installEventFilter(self)
        self._btn_remote_up.installEventFilter(self)

    # ── Initial connect ───────────────────────────────────────────────────────

    def _initial_connect(self):
        self._populate_drives()
        self._refresh_local()
        self._set_status("Connecting to the instrument FTP…")
        self._run_bg(self._bg_connect)

    def _bg_connect(self):
        try:
            self._ftp.connect()
            self._status_signal.emit("Connected.")
            start = normalize_remote_folder(
                self._settings.file_manager_default_remote_folder if self._settings else "")
            if start != "/":
                # LIST of a missing folder is an empty success on the instruments, so check
                # the folder is really there before opening it.
                name = start.rsplit("/", 1)[-1]
                try:
                    found = any(e.name == name and e.is_directory
                                for e in self._ftp.list_dir(_ftp_parent(start), include_hidden=True))
                except ftplib.error_perm:
                    found = False
                if found:
                    self._remote_path = start
                else:
                    self._status_signal.emit(f"Default start folder {start} not found; showing /.")
            self._bg_refresh_remote()
        except Exception as e:
            self._status_signal.emit(f"FTP connect failed: {e}")

    # ── Background task runner ────────────────────────────────────────────────

    def _run_bg(self, fn, *args):
        threading.Thread(target=self._bg_wrapper, args=(fn, *args),
                         daemon=True).start()

    def _bg_wrapper(self, fn, *args):
        try:
            fn(*args)
        except Exception as e:
            self._status_signal.emit(f"Error: {e}")
            self._busy_signal.emit(False, "")

    # ── Navigation ────────────────────────────────────────────────────────────

    def _on_local_up(self):
        parent = str(pathlib.Path(self._local_path).parent)
        if parent == self._local_path:
            return
        self._local_path = parent
        self._refresh_local()

    def _on_remote_up(self):
        parent = _ftp_parent(self._remote_path)
        if parent == self._remote_path:
            return
        self._remote_path = parent
        self._refresh_remote()

    def _on_local_double_click(self, item: QTreeWidgetItem, col: int):
        entry = self._entry_from_item(item, self._local_entries)
        if entry and entry.is_directory:
            self._local_path = entry.full_path
            self._refresh_local()

    def _on_remote_double_click(self, item: QTreeWidgetItem, col: int):
        entry = self._entry_from_item(item, self._remote_entries)
        if entry and entry.is_directory:
            self._remote_path = entry.full_path
            self._refresh_remote()

    # ── Refresh ───────────────────────────────────────────────────────────────

    def _refresh_local(self):
        self._local_path_box.setText(self._local_path)
        self._sync_drive_combo()
        try:
            entries: List[FileEntry] = []
            p = pathlib.Path(self._local_path)
            for d in sorted(p.iterdir()):
                try:
                    st = d.stat()
                    is_dir = d.is_dir()
                    # Windows sets the READONLY attribute on customised folders (Documents, Desktop…),
                    # so it only means "read-only" for files.
                    ro = not is_dir and (bool(getattr(st, "st_file_attributes", 0) & 0x1)
                                         or not os.access(d, os.W_OK))
                    entries.append(FileEntry(d.name, str(d), is_dir, 0 if is_dir else st.st_size,
                                             datetime.fromtimestamp(st.st_mtime), read_only=ro))
                except PermissionError:
                    continue
            parent = str(p.parent)
            if parent != self._local_path:
                entries.append(FileEntry("..", parent, True, 0, None, is_parent=True))
            self._local_entries = _sort_entries(entries, self._local_sort_col, self._local_sort_asc)
            self._populate_tree(self._local_tree, self._local_entries)
            self._set_status(f"{sum(not e.is_parent for e in entries)} item(s) in {self._local_path}")
        except Exception as e:
            self._set_status(f"Error listing local: {e}")

    def _refresh_remote(self):
        if self._busy:
            return
        self._set_status(f"Loading {self._remote_path}…")
        self._run_bg(self._bg_refresh_remote)

    def _bg_refresh_remote(self):
        try:
            entries = self._ftp.list_dir(self._remote_path)
            self._refresh_remote_signal.emit(entries)
        except Exception as e:
            self._status_signal.emit(f"Error listing remote: {e}")

    @Slot(list)
    def _on_remote_listing(self, entries: List[FileEntry]):
        parent = _ftp_parent(self._remote_path)
        if parent != self._remote_path:
            entries = entries + [FileEntry("..", parent, True, 0, None, is_parent=True)]
        self._remote_entries = _sort_entries(entries, self._remote_sort_col, self._remote_sort_asc)
        self._remote_path_box.setText(self._remote_path)
        self._populate_tree(self._remote_tree, self._remote_entries)
        count = f"{sum(not e.is_parent for e in entries)} item(s) in {self._remote_path}"
        note = getattr(self, "_result_note", None)
        self._result_note = None
        # Keep a transfer's outcome (incl. failures) visible instead of replacing it with the item count.
        self._set_status(f"{note}  ·  {count}" if note else count)

    def _populate_tree(self, tree: QTreeWidget, entries: List[FileEntry]):
        tree.clear()
        for entry in entries:
            item = QTreeWidgetItem([entry.display_name, entry.size_text_or_blank, entry.date_text])
            if entry.read_only:
                item.setForeground(0, QColor("#8FB8E8"))
            tree.addTopLevelItem(item)

    # ── Upload ────────────────────────────────────────────────────────────────

    def _on_upload(self):
        if self._busy:
            return
        items = self._selected_entries(self._local_tree, self._local_entries)
        if not items:
            self._set_status("Select one or more local files or folders to upload.")
            return
        self._set_busy(True, f"Uploading {len(items)} item(s)…")
        self._run_bg(self._bg_upload, items, False)

    def _bg_upload(self, items: List[FileEntry], delete_source: bool):
        done, cancelled = self._do_upload_batch(items, self._remote_path, delete_source)
        status = "Upload cancelled." if cancelled else f"Uploaded {done} file(s) → {self._remote_path}"
        self._status_signal.emit(status)
        self._result_note = status
        self._busy_signal.emit(False, "")
        if delete_source:
            self._refresh_local_signal.emit()
        self._bg_refresh_remote()

    # ── Download ──────────────────────────────────────────────────────────────

    def _on_download(self):
        if self._busy:
            return
        items = self._selected_entries(self._remote_tree, self._remote_entries)
        if not items:
            self._set_status("Select one or more instrument files or folders to download.")
            return
        self._set_busy(True, f"Downloading {len(items)} item(s)…")
        self._run_bg(self._bg_download, items, False)

    def _bg_download(self, items: List[FileEntry], delete_source: bool):
        done, cancelled = self._do_download_batch(items, self._local_path, delete_source)
        status = "Download cancelled." if cancelled else f"Downloaded {done} file(s) → {self._local_path}"
        self._status_signal.emit(status)
        self._busy_signal.emit(False, "")
        self._refresh_local_signal.emit()
        if delete_source:
            self._result_note = status
            self._bg_refresh_remote()

    # ── New Folder ────────────────────────────────────────────────────────────

    def _on_local_new_folder(self):
        if self._busy:
            return
        name, ok = QInputDialog.getText(self, "New Folder", "Folder name:", text="NewFolder")
        if not ok or not name.strip():
            return
        path = os.path.join(self._local_path, name.strip())
        try:
            os.makedirs(path, exist_ok=True)
            self._refresh_local()
            self._set_status(f"Created {path}")
        except Exception as e:
            self._set_status(f"Failed: {e}")

    def _on_remote_new_folder(self):
        if self._busy:
            return
        name, ok = QInputDialog.getText(self, "New Folder", "Folder name:", text="NewFolder")
        if not ok or not name.strip():
            return
        path = f"{self._remote_path.rstrip('/')}/{name.strip()}"
        self._set_busy(True, f"Creating {path}…")
        self._run_bg(self._bg_remote_mkdir, path)

    def _bg_remote_mkdir(self, path: str):
        try:
            self._ftp.mkdir(path)
            self._status_signal.emit(f"Created {path}")
        except Exception as e:
            self._status_signal.emit(f"Failed: {e}")
        self._busy_signal.emit(False, "")
        self._bg_refresh_remote()

    # ── Delete ────────────────────────────────────────────────────────────────

    def _on_local_delete(self):
        if self._busy:
            return
        items = self._selected_entries(self._local_tree, self._local_entries)
        if not items:
            self._set_status("Select items to delete.")
            return
        if self._refuse_read_only(items):
            return
        r = QMessageBox.question(self, "Delete",
                                 f"Delete {len(items)} item(s)?",
                                 QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r != QMessageBox.Yes:
            return
        # On a worker thread: rmtree of a big folder (or any delete on a slow network drive) would freeze the window.
        self._set_busy(True, f"Deleting {len(items)} item(s)…")
        self._run_bg(self._bg_local_delete, items)

    def _bg_local_delete(self, items: List[FileEntry]):
        msg = ""
        done = 0
        failed: List[str] = []
        try:
            for i, entry in enumerate(items):
                self._count_signal.emit(i, len(items), -1.0)
                try:
                    if entry.is_directory:
                        shutil.rmtree(entry.full_path)
                    else:
                        os.remove(entry.full_path)
                    done += 1
                except Exception as e:
                    failed.append(f"{entry.name}: {e}")
            msg = f"Deleted {done}/{len(items)} item(s)."
            if failed:
                msg += f" Failed {failed[0]}" + (" …" if len(failed) > 1 else "")
        finally:
            self._busy_signal.emit(False, "")
            self._refresh_local_signal.emit()
            if msg:
                self._status_signal.emit(msg)

    def _on_remote_delete(self):
        if self._busy:
            return
        items = self._selected_entries(self._remote_tree, self._remote_entries)
        if not items:
            self._set_status("Select items to delete.")
            return
        if self._refuse_read_only(items):
            return
        # A top-level entry is a Kronos storage volume (SSD1/SSD2/...), not a
        # user folder — deleting one would recursively wipe the whole volume.
        # Refused outright, not just warned about: a generic "Delete N item(s)?"
        # confirmation doesn't convey what's actually about to happen (matches
        # C#'s BuildContextMenu, which disables Delete entirely for these
        # rather than relying on the confirmation dialog).
        top_level = [e for e in items if _is_top_level_ftp_path(e.full_path)]
        if top_level:
            names = ", ".join(e.name for e in top_level)
            self._set_status(f"Cannot delete {names} — top-level instrument storage "
                             "volumes can never be deleted.")
            return
        r = QMessageBox.question(self, "Delete",
                                 f"Delete {len(items)} item(s) from the instrument?",
                                 QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r != QMessageBox.Yes:
            return
        self._set_busy(True, f"Deleting {len(items)} item(s)…")
        self._run_bg(self._bg_remote_delete, items)

    def _bg_remote_delete(self, items: List[FileEntry]):
        done = 0
        for i, entry in enumerate(items):
            self._count_signal.emit(i, len(items), -1.0)
            try:
                if entry.is_directory:
                    self._ftp.delete_dir(entry.full_path)
                else:
                    self._ftp.delete_file(entry.full_path)
                done += 1
            except Exception as e:
                self._status_signal.emit(f"Failed {entry.name}: {e}")
        self._status_signal.emit(f"Deleted {done}/{len(items)} item(s).")
        self._busy_signal.emit(False, "")
        self._bg_refresh_remote()

    # ── Rename ────────────────────────────────────────────────────────────────

    def _on_local_rename(self):
        if self._busy:
            return
        items = self._selected_entries(self._local_tree, self._local_entries)
        if len(items) != 1:
            self._set_status("Select exactly one item to rename.")
            return
        if self._refuse_read_only(items):
            return
        entry = items[0]
        new_name, ok = QInputDialog.getText(self, "Rename", "New name:", text=entry.name)
        if not ok or not new_name.strip() or new_name.strip() == entry.name:
            return
        new_path = _safe_local_join(os.path.dirname(entry.full_path), new_name.strip())
        if new_path is None or new_name.strip() != os.path.basename(new_path):
            self._set_status("Rename cancelled — the new name can't contain a folder path.")
            return
        # os.rename silently replaces an existing file on Linux/macOS; a case-only rename of the same item is fine.
        if os.path.lexists(new_path) and not _same_local_path(entry.full_path, new_path):
            self._set_status(f"Rename cancelled — '{new_name.strip()}' already exists here.")
            return
        try:
            os.rename(entry.full_path, new_path)
            self._refresh_local()
            self._set_status(f"Renamed → {new_name.strip()}")
        except Exception as e:
            self._set_status(f"Rename failed: {e}")

    def _on_remote_rename(self):
        if self._busy:
            return
        items = self._selected_entries(self._remote_tree, self._remote_entries)
        if len(items) != 1:
            self._set_status("Select exactly one item to rename.")
            return
        if self._refuse_read_only(items):
            return
        entry = items[0]
        # Checked here, ahead of even prompting for a new name, so the context
        # menu's own disabled state isn't the only thing standing between the
        # user and this — _FtpWorker.rename's guard is the last line of
        # defense, not the first (matches C#'s OnRemoteRename).
        if _is_top_level_ftp_path(entry.full_path):
            self._set_status(f"Cannot rename '{entry.name}' — top-level instrument storage "
                             "volumes can never be renamed or moved.")
            return
        new_name, ok = QInputDialog.getText(self, "Rename", "New name:", text=entry.name)
        if not ok or not new_name.strip() or new_name.strip() == entry.name:
            return
        new_path = f"{_ftp_parent(entry.full_path).rstrip('/')}/{new_name.strip()}"
        self._set_busy(True, "Renaming…")
        self._run_bg(self._bg_remote_rename, entry.full_path, new_path, new_name.strip())

    def _bg_remote_rename(self, old_path: str, new_path: str, display_name: str):
        try:
            # Both instruments accept RNTO over an existing file, which would silently destroy it.
            if self._ftp.file_exists(new_path):
                self._status_signal.emit(f"Rename cancelled — '{display_name}' already exists here.")
                self._busy_signal.emit(False, "")
                return
            self._ftp.rename(old_path, new_path)
            self._status_signal.emit(f"Renamed → {display_name}")
        except Exception as e:
            self._status_signal.emit(f"Rename failed: {e}")
        self._busy_signal.emit(False, "")
        self._bg_refresh_remote()

    # ── Context menu ──────────────────────────────────────────────────────────

    def _show_context_menu(self, tree: QTreeWidget, is_remote: bool, pos):
        item = tree.itemAt(pos)
        entry = self._entry_from_item(item, self._remote_entries if is_remote else self._local_entries) if item else None
        entries = self._selected_entries(tree, self._remote_entries if is_remote else self._local_entries)
        has_selection = len(entries) > 0
        is_single = len(entries) == 1
        # A top-level remote entry (SSD1/SSD2/...) can never be cut, copied,
        # renamed, or deleted — matches C#'s BuildContextMenu. Open (navigating
        # INTO the volume) is unaffected.
        remote_top_level_selected = is_remote and any(
            _is_top_level_ftp_path(e.full_path) for e in entries)
        read_only_selected = any(e.read_only for e in entries)

        menu = QMenu(self)

        if entry and entry.is_directory:
            a = menu.addAction(_toolbar_icon("open"), "Open")
            a.triggered.connect(lambda: self._ctx_open(entry, is_remote))
            a = menu.addAction(_toolbar_icon("star"), "Set as Default Start Folder")
            a.setEnabled(self._settings is not None)
            a.triggered.connect(lambda: self._set_default_start_folder(entry.full_path, is_remote))
        a = menu.addAction(_toolbar_icon("left" if is_remote else "right"),
                           "Send to PC" if is_remote else "Send to Instrument")
        a.setEnabled(has_selection and not self._busy)
        a.triggered.connect(self._on_download if is_remote else self._on_upload)

        menu.addSeparator()

        a = menu.addAction(_toolbar_icon("cut"), "Cut")
        a.setEnabled(has_selection and not remote_top_level_selected and not read_only_selected)
        a.triggered.connect(lambda: self._do_cut(tree, is_remote))

        a = menu.addAction(_toolbar_icon("copy"), "Copy")
        a.setEnabled(has_selection and not remote_top_level_selected and not read_only_selected)
        a.triggered.connect(lambda: self._do_copy(tree, is_remote))

        a = menu.addAction(_toolbar_icon("paste"), "Paste")
        a.setEnabled(self._clipboard is not None and not self._busy)
        a.triggered.connect(lambda: self._do_paste(is_remote))

        menu.addSeparator()

        a = menu.addAction(_toolbar_icon("rename"), "Rename")
        a.setEnabled(is_single and entry is not None and not remote_top_level_selected
                     and not read_only_selected)
        a.triggered.connect(self._on_remote_rename if is_remote else self._on_local_rename)

        a = menu.addAction(_toolbar_icon("delete"), "Delete")
        a.setEnabled(has_selection and not remote_top_level_selected and not read_only_selected)
        a.triggered.connect(self._on_remote_delete if is_remote else self._on_local_delete)

        menu.addSeparator()

        a = menu.addAction(_toolbar_icon("info"), "Properties")
        a.triggered.connect(lambda: self._on_properties(is_remote))

        menu.addSeparator()

        a = menu.addAction(_toolbar_icon("newfolder"), "New Folder")
        a.setEnabled(not self._busy)
        a.triggered.connect(self._on_remote_new_folder if is_remote else self._on_local_new_folder)

        a = menu.addAction(_toolbar_icon("refresh"), "Refresh")
        a.setEnabled(not self._busy)
        a.triggered.connect(self._refresh_remote if is_remote else self._refresh_local)

        menu.exec(tree.viewport().mapToGlobal(pos))

    def _on_properties(self, is_remote: bool):
        """Properties of the selection, or of the folder being viewed when nothing is selected."""
        tree = self._remote_tree if is_remote else self._local_tree
        entries = self._selected_entries(tree, self._remote_entries if is_remote else self._local_entries)
        if not entries:
            path = self._remote_path if is_remote else self._local_path
            name = path.rstrip("/").rsplit("/", 1)[-1] or path if is_remote else (os.path.basename(path) or path)
            entries = [FileEntry(name, path, True, 0, None)]
        listdir = (lambda p: self._ftp.list_dir(p, include_hidden=True)) if is_remote else _local_children
        dlg = _PropertiesDialog(entries, is_remote, listdir, self)
        try:
            dlg.exec()
        finally:
            dlg.deleteLater()

    def _ctx_open(self, entry: FileEntry, is_remote: bool):
        if is_remote:
            self._remote_path = entry.full_path
            self._refresh_remote()
        else:
            self._local_path = entry.full_path
            self._refresh_local()

    def _set_default_start_folder(self, path: str, is_remote: bool):
        from Models.storage import save_settings_async
        if is_remote:
            path = normalize_remote_folder(path)
            self._settings.file_manager_default_remote_folder = path
        else:
            self._settings.file_manager_default_local_folder = path
        save_settings_async(self._settings)
        pane = "instrument" if is_remote else "computer"
        self._set_status(f"Default {pane} start folder: {path}")

    # ── Clipboard (cut/copy/paste) ────────────────────────────────────────────

    def _refuse_read_only(self, items: List[FileEntry]) -> bool:
        if any(e.read_only for e in items):
            self._set_status("Read-only item(s) can't be cut, copied, renamed or deleted.")
            return True
        return False

    def _do_cut(self, tree: QTreeWidget, is_remote: bool):
        items = self._selected_entries(tree, self._remote_entries if is_remote else self._local_entries)
        if not items:
            return
        if self._refuse_read_only(items):
            return
        self._clipboard = ClipboardPayload(is_cut=True, from_remote=is_remote, items=items)
        self._set_status(f"Cut {len(items)} item(s) — paste to move.")

    def _do_copy(self, tree: QTreeWidget, is_remote: bool):
        items = self._selected_entries(tree, self._remote_entries if is_remote else self._local_entries)
        if not items:
            return
        if self._refuse_read_only(items):
            return
        self._clipboard = ClipboardPayload(is_cut=False, from_remote=is_remote, items=items)
        self._set_status(f"Copied {len(items)} item(s) — paste to copy.")

    def _do_paste(self, to_remote: bool):
        if not self._clipboard or self._busy:
            return
        cb = self._clipboard
        items = cb.items

        if not cb.from_remote and not to_remote:
            # Local → Local
            if cb.is_cut:
                self._move_local(items, self._local_path)
            else:
                self._copy_local(items, self._local_path)
            if cb.is_cut:
                self._clipboard = None
        elif cb.from_remote and to_remote:
            # Remote → Remote (cut = rename/move)
            if cb.is_cut:
                self._set_busy(True, f"Moving {len(items)} item(s)…")
                self._run_bg(self._bg_move_remote, items, self._remote_path)
                self._clipboard = None
            else:
                self._set_status("Remote-to-remote copy not supported — download then re-upload.")
        elif not cb.from_remote and to_remote:
            # Local → Remote (upload; delete source inside bg task if cut)
            if items:
                self._set_busy(True, f"Uploading {len(items)} item(s)…")
                self._run_bg(self._bg_upload, items, cb.is_cut)
            if cb.is_cut:
                self._clipboard = None
        else:
            # Remote → Local (download; delete source inside bg task if cut)
            if items:
                self._set_busy(True, f"Downloading {len(items)} item(s)…")
                self._run_bg(self._bg_download, items, cb.is_cut)
            if cb.is_cut:
                self._clipboard = None

    def _move_local(self, items: List[FileEntry], dest_folder: str):
        self._start_local_transfer(items, dest_folder, is_move=True)

    def _copy_local(self, items: List[FileEntry], dest_folder: str):
        self._start_local_transfer(items, dest_folder, is_move=False)

    def _start_local_transfer(self, items: List[FileEntry], dest_folder: str, is_move: bool):
        """Local copy/move runs on a worker thread (a big folder tree would otherwise freeze the window) and
        prompts on every name collision exactly like the cross-pane transfers do."""
        if self._busy or not items:
            return
        self._set_busy(True, f"{'Moving' if is_move else 'Copying'} {len(items)} item(s)…")
        self._run_bg(self._bg_local_transfer, items, dest_folder, is_move)

    def _bg_local_transfer(self, items: List[FileEntry], dest_folder: str, is_move: bool):
        msg = ""
        try:
            done, cancelled, failures = self._do_local_batch(items, dest_folder, is_move)
            verb = "Move" if is_move else "Copy"
            msg = (f"{verb} cancelled after {done} item(s)." if cancelled else
                   f"{'Moved' if is_move else 'Copied'} {done}/{len(items)} item(s) → {dest_folder}")
            if failures:
                msg += f" — {len(failures)} not done: {failures[0]}" + (" …" if len(failures) > 1 else "")
        finally:
            self._busy_signal.emit(False, "")
            self._refresh_local_signal.emit()   # queued before the result, which the listing's count would replace
            if msg:
                self._status_signal.emit(msg)

    def _do_local_batch(self, items: List[FileEntry], dest_folder: str,
                        is_move: bool) -> Tuple[int, bool, List[str]]:
        """Copies/moves items into dest_folder on the calling (worker) thread, prompting on name collisions
        (Rename / Overwrite / Skip / Cancel, with apply-to-all) the way _do_upload_batch does. Overwrite
        replaces a file via a temp sibling + os.replace and merges a folder into the existing one. Returns
        (done_count, cancelled, messages for the items that were skipped or failed)."""
        total = len(items)
        done = 0
        forced_action: Optional[str] = None
        cancelled = False
        verb = "move" if is_move else "copy"
        failures: List[str] = []

        def note(msg: str):
            failures.append(msg)
            self._status_signal.emit(msg)

        for i, entry in enumerate(items):
            src = entry.full_path
            name = entry.name
            dest = _safe_local_join(dest_folder, name)
            if dest is None:
                note(f"Skipped {name}: unsafe file name")
                continue
            if entry.is_directory and _local_is_inside(dest_folder, src):
                note(f"Skipped {name}: can't {verb} a folder into itself")
                continue
            onto_itself = _same_local_path(src, dest)
            if onto_itself and is_move:
                continue   # already there: nothing to do (and "overwrite" would delete it)
            overwrite = False
            if onto_itself or os.path.lexists(dest):
                exists_fn = lambda n, d=dest_folder: os.path.lexists(os.path.join(d, n))
                action = forced_action
                new_name = None
                if action is None:
                    req = self._resolve_conflict(name, self._suggest_name(name, exists_fn))
                    if req.action == "cancel":
                        cancelled = True
                        break
                    action = req.action
                    new_name = req.new_name
                    if req.apply_all:
                        forced_action = action
                if action == "skip":
                    continue
                if action == "rename":
                    if not new_name or exists_fn(new_name):
                        new_name = self._suggest_name(name, exists_fn)
                    dest = _safe_local_join(dest_folder, new_name)
                    if dest is None:
                        note(f"Skipped {name}: unsafe replacement name")
                        continue
                elif action == "overwrite":
                    if onto_itself:
                        note(f"Skipped {name}: it can't overwrite itself — choose Rename to make a copy")
                        continue
                    if entry.is_directory != os.path.isdir(dest):
                        note(f"Skipped {name}: a {'file' if entry.is_directory else 'folder'} with that name "
                             "is already there")
                        continue
                    if entry.is_directory and _local_is_inside(src, dest):
                        note(f"Skipped {name}: the destination folder contains it")
                        continue
                    overwrite = True
            try:
                self._status_signal.emit(f"[{i+1}/{total}] {'Moving' if is_move else 'Copying'} "
                                         f"{os.path.basename(dest)}…")
                self._count_signal.emit(i, total, -1.0)
                if entry.is_directory:
                    if overwrite:
                        errors: List[str] = []
                        _merge_local_dir(src, dest, is_move, errors)
                        if errors:
                            raise OSError(f"{len(errors)} problem(s), first: {errors[0]}")
                    else:
                        _put_local_dir(src, dest, is_move)
                else:
                    _put_local_file(src, dest, is_move)
                done += 1
            except Exception as e:
                note(f"Failed {entry.name}: {e}")
        self._progress_signal.emit(100)
        if failures and not cancelled:
            log.warning("Local %s: %s", verb, failures)
        return done, cancelled, failures

    def _bg_move_remote(self, items: List[FileEntry], dest_folder: str):
        done = 0
        for i, entry in enumerate(items):
            self._count_signal.emit(i, len(items), -1.0)
            dest = f"{dest_folder.rstrip('/')}/{entry.name}"
            try:
                if self._ftp.file_exists(dest):   # RNTO would silently replace it
                    self._status_signal.emit(f"Skipped {entry.name}: already exists in {dest_folder}")
                    continue
                self._ftp.rename(entry.full_path, dest)
                done += 1
            except Exception as e:
                self._status_signal.emit(f"Failed {entry.name}: {e}")
        self._status_signal.emit(f"Moved {done}/{len(items)} item(s) → {dest_folder}")
        self._busy_signal.emit(False, "")
        self._bg_refresh_remote()

    # ── Drag-drop handlers ─────────────────────────────────────────────────────

    def _handle_same_pane_drop(self, is_remote: bool, items: List[FileEntry],
                               target_folder: Optional[FileEntry]):
        if self._refuse_read_only(items):
            return
        if is_remote:
            dest = target_folder.full_path if target_folder else self._remote_path
            source_dir = _ftp_parent(items[0].full_path) if items else self._remote_path
            if dest.rstrip("/") == source_dir.rstrip("/") and not target_folder:
                return
            for entry in items:
                if entry.is_directory and dest.startswith(entry.full_path.rstrip("/") + "/"):
                    self._set_status("Cannot move a folder into itself.")
                    return
            self._set_busy(True, f"Moving {len(items)} item(s)…")
            self._run_bg(self._bg_move_remote, items, dest)
        else:
            dest = target_folder.full_path if target_folder else self._local_path
            source_dir = os.path.dirname(items[0].full_path) if items else self._local_path
            if os.path.normpath(dest) == os.path.normpath(source_dir) and not target_folder:
                return
            for entry in items:
                if entry.is_directory:
                    norm_entry = os.path.normpath(entry.full_path)
                    norm_dest = os.path.normpath(dest)
                    if norm_dest == norm_entry or norm_dest.startswith(norm_entry + os.sep):
                        self._set_status("Cannot move a folder into itself.")
                        return
            self._move_local(items, dest)

    def _handle_cross_pane_drop(self, from_remote: bool, to_remote: bool,
                                items: List[FileEntry],
                                target_folder: Optional[FileEntry]):
        if not items:
            return
        if to_remote:
            dest = target_folder.full_path if target_folder else self._remote_path
            self._set_busy(True, f"Uploading {len(items)} item(s)…")
            self._run_bg(self._bg_upload_to, items, dest)
        else:
            dest = target_folder.full_path if target_folder else self._local_path
            self._set_busy(True, f"Downloading {len(items)} item(s)…")
            self._run_bg(self._bg_download_to, items, dest)

    def _bg_upload_to(self, items: List[FileEntry], dest_folder: str):
        done, cancelled = self._do_upload_batch(items, dest_folder)
        status = "Upload cancelled." if cancelled else f"Uploaded {done} file(s) → {dest_folder}"
        self._status_signal.emit(status)
        self._result_note = status
        self._busy_signal.emit(False, "")
        self._bg_refresh_remote()

    def _bg_download_to(self, items: List[FileEntry], dest_folder: str):
        done, cancelled = self._do_download_batch(items, dest_folder)
        status = "Download cancelled." if cancelled else f"Downloaded {done} file(s) → {dest_folder}"
        self._status_signal.emit(status)
        self._busy_signal.emit(False, "")
        self._refresh_local_signal.emit()

    # ── Keyboard shortcuts ────────────────────────────────────────────────────

    def keyPressEvent(self, event: QKeyEvent):
        key = event.key()
        mods = event.modifiers()
        ctrl = bool(mods & Qt.ControlModifier)

        remote_focus = self._remote_tree.hasFocus()
        local_focus = self._local_tree.hasFocus()
        any_pane = remote_focus or local_focus
        is_remote = remote_focus
        tree = self._remote_tree if remote_focus else self._local_tree if local_focus else None

        if ctrl and any_pane:
            if key == Qt.Key_C:
                self._do_copy(tree, is_remote); return
            if key == Qt.Key_X:
                self._do_cut(tree, is_remote); return
            if key == Qt.Key_V:
                self._do_paste(is_remote); return
            if key == Qt.Key_A:
                tree.selectAll(); return

        if any_pane and not event.isAutoRepeat():
            if key == Qt.Key_Delete:
                (self._on_remote_delete if is_remote else self._on_local_delete)()
                return
            if key in (Qt.Key_Return, Qt.Key_Enter) and mods & Qt.AltModifier:
                self._on_properties(is_remote)
                return
            if key == Qt.Key_F2:
                (self._on_remote_rename if is_remote else self._on_local_rename)()
                return
            if key == Qt.Key_F5:
                (self._refresh_remote if is_remote else self._refresh_local)()
                return
            if key == Qt.Key_Backspace:
                (self._on_remote_up if is_remote else self._on_local_up)()
                return
            if key == Qt.Key_Return or key == Qt.Key_Enter:
                entries = self._selected_entries(
                    tree, self._remote_entries if is_remote else self._local_entries,
                    include_parent=True)
                if len(entries) == 1 and entries[0].is_directory:
                    if is_remote:
                        self._remote_path = entries[0].full_path
                        self._refresh_remote()
                    else:
                        self._local_path = entries[0].full_path
                        self._refresh_local()
                return

        super().keyPressEvent(event)

    # ── Column sorting ────────────────────────────────────────────────────────

    def _on_local_header_click(self, col: int):
        if col == self._local_sort_col:
            self._local_sort_asc = not self._local_sort_asc
        else:
            self._local_sort_col = col
            self._local_sort_asc = True
        self._local_entries = _sort_entries(self._local_entries, self._local_sort_col, self._local_sort_asc)
        self._populate_tree(self._local_tree, self._local_entries)
        self._update_sort_indicators()

    def _on_remote_header_click(self, col: int):
        if col == self._remote_sort_col:
            self._remote_sort_asc = not self._remote_sort_asc
        else:
            self._remote_sort_col = col
            self._remote_sort_asc = True
        self._remote_entries = _sort_entries(self._remote_entries, self._remote_sort_col, self._remote_sort_asc)
        self._populate_tree(self._remote_tree, self._remote_entries)
        self._update_sort_indicators()

    def _update_sort_indicators(self):
        for tree, sort_col, sort_asc, base_names in (
            (self._local_tree, self._local_sort_col, self._local_sort_asc,
             ["Name (Local)", "Size", "Modified"]),
            (self._remote_tree, self._remote_sort_col, self._remote_sort_asc,
             ["Name (Instrument)", "Size", "Modified"]),
        ):
            model = tree.headerItem()
            for i, base in enumerate(base_names):
                suffix = ""
                if i == sort_col:
                    suffix = " ▲" if sort_asc else " ▼"
                model.setText(i, base + suffix)

    # ── Drive selector ────────────────────────────────────────────────────────

    def _populate_drives(self):
        self._suppress_drive_change = True
        self._drive_combo.clear()
        for root, text in list_local_drives():
            self._drive_combo.addItem(text, root)
        if os.name == "nt":
            # Volume names can stall (empty card reader, sleeping network drive): fetch them off the GUI thread.
            roots = [self._drive_combo.itemData(i) for i in range(self._drive_combo.count())]
            QTimer.singleShot(0, lambda: threading.Thread(
                target=lambda: self._drive_labels_signal.emit(_windows_drive_labels(roots)),
                daemon=True, name="DriveLabels").start())
        self._sync_drive_combo()
        self._suppress_drive_change = False

    def _sync_drive_combo(self):
        """Select the most specific drive that contains the current local folder (so '~' wins inside the home
        folder, a USB stick inside its mount, '/' elsewhere)."""
        best, best_len = -1, -1
        for i in range(self._drive_combo.count()):
            root = self._drive_combo.itemData(i)
            if root and _is_under(self._local_path, root) and len(root) > best_len:
                best, best_len = i, len(root)
        if best >= 0:
            self._suppress_drive_change = True
            self._drive_combo.setCurrentIndex(best)
            self._suppress_drive_change = False

    _suppress_drive_change = False

    @Slot(object)
    def _on_drive_labels(self, labels: dict):
        for i in range(self._drive_combo.count()):
            root = self._drive_combo.itemData(i)
            if labels.get(root):
                self._drive_combo.setItemText(i, f"\U0001F4BD {root[0]}: ({labels[root]})")

    def _on_drive_changed(self, index: int):
        if self._suppress_drive_change or index < 0:
            return
        root = self._drive_combo.itemData(index)
        if root:
            self._local_path = root
            self._refresh_local()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _entry_from_item(self, item: Optional[QTreeWidgetItem],
                         entries: List[FileEntry]) -> Optional[FileEntry]:
        if item is None:
            return None
        tree = item.treeWidget()
        if tree is None:
            return None
        idx = tree.indexOfTopLevelItem(item)
        if 0 <= idx < len(entries):
            return entries[idx]
        return None

    def _selected_entries(self, tree: QTreeWidget,
                          entries: List[FileEntry],
                          include_parent: bool = False) -> List[FileEntry]:
        result = []
        for item in tree.selectedItems():
            e = self._entry_from_item(item, entries)
            if e and (include_parent or not e.is_parent):
                result.append(e)
        return result

    def _selected_files(self, tree: QTreeWidget,
                        entries: List[FileEntry]) -> List[FileEntry]:
        return [e for e in self._selected_entries(tree, entries) if not e.is_directory]

    def _set_status(self, msg: str):
        self._status_label.setText(msg)

    @Slot(str)
    def _on_status(self, msg: str):
        self._status_label.setText(msg)

    def _set_busy(self, busy: bool, msg: str = ""):
        self._busy = busy
        for btn in (self._btn_upload, self._btn_download,
                    self._btn_local_new, self._btn_local_del, self._btn_local_ren,
                    self._btn_local_ref, self._btn_remote_new, self._btn_remote_del,
                    self._btn_remote_ren, self._btn_remote_ref):
            btn.setEnabled(not busy)
        self._progress_bar.setVisible(busy)
        if busy:
            self._progress_bar.setRange(0, 100)
            self._progress_bar.setFormat("%p%")
            self._progress_bar.setValue(0)
            self._set_status(msg)

    @Slot(bool, str)
    def _on_busy(self, busy: bool, msg: str):
        self._set_busy(busy, msg)

    @Slot(int)
    def _on_progress(self, value: int):
        self._progress_bar.setValue(value)

    @Slot(int, int, float)
    def _on_count(self, done: int, total: int, eta: float):
        """x / total on the bar itself, plus an estimate of the time left once a few items have gone."""
        if total <= 0:
            return
        self._progress_bar.setRange(0, total)
        self._progress_bar.setValue(min(done, total))
        text = f"{done:,} / {total:,}"
        if eta >= 0 and done < total:
            text += f"  ·  ~{_fmt_duration(eta)} left"
        self._progress_bar.setFormat(text)

    def _emit_tally(self, ctx: dict, advance: bool = True):
        """Worker thread: publish files completed so far for the running batch; `advance` then counts the file
        about to be processed."""
        n, total = ctx["n"], ctx["total"]
        elapsed = time.monotonic() - ctx["t0"]
        self._count_signal.emit(n, total, elapsed / n * (total - n) if n >= 3 else -1.0)
        if advance:
            ctx["n"] += 1

    def _count_local_files(self, items: List[FileEntry]) -> int:
        n = 0
        for e in items:
            if not e.is_directory:
                n += 1
                continue
            for _root, _dirs, files in os.walk(e.full_path):
                n += len(files)
        return n

    def _count_remote_files(self, items: List[FileEntry]) -> int:
        n = 0

        def walk(path: str):
            nonlocal n
            for k in self._ftp.list_dir(path, include_hidden=True):
                if k.is_directory:
                    walk(k.full_path)
                else:
                    n += 1
        for e in items:
            if not e.is_directory:
                n += 1
                continue
            try:
                walk(e.full_path)
            except Exception:
                pass
        return n

    @Slot(object)
    def _on_conflict_request(self, req: _ConflictRequest):
        dlg = _ConflictDialog(req.name, req.new_name, self)
        dlg.exec()
        req.action = dlg.action
        req.new_name = dlg.new_name
        req.apply_all = dlg.apply_all
        req.event.set()

    def _resolve_conflict(self, name: str, suggested_name: str) -> _ConflictRequest:
        """Called from a background transfer thread; blocks until the user
        answers the conflict dialog shown on the GUI thread."""
        req = _ConflictRequest(name, suggested_name)
        self._conflict_signal.emit(req)
        req.event.wait()
        return req

    @staticmethod
    def _suggest_name(base_name: str, exists_fn) -> str:
        if not exists_fn(base_name):
            return base_name
        stem, ext = os.path.splitext(base_name)
        n = 1
        while True:
            candidate = f"{stem} ({n}){ext}"
            if not exists_fn(candidate):
                return candidate
            n += 1

    def _remote_entry(self, path: str) -> Optional[FileEntry]:
        parent = _ftp_parent(path)
        name = path.rstrip("/").rsplit("/", 1)[-1]
        try:
            for e in self._ftp.list_dir(parent, include_hidden=True):
                if e.name == name:
                    return e
        except ftplib.error_perm:
            pass
        return None

    def _upload_dir(self, entry: FileEntry, remote_dir: str, delete_source: bool, ctx: dict) -> Tuple[int, bool]:
        """Recursively uploads a local folder into remote_dir (merging into a folder of the same name)."""
        dest = f"{remote_dir}/{entry.name}"
        try:
            existing = self._remote_entry(dest)
            if existing is not None and not existing.is_directory:
                self._status_signal.emit(
                    f"Skipped folder {entry.name}: a file with that name is already on the instrument")
                return 0, False
            if existing is None:
                self._ftp.mkdir(dest)
            children = []
            with os.scandir(entry.full_path) as it:
                for c in it:
                    if c.is_dir(follow_symlinks=False):
                        children.append(FileEntry(c.name, c.path, True, 0, None))
                    elif c.is_file():
                        children.append(FileEntry(c.name, c.path, False, c.stat().st_size, None))
        except Exception as e:
            self._status_signal.emit(f"Failed {entry.name}: {e}")
            return 0, False
        done, cancelled = self._do_upload_batch(children, dest, delete_source, ctx)
        if delete_source and not cancelled:
            try:
                os.rmdir(entry.full_path)   # only succeeds when everything inside went across
            except OSError:
                pass
        return done, cancelled

    def _download_dir(self, entry: FileEntry, local_dir: str, delete_source: bool, ctx: dict) -> Tuple[int, bool]:
        """Recursively downloads an instrument folder into local_dir (merging into a folder of the same name)."""
        dest = _safe_local_join(local_dir, entry.name)
        if dest is None:
            self._status_signal.emit(f"Skipped {entry.name}: unsafe folder name from server")
            return 0, False
        try:
            if os.path.exists(dest) and not os.path.isdir(dest):
                self._status_signal.emit(f"Skipped folder {entry.name}: a file with that name is already here")
                return 0, False
            os.makedirs(dest, exist_ok=True)
            children = self._ftp.list_dir(entry.full_path, include_hidden=True)
        except Exception as e:
            self._status_signal.emit(f"Failed {entry.name}: {e}")
            return 0, False
        done, cancelled = self._do_download_batch(children, dest, delete_source, ctx)
        if delete_source and not cancelled:
            try:
                self._ftp.rmdir(entry.full_path)   # only succeeds when everything inside went across
            except Exception:
                pass
        return done, cancelled

    def _do_upload_batch(self, items: List[FileEntry], remote_dir: str,
                         delete_source: bool = False, _ctx: Optional[dict] = None) -> Tuple[int, bool]:
        """Uploads items (files, and folders recursively) into remote_dir, prompting on name collisions.
        Returns (files_uploaded, cancelled)."""
        remote_dir = remote_dir.rstrip("/") or "/"
        top = _ctx is None
        ctx = _ctx if _ctx is not None else {"forced": None, "n": 0, "total": 0, "t0": time.monotonic()}
        if top:   # one ctx per batch, shared so "apply to all" and the x/total count span subfolders
            self._status_signal.emit("Scanning…")
            ctx["total"] = self._count_local_files(items)
            ctx["t0"] = time.monotonic()
        done = 0
        cancelled = False
        for i, entry in enumerate(items):
            if entry.is_directory:
                d, cancelled = self._upload_dir(entry, remote_dir, delete_source, ctx)
                done += d
                if cancelled:
                    break
                continue
            self._emit_tally(ctx)
            forced_action = ctx["forced"]
            name = entry.name
            dest = f"{remote_dir}/{name}"
            if self._ftp.file_exists(dest):
                action = forced_action
                new_name = None
                if action is None:
                    suggested = self._suggest_name(
                        name, lambda n, d=remote_dir: self._ftp.file_exists(f"{d}/{n}"))
                    req = self._resolve_conflict(name, suggested)
                    if req.action == "cancel":
                        cancelled = True
                        break
                    action = req.action
                    new_name = req.new_name
                    if req.apply_all:
                        ctx["forced"] = action
                if action == "skip":
                    continue
                if action == "rename":
                    if not new_name:
                        new_name = self._suggest_name(
                            name, lambda n, d=remote_dir: self._ftp.file_exists(f"{d}/{n}"))
                    dest = f"{remote_dir}/{new_name}"
            try:
                self._status_signal.emit(f"[{ctx['n']}/{ctx['total']}] Uploading {os.path.basename(dest)}…")
                self._ftp.upload(entry.full_path, dest)
                done += 1
                if delete_source:
                    try:
                        os.remove(entry.full_path)
                    except Exception:
                        pass
            except Exception as e:
                self._status_signal.emit(f"Failed {entry.name}: {e}")
        if top:
            self._emit_tally(ctx, advance=False)
        return done, cancelled

    def _do_download_batch(self, items: List[FileEntry], local_dir: str,
                           delete_source: bool = False, _ctx: Optional[dict] = None) -> Tuple[int, bool]:
        """Downloads items (files, and folders recursively) into local_dir, prompting on name collisions.
        Returns (files_downloaded, cancelled)."""
        top = _ctx is None
        ctx = _ctx if _ctx is not None else {"forced": None, "n": 0, "total": 0, "t0": time.monotonic()}
        if top:
            self._status_signal.emit("Scanning…")
            ctx["total"] = self._count_remote_files(items)
            ctx["t0"] = time.monotonic()
        done = 0
        cancelled = False
        for i, entry in enumerate(items):
            if entry.is_directory:
                d, cancelled = self._download_dir(entry, local_dir, delete_source, ctx)
                done += d
                if cancelled:
                    break
                continue
            self._emit_tally(ctx)
            forced_action = ctx["forced"]
            name = entry.name
            dest = _safe_local_join(local_dir, name)
            if dest is None:
                self._status_signal.emit(f"Skipped {name}: unsafe file name from server")
                continue
            if os.path.exists(dest):
                action = forced_action
                new_name = None
                if action is None:
                    suggested = self._suggest_name(
                        name, lambda n, d=local_dir: os.path.exists(os.path.join(d, n)))
                    req = self._resolve_conflict(name, suggested)
                    if req.action == "cancel":
                        cancelled = True
                        break
                    action = req.action
                    new_name = req.new_name
                    if req.apply_all:
                        ctx["forced"] = action
                if action == "skip":
                    continue
                if action == "rename":
                    if not new_name:
                        new_name = self._suggest_name(
                            name, lambda n, d=local_dir: os.path.exists(os.path.join(d, n)))
                    dest = _safe_local_join(local_dir, new_name)
                    if dest is None:
                        self._status_signal.emit(f"Skipped {name}: unsafe replacement name")
                        continue
            try:
                self._status_signal.emit(f"[{ctx['n']}/{ctx['total']}] Downloading {os.path.basename(dest)}…")
                self._ftp.download(entry.full_path, dest)
                done += 1
                if delete_source:
                    try:
                        self._ftp.delete_file(entry.full_path)
                    except Exception:
                        pass
            except Exception as e:
                self._status_signal.emit(f"Failed {entry.name}: {e}")
        if top:
            self._emit_tally(ctx, advance=False)
        return done, cancelled

    def eventFilter(self, watched, event):
        from PySide6.QtCore import QEvent
        if watched in (self._btn_local_up, self._btn_remote_up):
            if event.type() == QEvent.Type.DragEnter:
                if event.mimeData().hasFormat(_DRAG_MIME) and self._drag_payload:
                    is_remote_btn = watched is self._btn_remote_up
                    if is_remote_btn == self._drag_payload.from_remote:
                        event.acceptProposedAction()
                        watched.setStyleSheet("background: #3C78B4;")
                        return True
                event.ignore()
                return True
            if event.type() == QEvent.Type.DragLeave:
                watched.setStyleSheet("")
                return True
            if event.type() == QEvent.Type.Drop:
                watched.setStyleSheet("")
                if self._drag_payload and not self._busy:
                    is_remote_btn = watched is self._btn_remote_up
                    if is_remote_btn == self._drag_payload.from_remote:
                        event.acceptProposedAction()
                        self._handle_up_button_drop(is_remote_btn, self._drag_payload.items)
                        return True
                event.ignore()
                return True
        return super().eventFilter(watched, event)

    def _handle_up_button_drop(self, is_remote: bool, items: List[FileEntry]):
        if is_remote:
            parent = _ftp_parent(self._remote_path)
            if parent == self._remote_path:
                return
            self._set_busy(True, f"Moving {len(items)} item(s) to parent…")
            self._run_bg(self._bg_move_remote, items, parent)
        else:
            parent = str(pathlib.Path(self._local_path).parent)
            if parent == self._local_path:
                return
            self._move_local(items, parent)

    def closeEvent(self, event):
        if self._busy:
            # A transfer/rename is mid-flight; QUIT now would cut it off.
            self._set_status("A transfer is still running — wait for it to finish before closing.")
            event.ignore()
            return
        threading.Thread(target=self._ftp.disconnect, daemon=True).start()
        event.accept()
