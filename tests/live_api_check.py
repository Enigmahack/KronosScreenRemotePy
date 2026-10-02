"""Read-only live API check: handshake via the real StreamReceiver, MODEL parse, SYSINFO display fields, and BTN code
validation (BTN 999 must be rejected, never pressed). usage: live_api_check.py <host> [--user U --pass P]"""
import argparse, json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_liveapi_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication
app = QApplication([])
import Core.ctrl_client as CC
from Core.device_family import ModelInfo
from Core.stream_receiver import StreamReceiver
from Views.perf_window import unit_summary, cpu_summary
from Core.device_family import parse_kv

ap = argparse.ArgumentParser()
ap.add_argument("host"); ap.add_argument("--user"); ap.add_argument("--pass", dest="pw")
a = ap.parse_args()
creds = {}
p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "settings.json")
if os.path.exists(p):
    creds = json.load(open(p, encoding="utf-8"))
user = a.user or creds.get("ftp_username", "")
pw = a.pw or creds.get("ftp_password", "")

rx = StreamReceiver(a.host, 7373, False, 5, user, pw)
try:
    rx.connect_to_host()
    print("handshake OK:", rx.width, "x", rx.height, "fmt", "RGB565" if rx.stream_fmt else "INDEX8")
except Exception as e:
    print("handshake FAILED:", type(e).__name__, e)
finally:
    rx.stop()

c = CC.CtrlClient()
resp = c.query(a.host, 7374, "MODEL", timeout_ms=3000)
print("MODEL:", resp)
m = ModelInfo.parse(resp)
print("  parsed:", m)
sys_resp = c.query_multi(a.host, 7374, "SYSINFO", timeout_ms=6000)
kv = parse_kv(" ".join((sys_resp or "").split("\n")))
print("SYSINFO display fields:", {k: kv.get(k) for k in ("TEMP_CPU", "TEMP_ACPI", "FAN_RPM", "BOARD_VENDOR", "BOARD_NAME",
                                                           "BIOS_VERSION", "CPU_COUNT", "CPU_CORES", "CPU_DAEMON_MASK")})
print("  unit_summary:", unit_summary(kv))
print("  cpu_summary :", cpu_summary(kv))
print("BTN 999 ->", repr(c.query(a.host, 7374, "BTN 999", timeout_ms=3000)))
