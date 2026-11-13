"""MainWindow settings saves: debounced, written off the GUI thread from a GUI-thread deep copy,
newest wins; import flushes (a pending save never lands on an import); exit saves blocking."""
import os, sys, tempfile, time, threading
os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_settings_async_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unittest.mock import Mock, patch
from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])

import Views.main_window as ui
from Models.app_settings import AppSettings

fails = []
def check(name, cond):
    if not cond: fails.append(name)
    print(("ok   " if cond else "FAIL ") + name)


def pump(sec):
    end = time.monotonic() + sec
    while time.monotonic() < end:
        app.processEvents(); time.sleep(0.01)


saved = []      # (thread, object, max_fps)
def fake_save(s):
    time.sleep(0.2)                          # an SMB-slow write
    saved.append((threading.current_thread(), s, s.max_fps))

with patch.object(ui.Models.storage, "save_settings", fake_save), patch.object(ui, "_setup_logging"):
    w = ui.MainWindow(AppSettings(midi_monitor_enabled=False))
    w._ctrl = Mock()
    pump(0.5); ui.Models.storage.flush_settings(5); saved.clear()

    for fps in (3, 4, 5):                    # a burst of changes
        w._settings.max_fps = fps
        t0 = time.monotonic(); w._save_settings_soon(); dt = time.monotonic() - t0
    check(f"save request returns at once ({dt*1000:.0f} ms)", dt < 0.05)
    pump(0.6); ui.Models.storage.flush_settings(5)
    check("a burst is written once", len(saved) == 1)
    check("written off the GUI thread", saved and saved[0][0] is not threading.main_thread())
    check("from a copy, not the live object", saved and saved[0][1] is not w._settings and saved[0][2] == 5)

    # Import: the pending save is dropped and waited for, so it can't overwrite the import.
    saved.clear()
    w._settings.max_fps = 7; w._save_settings_soon()
    with patch.object(ui.QFileDialog, "getOpenFileName", return_value=("x.json", "")), \
            patch.object(ui.Models.storage, "import_settings",
                         side_effect=lambda p: (saved.append(("import",)), AppSettings(max_fps=9))[1]), \
            patch.object(w, "_apply_settings_side_effects"), patch.object(ui.QMessageBox, "information"):
        w._import_settings()
    pump(0.5); ui.Models.storage.flush_settings(5)
    check("no settings write lands after an import", saved == [("import",)])

    # Exit: blocking save on the GUI thread, after any queued write.
    saved.clear()
    w._settings.max_fps = 11; w._save_settings_soon()
    w._sysex_service = Mock()
    w._shutting_down = True
    with patch.object(w, "close"):
        w._do_shutdown()
    check("exit saves synchronously, once, with the latest values",
          len(saved) == 1 and saved[0][0] is threading.main_thread() and saved[0][2] == 11)
    check("exit flushes the SysEx service", w._sysex_service.flush.called)
    pump(0.5); ui.Models.storage.flush_settings(5)
    check("nothing written after the exit save", len(saved) == 1)
    w._receiver = None
    w.close(); w.deleteLater(); app.processEvents()

print("ALL PASS" if not fails else "FAILED: " + ", ".join(fails))
sys.exit(1 if fails else 0)
