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
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

from PySide6.QtCore import QMimeData, QPoint, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QDrag, QKeyEvent
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow,
    QMenu, QMessageBox, QProgressBar, QPushButton, QSizePolicy, QSplitter,
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

    @property
    def display_name(self) -> str:
        return f"\U0001F4C1 {self.name}" if self.is_directory else self.name

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
        if not self.modified:
            return ""
        return self.modified.strftime("%Y-%m-%d %H:%M")


@dataclass
class ClipboardPayload:
    is_cut: bool
    from_remote: bool
    items: List[FileEntry]


# Sort helpers
_SORT_NAME = 0
_SORT_SIZE = 1
_SORT_DATE = 2


def _sort_entries(entries: List[FileEntry], col: int, ascending: bool) -> List[FileEntry]:
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
QPushButton {
    background: #3A3A3A; color: white; border: 1px solid #666666;
    padding: 4px 10px;
}
QPushButton:hover { background: #4A4A4A; }
QPushButton:disabled { color: #555555; }
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
QMenu::item { padding: 6px 20px; }
QMenu::item:selected { background: #1A4E6E; }
QMenu::item:disabled { color: #606060; }
QMenu::separator { height: 1px; background: #444444; margin: 3px 4px; }
"""

# ── FTP worker (runs blocking ftplib calls off the main thread) ───────────────

class _FtpWorker:
    """Wraps a single ftplib.FTP connection; all methods are blocking."""

    def __init__(self, host: str, port: int, user: str, password: str):
        self._host = host
        self._port = port
        self._user = user
        self._pass = password
        self._ftp: Optional[ftplib.FTP] = None

    def connect(self):
        ftp = ftplib.FTP()
        ftp.connect(self._host, self._port, timeout=10)
        ftp.login(self._user, self._pass)
        ftp.set_pasv(True)
        self._ftp = ftp

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

    def ensure_connected(self):
        if self._ftp is None:
            self.connect()
            return
        try:
            self._ftp.voidcmd("NOOP")
        except Exception:
            self.connect()

    def list_dir(self, path: str) -> List[FileEntry]:
        self.ensure_connected()
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
                entries.append(FileEntry(name, full, is_dir, size, modified))
        except Exception:
            entries = self._list_dir_fallback(path)
        return entries

    def _list_dir_fallback(self, path: str) -> List[FileEntry]:
        """Parse LIST output when MLSD is not supported."""
        self.ensure_connected()
        lines: List[str] = []
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
            entries.append(FileEntry(name, full, is_dir, size, None))
        return entries

    def upload(self, local_path: str, remote_path: str):
        self.ensure_connected()
        with open(local_path, "rb") as f:
            self._ftp.storbinary(f"STOR {remote_path}", f)

    def download(self, remote_path: str, local_path: str):
        self.ensure_connected()
        with open(local_path, "wb") as f:
            self._ftp.retrbinary(f"RETR {remote_path}", f.write)

    def delete_file(self, path: str):
        self.ensure_connected()
        self._ftp.delete(path)

    def delete_dir(self, path: str):
        self.ensure_connected()
        for name, facts in self._ftp.mlsd(path):
            if name in (".", ".."):
                continue
            full = f"{path.rstrip('/')}/{name}"
            if facts.get("type", "").startswith("dir"):
                self.delete_dir(full)
            else:
                self._ftp.delete(full)
        self._ftp.rmd(path)

    def rename(self, old: str, new: str):
        self.ensure_connected()
        self._ftp.rename(old, new)

    def mkdir(self, path: str):
        self.ensure_connected()
        self._ftp.mkd(path)

    def file_exists(self, path: str) -> bool:
        self.ensure_connected()
        parent = _ftp_parent(path)
        name = path.rsplit("/", 1)[-1]
        try:
            for n, _ in self._ftp.mlsd(parent):
                if n == name:
                    return True
        except Exception:
            pass
        return False


def _ftp_parent(path: str) -> str:
    clean = path.rstrip("/")
    idx = clean.rfind("/")
    return "/" if idx <= 0 else clean[:idx]


# ── Drag-drop constants and tree subclass ─────────────────────────────────────

_DRAG_MIME = "application/x-kronos-fileentries"
_DRAG_THRESHOLD = 8


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

        self._drag_start_pos: Optional[QPoint] = None
        self._drag_started = False

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            item = self.itemAt(event.position().toPoint())
            if item and item.isSelected() and len(self.selectedItems()) >= 1:
                self._drag_start_pos = event.position().toPoint()
                self._drag_started = False
                return
        self._drag_start_pos = None
        self._drag_started = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (self._drag_start_pos is not None
                and not self._drag_started
                and event.buttons() & Qt.MouseButton.LeftButton):
            delta = event.position().toPoint() - self._drag_start_pos
            if (abs(delta.x()) > _DRAG_THRESHOLD
                    or abs(delta.y()) > _DRAG_THRESHOLD):
                self._drag_started = True
                self._initiate_drag()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag_start_pos is not None and not self._drag_started:
            item = self.itemAt(self._drag_start_pos)
            if item:
                mods = event.modifiers()
                if not (mods & (Qt.ControlModifier | Qt.ShiftModifier)):
                    self.clearSelection()
                    item.setSelected(True)
                    self.setCurrentItem(item)
        self._drag_start_pos = None
        self._drag_started = False
        super().mouseReleaseEvent(event)

    def _initiate_drag(self):
        w = self._window
        entries = self._entries()
        items = w._selected_entries(self, entries)
        if not items or w._busy:
            return

        w._drag_payload = DragPayload(from_remote=self._is_remote, items=items)

        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(_DRAG_MIME, b"1")
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction | Qt.DropAction.MoveAction)
        w._drag_payload = None

    def startDrag(self, supportedActions):
        pass

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


# ── File manager window ──────────────────────────────────────────────────────

class FileManagerWindow(QMainWindow):
    _status_signal = Signal(str)
    _refresh_local_signal = Signal()
    _refresh_remote_signal = Signal(list)
    _busy_signal = Signal(bool, str)
    _progress_signal = Signal(int)

    def __init__(self, host: str, ftp_port: int, user: str, password: str, parent=None):
        super().__init__(parent)
        self._host = host
        self._ftp_port = ftp_port
        self._user = user
        self._pass = password

        self._ftp = _FtpWorker(host, ftp_port, user, password)
        self._remote_path = "/"
        self._local_path = str(pathlib.Path.home() / "Desktop")
        if not os.path.isdir(self._local_path):
            self._local_path = str(pathlib.Path.home())

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

        QTimer.singleShot(0, self._initial_connect)

    # ── UI construction ───────────────────────────────────────────────────────

    def _setup_ui(self):
        self.setWindowTitle("File Manager — Kronos")
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
        self._drive_combo.setFixedWidth(135)
        self._drive_combo.setFixedHeight(24)
        self._drive_combo.setToolTip("Select drive")
        ln_lay.addWidget(self._drive_combo)
        self._btn_local_up = QPushButton("↑")
        self._btn_local_up.setFixedSize(28, 24)
        self._btn_local_up.setToolTip("Parent directory — drag files here to move up")
        self._btn_local_up.setAcceptDrops(True)
        ln_lay.addWidget(self._btn_local_up)
        lbl_local = QLabel("Local: ")
        lbl_local.setStyleSheet("color: #88DD88; font-weight: bold;")
        ln_lay.addWidget(lbl_local)
        self._local_path_box = QLineEdit()
        self._local_path_box.setReadOnly(True)
        ln_lay.addWidget(self._local_path_box, 1)
        nav_row.addWidget(local_nav, 1)

        # Remote nav
        remote_nav = QWidget()
        remote_nav.setStyleSheet("background: #252525;")
        rn_lay = QHBoxLayout(remote_nav)
        rn_lay.setContentsMargins(6, 4, 6, 4)
        rn_lay.setSpacing(4)
        self._btn_remote_up = QPushButton("↑")
        self._btn_remote_up.setFixedSize(28, 24)
        self._btn_remote_up.setToolTip("Parent directory — drag files here to move up")
        self._btn_remote_up.setAcceptDrops(True)
        rn_lay.addWidget(self._btn_remote_up)
        lbl_remote = QLabel("Kronos: ")
        lbl_remote.setStyleSheet("color: #88AADD; font-weight: bold;")
        rn_lay.addWidget(lbl_remote)
        self._remote_path_box = QLineEdit()
        self._remote_path_box.setReadOnly(True)
        rn_lay.addWidget(self._remote_path_box, 1)
        nav_row.addWidget(remote_nav, 1)

        outer.addLayout(nav_row)

        # Splitter with two tree widgets
        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(4)

        self._local_tree = self._make_tree(False, "Name (Local)", "Size", "Modified")
        self._remote_tree = self._make_tree(True, "Name (Kronos)", "Size", "Modified")
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
        self._btn_upload = QPushButton("→ Send to Kronos")
        self._btn_upload.setToolTip("Upload selected local files to Kronos")
        self._btn_local_new = QPushButton("New Folder")
        self._btn_local_del = QPushButton("Delete")
        self._btn_local_ren = QPushButton("Rename")
        self._btn_local_ref = QPushButton("↺")
        self._btn_local_ref.setFixedWidth(32)
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
        self._btn_download = QPushButton("← Send to PC")
        self._btn_download.setToolTip("Download selected Kronos files to local")
        self._btn_remote_new = QPushButton("New Folder")
        self._btn_remote_del = QPushButton("Delete")
        self._btn_remote_ren = QPushButton("Rename")
        self._btn_remote_ref = QPushButton("↺")
        self._btn_remote_ref.setFixedWidth(32)
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
        self._progress_bar.setFixedWidth(180)
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
        self._set_status("Connecting to Kronos FTP…")
        self._run_bg(self._bg_connect)

    def _bg_connect(self):
        try:
            self._ftp.connect()
            self._status_signal.emit("Connected.")
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
                    if d.is_dir():
                        entries.append(FileEntry(d.name, str(d), True, 0,
                                                 datetime.fromtimestamp(d.stat().st_mtime)))
                    else:
                        st = d.stat()
                        entries.append(FileEntry(d.name, str(d), False, st.st_size,
                                                 datetime.fromtimestamp(st.st_mtime)))
                except PermissionError:
                    continue
            self._local_entries = _sort_entries(entries, self._local_sort_col, self._local_sort_asc)
            self._populate_tree(self._local_tree, self._local_entries)
            self._set_status(f"{len(entries)} item(s) in {self._local_path}")
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
        self._remote_entries = _sort_entries(entries, self._remote_sort_col, self._remote_sort_asc)
        self._remote_path_box.setText(self._remote_path)
        self._populate_tree(self._remote_tree, self._remote_entries)
        self._set_status(f"{len(entries)} item(s) in {self._remote_path}")

    def _populate_tree(self, tree: QTreeWidget, entries: List[FileEntry]):
        tree.clear()
        for entry in entries:
            item = QTreeWidgetItem([entry.display_name, entry.size_text, entry.date_text])
            if entry.is_directory:
                item.setForeground(0, Qt.GlobalColor.cyan)
            tree.addTopLevelItem(item)

    # ── Upload ────────────────────────────────────────────────────────────────

    def _on_upload(self):
        if self._busy:
            return
        items = self._selected_files(self._local_tree, self._local_entries)
        if not items:
            self._set_status("Select one or more local files to upload.")
            return
        self._set_busy(True, f"Uploading {len(items)} file(s)…")
        self._run_bg(self._bg_upload, items, False)

    def _bg_upload(self, items: List[FileEntry], delete_source: bool):
        done = 0
        total = len(items)
        for i, entry in enumerate(items):
            dest = f"{self._remote_path.rstrip('/')}/{entry.name}"
            try:
                self._status_signal.emit(f"[{i+1}/{total}] Uploading {entry.name}…")
                self._progress_signal.emit(int((i / total) * 100))
                self._ftp.upload(entry.full_path, dest)
                done += 1
                if delete_source:
                    try:
                        os.remove(entry.full_path)
                    except Exception:
                        pass
            except Exception as e:
                self._status_signal.emit(f"Failed {entry.name}: {e}")
        self._progress_signal.emit(100)
        self._status_signal.emit(f"Uploaded {done}/{total} file(s) → {self._remote_path}")
        self._busy_signal.emit(False, "")
        if delete_source:
            self._refresh_local_signal.emit()
        self._bg_refresh_remote()

    # ── Download ──────────────────────────────────────────────────────────────

    def _on_download(self):
        if self._busy:
            return
        items = self._selected_files(self._remote_tree, self._remote_entries)
        if not items:
            self._set_status("Select one or more Kronos files to download.")
            return
        self._set_busy(True, f"Downloading {len(items)} file(s)…")
        self._run_bg(self._bg_download, items, False)

    def _bg_download(self, items: List[FileEntry], delete_source: bool):
        done = 0
        total = len(items)
        for i, entry in enumerate(items):
            dest = os.path.join(self._local_path, entry.name)
            try:
                self._status_signal.emit(f"[{i+1}/{total}] Downloading {entry.name}…")
                self._progress_signal.emit(int((i / total) * 100))
                self._ftp.download(entry.full_path, dest)
                done += 1
                if delete_source:
                    try:
                        self._ftp.delete_file(entry.full_path)
                    except Exception:
                        pass
            except Exception as e:
                self._status_signal.emit(f"Failed {entry.name}: {e}")
        self._progress_signal.emit(100)
        self._status_signal.emit(f"Downloaded {done}/{total} file(s) → {self._local_path}")
        self._busy_signal.emit(False, "")
        self._refresh_local_signal.emit()
        if delete_source:
            self._bg_refresh_remote()

    # ── New Folder ────────────────────────────────────────────────────────────

    def _on_local_new_folder(self):
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
        items = self._selected_entries(self._local_tree, self._local_entries)
        if not items:
            self._set_status("Select items to delete.")
            return
        r = QMessageBox.question(self, "Delete",
                                 f"Delete {len(items)} item(s)?",
                                 QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r != QMessageBox.Yes:
            return
        done = 0
        for entry in items:
            try:
                if entry.is_directory:
                    shutil.rmtree(entry.full_path)
                else:
                    os.remove(entry.full_path)
                done += 1
            except Exception as e:
                self._set_status(f"Failed {entry.name}: {e}")
        self._refresh_local()
        self._set_status(f"Deleted {done}/{len(items)} item(s).")

    def _on_remote_delete(self):
        if self._busy:
            return
        items = self._selected_entries(self._remote_tree, self._remote_entries)
        if not items:
            self._set_status("Select items to delete.")
            return
        r = QMessageBox.question(self, "Delete",
                                 f"Delete {len(items)} item(s) from Kronos?",
                                 QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r != QMessageBox.Yes:
            return
        self._set_busy(True, f"Deleting {len(items)} item(s)…")
        self._run_bg(self._bg_remote_delete, items)

    def _bg_remote_delete(self, items: List[FileEntry]):
        done = 0
        for entry in items:
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
        items = self._selected_entries(self._local_tree, self._local_entries)
        if len(items) != 1:
            self._set_status("Select exactly one item to rename.")
            return
        entry = items[0]
        new_name, ok = QInputDialog.getText(self, "Rename", "New name:", text=entry.name)
        if not ok or not new_name.strip() or new_name.strip() == entry.name:
            return
        new_path = os.path.join(os.path.dirname(entry.full_path), new_name.strip())
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
        entry = items[0]
        new_name, ok = QInputDialog.getText(self, "Rename", "New name:", text=entry.name)
        if not ok or not new_name.strip() or new_name.strip() == entry.name:
            return
        new_path = f"{_ftp_parent(entry.full_path).rstrip('/')}/{new_name.strip()}"
        self._set_busy(True, "Renaming…")
        self._run_bg(self._bg_remote_rename, entry.full_path, new_path, new_name.strip())

    def _bg_remote_rename(self, old_path: str, new_path: str, display_name: str):
        try:
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
        has_files = any(not e.is_directory for e in entries)
        has_selection = len(entries) > 0
        is_single = len(entries) == 1

        menu = QMenu(self)

        if entry and entry.is_directory:
            a = menu.addAction("Open")
            a.triggered.connect(lambda: self._ctx_open(entry, is_remote))
        else:
            label = "← Send to PC" if is_remote else "→ Send to Kronos"
            a = menu.addAction(label)
            a.setEnabled(has_files)
            a.triggered.connect(self._on_download if is_remote else self._on_upload)

        menu.addSeparator()

        a = menu.addAction("Cut")
        a.setEnabled(has_selection)
        a.triggered.connect(lambda: self._do_cut(tree, is_remote))

        a = menu.addAction("Copy")
        a.setEnabled(has_selection)
        a.triggered.connect(lambda: self._do_copy(tree, is_remote))

        a = menu.addAction("Paste")
        a.setEnabled(self._clipboard is not None and not self._busy)
        a.triggered.connect(lambda: self._do_paste(is_remote))

        menu.addSeparator()

        a = menu.addAction("Rename")
        a.setEnabled(is_single and entry is not None)
        a.triggered.connect(self._on_remote_rename if is_remote else self._on_local_rename)

        a = menu.addAction("Delete")
        a.setEnabled(has_selection)
        a.triggered.connect(self._on_remote_delete if is_remote else self._on_local_delete)

        menu.addSeparator()

        a = menu.addAction("New Folder")
        a.setEnabled(not self._busy)
        a.triggered.connect(self._on_remote_new_folder if is_remote else self._on_local_new_folder)

        a = menu.addAction("Refresh")
        a.setEnabled(not self._busy)
        a.triggered.connect(self._refresh_remote if is_remote else self._refresh_local)

        menu.exec(tree.viewport().mapToGlobal(pos))

    def _ctx_open(self, entry: FileEntry, is_remote: bool):
        if is_remote:
            self._remote_path = entry.full_path
            self._refresh_remote()
        else:
            self._local_path = entry.full_path
            self._refresh_local()

    # ── Clipboard (cut/copy/paste) ────────────────────────────────────────────

    def _do_cut(self, tree: QTreeWidget, is_remote: bool):
        items = self._selected_entries(tree, self._remote_entries if is_remote else self._local_entries)
        if not items:
            return
        self._clipboard = ClipboardPayload(is_cut=True, from_remote=is_remote, items=items)
        self._set_status(f"Cut {len(items)} item(s) — paste to move.")

    def _do_copy(self, tree: QTreeWidget, is_remote: bool):
        items = self._selected_entries(tree, self._remote_entries if is_remote else self._local_entries)
        if not items:
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
            file_items = [e for e in items if not e.is_directory]
            if file_items:
                self._set_busy(True, f"Uploading {len(file_items)} file(s)…")
                self._run_bg(self._bg_upload, file_items, cb.is_cut)
            if cb.is_cut:
                self._clipboard = None
        else:
            # Remote → Local (download; delete source inside bg task if cut)
            file_items = [e for e in items if not e.is_directory]
            if file_items:
                self._set_busy(True, f"Downloading {len(file_items)} file(s)…")
                self._run_bg(self._bg_download, file_items, cb.is_cut)
            if cb.is_cut:
                self._clipboard = None

    def _move_local(self, items: List[FileEntry], dest_folder: str):
        done = 0
        for entry in items:
            dest = os.path.join(dest_folder, entry.name)
            try:
                shutil.move(entry.full_path, dest)
                done += 1
            except Exception as e:
                self._set_status(f"Failed {entry.name}: {e}")
        self._refresh_local()
        self._set_status(f"Moved {done}/{len(items)} item(s) → {dest_folder}")

    def _copy_local(self, items: List[FileEntry], dest_folder: str):
        done = 0
        for entry in items:
            dest = os.path.join(dest_folder, entry.name)
            try:
                if entry.is_directory:
                    shutil.copytree(entry.full_path, dest, dirs_exist_ok=True)
                else:
                    shutil.copy2(entry.full_path, dest)
                done += 1
            except Exception as e:
                self._set_status(f"Failed {entry.name}: {e}")
        self._refresh_local()
        self._set_status(f"Copied {done}/{len(items)} item(s) → {dest_folder}")

    def _bg_move_remote(self, items: List[FileEntry], dest_folder: str):
        done = 0
        for entry in items:
            dest = f"{dest_folder.rstrip('/')}/{entry.name}"
            try:
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
        file_items = [e for e in items if not e.is_directory]
        folder_items = [e for e in items if e.is_directory]
        if not file_items and folder_items:
            self._set_status("Cross-pane folder transfer not yet supported.")
            return

        if to_remote:
            dest = target_folder.full_path if target_folder else self._remote_path
            self._set_busy(True, f"Uploading {len(file_items)} file(s)…")
            self._run_bg(self._bg_upload_to, file_items, dest)
        else:
            dest = target_folder.full_path if target_folder else self._local_path
            self._set_busy(True, f"Downloading {len(file_items)} file(s)…")
            self._run_bg(self._bg_download_to, file_items, dest)

    def _bg_upload_to(self, items: List[FileEntry], dest_folder: str):
        done = 0
        total = len(items)
        for i, entry in enumerate(items):
            dest = f"{dest_folder.rstrip('/')}/{entry.name}"
            try:
                self._status_signal.emit(f"[{i+1}/{total}] Uploading {entry.name}…")
                self._progress_signal.emit(int((i / total) * 100))
                self._ftp.upload(entry.full_path, dest)
                done += 1
            except Exception as e:
                self._status_signal.emit(f"Failed {entry.name}: {e}")
        self._progress_signal.emit(100)
        self._status_signal.emit(f"Uploaded {done}/{total} file(s) → {dest_folder}")
        self._busy_signal.emit(False, "")
        self._bg_refresh_remote()

    def _bg_download_to(self, items: List[FileEntry], dest_folder: str):
        done = 0
        total = len(items)
        for i, entry in enumerate(items):
            dest = os.path.join(dest_folder, entry.name)
            try:
                self._status_signal.emit(f"[{i+1}/{total}] Downloading {entry.name}…")
                self._progress_signal.emit(int((i / total) * 100))
                self._ftp.download(entry.full_path, dest)
                done += 1
            except Exception as e:
                self._status_signal.emit(f"Failed {entry.name}: {e}")
        self._progress_signal.emit(100)
        self._status_signal.emit(f"Downloaded {done}/{total} file(s) → {dest_folder}")
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
                    tree, self._remote_entries if is_remote else self._local_entries)
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
             ["Name (Kronos)", "Size", "Modified"]),
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
        if os.name == "nt":
            import ctypes
            bitmask = ctypes.windll.kernel32.GetLogicalDrives()
            for i in range(26):
                if bitmask & (1 << i):
                    letter = chr(ord("A") + i)
                    root = f"{letter}:\\"
                    self._drive_combo.addItem(f"\U0001F4BD {letter}:", root)
        else:
            self._drive_combo.addItem("/ (root)", "/")
            home = str(pathlib.Path.home())
            self._drive_combo.addItem(f"~ ({home})", home)
        self._sync_drive_combo()
        self._suppress_drive_change = False

    def _sync_drive_combo(self):
        if os.name == "nt":
            root = os.path.splitdrive(self._local_path)[0] + "\\"
        else:
            root = "/"
        for i in range(self._drive_combo.count()):
            if self._drive_combo.itemData(i) == root:
                self._suppress_drive_change = True
                self._drive_combo.setCurrentIndex(i)
                self._suppress_drive_change = False
                return

    _suppress_drive_change = False

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
                          entries: List[FileEntry]) -> List[FileEntry]:
        result = []
        for item in tree.selectedItems():
            e = self._entry_from_item(item, entries)
            if e:
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
            self._progress_bar.setValue(0)
            self._set_status(msg)

    @Slot(bool, str)
    def _on_busy(self, busy: bool, msg: str):
        self._set_busy(busy, msg)

    @Slot(int)
    def _on_progress(self, value: int):
        self._progress_bar.setValue(value)

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
        threading.Thread(target=self._ftp.disconnect, daemon=True).start()
        event.accept()
