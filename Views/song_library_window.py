"""The Song Library window: songs kept on this computer (Data.song_library), with import from .SNG files or an instrument,
export (with Kronos <-> Nautilus conversion) to .SNG, and sending Timbre Sets to a connected Nautilus."""
from __future__ import annotations

import logging
import threading
from typing import Callable, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QFileDialog, QHBoxLayout, QInputDialog, QLabel,
                               QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout)

import Utils.theme as T
from Conversion.push import ConvertTables
from Conversion.song_convert import MODEL_NAMES
from Conversion.song_source import convert_items, read_songs
from Data.sng_file import MODEL_KRONOS, MODEL_NAUTILUS
from Data.song_library import SongEntry, SongLibrary
from Data.song_model import SongError, from_whole_body

log = logging.getLogger(__name__)

SONG_OBJECT = 0x18
SONG_SLOTS = 200
GIVE_UP_AFTER = 3          # consecutive empty slots before the instrument is assumed to have no more songs
_COLUMNS = ("Name", "Instrument", "Source", "Tracks", "Notes", "Bars", "MIDI data")


def _is_placeholder(name: str) -> bool:
    return name.startswith("NEW SONG ") and name[9:].isdigit()


class SongLibraryWindow(QDialog):
    _pulled = Signal(list, str)          # [SongData], status
    _progress = Signal(str)

    def __init__(self, library: SongLibrary, get_service: Callable[[], object],
                 send_to_nautilus: Optional[Callable[[list], None]] = None, parent=None) -> None:
        super().__init__(parent)
        self._lib = library
        self._get_service = get_service
        self._send = send_to_nautilus
        self._busy = False
        self._cancel = threading.Event()
        self.setWindowTitle("Song Library")
        self.resize(900, 460)
        self.setStyleSheet(f"QDialog {{ background-color: {T.BG}; color: {T.TEXT}; }}")
        root = QVBoxLayout(self)
        self._table = QTableWidget(0, len(_COLUMNS))
        self._table.setHorizontalHeaderLabels(list(_COLUMNS))
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setColumnWidth(0, 260)
        self._table.setSortingEnabled(True)
        root.addWidget(self._table, 1)
        self._status = QLabel()
        root.addWidget(self._status)
        row = QHBoxLayout()
        self._btn_import_file = self._button(row, "Import .SNG...", "Add the songs in a Kronos or Nautilus .SNG file, "
                                             "with all their MIDI data", self.import_file)
        self._btn_import_inst = self._button(row, "Import from instrument", "Read every song's Timbre Set and Song "
                                             "Control over SysEx (the MIDI data cannot be read this way)", self.import_instrument)
        self._chk_placeholders = QCheckBox("Skip empty \"NEW SONG\" slots")
        self._chk_placeholders.setChecked(True)
        row.addWidget(self._chk_placeholders)
        row.addStretch(1)
        self._btn_export = self._button(row, "Export .SNG...", "Write the selected songs to a .SNG file for the "
                                        "Kronos or the Nautilus", self.export_sng)
        self._btn_send = self._button(row, "Send Timbre Sets to Nautilus...", "Write the selected songs' Timbre Sets to "
                                      "song slots on the connected Nautilus", self.send_timbre_sets)
        self._btn_remove = self._button(row, "Remove", "Remove the selected songs from the library", self.remove_selected)
        root.addLayout(row)
        self._pulled.connect(self._on_pulled)
        self._progress.connect(self._set_status)
        self._table.itemSelectionChanged.connect(self._refresh_enable)
        self.refresh()

    def _button(self, layout, text: str, tip: str, slot) -> QPushButton:
        b = QPushButton(text)
        b.setToolTip(tip)
        b.clicked.connect(slot)
        layout.addWidget(b)
        return b

    # ── table ──
    def refresh(self) -> None:
        self._table.setSortingEnabled(False)
        entries = self._lib.entries()
        self._table.setRowCount(len(entries))
        for r, e in enumerate(entries):
            cells = (e.name or "(unnamed)", MODEL_NAMES.get(e.model, "?"), e.source, str(e.tracks) if e.has_events else "",
                     str(e.notes) if e.has_events else "", str(e.bars) if e.has_events else "",
                     ("Yes" + (", plays audio" if e.has_audio else "")) if e.has_events else "No (SysEx only)")
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if c == 0:
                    item.setData(Qt.ItemDataRole.UserRole, e.id)
                self._table.setItem(r, c, item)
        self._table.setSortingEnabled(True)
        self._set_status(f"{len(entries)} song(s) in the library.")
        self._refresh_enable()

    def _selected(self) -> List[SongEntry]:
        ids = {self._table.item(i.row(), 0).data(Qt.ItemDataRole.UserRole) for i in self._table.selectedIndexes()}
        return [e for e in self._lib.entries() if e.id in ids]

    def _refresh_enable(self) -> None:
        sel = bool(self._selected())
        idle = not self._busy
        self._btn_import_file.setEnabled(idle)
        self._btn_import_inst.setEnabled(idle)
        self._btn_export.setEnabled(sel and idle)
        self._btn_send.setEnabled(sel and idle and self._send is not None)
        self._btn_remove.setEnabled(sel and idle)

    def _set_status(self, text: str) -> None:
        self._status.setText(text)

    # ── import ──
    def import_file(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Import songs", "", "Songs (*.SNG *.sng *.syx);;All files (*)")
        added_total, notes = 0, []
        for path in paths:
            songs, n = read_songs(path)
            notes += [f"{path}: {m}" for m in n]
            added, n2 = self._lib.add(songs, path.replace("\\", "/").rsplit("/", 1)[-1])
            added_total += len(added)
            notes += n2
        self.refresh()
        self._set_status(f"Imported {added_total} song(s)." + (f" {len(notes)} note(s): see Details." if notes else ""))
        if notes:
            self._show("Import", f"Imported {added_total} song(s).", notes)

    def import_instrument(self) -> None:
        service = self._get_service()
        if service is None or not service.can_dump:
            self._set_status("Connect to an instrument first.")
            return
        self._busy = True
        self._cancel.clear()
        self._refresh_enable()
        skip = self._chk_placeholders.isChecked()

        def worker() -> None:
            songs, empty = [], 0
            status = "Done."
            try:
                for i in range(SONG_SLOTS):
                    if self._cancel.is_set():
                        status = "Cancelled."
                        break
                    self._progress.emit(f"Reading song {i} of {SONG_SLOTS}...")
                    d = service.dump_object_parsed(SONG_OBJECT, 0, i, no_response_ms=4000)
                    if d is None:
                        empty += 1
                        if empty >= GIVE_UP_AFTER:
                            break
                        continue
                    empty = 0
                    try:
                        song = from_whole_body(d.body)
                    except SongError as ex:
                        log.info("song %d skipped: %s", i, ex)
                        continue
                    if not (skip and _is_placeholder(song.name)):
                        songs.append(song)
            except Exception as ex:
                log.exception("Reading songs failed")
                status = f"Stopped: {type(ex).__name__}: {ex}"
            self._pulled.emit(songs, status)

        threading.Thread(target=worker, daemon=True, name="SongLibraryPull").start()

    def _on_pulled(self, songs: list, status: str) -> None:
        self._busy = False
        added, notes = self._lib.add(songs, "instrument")
        self.refresh()
        self._set_status(f"{status} Read {len(songs)} song(s), added {len(added)}.")
        if notes:
            self._show("Import from instrument", f"Added {len(added)} of {len(songs)} song(s).", notes)

    def closeEvent(self, event) -> None:
        self._cancel.set()
        super().closeEvent(event)

    # ── export / send / remove ──
    def export_sng(self) -> None:
        sel = self._selected()
        if not sel:
            return
        first = sel[0].model
        labels = [f"{MODEL_NAMES[m]}" for m in (MODEL_KRONOS, MODEL_NAUTILUS)]
        default = 0 if first == MODEL_NAUTILUS else 1          # offer the other instrument first
        choice, ok = QInputDialog.getItem(self, "Export .SNG", "Write the .SNG for:", labels, default, False)
        if not ok:
            return
        target = MODEL_KRONOS if choice == MODEL_NAMES[MODEL_KRONOS] else MODEL_NAUTILUS
        data, warnings, refusals = self._lib.export_sng([e.id for e in sel], target, ConvertTables.load())
        if data is None:
            self._show("Export .SNG", "Nothing was written.", refusals + warnings)
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export .SNG", "songs.SNG", "Song files (*.SNG)")
        if not path:
            return
        try:
            with open(path, "wb") as f:
                f.write(data)
        except OSError as ex:
            self._show("Export .SNG", f"Could not write the file: {ex}", [])
            return
        self._set_status(f"Wrote {len(sel)} song(s) for the {choice} to {path}")
        self._show("Export .SNG", f"Wrote {len(sel)} song(s) for the {choice}.", warnings)

    def send_timbre_sets(self) -> None:
        if self._send is None:
            return
        songs = [self._lib.get(e.id) for e in self._selected()]
        self._send(convert_items(songs))

    def remove_selected(self) -> None:
        sel = self._selected()
        if not sel:
            return
        names = ", ".join(e.name or "(unnamed)" for e in sel[:5]) + (" ..." if len(sel) > 5 else "")
        if QMessageBox.question(self, "Remove songs", f"Remove {len(sel)} song(s) from the library?\n{names}") \
                != QMessageBox.StandardButton.Yes:
            return
        self._lib.remove([e.id for e in sel])
        self.refresh()

    def _show(self, title: str, text: str, details: List[str]) -> None:
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setText(text)
        if details:
            box.setDetailedText("\n".join(details))
        box.exec()
