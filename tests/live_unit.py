"""Live end-to-end check against a real unit: connect, device family, UI state, unit calibration
load, and (opt-in) a CAL_SET round-trip that ALWAYS restores the original calibration.
usage: live_unit.py <host> [--user U --pass P] [--caltest]"""
import os, sys, time, json, tempfile, argparse
ap = argparse.ArgumentParser()
ap.add_argument("host"); ap.add_argument("--user"); ap.add_argument("--pass", dest="pw")
ap.add_argument("--caltest", action="store_true")
a = ap.parse_args()
host = a.host
os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_live_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtWidgets import QApplication
app = QApplication([])
from Models.app_settings import AppSettings
import Models.cal_text as cal_text
from Views.main_window import MainWindow

creds = json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "settings.json"), encoding="utf-8"))
s = AppSettings()
s.kronos_host = host
s.ftp_username = a.user or creds["ftp_username"]
s.ftp_password = a.pw or creds["ftp_password"]
w = MainWindow(s)
fw = w._frame_w


def pump(cond, timeout=25):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.05)
    return False


w._host = host
w._connect_async()
if not pump(lambda: w._receiver is not None and fw._is_connected, 20):
    print("NOT CONNECTED. status:", w._status_label.text(), "| notes:", w._notify_msgs[-3:]); sys.exit(2)
pump(lambda: False, 3)   # let MODEL + CAL_GET land
print("connected:", host, "| is_nautilus:", w._is_nautilus)
print("Mode Select items visible: kronos", [a_.text() for a_ in w._act_modes if a_.isVisible()][:2],
      "| nautilus", [a_.text() for a_ in w._act_naut_modes if a_.isVisible()])
print("bank menu enabled:", w._bank_menu.menuAction().isEnabled(),
      "| ctrl surface skin:", w._ctrl_surface._active_skin, "| frame:", fw._fw, "x", fw._fh)
raw = w._ctrl.query(host, w._ctrl_port, "CAL_GET", timeout_ms=3000)
print("CAL_GET raw:", repr(raw)[:140], "| app mesh grid:", fw._cal_mesh.cols, "| dots:", len(fw._cal_bias_dots))

if a.caltest:
    assert raw is not None, "CAL_GET unanswered while connected"
    had_cal = raw.startswith("CAL ") and raw != "CAL NONE"
    backup = raw[4:] if had_cal else None
    with open(os.path.join(tempfile.gettempdir(), f"cal_backup_{host}.txt"), "w") as f:
        f.write(raw)
    print("backup written; had_cal =", had_cal)
    try:
        fw._cal_mesh.set_offset(1, 1, 3, -2)
        notes_before = w._notify_count
        w._save_calibration(); pump(lambda: False, 2.5)
        after = w._ctrl.query(host, w._ctrl_port, "CAL_GET", timeout_ms=3000)
        print("after save, CAL_GET:", repr(after)[:140])
        assert after and "1,1,3,-2" in after, "unit did not store the edit"
        assert w._notify_count == notes_before, "save reported a failure"
        # failure path against the real unit: oversize text must be refused client-side
        fw._cal_bias_dots.extend(__import__("Models.models", fromlist=["CalBiasDot"]).CalBiasDot(i % 800, i % 400) for i in range(1200))
        n = w._notify_count
        w._save_calibration(); pump(lambda: False, 2)
        assert w._notify_count == n + 1 and "too many" in w._notify_msgs[-1], w._notify_msgs[-1:]
        print("oversize save refused with message:", w._notify_msgs[-1])
    finally:
        if had_cal:
            r = w._ctrl.query(host, w._ctrl_port, f"CAL_SET {backup}", timeout_ms=3000)
            ok = r == "OK" and w._ctrl.query(host, w._ctrl_port, "CAL_GET", timeout_ms=3000) == raw
        else:
            import ftplib
            ftp = ftplib.FTP(); ftp.connect(host, 21, 8); ftp.login(s.ftp_username, s.ftp_password)
            ftp.cwd("SSD1/ScreenRemote"); ftp.delete("calibration.txt"); ftp.quit()
            ok = w._ctrl.query(host, w._ctrl_port, "CAL_GET", timeout_ms=3000) == "CAL NONE"
        print("RESTORED EXACTLY:", ok)
        assert ok, "RESTORE FAILED - see cal_backup file in temp dir"
    print("CAL round-trip OK")
w._shutting_down = True
print("DONE")
