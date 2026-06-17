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
import sys

import pathlib

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QInputDialog

import storage
from app_settings import AppSettings
from main_window import MainWindow
# Trigger lazy Qt key-name build after Qt is initialised
import models


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

    _icons = pathlib.Path(__file__).parent / "Resources" / "Icons"
    _ico = _icons / "AppIcon.ico"
    _png = _icons / "AppIcon.png"
    app_icon = QIcon(str(_ico if _ico.exists() else _png))
    app.setWindowIcon(app_icon)

    app.setStyleSheet(
        "QToolTip { color: #CCCCCC; background-color: #2A2A2A;"
        " border: 1px solid #555; padding: 3px; }"
    )

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

    # Auto-connect if host is already known (bypasses the prompt in _trigger_reconnect)
    if settings.kronos_host:
        win._connect_async()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
