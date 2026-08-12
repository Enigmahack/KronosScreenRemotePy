#!/usr/bin/env python3
"""
Kronos ScreenRemote — Python/PySide6 cross-platform client.

Usage:
    python main.py [host] [--port PORT] [--pull] [--fps N]

If no host is given, the last saved host is used; if none is saved,
a connect dialog is shown on startup.

Dependencies:
    pip install PySide6
"""
from __future__ import annotations
import argparse
import logging
import sys
import threading
import traceback

import pathlib

from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

import storage
import theme as T
from app_settings import AppSettings
from main_window import MainWindow
# Trigger lazy Qt key-name build after Qt is initialised
import models

log = logging.getLogger(__name__)


def _install_crash_guard():
    """Report unhandled exceptions instead of dying on them.

    Python exceptions raised inside a Qt slot do not unwind into any caller —
    they reach sys.excepthook, and depending on the PySide6 build that either
    prints and continues in an undefined state or aborts the process outright.
    Either way the user sees the application vanish. The realistic trigger here
    is I/O against a data directory on a network share that has gone read-only
    or away (see local_library_store.LocalLibraryWriteError); the individual
    operations handle that themselves, and this is the backstop for whatever
    they miss.

    KeyboardInterrupt/SystemExit still go to the default hook — those are
    deliberate exits, not faults."""

    def excepthook(exc_type, exc, tb):
        if issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            sys.__excepthook__(exc_type, exc, tb)
            return
        detail = "".join(traceback.format_exception(exc_type, exc, tb))
        log.error("unhandled exception:\n%s", detail)
        try:
            if QApplication.instance() is not None:
                box = QMessageBox(QMessageBox.Icon.Critical, "Unexpected error",
                                  f"{exc_type.__name__}: {exc}\n\n"
                                  "The application is still running, but this operation "
                                  "did not complete. Details are in the log.")
                box.setDetailedText(detail)
                box.exec()
        except Exception:                     # noqa: BLE001 - the reporter must never throw
            pass

    sys.excepthook = excepthook

    def thread_excepthook(args):
        if issubclass(args.exc_type, SystemExit):
            return
        log.error("unhandled exception in thread %s:\n%s",
                  getattr(args.thread, "name", "?"),
                  "".join(traceback.format_exception(args.exc_type, args.exc_value,
                                                     args.exc_traceback)))

    threading.excepthook = thread_excepthook


def _parse_args(settings: AppSettings) -> AppSettings:
    p = argparse.ArgumentParser(description="Kronos ScreenRemote")
    p.add_argument("host", nargs="?", default="", help="Kronos host/IP")
    p.add_argument("--port",  type=int, default=0, help="Stream port (default 7373)")
    p.add_argument("--ctrl",  type=int, default=0, help="Control port (default 7374)")
    p.add_argument("--pull",  action="store_true", help="Use pull mode")
    p.add_argument("--fps",   type=int, default=0, help="Max FPS (1–15)")
    args = p.parse_args()

    if args.host:
        settings.kronos_host = args.host
    if args.port:
        settings.stream_port = args.port
    if args.ctrl:
        settings.ctrl_port = args.ctrl
    if args.pull:
        settings.pull_mode = True
    if args.fps:
        settings.max_fps = max(1, min(15, args.fps))
    return settings


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("KronosScreenRemote")
    app.setOrganizationName("KronosHacking")
    _install_crash_guard()

    _icons = pathlib.Path(__file__).parent / "Resources" / "Icons"
    _ico = _icons / "AppIcon.ico"
    _png = _icons / "AppIcon.png"
    app_icon = QIcon(str(_ico if _ico.exists() else _png))
    app.setWindowIcon(app_icon)

    # Consistent base UI font (per-widget stylesheets override where needed,
    # e.g. monospace readouts). Then the app-wide token stylesheet.
    app.setFont(QFont(T.FONT_UI_FAMILY, 9))
    app.setStyleSheet(T.app_stylesheet())

    # Build Qt key name cache now that Qt is up
    models._build_key_names()

    # Load settings then apply CLI overrides
    settings = storage.load_settings()
    settings = _parse_args(settings)

    # Prompt for host if none configured
    if not settings.kronos_host:
        text, ok = QInputDialog.getText(
            None, "Kronos ScreenRemote",
            "Enter Kronos host/IP address:",
            text="192.168.100.15")
        if ok and text.strip():
            settings.kronos_host = text.strip()
            storage.save_settings(settings)
        elif not ok:
            sys.exit(0)

    win = MainWindow(settings)
    win.show()

    # If the data directory had to move (script dir on a read-only/disconnected
    # share), say so — otherwise the settings and the whole local library silently
    # look empty and the user has no way to know why.
    if storage.data_dir_fallback_reason:
        win._notify(storage.data_dir_fallback_reason, is_error=True)

    # Auto-connect if host is already known (bypasses the prompt in _trigger_reconnect)
    if settings.kronos_host:
        win._connect_async()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
