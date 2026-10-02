"""Live, READ-ONLY: the real MainWindow connected to a unit — Monitor MIDI toggled live, settings pushed
into the SysExService, hardware-slider mirror (injected via the service signal, nothing is sent to the
unit), faded menu/footer state, and the OUT CH persistence hook.  usage: live_midi_window.py <host> [--user U --pass P]"""
import os, sys, time, json, tempfile, argparse
ap = argparse.ArgumentParser(); ap.add_argument("host"); ap.add_argument("--user"); ap.add_argument("--pass", dest="pw")
a = ap.parse_args()
os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_livemw_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)
from PySide6.QtWidgets import QApplication
app = QApplication([])
from Models.app_settings import AppSettings
from Views.main_window import MainWindow

creds = json.load(open(os.path.join(root, "settings.json"), encoding="utf-8"))
s = AppSettings(); s.kronos_host = a.host
s.ftp_username = a.user or creds["ftp_username"]; s.ftp_password = a.pw or creds["ftp_password"]
s.value_slider_cc = 21; s.pull_names_on_change = True; s.sysex_poll_on_changes = False
w = MainWindow(s); svc = w._sysex_service


def pump(cond, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond(): return True
        time.sleep(0.05)
    return False


w._host = a.host; w._connect_async()
assert pump(lambda: w._receiver is not None and w._frame_w._is_connected), "no connect"
assert pump(lambda: svc.bridge is not None, 15), "MIDI bridge never started"
pump(lambda: svc.is_available, 8)
print("connected; MIDI bridge up; SysEx available:", svc.is_available, "| is_nautilus:", w._is_nautilus)
assert svc.value_slider_cc == 21 and svc.pull_names_on_change and svc._poll_on_changes is False, "settings not pushed to service"
print("settings reached the service: cc=%d pull_names=%s poll_on_changes=%s" % (
    svc.value_slider_cc, svc.pull_names_on_change, svc._poll_on_changes))
pump(lambda: bool(svc.performance_display), 6)
print("footer performance:", repr(w._perf_label.text()), "| service:", repr(svc.performance_display))

# hardware-slider mirror (display only) + drag guard
lp = w._left_panel
svc.value_slider_changed.emit(100); app.processEvents()
assert lp._value == 100 and abs(lp._thumb_top - lp._SLIDER_TRAVEL * 27 / 127.0) < 1e-6
lp._dragging = True; svc.value_slider_changed.emit(10); app.processEvents()
assert lp._value == 100, "must ignore the echo while the user drags"
lp._dragging = False
emitted = []; lp.slider_changed.connect(emitted.append)
svc.value_slider_changed.emit(33); app.processEvents()
assert lp._value == 33 and emitted == [], "following the hardware must not send VSLIDER back"
print("slider mirror OK (follows, ignores drag echo, never echoes back)")

# Monitor MIDI off, live
s.midi_monitor_enabled = False; w._apply_midi_settings()
assert svc.bridge is None and not w._act_sysex_tool.isEnabled() and not w._midi_io.isEnabled()
assert w._midi_io.graphicsEffect().opacity() < 1.0
print("Monitor MIDI OFF: bridge stopped, menu + footer cluster faded/disabled")
# ... and back on
s.midi_monitor_enabled = True; w._apply_midi_settings()
assert pump(lambda: svc.bridge is not None, 15), "did not restart"
assert w._act_sysex_tool.isEnabled() and w._midi_io.isEnabled() and w._midi_io.graphicsEffect().opacity() == 1.0
print("Monitor MIDI ON again: bridge restarted, SysEx available:", svc.is_available)

# OUT CH persistence hook
w._save_midi_output_channel(7)
assert s.midi_output_channel == 7
print("OUT CH saved:", s.midi_output_channel)
w._shutting_down = True
print("DONE")
