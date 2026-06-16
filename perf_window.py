"""
Performance / System Info window — mirrors the C# KeyboardInfoWindow.

Shows CPU usage (rolling 60-sample graph + per-core bars), memory, disk (/korg/rw
and optional /korg/rw2), USB drives, temperatures, fan RPM, audio engine state,
uptime, and current mode. Polls the daemon via SYSINFO at a configurable interval.
"""
from __future__ import annotations
import collections
import re
from typing import Dict, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QProgressBar, QSizePolicy, QVBoxLayout, QWidget,
)

from PySide6.QtCore import QPointF

_BG        = "#1E1E1E"
_PANEL_BG  = "#252525"
_GRID_LINE = "#333333"
_CPU_LINE  = "#88AADD"
_CPU_AREA  = "#1A3055"
_TEXT_DIM  = "#888888"
_TEXT_NORM = "#C8C8C8"
_GREEN     = "#55CC55"
_RED       = "#CC4444"
_AMBER     = "#CCAA33"
_BAR_BG    = "#333333"

_GRAPH_SAMPLES = 60


class _CpuGraph(QWidget):
    """Rolling CPU usage graph (area + line)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(60)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._samples: collections.deque[float] = collections.deque(
            [0.0] * _GRAPH_SAMPLES, maxlen=_GRAPH_SAMPLES)

    def push(self, pct: float):
        self._samples.append(max(0.0, min(100.0, pct)))
        self.update()

    def paintEvent(self, _ev):
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor(_PANEL_BG))

        # Grid lines at 25 / 50 / 75 %
        p.setPen(QPen(QColor(_GRID_LINE), 1))
        for frac in (0.25, 0.5, 0.75):
            y = int((1 - frac) * h)
            p.drawLine(0, y, w, y)

        if not self._samples:
            return

        samples = list(self._samples)
        n = len(samples)
        step = w / max(n - 1, 1)

        def pt(i: int) -> QPointF:
            x = i * step
            y = (1.0 - samples[i] / 100.0) * h
            return QPointF(x, y)

        # Area fill
        poly = QPolygonF()
        poly.append(QPointF(0, h))
        for i in range(n):
            poly.append(pt(i))
        poly.append(QPointF((n - 1) * step, h))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor(_CPU_AREA)))
        p.drawPolygon(poly)

        # Line
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(_CPU_LINE), 1.5))
        for i in range(n - 1):
            p.drawLine(pt(i), pt(i + 1))

        p.end()


def _bar(value: int, total: int, color: str = _CPU_LINE) -> QProgressBar:
    bar = QProgressBar()
    bar.setRange(0, max(total, 1))
    bar.setValue(value)
    bar.setTextVisible(False)
    bar.setFixedHeight(12)
    bar.setStyleSheet(
        f"QProgressBar {{ background: {_BAR_BG}; border: none; border-radius: 2px; }}"
        f"QProgressBar::chunk {{ background: {color}; border-radius: 2px; }}"
    )
    return bar


class _BarRow(QWidget):
    """Label + progress bar + value label on one line."""

    def __init__(self, label: str, color: str = _CPU_LINE, parent=None):
        super().__init__(parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)

        lbl = QLabel(label)
        lbl.setFixedWidth(60)
        lbl.setStyleSheet(f"color: {_TEXT_DIM}; font-size: 11px;")
        h.addWidget(lbl)

        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(12)
        self._bar.setStyleSheet(
            f"QProgressBar {{ background: {_BAR_BG}; border: none; border-radius: 2px; }}"
            f"QProgressBar::chunk {{ background: {color}; border-radius: 2px; }}"
        )
        h.addWidget(self._bar, 1)

        self._val = QLabel("0%")
        self._val.setFixedWidth(38)
        self._val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._val.setStyleSheet(f"color: {_TEXT_NORM}; font-size: 11px;")
        h.addWidget(self._val)

    def set_pct(self, pct: float):
        v = int(pct)
        self._bar.setValue(v)
        self._val.setText(f"{v}%")

    def set_used_of(self, used_mb: int, total_mb: int):
        if total_mb <= 0:
            self._bar.setRange(0, 1)
            self._bar.setValue(0)
            self._val.setText("—")
            return
        self._bar.setRange(0, total_mb)
        self._bar.setValue(used_mb)
        pct = used_mb * 100 / total_mb
        self._val.setText(f"{pct:.0f}%")


def _stat_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color: {_TEXT_NORM}; font-size: 11px;")
    return lbl


def _dim_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color: {_TEXT_DIM}; font-size: 11px;")
    return lbl


class PerformanceWindow(QDialog):
    """System info polling window (equivalent to C# KeyboardInfoWindow)."""

    _INTERVALS = [("1 s", 1000), ("5 s", 5000), ("10 s", 10000),
                  ("30 s", 30000), ("1 min", 60000)]

    def __init__(self, host: str, ctrl_port: int, parent=None):
        super().__init__(parent)
        self._host      = host
        self._ctrl_port = ctrl_port
        self.setWindowTitle("Keyboard Info")
        self.setMinimumWidth(400)
        self.resize(420, 530)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)

        self._build_ui()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._set_interval(0)  # start at 1 s

        if host:
            self._timer.start()
            self._poll()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)
        root.setContentsMargins(10, 10, 10, 10)

        # ── CPU graph ──────────────────────────────────────────────────────────
        cpu_group = QGroupBox("CPU")
        cpu_group.setStyleSheet("QGroupBox { color: #88AADD; border: 1px solid #444; "
                                "border-radius: 3px; margin-top: 6px; font-weight: bold; } "
                                "QGroupBox::title { subcontrol-origin: margin; padding: 0 4px; }")
        cpu_vbox = QVBoxLayout(cpu_group)
        cpu_vbox.setSpacing(4)

        self._graph = _CpuGraph()
        cpu_vbox.addWidget(self._graph)

        self._cpu_overall = _BarRow("Overall")
        cpu_vbox.addWidget(self._cpu_overall)

        self._cpu_bars: list[_BarRow] = []
        for i in range(4):
            bar = _BarRow(f"  CPU{i}")
            self._cpu_bars.append(bar)
            cpu_vbox.addWidget(bar)

        root.addWidget(cpu_group)

        # ── Memory ────────────────────────────────────────────────────────────
        mem_group = QGroupBox("Memory")
        mem_group.setStyleSheet(cpu_group.styleSheet())
        mem_vbox = QVBoxLayout(mem_group)
        self._mem_bar = _BarRow("RAM", color="#AACC88")
        mem_vbox.addWidget(self._mem_bar)
        root.addWidget(mem_group)

        # ── Storage ───────────────────────────────────────────────────────────
        disk_group = QGroupBox("Storage")
        disk_group.setStyleSheet(cpu_group.styleSheet())
        disk_vbox = QVBoxLayout(disk_group)
        disk_vbox.setSpacing(3)

        self._rw_bar  = _BarRow("/korg/rw",  color="#DDAA55")
        disk_vbox.addWidget(self._rw_bar)

        self._rw2_widget = QWidget()
        rw2_vbox = QVBoxLayout(self._rw2_widget)
        rw2_vbox.setContentsMargins(0, 0, 0, 0)
        self._rw2_bar = _BarRow("/korg/rw2", color="#DDAA55")
        rw2_vbox.addWidget(self._rw2_bar)
        self._rw2_widget.setVisible(False)
        disk_vbox.addWidget(self._rw2_widget)

        self._usb_widgets: list[QWidget] = []
        self._usb_bars:    list[_BarRow] = []
        for i in range(2):
            w = QWidget()
            vb = QVBoxLayout(w)
            vb.setContentsMargins(0, 0, 0, 0)
            bar = _BarRow(f"USB{i}", color="#88DDDD")
            vb.addWidget(bar)
            w.setVisible(False)
            disk_vbox.addWidget(w)
            self._usb_widgets.append(w)
            self._usb_bars.append(bar)

        root.addWidget(disk_group)

        # ── Stats table ───────────────────────────────────────────────────────
        stats_group = QGroupBox("System")
        stats_group.setStyleSheet(cpu_group.styleSheet())
        grid = QGridLayout(stats_group)
        grid.setSpacing(4)

        def stat_row(row: int, label: str) -> QLabel:
            dim = _dim_label(label + ":")
            dim.setFixedWidth(90)
            val = _stat_label("—")
            grid.addWidget(dim, row, 0)
            grid.addWidget(val, row, 1)
            return val

        self._lbl_uptime = stat_row(0, "Uptime")
        self._lbl_mode   = stat_row(1, "Mode")
        self._lbl_load   = stat_row(2, "Load avg")
        self._lbl_temp1  = stat_row(3, "Temp 1")
        self._lbl_temp2  = stat_row(4, "Temp 2")
        self._lbl_temp3  = stat_row(5, "Temp 3")
        self._lbl_fan    = stat_row(6, "Fan 1")
        self._lbl_audio  = stat_row(7, "Audio")

        root.addWidget(stats_group)

        # ── Footer: interval selector + status ────────────────────────────────
        foot = QHBoxLayout()

        self._status_lbl = QLabel("● Not connected")
        self._status_lbl.setStyleSheet(f"color: {_RED}; font-size: 11px;")
        foot.addWidget(self._status_lbl)
        foot.addStretch()

        foot.addWidget(_dim_label("Update:"))
        self._interval_combo = QComboBox()
        for label, _ms in self._INTERVALS:
            self._interval_combo.addItem(label)
        self._interval_combo.setCurrentIndex(0)
        self._interval_combo.currentIndexChanged.connect(self._set_interval)
        foot.addWidget(self._interval_combo)

        root.addLayout(foot)

    def _set_interval(self, idx: int):
        ms = self._INTERVALS[idx][1]
        self._timer.setInterval(ms)

    def _set_status(self, connected: bool):
        if connected:
            self._status_lbl.setText("● Connected")
            self._status_lbl.setStyleSheet(f"color: {_GREEN}; font-size: 11px;")
        else:
            self._status_lbl.setText("● Not connected")
            self._status_lbl.setStyleSheet(f"color: {_RED}; font-size: 11px;")

    def update_host(self, host: str, ctrl_port: int):
        self._host      = host
        self._ctrl_port = ctrl_port
        if host:
            self._timer.start()
            self._poll()
        else:
            self._timer.stop()
            self._set_status(False)

    def _poll(self):
        if not self._host:
            return
        import threading
        threading.Thread(target=self._fetch, daemon=True, name="PerfPoll").start()

    def _fetch(self):
        import ctrl_client as CC
        resp = CC.get().query_multi(self._host, self._ctrl_port, "SYSINFO", timeout_ms=4000)
        from PySide6.QtCore import QTimer as _QT
        try:
            _QT.singleShot(0, self, lambda r=resp: self._apply(r))
        except RuntimeError:
            pass  # dialog C++ object deleted before thread finished — safe to ignore

    def _apply(self, resp: Optional[str]):
        if not resp:
            self._set_status(False)
            return

        kv: Dict[str, str] = {}
        for line in resp.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                kv[k.strip()] = v.strip()

        self._set_status(True)

        # CPU overall
        try:
            cpu_pct = float(kv.get("CPU_PCT", "-1"))
            if cpu_pct >= 0:
                self._graph.push(cpu_pct)
                self._cpu_overall.set_pct(cpu_pct)
        except ValueError:
            pass

        # Per-core
        for i, bar in enumerate(self._cpu_bars):
            try:
                v = float(kv.get(f"CPU{i}_PCT", "-1"))
                if v >= 0:
                    bar.set_pct(v)
                    bar.setVisible(True)
                else:
                    bar.setVisible(False)
            except ValueError:
                bar.setVisible(False)

        # Memory
        try:
            total_kb = int(kv.get("MEM_TOTAL_KB", "0"))
            avail_kb = int(kv.get("MEM_AVAIL_KB", kv.get("MEM_FREE_KB", "0")))
            used_kb  = total_kb - avail_kb
            if total_kb > 0:
                self._mem_bar.set_used_of(used_kb // 1024, total_kb // 1024)
        except ValueError:
            pass

        # Disk /korg/rw
        try:
            total_mb = int(kv.get("DISK_TOTAL_MB", "0"))
            free_mb  = int(kv.get("DISK_FREE_MB",  "0"))
            used_mb  = total_mb - free_mb
            self._rw_bar.set_used_of(used_mb, total_mb)
        except ValueError:
            pass

        # Disk /korg/rw2
        try:
            rw2_total = int(kv.get("RW2_TOTAL_MB", "0"))
            rw2_free  = int(kv.get("RW2_FREE_MB",  "0"))
            if rw2_total > 0:
                self._rw2_bar.set_used_of(rw2_total - rw2_free, rw2_total)
                self._rw2_widget.setVisible(True)
            else:
                self._rw2_widget.setVisible(False)
        except ValueError:
            self._rw2_widget.setVisible(False)

        # USB drives
        try:
            usb_count = int(kv.get("USB_COUNT", "0"))
        except ValueError:
            usb_count = 0
        for i in range(2):
            try:
                mnt   = kv.get(f"USB{i}_MNT", "")
                total = int(kv.get(f"USB{i}_TOTAL_MB", "0"))
                free  = int(kv.get(f"USB{i}_FREE_MB",  "0"))
                if i < usb_count and total > 0:
                    self._usb_bars[i].set_used_of(total - free, total)
                    self._usb_widgets[i].setVisible(True)
                else:
                    self._usb_widgets[i].setVisible(False)
            except ValueError:
                self._usb_widgets[i].setVisible(False)

        # Uptime
        try:
            up = int(kv.get("UPTIME", "0"))
            h, r = divmod(up, 3600)
            m, s = divmod(r, 60)
            self._lbl_uptime.setText(f"{h}h {m:02d}m {s:02d}s")
        except ValueError:
            pass

        # Mode
        mode_names = {
            "1": "Setlist", "2": "Combi", "3": "Program",
            "4": "Sequence", "5": "Sampling", "6": "Global", "7": "Disk",
        }
        self._lbl_mode.setText(mode_names.get(kv.get("MODE", ""), kv.get("MODE", "—")))

        # Load
        self._lbl_load.setText(kv.get("LOAD", "—"))

        # Temps (color coded)
        for lbl, key in ((self._lbl_temp1, "TEMP1"),
                         (self._lbl_temp2, "TEMP2"),
                         (self._lbl_temp3, "TEMP3")):
            raw = kv.get(key, "")
            if raw:
                try:
                    temp = float(raw)
                    color = _RED if temp >= 90 else (_AMBER if temp >= 80 else _TEXT_NORM)
                    lbl.setText(f"{temp:.1f} °C")
                    lbl.setStyleSheet(f"color: {color}; font-size: 11px;")
                except ValueError:
                    lbl.setText(raw)
            else:
                lbl.setText("—")

        # Fan
        fan = kv.get("FAN1_RPM", "")
        self._lbl_fan.setText(f"{fan} RPM" if fan else "—")

        # Audio
        sr       = kv.get("AUDIO_SR", "")
        ch       = kv.get("AUDIO_OUT_CH", "")
        rto      = kv.get("AUDIO_RTO", "")
        midi_rt  = kv.get("AUDIO_MIDI_RT", "")
        parts = []
        if sr:
            parts.append(f"{sr} Hz")
        if ch:
            parts.append(f"{ch}ch")
        if rto == "1":
            parts.append("Active")
        elif rto == "0":
            parts.append("Idle")
        self._lbl_audio.setText("  ".join(parts) if parts else "—")

    def closeEvent(self, event):
        self._timer.stop()
        event.accept()
