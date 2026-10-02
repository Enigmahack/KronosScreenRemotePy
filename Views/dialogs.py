"""
Unified dialog interface - centralized access to all application dialogs.

This module provides a clean interface to access all dialogs across the
application, ensuring consistent behavior and styling.
"""
from __future__ import annotations

# Base classes
from Views.dialog_base import (
    BaseDialog,
    InputDialog,
    PromptDialog,
    MessageBox,
    DialogResult,
)

# Connection dialogs
from Views.connection_dialogs import (
    ConnectionFailedDialog,
)

# Property and info dialogs
from Views.property_dialogs import (
    ObjectInfoDialog,
    FtpPropertiesDialog,
)

# Testing dialogs
from Views.testing_dialogs import (
    InputTesterWindow,
    ButtonInjectorWindow,
)

__all__ = [
    # Base classes
    'BaseDialog',
    'InputDialog',
    'PromptDialog',
    'MessageBox',
    'DialogResult',
    # Connection dialogs
    'ConnectionFailedDialog',
    # Property dialogs
    'ObjectInfoDialog',
    'FtpPropertiesDialog',
    # Testing dialogs
    'InputTesterWindow',
    'ButtonInjectorWindow',
]
