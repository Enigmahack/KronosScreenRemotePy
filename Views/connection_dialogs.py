"""Connection-related dialogs."""
from __future__ import annotations
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTextEdit,
    QGroupBox,
)
from PySide6.QtGui import QFont, QIcon, QPixmap

import Utils.theme as T
from Views.dialog_base import BaseDialog


class ConnectionFailedDialog(BaseDialog):
    """Dialog displayed when connection to Kronos fails."""

    def __init__(self, host: str, port: int, error: str, parent=None):
        super().__init__("Connection Failed", parent)
        self.host = host
        self.port = port
        self.error = error
        self.retry = False
        self.open_settings = False   # mirrors C#'s ConnectionFailedDialog.OpenSettings
        self._setup_ui()
        self.resize(500, 300)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Title
        title = QLabel("Unable to connect to Kronos")
        title_font = QFont()
        title_font.setPointSize(11)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setStyleSheet(f"color: {T.ERROR};")
        layout.addWidget(title)

        # Connection details
        details_layout = QHBoxLayout()
        details_layout.addWidget(QLabel("Host:"))
        host_label = QLabel(self.host)
        host_label.setStyleSheet(f"color: {T.TEXT_DIM};")
        details_layout.addWidget(host_label)
        details_layout.addSpacing(20)
        details_layout.addWidget(QLabel("Port:"))
        port_label = QLabel(str(self.port))
        port_label.setStyleSheet(f"color: {T.TEXT_DIM};")
        details_layout.addWidget(port_label)
        details_layout.addStretch()
        layout.addLayout(details_layout)

        # Error details box
        error_box = QGroupBox("Error Details")
        error_box.setStyleSheet(f"""
            QGroupBox {{
                color: {T.TEXT};
                border: 1px solid {T.BORDER};
                border-radius: 3px;
                margin-top: 6px;
                padding-top: 6px;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 3px 0 3px;
            }}
        """)
        error_layout = QVBoxLayout(error_box)

        error_text = QTextEdit()
        error_text.setPlainText(self.error)
        error_text.setReadOnly(True)
        error_text.setMaximumHeight(100)
        error_text.setStyleSheet(f"""
            QTextEdit {{
                background-color: {T.INSET};
                color: {T.ERROR_TEXT};
                border: none;
                font-family: monospace;
                font-size: 9pt;
            }}
        """)
        error_layout.addWidget(error_text)
        layout.addWidget(error_box, 1)

        # Help text
        help_text = QLabel(
            "Make sure the Kronos is powered on and connected to the network.\n"
            "Check the IP address and port number in the connection settings."
        )
        help_text.setStyleSheet(f"color: {T.TEXT_DIM}; font-size: 9pt;")
        help_text.setWordWrap(True)
        layout.addWidget(help_text)

        # Buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch()

        settings_btn = QPushButton("Settings…")
        settings_btn.clicked.connect(self._on_settings)
        button_layout.addWidget(settings_btn)

        retry_btn = QPushButton("Retry")
        retry_btn.setDefault(True)
        retry_btn.clicked.connect(self._on_retry)
        button_layout.addWidget(retry_btn)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        button_layout.addWidget(close_btn)

        layout.addLayout(button_layout)

    def _on_retry(self):
        self.retry = True
        self.accept()

    def _on_settings(self):
        # Port of C#'s ConnectionFailedDialog.OnSettings: this dialog only sets
        # the flag and closes — the caller (whoever owns the real AppSettings
        # instance) is responsible for actually opening Settings, same as
        # C#'s OnSessionConnectionFailed checking dlg.OpenSettings afterward.
        self.open_settings = True
        self.accept()

    def should_retry(self) -> bool:
        return self.retry
