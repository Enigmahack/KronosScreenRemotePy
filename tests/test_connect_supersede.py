"""Picking another connection while one is still connecting supersedes it: the old attempt is aborted at once
(socket closed, result discarded) and the new one starts. Headless; loopback sockets only."""
import os
import pathlib
import socket
import sys
import tempfile
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_data_")

from PySide6.QtWidgets import QApplication
from Models.app_settings import AppSettings
from Views import main_window as ui

app = QApplication.instance() or QApplication([])
fails = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def pump(sec, until=lambda: False):
    end = time.monotonic() + sec
    while time.monotonic() < end and not until():
        app.processEvents()
        time.sleep(0.01)


# A "black hole" daemon: accepts the TCP connection, then never answers the hello.
srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(5)
conns = []
threading.Thread(target=lambda: [conns.append(srv.accept()) for _ in range(3)], daemon=True).start()
port = srv.getsockname()[1]

s = AppSettings(midi_monitor_enabled=False)
s.ftp_username, s.ftp_password = "u", "p"
w = ui.MainWindow(s)
w._stream_port = port
w._show_connection_failed_dialog = lambda msg: None
shown = []

w._host = "127.0.0.1"
w._connect_async()
first = w._pending_rx
pump(0.3)
check("first attempt in flight", w._connecting and first is not None)

t0 = time.monotonic()
w._connect_to_recent("127.0.0.2")          # superseding request while connecting
check("second request not ignored", w._pending_rx is not None and w._pending_rx is not first)
check("host switched", w._host == "127.0.0.2")
pump(2.0, until=lambda: first._connecting_sock is None and not first.isRunning())
check("first attempt aborted promptly (not 10 s timeout)", time.monotonic() - t0 < 5 and first._stop)
check("only the newest attempt is live", w._connecting)
w._disconnect()
pump(0.5)
check("clean after disconnect", not w._connecting and w._pending_rx is None)

w._shutting_down = True
print("FAILED: %s" % fails if fails else "ALL PASS")
sys.stdout.flush(); os._exit(1 if fails else 0)
