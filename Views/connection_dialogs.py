"""Connection-related dialogs."""
from __future__ import annotations
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTextEdit,
    QGroupBox, QLineEdit, QSpinBox,
)
from PySide6.QtGui import QFont, QIcon, QPixmap

import Utils.theme as T
from Views.dialog_base import BaseDialog, MessageBox


class ConnectionFailedDialog(BaseDialog):
    """Dialog displayed when connection to Kronos fails."""

    def __init__(self, host: str, port: int, error: str, parent=None):
        super().__init__("Connection Failed", parent)
        self.host = host
        self.port = port
        self.error = error
        self.retry = False
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
        # This will be connected to open settings window
        from Views.settings_window import SettingsWindow
        dlg = SettingsWindow(None, parent=self, initial_tab="Connection")
        dlg.exec()

    def should_retry(self) -> bool:
        return self.retry


class LoginDialog(BaseDialog):
    """Enhanced login dialog for authentication."""

    def __init__(self, host: str = "", parent=None):
        super().__init__("Kronos Remote - Login", parent)
        self.host = host
        self.username = ""
        self.password = ""
        self.remember_password = False
        self._setup_ui()
        self.resize(400, 280)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        # Title
        title = QLabel("Connect to Kronos")
        title_font = QFont()
        title_font.setPointSize(12)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setStyleSheet(f"color: {T.ACCENT};")
        layout.addWidget(title)

        # Host
        host_layout = QHBoxLayout()
        host_label = QLabel("Host:")
        host_label.setMinimumWidth(80)
        host_layout.addWidget(host_label)
        self.host_input = QLineEdit()
        self.host_input.setText(self.host)
        self.host_input.setPlaceholderText("192.168.100.15")
        host_layout.addWidget(self.host_input)
        layout.addLayout(host_layout)

        # Port
        port_layout = QHBoxLayout()
        port_label = QLabel("Port:")
        port_label.setMinimumWidth(80)
        port_layout.addWidget(port_label)
        self.port_input = QSpinBox()
        self.port_input.setMinimum(1)
        self.port_input.setMaximum(65535)
        self.port_input.setValue(7373)
        port_layout.addWidget(self.port_input)
        port_layout.addStretch()
        layout.addLayout(port_layout)

        # Separator
        layout.addSpacing(12)

        # Username
        user_layout = QHBoxLayout()
        user_label = QLabel("Username:")
        user_label.setMinimumWidth(80)
        user_layout.addWidget(user_label)
        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("(optional)")
        user_layout.addWidget(self.username_input)
        layout.addLayout(user_layout)

        # Password
        pass_layout = QHBoxLayout()
        pass_label = QLabel("Password:")
        pass_label.setMinimumWidth(80)
        pass_layout.addWidget(pass_label)
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.setPlaceholderText("(optional)")
        pass_layout.addWidget(self.password_input)
        layout.addLayout(pass_layout)

        layout.addStretch()

        # Buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch()

        ok_btn = QPushButton("Connect")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self._on_ok)
        button_layout.addWidget(ok_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(cancel_btn)

        layout.addLayout(button_layout)

        self.host_input.setFocus()

    def _on_ok(self):
        if not self.host_input.text().strip():
            MessageBox.warning(self, "Input Error", "Please enter a host address.")
            return
        self.username = self.username_input.text().strip()
        self.password = self.password_input.text()
        self.host = self.host_input.text().strip()
        self.accept()

    def get_credentials(self) -> tuple[str, str, str, int]:
        """Return (host, username, password, port)."""
        return (self.host, self.username, self.password, self.port_input.value())
