"""File and resource selection dialogs."""
from __future__ import annotations
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTreeWidget, QTreeWidgetItem,
    QLineEdit, QProgressBar, QCheckBox, QGroupBox, QListWidget, QListWidgetItem,
    QSplitter, QMessageBox,
)
from PySide6.QtGui import QFont, QIcon

import Utils.theme as T
from Views.dialog_base import BaseDialog


class RemoteFilePickerDialog(BaseDialog):
    """Browse and select files from remote Kronos via FTP."""

    def __init__(self, parent=None, allow_multiple: bool = False):
        super().__init__("Remote File Browser", parent)
        self.allow_multiple = allow_multiple
        self.selected_files = []
        self._setup_ui()
        self.resize(600, 500)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Connection status
        status_layout = QHBoxLayout()
        status_layout.addWidget(QLabel("Status:"))
        self.status_label = QLabel("Disconnected")
        self.status_label.setStyleSheet(f"color: {T.ERROR};")
        status_layout.addWidget(self.status_label)
        status_layout.addSpacing(20)

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self._on_connect)
        status_layout.addWidget(self.connect_btn)
        status_layout.addStretch()

        layout.addLayout(status_layout)

        # File browser
        browser_group = QGroupBox("Files")
        browser_layout = QVBoxLayout(browser_group)

        self.file_tree = QTreeWidget()
        self.file_tree.setHeaderLabels(["Name", "Size", "Modified"])
        self.file_tree.setColumnWidth(0, 300)
        self.file_tree.setStyleSheet(f"""
            QTreeWidget {{
                background-color: {T.INSET};
                color: {T.TEXT};
                border: 1px solid {T.BORDER};
            }}
            QTreeWidget::item {{
                padding: 4px;
            }}
            QHeaderView::section {{
                background-color: {T.PANEL_ALT};
                color: {T.TEXT};
                padding: 4px;
                border: none;
            }}
        """)
        browser_layout.addWidget(self.file_tree, 1)

        layout.addWidget(browser_group, 1)

        # Progress
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress.setStyleSheet(f"""
            QProgressBar {{
                background-color: {T.INSET};
                border: 1px solid {T.BORDER};
                border-radius: 3px;
            }}
            QProgressBar::chunk {{
                background-color: {T.ACCENT};
            }}
        """)
        layout.addWidget(self.progress)

        # Path display
        path_layout = QHBoxLayout()
        path_layout.addWidget(QLabel("Path:"))
        self.path_display = QLineEdit()
        self.path_display.setReadOnly(True)
        self.path_display.setText("/korg/rw/HD")
        path_layout.addWidget(self.path_display)
        layout.addLayout(path_layout)

        # Buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch()

        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._on_refresh)
        button_layout.addWidget(refresh_btn)

        ok_btn = QPushButton("OK")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self._on_ok)
        button_layout.addWidget(ok_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(cancel_btn)

        layout.addLayout(button_layout)

    def _on_connect(self):
        self.status_label.setText("Connecting...")
        self.status_label.setStyleSheet(f"color: {T.WARN};")
        self.connect_btn.setEnabled(False)
        # TODO: Implement FTP connection

    def _on_refresh(self):
        self.progress.setVisible(True)
        self.progress.setValue(0)
        # TODO: Implement file listing
        self.progress.setVisible(False)

    def _on_ok(self):
        # Get selected items
        selected = self.file_tree.selectedItems()
        if not selected:
            from Views.dialog_base import MessageBox
            MessageBox.warning(self, "Selection Error", "Please select at least one file.")
            return
        self.selected_files = [item.text(0) for item in selected]
        self.accept()

    def get_selected(self) -> list[str]:
        return self.selected_files


class RemoteSampleBrowserDialog(BaseDialog):
    """Browse Kronos samples via FTP."""

    def __init__(self, parent=None):
        super().__init__("Sample Browser", parent)
        self.selected_sample = None
        self._setup_ui()
        self.resize(700, 500)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        info = QLabel("Browse samples on the Kronos")
        info.setStyleSheet(f"color: {T.TEXT_DIM};")
        layout.addWidget(info)

        # Sample list
        samples_group = QGroupBox("Available Samples")
        samples_layout = QVBoxLayout(samples_group)

        self.sample_list = QListWidget()
        self.sample_list.setStyleSheet(f"""
            QListWidget {{
                background-color: {T.INSET};
                color: {T.TEXT};
                border: 1px solid {T.BORDER};
            }}
            QListWidget::item {{
                padding: 4px;
            }}
            QListWidget::item:selected {{
                background-color: {T.ACCENT_DEEP};
                color: {T.TEXT};
            }}
        """)
        samples_layout.addWidget(self.sample_list, 1)
        layout.addWidget(samples_group, 1)

        # Sample info
        info_group = QGroupBox("Sample Information")
        info_layout = QVBoxLayout(info_group)
        self.info_display = QLabel("Select a sample to view details")
        self.info_display.setStyleSheet(f"color: {T.TEXT_DIM};")
        self.info_display.setWordWrap(True)
        info_layout.addWidget(self.info_display)
        layout.addWidget(info_group)

        # Buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch()

        ok_btn = QPushButton("Select")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self._on_ok)
        button_layout.addWidget(ok_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(cancel_btn)

        layout.addLayout(button_layout)

    def _on_ok(self):
        selected = self.sample_list.currentItem()
        if selected:
            self.selected_sample = selected.text()
            self.accept()
        else:
            from Views.dialog_base import MessageBox
            MessageBox.warning(self, "Selection Error", "Please select a sample.")

    def get_selected(self) -> Optional[str]:
        return self.selected_sample
