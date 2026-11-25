"""Views/file_dialogs: dialogs start in the last folder a file was picked from; real start paths are kept."""
import os, sys, tempfile
from unittest.mock import patch
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QFileDialog
from Models.app_settings import AppSettings
from Views import file_dialogs as fd

app = QApplication.instance() or QApplication([])
fails = []
def check(n, c):
    if not c: fails.append(n)

d1, d2 = tempfile.mkdtemp(), tempfile.mkdtemp()
s = AppSettings(); fd.bind(s)
seen = []
with patch("Models.storage.save_settings_async") as saved, \
     patch.object(QFileDialog, "getOpenFileName", staticmethod(lambda p, t, start, f: (seen.append(start) or os.path.join(d1, "a.pcg"), ""))), \
     patch.object(QFileDialog, "getSaveFileName", staticmethod(lambda p, t, start, f: (seen.append(start) or "", ""))), \
     patch.object(QFileDialog, "getExistingDirectory", staticmethod(lambda p, t, start: (seen.append(start) or d2))):
    fd.open_file(None, "t", "", "")
    check("first-dialog-has-no-start", seen[-1] == "")
    check("pick-remembered-and-saved", s.last_file_dir == d1 and saved.call_count == 1)
    fd.open_file(None, "t", "", "")
    check("next-dialog-starts-there", seen[-1] == d1 and saved.call_count == 1)
    fd.save_file(None, "t", "report.txt", "")
    check("bare-suggested-name-joins-last-dir", seen[-1] == os.path.join(d1, "report.txt"))
    fd.save_file(None, "t", os.path.join(d2, "x.png"), "")
    check("explicit-start-kept", seen[-1] == os.path.join(d2, "x.png"))
    fd.open_dir(None, "t", "")
    check("folder-pick-remembered", s.last_file_dir == d2 and seen[-1] == d1)
    s.last_file_dir = os.path.join(d1, "gone")
    fd.open_file(None, "t", "", "")
    check("missing-last-dir-ignored", seen[-1] == "")
print("FAILED:" if fails else "ALL PASS", fails or "")
sys.exit(1 if fails else 0)
