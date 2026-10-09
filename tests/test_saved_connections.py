"""Saved Connections (Settings > Connection): storage round-trip, add/edit/remove, field fill,
Cancel semantics, and the fixed daemon ports. Run from the repo root: python tests/test_saved_connections.py
"""
import os
import sys
import tempfile
import pathlib

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_savedconn_")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication, QDialog

import Models.storage
from Models.app_settings import AppSettings
from Views import connection_dialogs
from Views.settings_window import SettingsWindow

app = QApplication.instance() or QApplication([])
fails = []


def check(name, cond):
    if not cond:
        fails.append(name)
        print("FAIL", name)


# ── storage round-trip (+ junk entries are dropped, old files without the key load empty) ──
s = AppSettings()
s.saved_connections = [
    {"name": "Studio Kronos", "host": "192.168.1.2", "username": "u1", "password": "p1", "ftp_port": 21},
    {"name": "Stage Nautilus", "host": "10.0.0.9", "username": "nautilus", "password": "pw", "ftp_port": 2121},
]
Models.storage.save_settings(s)
r = Models.storage.load_settings()
check("roundtrip-count", len(r.saved_connections) == 2)
check("roundtrip-fields", r.saved_connections[1] == s.saved_connections[1])
check("default-empty", AppSettings().saved_connections == [])


# ── window behavior ──
class FakeDialog:
    """Stands in for the modal SavedConnectionDialog so the flow runs headless."""
    next_entry = None

    def __init__(self, seed, is_new, parent=None):
        self.seed, self.is_new = seed, is_new
        self.result_entry = FakeDialog.next_entry

    def exec(self):
        return QDialog.Accepted if self.result_entry else QDialog.Rejected


connection_dialogs.SavedConnectionDialog = FakeDialog

cur = AppSettings()
cur.saved_connections = [dict(c) for c in s.saved_connections]
cur.stream_port, cur.ctrl_port = 9999, 8888          # stale custom ports from an older version
w = SettingsWindow(cur)
check("combo-populated", w._saved_combo.count() == 2 and w._saved_combo.currentIndex() == -1)
check("buttons-disabled-without-selection", not w._saved_edit.isEnabled() and not w._saved_remove.isEnabled())
check("no-port-widgets", not hasattr(w, "_sport_spin") and not hasattr(w, "_cport_spin"))

w._saved_combo.setCurrentIndex(1)                       # choose "Stage Nautilus"
check("select-fills-host", w._host_edit.text() == "10.0.0.9")
check("select-fills-user", w._ftp_user.text() == "nautilus")
check("select-fills-pass", w._ftp_pass.text() == "pw")
check("select-fills-port", w._ftp_port_spin.value() == 2121)

w._host_edit.setText("172.16.0.5")                      # + seeds from the current fields
FakeDialog.next_entry = {"name": "Rehearsal", "host": "172.16.0.5", "username": "x", "password": "y", "ftp_port": 21}
w._on_saved_add()
check("add-appends-and-selects", len(w._saved) == 3 and w._saved_combo.currentIndex() == 2
      and w._saved_combo.currentText() == "Rehearsal")

FakeDialog.next_entry = {"name": "Rehearsal 2", "host": "172.16.0.6", "username": "x", "password": "y", "ftp_port": 22}
w._on_saved_edit()
check("edit-replaces", w._saved[2]["name"] == "Rehearsal 2" and w._host_edit.text() == "172.16.0.6"
      and w._ftp_port_spin.value() == 22)

w._saved_combo.setCurrentIndex(0)
w._on_saved_remove()
check("remove-drops-entry", len(w._saved) == 2 and w._saved[0]["name"] == "Stage Nautilus"
      and w._saved_combo.currentIndex() == -1)

# Cancel: nothing reaches the settings object until the dialog is applied.
check("cancel-leaves-settings", len(cur.saved_connections) == 2 and cur.saved_connections[0]["name"] == "Studio Kronos")

# Apply: entries are written back, and the ports are forced to the daemon defaults.
w._save_to_settings_no_close()
check("apply-writes-entries", [c["name"] for c in cur.saved_connections] == ["Stage Nautilus", "Rehearsal 2"])
check("apply-fixes-ports", (cur.stream_port, cur.ctrl_port) == (7373, 7374))

# The real dialog: empty host rejected, blank name falls back to the host.
real = connection_dialogs.__dict__  # still the fake; import the class fresh from its source module
import importlib
importlib.reload(connection_dialogs)
d = connection_dialogs.SavedConnectionDialog({"name": "", "host": "", "username": "", "password": "", "ftp_port": 21}, True)
d._on_ok()
check("dialog-rejects-empty-host", d.result_entry is None)
d._host.setText("1.2.3.4")
d._on_ok()
check("dialog-name-defaults-to-host", d.result_entry is not None and d.result_entry["name"] == "1.2.3.4"
      and d.result_entry["ftp_port"] == 21)

print("FAIL: " + ", ".join(fails) if fails else "OK")
sys.exit(1 if fails else 0)
