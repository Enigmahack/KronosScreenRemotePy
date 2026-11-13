"""Views.song_library_window driven headless against the real library store and a fake instrument."""
import glob
import os
import sys
import tempfile
import time
from types import SimpleNamespace

os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_songlibui_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog, QMessageBox

app = QApplication([])

import pathlib

from Data.sng_file import MODEL_KRONOS, MODEL_NAUTILUS, read_sng
from Data.song_library import SongLibrary
from Data.song_model import SongData
from Views.song_library_window import SongLibraryWindow
from fixture_paths import SNG_EXAMPLES

fails = []


def check(name, cond):
    if not cond:
        fails.append(name)


def wait(cond, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


kr = sorted(glob.glob(os.path.join(SNG_EXAMPLES, "Kronos", "*.SNG")))
na = sorted(glob.glob(os.path.join(SNG_EXAMPLES, "Nautilus", "*.SNG")))
if not kr or not na:
    print("SKIP real files: fixtures not found in", SNG_EXAMPLES)
    print("ALL PASS")
    sys.exit(0)

shown = []
QMessageBox.exec = lambda self: shown.append((self.text(), self.detailedText())) or 0
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)

nsng = read_sng(open(na[1], "rb").read())          # LTE-KeySolo
whole = {i: SongData(MODEL_NAUTILUS, s.timbre_set, s.control).whole_body() for i, s in enumerate(nsng.songs)}


class FakeInstrument:
    can_dump = True

    def dump_object_parsed(self, obj, bank, index, **kw):
        return SimpleNamespace(version=4, body=whole[index]) if obj == 0x18 and index in whole else None


sent = []
lib = SongLibrary(pathlib.Path(tempfile.mkdtemp(prefix="kr_songlibui_store_")))
w = SongLibraryWindow(lib, lambda: FakeInstrument(), sent.extend)
w.show()

# import a Kronos .SNG
QFileDialog.getOpenFileNames = staticmethod(lambda *a, **k: ([kr[0]], ""))
w.import_file()
check("import-file-threaded", w._busy)          # the read runs off the GUI thread
check("import-file-done", wait(lambda: not w._busy))
check("import-file", w._table.rowCount() == 6 and any("plays audio" not in w._table.item(r, 6).text() and
      w._table.item(r, 6).text() == "Yes" for r in range(6)))
check("columns", {w._table.item(r, 1).text() for r in range(6)} == {"Kronos"})

# import from the instrument: 13 songs, the last is the empty placeholder "NEW SONG 012"
w.import_instrument()
check("instrument-import", wait(lambda: not w._busy and w._table.rowCount() == 6 + 12))
check("placeholder-skipped", not any(w._table.item(r, 0).text().startswith("NEW SONG") for r in range(w._table.rowCount())))
check("sysex-only-marked", sum(1 for r in range(w._table.rowCount()) if w._table.item(r, 6).text() == "No (SysEx only)") == 12)

# select one Kronos song and export it for the Nautilus
w._table.clearSelection()
row = next(r for r in range(w._table.rowCount()) if w._table.item(r, 0).text() == "CHRISTMAS CANON")
w._table.selectRow(row)
out = os.path.join(tempfile.mkdtemp(), "out.SNG")
QInputDialog.getItem = staticmethod(lambda *a, **k: ("Nautilus", True))
QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (out, ""))
w.export_sng()
check("export-done", wait(lambda: not w._busy))
conv = read_sng(open(out, "rb").read())
check("export", conv.model == MODEL_NAUTILUS and [s.name for s in conv.songs] == ["CHRISTMAS CANON"]
      and len(conv.songs[0].timbre_set) == 5640)

# a SysEx-only song cannot be exported
w._table.clearSelection()
row = next(r for r in range(w._table.rowCount()) if w._table.item(r, 6).text() == "No (SysEx only)")
w._table.selectRow(row)
shown.clear()
w.export_sng()
wait(lambda: not w._busy)
check("sysex-only-export-refused", shown and "no MIDI data" in shown[-1][1])

# send Timbre Sets to the Nautilus hands ConvertItems to the callback
w.send_timbre_sets()
wait(lambda: not w._busy)
check("send", len(sent) == 1 and sent[0].obj_type == 0x02 and sent[0].native)

# remove
n = w._table.rowCount()
w.remove_selected()
check("remove", w._table.rowCount() == n - 1)

# Esc (reject -> done, no closeEvent) must stop an instrument read instead of letting it run on hidden
class SlowInstrument(FakeInstrument):
    def dump_object_parsed(self, obj, bank, index, **kw):
        time.sleep(0.05)
        return super().dump_object_parsed(obj, bank, index, **kw)


w2 = SongLibraryWindow(lib, lambda: SlowInstrument(), sent.extend)
w2.show()
shown.clear()
w2.import_instrument()
w2.reject()
check("esc-cancels-pull", w2._cancel.is_set())
check("esc-pull-finishes", wait(lambda: not w2._busy))
check("esc-no-popup", not shown and "Cancelled" in w2._status.text())
w2.deleteLater()

# the parent (the Librarian) is destroyed while a read runs: the worker stops promptly
from PySide6.QtWidgets import QDialog


class CountingInstrument(SlowInstrument):
    calls = 0

    def dump_object_parsed(self, obj, bank, index, **kw):
        CountingInstrument.calls += 1
        return super().dump_object_parsed(obj, bank, index, **kw)


parent = QDialog()
w3 = SongLibraryWindow(lib, lambda: CountingInstrument(), sent.extend, parent)
w3._cancel.clear()
w3.import_instrument()
time.sleep(0.12)
import shiboken6
shiboken6.delete(parent)                          # the C++ windows go, as when the Librarian is deleted
n_at_close = CountingInstrument.calls
time.sleep(0.4)
check("parent-destroyed-stops-read", CountingInstrument.calls <= n_at_close + 1)
w.close()

if fails:
    print("FAIL:", sorted(set(fails)))
    sys.exit(1)
print("ALL PASS")
