"""File Manager default start folders (Settings > Tools > File Manager, and the folder right-click
"Set as Default Start Folder"). Runs the real window against tests/ftp_fake.py. Headless."""
import os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("KRONOS_DATA_DIR", tempfile.mkdtemp(prefix="kr_data_"))
import logging; logging.disable(logging.CRITICAL)

from PySide6.QtWidgets import QApplication

from ftp_fake import FakeFtpWorker, FtpFakeServer

app = QApplication.instance() or QApplication([])
import Models.storage as storage
import Tools.file_manager as fm
from Models.app_settings import AppSettings
from Views.settings_window import SettingsWindow

fails = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def pump(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


def open_window(settings, server):
    fm._FtpWorker = FakeFtpWorker.bound_to(server)
    win = fm.FileManagerWindow("127.0.0.1", 21, "u", "p", settings=settings)
    pump(lambda: win._remote_path_box.text() != "", 3.0)
    return win


check("normalize: relative, trailing slash", fm.normalize_remote_folder("SSD1/Samples/") == "/SSD1/Samples")
check("normalize: empty is root", fm.normalize_remote_folder("  ") == "/")
check("normalize: backslashes", fm.normalize_remote_folder("\\SSD1\\X") == "/SSD1/X")

local_tmp = tempfile.mkdtemp(prefix="kr_fm_local_")
server = FtpFakeServer({"/SSD1/Samples/a.KSC": b"x", "/SSD1/samplesfile": b"f"})

# 1. no settings / empty settings: "/" and Desktop-or-home, as before
win = open_window(None, server)
check("no settings: remote opens at /", win._remote_path == "/")
check("no settings: local opens at Desktop/home", win._local_path == fm._default_local_folder())
win.close(); win.deleteLater()

# 2. configured folders are opened
s = AppSettings()
s.file_manager_default_local_folder = local_tmp
s.file_manager_default_remote_folder = "/SSD1/Samples"
win = open_window(s, server)
check("remote opens at the configured folder", win._remote_path == "/SSD1/Samples"
      and win._remote_path_box.text() == "/SSD1/Samples")
check("local opens at the configured folder", win._local_path == local_tmp)
win.close(); win.deleteLater()

# 3. missing folders fall back (LIST of a missing folder is an empty success, so this needs the parent check)
s.file_manager_default_local_folder = os.path.join(local_tmp, "gone")
s.file_manager_default_remote_folder = "/SSD1/Gone"
win = open_window(s, server)
check("missing remote folder falls back to /", win._remote_path == "/")
check("missing local folder falls back", win._local_path == fm._default_local_folder())
win.close(); win.deleteLater()

# 4. a FILE with the configured name is not a folder; a case variant is a different folder
for bad in ("/SSD1/samplesfile", "/SSD1/samples"):
    s.file_manager_default_remote_folder = bad
    win = open_window(s, server)
    check(f"{bad}: not opened", win._remote_path == "/")
    win.close(); win.deleteLater()

# 5. right-click "Set as Default Start Folder" writes the live settings and persists them
s = AppSettings()
win = open_window(s, server)
win._set_default_start_folder("/SSD1/Samples/", True)
win._set_default_start_folder(local_tmp, False)
check("right-click: remote folder stored normalized", s.file_manager_default_remote_folder == "/SSD1/Samples")
check("right-click: local folder stored", s.file_manager_default_local_folder == local_tmp)
storage.flush_settings()
loaded = storage.load_settings()
check("right-click: persisted", loaded.file_manager_default_remote_folder == "/SSD1/Samples"
      and loaded.file_manager_default_local_folder == local_tmp)
win.close(); win.deleteLater()

# 6. Settings page round trip
s = AppSettings()
s.file_manager_default_local_folder = local_tmp
s.file_manager_default_remote_folder = "/SSD2"
dlg = SettingsWindow(s)
check("settings page loads values", dlg._fm_local.text() == local_tmp and dlg._fm_remote.text() == "/SSD2")
dlg._fm_remote.setText("SSD1/Samples/")
dlg._fm_local.setText("")
dlg._save()
check("settings page saves normalized remote", s.file_manager_default_remote_folder == "/SSD1/Samples")
check("settings page saves empty local", s.file_manager_default_local_folder == "")
dlg._fm_remote.setText("")
dlg._save()
check("settings page: empty remote stays empty", s.file_manager_default_remote_folder == "")
dlg.deleteLater()

print(f"\n{len(fails)} failure(s)")
sys.exit(1 if fails else 0)
