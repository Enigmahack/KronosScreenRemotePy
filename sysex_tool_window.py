"""
SysEx Tool window — port of Views/SysExToolWindow.xaml + .xaml.cs.

Layout (top to bottom, matching the XAML's DockPanel exactly):
  Filter bar, Column header (Time/Dir/Message), unified traffic list (fills),
  88-key virtual piano, bottom toolbar (Clear / Auto-scroll / SysEx+MIDI counts
  / active stream label).

Traffic is batched onto the UI thread every ~50 ms (mirrors the C# _flushTimer)
so a burst (a ~79 KB Set List dump streaming in) can't stall the widget.
"""
from __future__ import annotations

import collections
import datetime
from typing import Deque, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel,
    QPushButton, QSizePolicy, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

import kronos_sysex as ksx
from midi_bridge import MidiBridgeClient
from sysex_service import SysExService

import theme as T

_MAX_ENTRIES = 1000

_WIN_BG = T.BG
_WIN_FG = T.TEXT

# Message-type -> text color, verbatim from SysExMessageItem.TypeToBrush.
_TYPE_COLORS = {
    "Note": "#88BBFF", "CC": "#FFCC66", "ProgramChange": "#CC88FF",
    "SysEx": "#77DD99", "PitchBend": "#FF9966", "AfterTouch": "#FF77AA",
    "Transport": "#AADDFF", "Other": "#CCCCCC",
}

# Filter button (label, MsgType key) — matches the 7 XAML buttons; "Other" has
# no button and is never hidden by a solo filter (see FilterMessage in the C#).
_FILTER_BUTTONS = [
    ("Notes", "Note"), ("CC", "CC"), ("Prog/Bank", "ProgramChange"),
    ("SysEx", "SysEx"), ("Pitch Bend", "PitchBend"),
    ("AfterTouch", "AfterTouch"), ("Transport", "Transport"),
]

# Filter-state colors (bg, border, fg) — verbatim from StyleOn/StyleFilter/StyleOff.
_STYLE_ON = ("#1B3A1B", "#3A7A3A", "#7DC97D")
_STYLE_SOLO = ("#3A3000", "#7A6400", "#CCAA33")
_STYLE_OFF = ("#2A1515", "#6E2E2E", "#CC6666")

_ON, _SOLO, _OFF = "on", "solo", "off"


def _style(bg: str, border: str, fg: str) -> str:
    return (f"background-color: {bg}; border: 1px solid {border}; color: {fg}; "
            f"font-family: {T.FONT_MONO}; font-size: {T.FS_SMALL}px; font-weight: bold; "
            f"padding: 2px 10px; border-radius: 3px;")


class SysExToolWindow(QDialog):
    def __init__(self, host: str, bridge: Optional[MidiBridgeClient],
                 sysex_service: Optional[SysExService], parent=None):
        super().__init__(parent)
        self.setWindowTitle("SysEx / MIDI Monitor")
        self.resize(1600, 600)
        self.setStyleSheet(f"QDialog {{ background-color: {_WIN_BG}; color: {_WIN_FG}; }}")

        self._bridge = bridge
        self._service = sysex_service
        self._pending: Deque[Tuple[bool, bytes]] = collections.deque()
        self._filter_state: Dict[str, str] = {t: _ON for _, t in _FILTER_BUTTONS}
        self._out_channel = 0
        self._auto_scroll = True
        self._sysex_count = 0
        self._midi_count = 0

        self._build_ui()

        if self._bridge is not None:
            self._bridge.message_received.connect(self._on_incoming)
            self._bridge.connection_changed.connect(self._on_connection_changed)
        self.set_active_stream("TCP" if (self._bridge and self._bridge.is_connected) else None)

        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(50)
        self._flush_timer.timeout.connect(self._flush_incoming)
        self._flush_timer.start()

    # ── UI construction (top to bottom, matching the XAML DockPanel order) ──

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Filter bar (Dock=Top)
        filt_bar = QWidget()
        filt_bar.setStyleSheet(f"background-color: #191919; border-bottom: 1px solid #2E2E2E;")
        filt_row = QHBoxLayout(filt_bar)
        filt_row.setContentsMargins(8, 5, 8, 5)
        lbl = QLabel("Filter:")
        lbl.setStyleSheet("color: #666666; font-family: Consolas; font-size: 11px;")
        filt_row.addWidget(lbl)
        self._filter_buttons: Dict[str, QPushButton] = {}
        for label, key in _FILTER_BUTTONS:
            btn = QPushButton(label)
            btn.clicked.connect(lambda _=False, k=key: self._cycle_filter(k))
            self._filter_buttons[key] = btn
            filt_row.addWidget(btn)
        hint = QLabel("Click: On→Solo→Off")
        hint.setStyleSheet("color: #444444; font-family: Consolas; font-size: 10px;")
        filt_row.addWidget(hint)
        filt_row.addStretch(1)
        self._btn_copy_selected = QPushButton("Copy Selected")
        self._btn_copy_all = QPushButton("Copy All Shown")
        for b in (self._btn_copy_selected, self._btn_copy_all):
            b.setStyleSheet("background-color: #2A2A2A; color: #CCCCCC; border: 1px solid #444444;")
        self._btn_copy_selected.clicked.connect(self._copy_selected)
        self._btn_copy_all.clicked.connect(self._copy_all_shown)
        filt_row.addWidget(self._btn_copy_selected)
        filt_row.addWidget(self._btn_copy_all)
        root.addWidget(filt_bar)
        self._refresh_filter_buttons()

        # Column header (Dock=Top)
        header = QWidget()
        header.setStyleSheet("background-color: #212121; border-bottom: 1px solid #2E2E2E;")
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(6, 3, 6, 3)
        for text, width in (("Time", 82), ("Dir", 32)):
            hl = QLabel(text)
            hl.setFixedWidth(width)
            hl.setStyleSheet("color: #666666; font-family: Consolas; font-size: 11px;")
            header_row.addWidget(hl)
        msg_lbl = QLabel("Message")
        msg_lbl.setStyleSheet("color: #666666; font-family: Consolas; font-size: 11px;")
        header_row.addWidget(msg_lbl)
        header_row.addStretch(1)
        root.addWidget(header)

        # Unified traffic list (fills remaining space)
        self._table = QTableWidget(0, 3)
        self._table.horizontalHeader().setVisible(False)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setSelectionMode(QTableWidget.ExtendedSelection)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setColumnWidth(0, 82)
        self._table.setColumnWidth(1, 32)
        self._table.setShowGrid(False)
        self._table.setStyleSheet(
            "QTableWidget { background-color: #131313; color: #D0D0D0; border: none; "
            "font-family: Consolas; font-size: 11px; }"
            "QTableWidget::item:selected { background-color: #1E3A5A; color: #FFFFFF; }")
        root.addWidget(self._table, stretch=1)

        # Virtual piano (Dock=Bottom, sits above the bottom toolbar)
        piano_panel = QWidget()
        piano_panel.setStyleSheet("background-color: #0D0D0D; border-top: 1px solid #2E2E2E;")
        piano_row = QHBoxLayout(piano_panel)
        piano_row.setContentsMargins(10, 11, 10, 11)
        self._piano = _PianoWidget(self._on_piano_note)
        piano_row.addWidget(self._piano, stretch=1)
        side = QVBoxLayout()
        side_w = QWidget()
        side_w.setFixedWidth(104)
        side_w.setLayout(side)
        self._note_label = QLabel("")
        self._note_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._note_label.setStyleSheet("color: #88AADD; font-family: Consolas; font-size: 13px; font-weight: bold;")
        side.addWidget(self._note_label)
        caption = QLabel("Virtual Piano")
        caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        caption.setStyleSheet("color: #444444; font-family: Consolas; font-size: 9px;")
        side.addWidget(caption)
        ch_lbl = QLabel("OUT CH")
        ch_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ch_lbl.setStyleSheet("color: #555555; font-family: Consolas; font-size: 9px; margin-top: 10px;")
        side.addWidget(ch_lbl)
        self._chan_combo = QComboBox()
        self._chan_combo.addItems([f"CH {i + 1}" for i in range(16)])
        self._chan_combo.setStyleSheet("font-family: Consolas; font-size: 11px;")
        self._chan_combo.currentIndexChanged.connect(lambda i: setattr(self, "_out_channel", i))
        side.addWidget(self._chan_combo)
        side.addStretch(1)
        piano_row.addWidget(side_w)
        root.addWidget(piano_panel)

        # Bottom toolbar (Dock=Bottom — the outermost strip)
        toolbar = QWidget()
        toolbar.setStyleSheet("background-color: #121212; border-top: 1px solid #2E2E2E;")
        tb_row = QHBoxLayout(toolbar)
        tb_row.setContentsMargins(8, 5, 8, 5)
        self._btn_clear = QPushButton("Clear")
        self._btn_clear.setFixedSize(60, 22)
        self._btn_clear.setStyleSheet("background-color: #2A2A2A; color: #CCCCCC; border: 1px solid #444444; font-size: 11px;")
        self._btn_clear.clicked.connect(self._clear_log)
        tb_row.addWidget(self._btn_clear)
        self._chk_autoscroll = QCheckBox("Auto-scroll")
        self._chk_autoscroll.setChecked(True)
        self._chk_autoscroll.setStyleSheet("color: #888888; font-size: 11px;")
        self._chk_autoscroll.toggled.connect(lambda v: setattr(self, "_auto_scroll", v))
        tb_row.addWidget(self._chk_autoscroll)
        self._sysex_count_label = QLabel("")
        self._sysex_count_label.setStyleSheet("color: #555555; font-size: 11px;")
        tb_row.addWidget(self._sysex_count_label)
        self._midi_count_label = QLabel("")
        self._midi_count_label.setStyleSheet("color: #555555; font-size: 11px;")
        tb_row.addWidget(self._midi_count_label)
        tb_row.addStretch(1)
        self._stream_label = QLabel("Stream: —")
        self._stream_label.setStyleSheet("color: #88AADD; font-family: Consolas; font-size: 11px;")
        tb_row.addWidget(self._stream_label)
        root.addWidget(toolbar)

        self._update_counts()

    def _refresh_filter_buttons(self):
        for key, btn in self._filter_buttons.items():
            state = self._filter_state[key]
            colors = {_ON: _STYLE_ON, _SOLO: _STYLE_SOLO, _OFF: _STYLE_OFF}[state]
            btn.setStyleSheet(_style(*colors))

    def _cycle_filter(self, key: str):
        order = {_ON: _SOLO, _SOLO: _OFF, _OFF: _ON}
        self._filter_state[key] = order[self._filter_state[key]]
        self._refresh_filter_buttons()
        self._apply_filter()

    def _apply_filter(self):
        """Matches FilterMessage: with any Solo active, only Solo'd types show
        (a type with no button, i.e. "Other", is hidden during solo); with no
        Solo active, only Off'd types are hidden ("Other" is always shown)."""
        any_solo = any(s == _SOLO for s in self._filter_state.values())
        for row in range(self._table.rowCount()):
            t = self._table.item(row, 2).data(Qt.ItemDataRole.UserRole) if self._table.item(row, 2) else "Other"
            state = self._filter_state.get(t)
            if any_solo:
                visible = state == _SOLO
            else:
                visible = state != _OFF if state is not None else True
            self._table.setRowHidden(row, not visible)

    # ── Link status ──────────────────────────────────────────────────────────

    def _on_connection_changed(self, connected: bool):
        self.set_active_stream("TCP" if connected else None)

    def set_active_stream(self, label: Optional[str]):
        self._stream_label.setText(f"Stream: {label if label else '—'}")

    # ── Incoming traffic (batched) ───────────────────────────────────────────

    def _on_incoming(self, msg: bytes):
        self._pending.append((False, msg))

    def _flush_incoming(self):
        if not self._pending:
            return
        batch = list(self._pending)
        self._pending.clear()
        for is_send, msg in batch:
            self._append_row(is_send, msg)
        while self._table.rowCount() > _MAX_ENTRIES:
            self._table.removeRow(0)
        self._apply_filter()
        self._update_counts()
        self._apply_note_lighting(batch)
        if self._auto_scroll and self._table.rowCount() > 0:
            self._table.scrollToBottom()

    def _append_row(self, is_send: bool, msg: bytes):
        row = self._table.rowCount()
        self._table.insertRow(row)
        t = ksx.classify_message(msg)
        if t == "SysEx":
            self._sysex_count += 1
        else:
            self._midi_count += 1

        time_item = QTableWidgetItem(datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3])
        time_item.setForeground(QColor("#555555"))
        self._table.setItem(row, 0, time_item)

        dir_item = QTableWidgetItem("TX" if is_send else "RX")
        dir_item.setForeground(QColor("#AF4C50" if is_send else "#4CAF50"))
        self._table.setItem(row, 1, dir_item)

        max_hex = 96 if t == "SysEx" else None
        msg_item = QTableWidgetItem(ksx.decode_midi(msg, max_hex))
        msg_item.setForeground(QColor(_TYPE_COLORS.get(t, "#CCCCCC")))
        msg_item.setData(Qt.ItemDataRole.UserRole, t)
        self._table.setItem(row, 2, msg_item)

    def _update_counts(self):
        self._sysex_count_label.setText(f"SysEx: {self._sysex_count}")
        self._midi_count_label.setText(f"MIDI: {self._midi_count}")

    def _apply_note_lighting(self, batch: List[Tuple[bool, bytes]]):
        for is_send, msg in batch:
            if is_send or not msg or (msg[0] & 0xF0) not in (0x80, 0x90):
                continue
            note = msg[1] if len(msg) > 1 else -1
            is_on = (msg[0] & 0xF0) == 0x90 and len(msg) > 2 and msg[2] > 0
            self._piano.light_key(note, is_on)

    # ── Piano ────────────────────────────────────────────────────────────────

    def _on_piano_note(self, note: int, on: bool):
        status = (0x90 if on else 0x80) | (self._out_channel & 0x0F)
        vel = 100 if on else 0
        hex_str = f"{status:02X} {note:02X} {vel:02X}"
        if self._service is not None:
            self._service.send_midi(hex_str)
        elif self._bridge is not None:
            self._bridge.send_bytes(bytes([status, note, vel]))
        self._pending.append((True, bytes([status, note, vel])))
        self._note_label.setText(_PianoWidget.note_name(note) if on else "")

    # ── Clipboard / clear ────────────────────────────────────────────────────

    def _copy_selected(self):
        rows = sorted({idx.row() for idx in self._table.selectedIndexes()})
        self._copy_rows(rows)

    def _copy_all_shown(self):
        rows = [r for r in range(self._table.rowCount()) if not self._table.isRowHidden(r)]
        self._copy_rows(rows)

    def _copy_rows(self, rows: List[int]):
        lines = []
        for r in rows:
            cells = [self._table.item(r, c).text() if self._table.item(r, c) else "" for c in range(3)]
            lines.append("  ".join(cells))
        QApplication.clipboard().setText("\n".join(lines))

    def _clear_log(self):
        self._table.setRowCount(0)
        self._sysex_count = 0
        self._midi_count = 0
        self._update_counts()

    def closeEvent(self, ev):
        self._flush_timer.stop()
        if self._bridge is not None:
            try:
                self._bridge.message_received.disconnect(self._on_incoming)
                self._bridge.connection_changed.disconnect(self._on_connection_changed)
            except (RuntimeError, TypeError):
                pass
        super().closeEvent(ev)


# ── 88-key piano (A0..C8), port of SysExToolWindow.xaml.cs BuildPiano() ─────

_WHITE_SEMITONES = [0, 2, 4, 5, 7, 9, 11]
_BLACK_KEYS = [(0, 1), (1, 3), (3, 6), (4, 8), (5, 10)]  # (leftWhiteIdx, semitone)
_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

_WW, _WH = 16, 90    # scaled down from the XAML's 28x148 for a manageable widget width
_BW, _BH = 10, 56


class _PianoWidget(QWidget):
    def __init__(self, on_note, parent=None):
        super().__init__(parent)
        self._on_note = on_note
        self.setFixedHeight(_WH + 4)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._keys: Dict[int, QPushButton] = {}
        self._key_is_black: Dict[int, bool] = {}
        self._build_piano()

    @staticmethod
    def note_name(midi: int) -> str:
        return f"{_NOTE_NAMES[midi % 12]}{midi // 12 - 1}"

    def _build_piano(self):
        # Matches BuildPiano(): A0(21), B0(23) before octave 0, C8(108) after octave 6.
        # Black-key x values are *white-key-boundary* coordinates: a black key's
        # centre sits at wx * _WW, on the seam between two white keys (so it
        # overlaps them), matching C# BuildPiano() which uses (wBase+lw+3)*WW.
        # A#0 sits on the A0/B0 seam (wx=1.0); each octave black sits on the
        # seam to the right of its left white (left_w + 3, not + 2.5).
        whites: List[Tuple[float, int]] = [(0, 21)]
        blacks: List[Tuple[float, int]] = [(1.0, 22)]
        whites.append((1, 23))

        for octv in range(7):
            w_base = octv * 7
            m_base = octv * 12
            for i, semitone in enumerate(_WHITE_SEMITONES):
                whites.append((w_base + i + 2, 24 + m_base + semitone))
            for left_w, semitone in _BLACK_KEYS:
                blacks.append((w_base + left_w + 3.0, 24 + m_base + semitone))

        whites.append((len(whites), 108))

        x = 0.0
        for _, midi in whites:
            btn = self._make_key(midi, _WW, _WH, "white")
            btn.move(int(x), 0)
            btn.raise_()
            x += _WW
            self._keys[midi] = btn
            self._key_is_black[midi] = False

        for wx, midi in blacks:
            btn = self._make_key(midi, _BW, _BH, "black")
            btn.move(int(wx * _WW - _BW / 2), 0)
            btn.raise_()
            self._keys[midi] = btn
            self._key_is_black[midi] = True

        self.setMinimumWidth(int(x))

    def _make_key(self, note: int, w: int, h: int, kind: str) -> QPushButton:
        btn = QPushButton(self)
        btn.setFixedSize(w, h)
        btn.setStyleSheet(self._key_style(kind, False))
        btn.pressed.connect(lambda n=note: self._on_note(n, True))
        btn.released.connect(lambda n=note: self._on_note(n, False))
        return btn

    @staticmethod
    def _key_style(kind: str, lit: bool) -> str:
        if kind == "white":
            bg = "#90B8FF" if lit else "#DCDCDC"
            return f"background-color: {bg}; border: 1px solid #444444;"
        bg = "#183868" if lit else "#1E1E1E"
        return f"background-color: {bg}; border: 1px solid #000000;"

    def light_key(self, note: int, on: bool):
        btn = self._keys.get(note)
        if btn is None:
            return
        kind = "black" if self._key_is_black.get(note) else "white"
        btn.setStyleSheet(self._key_style(kind, on))
