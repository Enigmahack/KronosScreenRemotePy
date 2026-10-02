"""
Base dialog classes and common dialog patterns.

Provides reusable dialog infrastructure for consistent UI/UX across all
modal windows.
"""
from __future__ import annotations
from typing import Optional, Callable, Any
from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QTextEdit,
    QPushButton, QMessageBox, QDialogButtonBox, QComboBox, QCheckBox,
)
from PySide6.QtGui import QFont

import Utils.theme as T


class BaseDialog(QDialog):
    """Base class for all application dialogs with consistent styling."""

    def __init__(self, title: str, parent=None, modal: bool = True):
        super().__init__(parent)
        self.setWindowTitle(title)
        if modal:
            self.setModal(True)
        self._setup_style()

    def _setup_style(self):
        """Apply consistent styling to this dialog."""
        self.setStyleSheet(f"""
            QDialog {{
                background-color: {T.BG};
                color: {T.TEXT};
            }}
            QLabel {{
                color: {T.TEXT};
            }}
            QLineEdit {{
                background-color: {T.PANEL_ALT};
                color: {T.TEXT};
                border: 1px solid {T.BORDER};
                border-radius: 3px;
                padding: 4px;
            }}
            QLineEdit:focus {{
                border: 1px solid {T.ACCENT};
            }}
            QTextEdit {{
                background-color: {T.INSET};
                color: {T.TEXT};
                border: 1px solid {T.BORDER};
            }}
            QPushButton {{
                background-color: {T.PANEL_ALT};
                color: {T.TEXT};
                border: 1px solid {T.BORDER_STRONG};
                border-radius: 3px;
                padding: 6px 12px;
            }}
            QPushButton:hover {{
                background-color: {T.PANEL};
            }}
            QPushButton:pressed {{
                background-color: {T.ACCENT_DEEP};
            }}
            QComboBox {{
                background-color: {T.PANEL_ALT};
                color: {T.TEXT};
                border: 1px solid {T.BORDER};
                border-radius: 3px;
                padding: 4px;
            }}
            QCheckBox {{
                color: {T.TEXT};
            }}
        """)

    def exec_centered(self) -> int:
        """Execute dialog centered on parent window."""
        if self.parent():
            parent_rect = self.parent().frameGeometry()
            center = parent_rect.center()
            self.move(center.x() - self.width() // 2,
                     center.y() - self.height() // 2)
        return self.exec()


@dataclass
class DialogResult:
    """Result from a dialog with status and data."""
    accepted: bool
    data: Any = None
    error: Optional[str] = None


class InputDialog(BaseDialog):
    """Dialog for single text input with validation."""

    def __init__(self, title: str, label: str, parent=None,
                 default: str = "", password: bool = False):
        super().__init__(title, parent)
        self.result_value = default
        self._setup_ui(label, default, password)
        self.resize(400, 150)

    def _setup_ui(self, label: str, default: str, password: bool):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Label
        lbl = QLabel(label)
        lbl.setStyleSheet(f"color: {T.TEXT};")
        layout.addWidget(lbl)

        # Input field
        self.input = QLineEdit()
        self.input.setText(default)
        if password:
            self.input.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.input)

        # Buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch()

        ok_btn = QPushButton("OK")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self._on_ok)
        button_layout.addWidget(ok_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(cancel_btn)

        layout.addLayout(button_layout)
        self.input.setFocus()

    def _on_ok(self):
        self.result_value = self.input.text()
        self.accept()

    def value(self) -> str:
        return self.result_value


class PromptDialog(BaseDialog):
    """Generic prompt dialog for confirming actions."""

    def __init__(self, title: str, message: str, parent=None,
                 buttons=None, default_button=None):
        super().__init__(title, parent)
        if buttons is None:
            buttons = ["OK", "Cancel"]
        self.result_index = -1
        self._setup_ui(message, buttons, default_button)
        self.resize(450, 200)

    def _setup_ui(self, message: str, buttons: list, default_button: Optional[str]):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Message
        msg_lbl = QLabel(message)
        msg_lbl.setStyleSheet(f"color: {T.TEXT}; font-size: 10pt;")
        msg_lbl.setWordWrap(True)
        layout.addWidget(msg_lbl, 1)

        # Buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch()

        button_objs = []
        for i, btn_text in enumerate(buttons):
            btn = QPushButton(btn_text)
            btn.clicked.connect(lambda checked, idx=i: self._on_button(idx))
            button_objs.append(btn)
            button_layout.addWidget(btn)

            if btn_text == default_button or (i == 0 and default_button is None):
                btn.setDefault(True)
                btn.setFocus()

        layout.addLayout(button_layout)
        self.resize(450, 180)

    def _on_button(self, index: int):
        self.result_index = index
        self.accept()

    def button_index(self) -> int:
        """Return the index of the clicked button (-1 if canceled)."""
        return self.result_index


class MessageBox:
    """Static methods for common message box patterns."""

    @staticmethod
    def info(parent, title: str, message: str):
        """Show information message."""
        dlg = QMessageBox(QMessageBox.Icon.Information, title, message, parent=parent)
        dlg.setStyleSheet(_message_box_stylesheet())
        dlg.exec()

    @staticmethod
    def warning(parent, title: str, message: str):
        """Show warning message."""
        dlg = QMessageBox(QMessageBox.Icon.Warning, title, message, parent=parent)
        dlg.setStyleSheet(_message_box_stylesheet())
        dlg.exec()

    @staticmethod
    def error(parent, title: str, message: str):
        """Show error message."""
        dlg = QMessageBox(QMessageBox.Icon.Critical, title, message, parent=parent)
        dlg.setStyleSheet(_message_box_stylesheet())
        dlg.exec()

    @staticmethod
    def question(parent, title: str, message: str) -> bool:
        """Show yes/no question. Returns True if user clicked Yes."""
        dlg = QMessageBox(QMessageBox.Icon.Question, title, message,
                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                         parent=parent)
        dlg.setStyleSheet(_message_box_stylesheet())
        return dlg.exec() == QMessageBox.StandardButton.Yes


def _message_box_stylesheet() -> str:
    """Stylesheet for message boxes."""
    return f"""
        QMessageBox {{
            background-color: {T.BG};
        }}
        QMessageBox QLabel {{
            color: {T.TEXT};
        }}
        QMessageBox QPushButton {{
            background-color: {T.PANEL_ALT};
            color: {T.TEXT};
            border: 1px solid {T.BORDER_STRONG};
            border-radius: 3px;
            padding: 6px 12px;
            min-width: 50px;
        }}
        QMessageBox QPushButton:hover {{
            background-color: {T.PANEL};
        }}
    """
