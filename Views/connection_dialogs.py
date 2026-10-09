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
        title = QLabel("Unable to connect to the instrument")
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
            "Make sure the instrument is powered on and connected to the network.\n"
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


class SavedConnectionDialog(BaseDialog):
    """Create/edit one Saved Connection: friendly name + host + FTP login + FTP port."""

    def __init__(self, seed: dict, is_new: bool, parent=None):
        super().__init__("New Saved Connection" if is_new else "Edit Saved Connection", parent)
        from PySide6.QtWidgets import QFormLayout, QLineEdit, QSpinBox, QDialogButtonBox
        from Views.revealable_password_edit import RevealablePasswordEdit
        self.result_entry: Optional[dict] = None
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self._name = QLineEdit(str(seed.get("name", "")))
        self._name.setToolTip("Friendly name shown in the Saved Connections list. "
                              "Defaults to the IP address if left empty.")
        self._host = QLineEdit(str(seed.get("host", "")))
        self._user = QLineEdit(str(seed.get("username", "")))
        self._pass = RevealablePasswordEdit()
        self._pass.setText(str(seed.get("password", "")))
        self._port = QSpinBox()
        self._port.setRange(1, 65535)
        self._port.setValue(int(seed.get("ftp_port", 21) or 21))
        form.addRow("Name:", self._name)
        form.addRow("Instrument IP address:", self._host)
        form.addRow("FTP Username:", self._user)
        form.addRow("FTP Password:", self._pass)
        form.addRow("FTP Port:", self._port)
        layout.addLayout(form)
        self._error = QLabel("")
        self._error.setStyleSheet(f"color: {T.ERROR};")
        self._error.setVisible(False)
        layout.addWidget(self._error)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.setMinimumWidth(340)

    def _on_ok(self):
        host = self._host.text().strip()
        if not host:
            self._error.setText("Enter the instrument IP address.")
            self._error.setVisible(True)
            return
        name = self._name.text().strip()
        self.result_entry = {
            "name": name or host, "host": host,
            "username": self._user.text().strip(), "password": self._pass.text(),
            "ftp_port": self._port.value(),
        }
        self.accept()
