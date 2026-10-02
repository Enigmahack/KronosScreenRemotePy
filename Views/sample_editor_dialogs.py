"""Sample Editor dialogs — ports of InsertSilenceDialog, CreateMultisampleDialog, SampleNormalizationReportWindow
and the PromptDialog / MessageBox usages in SampleEditorWindow.xaml.cs."""
from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QHeaderView, QAbstractItemView)

import Utils.theme as T
from Views.dialog_base import BaseDialog, InputDialog, _message_box_stylesheet

# Hardware-verified: the Kronos truncates a longer Name safely, but a UI-side cap means what the user types is what
# ends up on the unit. The Suffix ("-L"/"-R") isn't counted against it. UI-only — never enforce this in Data/.
KRONOS_NAME_MAX_LENGTH = 22


def prompt_text(parent, title: str, label: str, default: str = "", max_length: int = 0) -> Optional[str]:
    """Single-line prompt. None when cancelled."""
    dlg = InputDialog(title, label, parent, default)
    if max_length > 0:
        dlg.input.setMaxLength(max_length)
    dlg.input.selectAll()
    if dlg.exec() != InputDialog.DialogCode.Accepted:
        return None
    return dlg.value()


def confirm(parent, title: str, text: str, default_no: bool = True) -> bool:
    box = QMessageBox(QMessageBox.Icon.Warning, title, text,
                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, parent)
    box.setDefaultButton(QMessageBox.StandardButton.No if default_no else QMessageBox.StandardButton.Yes)
    box.setStyleSheet(_message_box_stylesheet())
    return box.exec() == QMessageBox.StandardButton.Yes


class CreateMultisampleDialog(BaseDialog):
    """Asks only what the format forces a choice on: mono vs stereo (a stereo instrument is a matched pair of two
    full multisamples). Slot and name are not asked — the slot is the next free one, shown in the title, and the
    name is the auto-generated NEWMS<slot>."""

    def __init__(self, slot: int, parent=None):
        super().__init__(f"Create New Multisample {slot:03d}", parent)
        self.stereo = False
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"Create New Multisample {slot:03d}"))
        self._stereo_box = QCheckBox("Stereo")
        self._stereo_box.setToolTip("Creates a matched -L/-R pair (two adjacent slots)")
        lay.addWidget(self._stereo_box)
        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("OK")
        ok.setDefault(True)
        ok.clicked.connect(self._on_ok)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)
        self.setFixedWidth(320)

    def _on_ok(self) -> None:
        self.stereo = self._stereo_box.isChecked()
        self.accept()


class InsertSilenceDialog(BaseDialog):
    """Frames and Seconds are two entry points onto the SAME value (the effect only understands frames, like every
    other position field here); Seconds is a linked convenience. For a stereo pair, an explicit Left/Right choice."""

    def __init__(self, sample_rate: int, initial_frames: int, has_stereo_pair: bool = False, parent=None):
        super().__init__("Insert Silence", parent)
        self._rate = max(1, sample_rate)
        self._syncing = False
        self.frames = 0
        self.apply_to_left = True
        self.apply_to_right = True

        lay = QVBoxLayout(self)
        self._prompt = QLabel("Frames of silence to insert:")
        self._prompt.setWordWrap(True)
        lay.addWidget(self._prompt)
        lay.addWidget(QLabel("Frames"))
        self._frames = QLineEdit(str(initial_frames))
        lay.addWidget(self._frames)
        lay.addWidget(QLabel("Seconds"))
        self._seconds = QLineEdit(self._fmt(initial_frames / self._rate))
        lay.addWidget(self._seconds)

        self._picker = QHBoxLayout()
        self._left = QCheckBox("Left")
        self._left.setChecked(True)
        self._right = QCheckBox("Right")
        self._right.setChecked(True)
        self._has_pair = has_stereo_pair
        if has_stereo_pair:
            self._picker.addWidget(QLabel("Apply to:"))
            self._picker.addWidget(self._left)
            self._picker.addWidget(self._right)
            self._picker.addStretch()
            lay.addLayout(self._picker)

        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("OK")
        ok.setDefault(True)
        ok.clicked.connect(self._on_ok)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)
        self.setFixedWidth(320)
        self._frames.textChanged.connect(self._on_frames)
        self._seconds.textChanged.connect(self._on_seconds)
        self._frames.selectAll()
        self._frames.setFocus()

    @staticmethod
    def _fmt(seconds: float) -> str:
        return f"{seconds:.3f}".rstrip("0").rstrip(".") or "0"

    def _on_frames(self, text: str) -> None:
        if self._syncing:
            return
        try:
            frames = int(text)
        except ValueError:
            return
        self._syncing = True
        self._seconds.setText(self._fmt(frames / self._rate))
        self._syncing = False

    def _on_seconds(self, text: str) -> None:
        if self._syncing:
            return
        try:
            seconds = float(text)
        except ValueError:
            return
        if seconds < 0:
            return
        self._syncing = True
        # Round, not truncate: 0.5 s at 44100 Hz should land on the frame count that round-trips back to "0.5".
        self._frames.setText(str(int(round(seconds * self._rate))))
        self._syncing = False

    def _error(self, msg: str) -> None:
        self._prompt.setText(msg)
        self._prompt.setStyleSheet(f"color: {T.ERROR_TEXT};")

    def _on_ok(self) -> None:
        try:
            frames = int(self._frames.text())
        except ValueError:
            frames = 0
        if frames <= 0:
            self._error("Enter a positive whole number of frames.")
            return
        if self._has_pair and not self._left.isChecked() and not self._right.isChecked():
            self._error("Select at least one channel (Left, Right, or both).")
            return
        self.frames = frames
        self.apply_to_left = self._left.isChecked()
        self.apply_to_right = self._right.isChecked()
        self.accept()


class SampleReportWindow(BaseDialog):
    """Read-only sample-rate / bit-depth report across a collection; rows that differ from the majority (or have no
    audio) are flagged."""

    def __init__(self, entries: list, parent=None):
        super().__init__("Sample Report", parent, modal=False)
        self.resize(720, 480)
        lay = QVBoxLayout(self)
        flagged = sum(1 for e in entries if e.flagged)
        summary = QLabel("No samples found - open a collection first." if not entries else
                         f"{len(entries)} sample(s), {flagged} flagged (sample rate/bit depth differs from the "
                         "collection's own majority, or has no audio data).")
        summary.setWordWrap(True)
        summary.setStyleSheet(f"color: {T.TEXT_DIM};")
        lay.addWidget(summary)

        table = QTableWidget(len(entries), 6)
        table.setHorizontalHeaderLabels(["Location", "Sample Name", "Rate", "Bits", "Ch", "Flag"])
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        for r, e in enumerate(entries):
            flag = ("no audio data (header-only)" if e.is_header_only
                    else "differs from collection majority" if e.flagged else "")
            for c, v in enumerate((e.location, e.sample_name, e.sample_rate, e.bits, e.channels, flag)):
                item = QTableWidgetItem(str(v))
                if e.flagged:
                    item.setForeground(QColor(T.ERROR_TEXT))
                table.setItem(r, c, item)
        table.resizeColumnsToContents()
        table.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(table, 1)
