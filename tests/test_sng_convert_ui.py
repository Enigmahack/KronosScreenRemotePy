"""Librarian Songs menu: 'Convert a .SNG file to the other instrument...' and the Song Library entry, real window code."""
import glob
import os
import sys
import tempfile
from unittest.mock import MagicMock

os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_sngconvui_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

app = QApplication([])

from Data.sng_file import MODEL_KRONOS, MODEL_NAUTILUS, read_sng
from Models.app_settings import AppSettings
import Views.librarian_shell_window as L
from fixture_paths import SNG_EXAMPLES

kr = sorted(glob.glob(os.path.join(SNG_EXAMPLES, "Kronos", "*.SNG")))
if not kr:
    print("SKIP: fixtures not found in", SNG_EXAMPLES)
    print("ALL PASS")
    sys.exit(0)

fails = []


def check(name, cond):
    if not cond:
        fails.append(name)


class Fake:
    can_dump = True
    is_nautilus = False

    def cached_bank_names(self, *a, **k): return {}
    def sync_names(self, **k): return None
    def __getattr__(self, name): return MagicMock()


w = L.LibrarianShellWindow("1.2.3.4", Fake(), parent=None, settings=AppSettings())
shown = []
QMessageBox.exec = lambda self: shown.append((self.text(), self.detailedText())) or 0
QMessageBox.warning = staticmethod(lambda *a, **k: shown.append((a[2], "")))
out = os.path.join(tempfile.mkdtemp(), "converted.SNG")
QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (kr[0], ""))
QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (out, ""))
before = open(kr[0], "rb").read()
w._convert_sng_file()
orig = read_sng(before)
conv = read_sng(open(out, "rb").read())
check("converted-for-the-other-instrument", orig.model == MODEL_KRONOS and conv.model == MODEL_NAUTILUS)
check("all-songs-kept", [s.name for s in conv.songs] == [s.name for s in orig.songs])
check("events-untouched", [s.tracks for s in conv.songs] == [s.tracks for s in orig.songs])
check("source-untouched", open(kr[0], "rb").read() == before)
check("reported", shown and "Nautilus" in shown[-1][0] and shown[-1][1])

# a file that is not a .SNG is refused, nothing written
bad = os.path.join(tempfile.mkdtemp(), "bad.SNG")
open(bad, "wb").write(b"not a song")
QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (bad, ""))
out2 = os.path.join(os.path.dirname(bad), "never.SNG")
QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (out2, ""))
w._convert_sng_file()
check("garbage-refused", not os.path.exists(out2))

# the Song Library window opens from the menu action and is reused
w._open_song_library()
first = w._song_library_win
w._open_song_library()
check("library-window-reused", w._song_library_win is first and first.isVisible())
first.close()
w._closed = True

if fails:
    print("FAIL:", sorted(set(fails)))
    sys.exit(1)
print("ALL PASS")
