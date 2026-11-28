"""Tools opens one Nautilus-only panel on the existing authenticated session."""
import os
import logging
from pathlib import Path
import sys
import tempfile

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication
from Core.stream_receiver import StreamReceiver
from Models.app_settings import AppSettings
from Views.main_window import MainWindow
from Views.nautilus_perf_surface import NautilusPerfSurface

app = QApplication.instance() or QApplication([])
with tempfile.TemporaryDirectory(prefix="kr_perf_menu_") as data:
    os.environ["KRONOS_DATA_DIR"] = data
    remote = MainWindow(AppSettings())
    remote._host = "127.0.0.1"
    action = remote._act_performance_surface
    assert action in remote._tools_menu.actions()
    assert not action.isEnabled()
    remote.show_performance_surface()
    assert remote._performance_surface is None

    rx = StreamReceiver("127.0.0.1", 7373, False, 15)
    remote._receiver = rx
    remote._is_nautilus = False
    remote._apply_device_family_ui()
    remote.show_performance_surface()
    assert not action.isEnabled() and remote._performance_surface is None
    remote._is_nautilus = True
    remote._apply_device_family_ui()
    remote._daemon_booting = False
    assert action.isEnabled()
    sent = []
    remote._ctrl.send = lambda host, port, command: sent.append(command)
    action.trigger()
    app.processEvents()
    panel = remote._performance_surface
    assert isinstance(panel, NautilusPerfSurface)
    assert panel.isVisible() and panel.parentWidget() is remote
    assert QApplication.instance() is app
    action.trigger()
    assert remote._performance_surface is panel
    panel.buttons["LATCH"].click()
    assert sent[-1] == "BTN 15"
    panel._shift_down()
    remote._is_nautilus = False
    remote._apply_device_family_ui()
    assert sent[-1] == "BTN_UP 19"
    assert not action.isEnabled() and not panel.isVisible()
    count = len(sent)
    panel.buttons["LATCH"].click()
    assert len(sent) == count

    remote._is_nautilus = True
    remote._apply_device_family_ui()
    action.trigger()
    panel._shift_down()
    remote._disconnect(quiet=True)
    assert sent[-1] == "BTN_UP 19"
    assert not action.isEnabled() and not panel.isVisible()
    remote.show_performance_surface()
    assert not panel.isVisible()
    remote._receiver = StreamReceiver("127.0.0.1", 7373, False, 15)
    remote._daemon_booting = False
    remote._apply_device_family_ui()
    action.trigger()
    panel._shift_down()
    remote._shutting_down = True
    remote._do_shutdown()
    assert sent[-1] == "BTN_UP 19" and not panel.isVisible()
    app.processEvents()
    logging.shutdown()
print("Nautilus performance menu OK")
