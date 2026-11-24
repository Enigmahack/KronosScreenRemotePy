"""Dump reply check interval: setting persists, clamps 1-250 ms, reaches the live collector, and is in the Connection tab."""
import os, sys, tempfile
os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_poll_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unittest.mock import MagicMock
from PySide6.QtWidgets import QApplication
app = QApplication([])
from Core.sysex_service import SysExService
from Models.app_settings import AppSettings
import Models.storage as storage
from Tools.sysex_dump_collector import SysExDumpCollector

fails = []
def check(n, c):
    if not c: fails.append(n)

check("default-5ms", AppSettings().sysex_reply_poll_ms == 5 and SysExDumpCollector(MagicMock()).poll_s == 0.005)
svc = SysExService()
svc.set_reply_poll_ms(0); check("clamp-low", svc._reply_poll_s == 0.001)
svc.set_reply_poll_ms(9999); check("clamp-high", svc._reply_poll_s == 0.25)
svc._dump = SysExDumpCollector(MagicMock())
svc.set_reply_poll_ms(50); check("live-collector", svc._dump.poll_s == 0.05)

s = AppSettings(); s.sysex_reply_poll_ms = 40
storage.save_settings(s); check("persists", storage.load_settings().sysex_reply_poll_ms == 40)

from Views.settings_window import SettingsWindow
w = SettingsWindow(AppSettings())
tip = w._reply_poll_spin.toolTip()
check("tooltip", tip == "Recommended safe value is 50ms, though will vary on different connection types. Faster connections can go lower.")
check("range", (w._reply_poll_spin.minimum(), w._reply_poll_spin.maximum()) == (1, 250))
if fails:
    print("FAIL:", fails); sys.exit(1)
print("reply poll setting tests passed")
