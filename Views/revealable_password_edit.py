"""
RevealablePasswordEdit — a masked QLineEdit with a small eye-icon toggle to
reveal/hide the value.

Port of C#'s Views/RevealablePasswordBox.xaml(.cs). C# needed a dual
PasswordBox/TextBox hack because WPF's PasswordBox has no built-in reveal
mode; Qt's QLineEdit already supports this natively via EchoMode, so this
port is a single QLineEdit whose echoMode toggles between Password and
Normal — no synchronization logic needed.

Used for credential fields where the user occasionally needs to visually
confirm what they typed (Settings' FTP password, the FTP login dialog's
password prompt).
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLineEdit, QToolButton, QHBoxLayout, QWidget

import Utils.theme as T


class RevealablePasswordEdit(QWidget):
    """Drop-in QLineEdit replacement with a reveal/hide eye toggle."""

    def __init__(self, parent=None):
        super().__init__(parent)

        self._edit = QLineEdit(self)
        self._edit.setEchoMode(QLineEdit.EchoMode.Password)

        self._eye_btn = QToolButton(self)
        self._eye_btn.setText("\U0001F441")  # 👁
        self._eye_btn.setCheckable(True)
        self._eye_btn.setToolTip("Show password")
        self._eye_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._eye_btn.setStyleSheet(f"""
            QToolButton {{
                background: transparent;
                border: none;
                color: {T.TEXT_IDLE};
                font-size: {T.FS_BODY}px;
                padding: 0 4px;
            }}
            QToolButton:checked {{
                color: {T.TEXT};
            }}
        """)
        self._eye_btn.toggled.connect(self._on_toggled)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._edit, 1)
        layout.addWidget(self._eye_btn, 0)

        self.setFocusProxy(self._edit)

    def _on_toggled(self, revealed: bool):
        self._edit.setEchoMode(
            QLineEdit.EchoMode.Normal if revealed else QLineEdit.EchoMode.Password
        )
        self._eye_btn.setToolTip("Hide password" if revealed else "Show password")

    # ── PasswordBox-like surface (text() and setText(), not Password) ──────
    def text(self) -> str:
        return self._edit.text()

    def setText(self, value: str):
        self._edit.setText(value or "")

    def clear(self):
        self._edit.clear()

    def setPlaceholderText(self, text: str):
        self._edit.setPlaceholderText(text)

    def setFocus(self):
        self._edit.setFocus()

    @property
    def edit(self) -> QLineEdit:
        """Escape hatch for callers that need the raw QLineEdit (e.g. to
        connect textChanged/returnPressed)."""
        return self._edit
