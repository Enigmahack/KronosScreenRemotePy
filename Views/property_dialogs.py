"""Property and information dialogs."""
from __future__ import annotations
from typing import Optional, Dict, Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QLineEdit, QSpinBox, QCheckBox, QGroupBox,
    QFormLayout, QScrollArea, QWidget,
)
from PySide6.QtGui import QFont

import Utils.theme as T
from Views.dialog_base import BaseDialog


class ObjectInfoDialog(BaseDialog):
    """Display object (Program/Combi/SetList) properties."""

    def __init__(self, obj_type: str, bank: int, number: int,
                 name: str, properties: Dict[str, Any], parent=None):
        super().__init__(f"{obj_type} - {name}", parent)
        self.obj_type = obj_type
        self.bank = bank
        self.number = number
        self.name = name
        self.properties = properties
        self._setup_ui()
        self.resize(500, 400)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Object header
        header_layout = QHBoxLayout()

        type_label = QLabel(self.obj_type)
        type_font = QFont()
        type_font.setBold(True)
        type_label.setFont(type_font)
        type_label.setStyleSheet(f"color: {T.ACCENT};")
        header_layout.addWidget(type_label)

        name_label = QLabel(f"#{self.number + 1}: {self.name}")
        name_label.setStyleSheet(f"color: {T.TEXT};")
        header_layout.addWidget(name_label)
        header_layout.addStretch()

        layout.addLayout(header_layout)

        # Properties table
        table = QTableWidget()
        table.setColumnCount(2)
        table.setHorizontalHeaderLabels(["Property", "Value"])
        table.setColumnWidth(0, 180)
        table.setColumnWidth(1, 280)
        table.setStyleSheet(f"""
            QTableWidget {{
                background-color: {T.INSET};
                alternate-background-color: {T.PANEL};
                gridline-color: {T.BORDER};
            }}
            QTableWidget::item {{
                color: {T.TEXT};
                padding: 4px;
            }}
            QHeaderView::section {{
                background-color: {T.PANEL_ALT};
                color: {T.TEXT};
                padding: 4px;
                border: none;
            }}
        """)
        table.setAlternatingRowColors(True)

        row = 0
        for key, value in self.properties.items():
            table.insertRow(row)

            key_item = QTableWidgetItem(key)
            key_item.setForeground(T.TEXT_DIM)
            table.setItem(row, 0, key_item)

            val_item = QTableWidgetItem(str(value))
            table.setItem(row, 1, val_item)
            row += 1

        layout.addWidget(table, 1)

        # Buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch()

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        button_layout.addWidget(close_btn)

        layout.addLayout(button_layout)


class FtpPropertiesDialog(BaseDialog):
    """FTP configuration dialog."""

    def __init__(self, host: str = "", port: int = 21, user: str = "", parent=None):
        super().__init__("FTP Configuration", parent)
        self.ftp_host = host
        self.ftp_port = port
        self.ftp_user = user
        self._setup_ui()
        self.resize(400, 250)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Form group
        form = QFormLayout()
        form.setSpacing(10)

        # Host
        self.host_input = QLineEdit()
        self.host_input.setText(self.ftp_host)
        self.host_input.setPlaceholderText("192.168.100.15")
        form.addRow("Host:", self.host_input)

        # Port
        self.port_input = QSpinBox()
        self.port_input.setMinimum(1)
        self.port_input.setMaximum(65535)
        self.port_input.setValue(self.ftp_port)
        form.addRow("Port:", self.port_input)

        # Username
        self.user_input = QLineEdit()
        self.user_input.setText(self.ftp_user)
        self.user_input.setPlaceholderText("root")
        form.addRow("Username:", self.user_input)

        # Path
        self.path_input = QLineEdit()
        self.path_input.setText("/korg/rw/HD")
        self.path_input.setPlaceholderText("/korg/rw/HD")
        form.addRow("Base Path:", self.path_input)

        layout.addLayout(form)

        # Info
        info = QLabel("FTP is used to transfer files to/from the instrument.\n"
                     "Anonymous login is typically available.")
        info.setStyleSheet(f"color: {T.TEXT_DIM}; font-size: 9pt;")
        info.setWordWrap(True)
        layout.addWidget(info)

        layout.addStretch()

        # Buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch()

        test_btn = QPushButton("Test Connection")
        test_btn.clicked.connect(self._on_test)
        button_layout.addWidget(test_btn)

        ok_btn = QPushButton("OK")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self._on_ok)
        button_layout.addWidget(ok_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(cancel_btn)

        layout.addLayout(button_layout)

    def _on_test(self):
        # TODO: Test FTP connection
        from Views.dialog_base import MessageBox
        MessageBox.info(self, "FTP Test", "FTP connection test not yet implemented.")

    def _on_ok(self):
        self.ftp_host = self.host_input.text().strip()
        self.ftp_port = self.port_input.value()
        self.ftp_user = self.user_input.text().strip()
        self.accept()

    def get_config(self) -> tuple[str, int, str]:
        """Return (host, port, username)."""
        return (self.ftp_host, self.ftp_port, self.ftp_user)
