"""
About dialog — shows app version, Python version, and daemon version (fetched async).
"""
from __future__ import annotations
import platform
import sys
import threading
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
)

APP_VERSION = "1.0.0"
APP_NAME    = "Kronos ScreenRemote (Python)"


class AboutDialog(QDialog):
    def __init__(self, host: str, ctrl_port: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About Kronos ScreenRemote")
        self.setMinimumWidth(400)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self._host      = host
        self._ctrl_port = ctrl_port
        self._build_ui()
        if host:
            threading.Thread(target=self._fetch_daemon_ver, daemon=True).start()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        title = QLabel(f"<b style='font-size:14px;'>{APP_NAME}</b>")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        subtitle = QLabel("Cross-platform remote display for the Korg Kronos synthesizer.")
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        layout.addSpacing(10)

        def row(label: str, value: str) -> QHBoxLayout:
            h = QHBoxLayout()
            lbl = QLabel(f"<b>{label}</b>")
            lbl.setFixedWidth(140)
            lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            val = QLabel(value)
            val.setTextInteractionFlags(Qt.TextSelectableByMouse)
            h.addWidget(lbl)
            h.addWidget(val)
            h.addStretch()
            return h

        layout.addLayout(row("Client version:", APP_VERSION))
        layout.addLayout(row("Python version:", platform.python_version()))
        layout.addLayout(row("Qt binding:", f"PySide6 {self._pyside6_version()}"))

        layout.addSpacing(8)

        self._daemon_ver_row  = QLabel("<b>Daemon version:</b>")
        self._daemon_ver_row.setFixedWidth(140)
        self._daemon_ver_row.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._daemon_ver_val  = QLabel("Fetching…" if self._host else "(not connected)")
        self._daemon_ver_val.setTextInteractionFlags(Qt.TextSelectableByMouse)
        h2 = QHBoxLayout()
        h2.addWidget(self._daemon_ver_row)
        h2.addWidget(self._daemon_ver_val)
        h2.addStretch()
        layout.addLayout(h2)

        self._daemon_build_row = QLabel("<b>Daemon build:</b>")
        self._daemon_build_row.setFixedWidth(140)
        self._daemon_build_row.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._daemon_build_val = QLabel("")
        self._daemon_build_val.setTextInteractionFlags(Qt.TextSelectableByMouse)
        h3 = QHBoxLayout()
        h3.addWidget(self._daemon_build_row)
        h3.addWidget(self._daemon_build_val)
        h3.addStretch()
        layout.addLayout(h3)

        layout.addSpacing(12)

        note = QLabel("Protocol compatible with the C# Windows client.")
        note.setAlignment(Qt.AlignCenter)
        note.setStyleSheet("color: #888;")
        layout.addWidget(note)

        layout.addSpacing(8)
        btn = QPushButton("Close")
        btn.clicked.connect(self.accept)
        btn.setDefault(True)
        layout.addWidget(btn, alignment=Qt.AlignCenter)

    def _pyside6_version(self) -> str:
        try:
            import PySide6
            return PySide6.__version__
        except Exception:
            return "unknown"

    def _fetch_daemon_ver(self):
        import ctrl_client as CC
        resp = CC.get().query(self._host, self._ctrl_port, "VERSION", timeout_ms=3000)
        QTimer.singleShot(0, self, lambda r=resp: self._apply_daemon_ver(r))

    def _apply_daemon_ver(self, resp: Optional[str]):
        if not resp:
            self._daemon_ver_val.setText("(no response)")
            return
        # Expected: "VER=1.1.0 BUILD=abc1234"
        ver = build = ""
        for part in resp.split():
            if part.startswith("VER="):
                ver = part[4:]
            elif part.startswith("BUILD="):
                build = part[6:]
        self._daemon_ver_val.setText(ver or resp)
        self._daemon_build_val.setText(build)
