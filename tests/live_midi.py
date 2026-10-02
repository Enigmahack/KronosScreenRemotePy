"""Live, READ-ONLY check of SysExService against a real unit: probe, perf-id loop, refresh,
proactive polling, the per-change name pull (a func-0x72 NAME request only), and — if --listen N is
given — N seconds of listening for the hardware VALUE slider (move it on the unit).
usage: live_midi.py <host> [--listen 20]"""
import os, sys, time, tempfile, argparse
ap = argparse.ArgumentParser(); ap.add_argument("host"); ap.add_argument("--listen", type=int, default=0)
a = ap.parse_args()
os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_livemidi_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtCore import QCoreApplication
app = QCoreApplication([])
from Core.sysex_service import SysExService


def pump(sec):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents(); time.sleep(0.02)


svc = SysExService()
perf, slider, avail = [], [], []
svc.performance_changed.connect(perf.append)
svc.value_slider_changed.connect(slider.append)
svc.available_changed.connect(avail.append)
svc.start(a.host)
t = time.time()
while time.time() - t < 12 and not svc.is_available:
    pump(0.1)
print("bridge connected:", svc.bridge is not None and svc.bridge.is_connected, "| SysEx available:", svc.is_available)
if not svc.is_available:
    svc.stop(); sys.exit(2)
pump(2.5)
print("performance after probe:", repr(svc.performance_display))
assert svc.performance_display, "perf-id loop produced nothing"

# explicit refresh (debounced) — display must come back identical
before = svc.performance_display
n_before = len(perf)
svc.refresh_now(); pump(1.5)
print("after refresh_now:", repr(svc.performance_display), "(unchanged value emits no signal)")
assert svc.performance_display == before

# proactive polling: count real round-trips for 6 s at a 2 s interval
calls = []
orig = svc._refresh_once
svc._refresh_once = lambda: (calls.append(time.time()), orig())[1]
svc.apply_midi_settings(True, 2, True)
pump(6.5)
print("proactive @2s over 6.5s -> queries:", len(calls))
assert 2 <= len(calls) <= 5
# C# parity: turning it off lets the in-flight interval finish (one last query), then it parks
svc.apply_midi_settings(False, 2, True); pump(2.6); k = len(calls); pump(4.0)
assert len(calls) == k, "proactive off must stop polling"
print("proactive off -> parked OK")

# per-change name pull: forget the current name, then pull ONLY that name object (func 0x72)
bid = svc._last_bank_id
if bid is not None:
    key = (bid.type, bid.obj_bank, bid.number)
    with svc._names_lock:
        svc._stream_names.pop(key, None)
    svc._name_pull_after_settle(bid, svc._name_pull_epoch)
    pump(3.0)
    with svc._names_lock:
        got = svc._stream_names.get(key)
    print("name pull for", bid.display, "->", repr(got))
    assert got, "no name captured from the 0x73 reply"
else:
    print("no current bank id (not in Program/Combi mode?) — name pull skipped")

if a.listen:
    print(f"LISTENING {a.listen}s for the VALUE slider (CC#{svc.value_slider_cc}) — move it now ...", flush=True)
    pump(a.listen)
    print("value slider events:", len(slider), "| range:", (min(slider), max(slider)) if slider else None)
svc.stop()
print("DONE")
