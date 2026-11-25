"""File dialogs that start where the user last picked something.

Every Open / Save / folder dialog goes through here, so they share one remembered folder (`AppSettings.last_file_dir`,
persisted). `bind` is called once by MainWindow with the live settings object. A dialog given a real starting path keeps
it; one given nothing or a bare file name (a suggested name) starts in the remembered folder instead."""
from __future__ import annotations

import os
from typing import Optional

from PySide6.QtWidgets import QFileDialog

_settings = None


def bind(settings) -> None:
    global _settings
    _settings = settings


def _start(initial: str = "") -> str:
    last = getattr(_settings, "last_file_dir", "") if _settings is not None else ""
    if not last or not os.path.isdir(last):
        return initial
    if not initial:
        return last
    return initial if os.path.dirname(initial) else os.path.join(last, initial)


def _remember(path: str, is_dir: bool = False) -> None:
    if _settings is None or not path:
        return
    folder = path if is_dir else os.path.dirname(path)
    if folder and folder != _settings.last_file_dir:
        _settings.last_file_dir = folder
        import Models.storage as storage
        storage.save_settings_async(_settings)


def open_file(parent, title: str, initial: str = "", filters: str = ""):
    path, flt = QFileDialog.getOpenFileName(parent, title, _start(initial), filters)
    _remember(path)
    return path, flt


def open_files(parent, title: str, initial: str = "", filters: str = ""):
    paths, flt = QFileDialog.getOpenFileNames(parent, title, _start(initial), filters)
    _remember(paths[0] if paths else "")
    return paths, flt


def save_file(parent, title: str, initial: str = "", filters: str = ""):
    path, flt = QFileDialog.getSaveFileName(parent, title, _start(initial), filters)
    _remember(path)
    return path, flt


def open_dir(parent, title: str, initial: str = "") -> str:
    path = QFileDialog.getExistingDirectory(parent, title, _start(initial))
    _remember(path, is_dir=True)
    return path
