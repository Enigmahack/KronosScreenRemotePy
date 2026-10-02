"""SysExService behavior against a fake bridge: value-slider follow, per-change name pull,
poll-on-changes, proactive polling, bank-digest wake, debounce, and shutdown."""
import os, sys, time, tempfile
os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_sysex_test_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtCore import QCoreApplication
app = QCoreApplication([])

import Core.kronos_sysex as ksx
from Core.sysex_service import SysExService
from Tools.sysex_dump_collector import SysExDumpCollector

PERF_REQ = bytes.fromhex("F0 42 30 68 32 F7")


class FakeBridge:
    is_connected = True

    def __init__(self):
        self.sent = []
        self.listeners = []

    def add_raw_listener(self, cb): self.listeners.append(cb)
    def remove_raw_listener(self, cb):
        if cb in self.listeners: self.listeners.remove(cb)
    def dispose(self): pass
    def add_aborted_listener(self, cb): pass
    def remove_aborted_listener(self, cb): pass

    def send_bytes(self, data):
        self.sent.append((time.monotonic(), bytes(data)))
        if bytes(data) == PERF_REQ:      # answer: Program, func33 bank 0 (I-A), number 7
            reply = bytes([0xF0, 0x42, 0x30, 0x68, 0x33, 0x01, 0x00, 0x00, 0x00, 0x07, 0xF7])
            for cb in list(self.listeners):
                cb(reply)
        return True

    def perf_requests(self): return [t for t, d in self.sent if d == PERF_REQ]
    def name_requests(self): return [d for t, d in self.sent if len(d) > 5 and d[4] == 0x72]


def make():
    svc = SysExService()
    br = FakeBridge()
    svc._bridge = br
    svc._dump = SysExDumpCollector(br)
    svc._cache_key = "fake"
    br.add_raw_listener(svc._on_raw_message)
    svc._is_available = True
    svc._loop_gen += 1
    import threading
    threading.Thread(target=svc._perf_loop, args=(svc._loop_gen,), daemon=True).start()
    return svc, br


def settle(sec): time.sleep(sec)


fails = []
def check(name, cond):
    if not cond: fails.append(name)
    print(("ok   " if cond else "FAIL ") + name)


# ---------------------------------------------------------------- value slider follow
svc, br = make(); settle(0.2)
got = []
svc.value_slider_changed.connect(got.append)
svc._on_raw_message(bytes([0xB0, 18, 64]))
check("cc18 default -> slider 64", got == [64])
svc._on_raw_message(bytes([0xB0, 0, 3])); svc._on_raw_message(bytes([0xB0, 32, 3]))
check("bank select CCs never reach the slider", got == [64])
svc.value_slider_cc = 21
svc._on_raw_message(bytes([0xB0, 18, 10])); svc._on_raw_message(bytes([0xB0, 21, 5]))
check("custom CC# honoured, old CC ignored", got == [64, 5])
svc.value_slider_cc = 0
svc._on_raw_message(bytes([0xB0, 0, 9]))
check("a CC# of 0 can never shadow Bank Select", got == [64, 5])
svc.stop()

# ---------------------------------------------------------------- name pull on change
svc, br = make(); settle(0.2)
svc._state_mode = 3
svc._last_bank_id = ksx.BankId(1, "I-A", 0, 0)
svc.pull_names_on_change = False
svc._on_raw_message(bytes([0xC0, 9])); settle(0.4)
check("pull-names OFF: no request", br.name_requests() == [])
svc.pull_names_on_change = True
for pc in (10, 11, 12, 13, 14):      # a fast wheel scroll
    svc._on_raw_message(bytes([0xC0, pc])); settle(0.02)
settle(0.5)
reqs = br.name_requests()
check("fast scroll pulls ONLY where it settles", len(reqs) == 1 and reqs[0][8] == 14)
check("request is a func-0x72 Program-name (0x13) dump request", reqs and reqs[0][5] == 0x13 and reqs[0][3] == 0x68)
svc._stream_names[(1, 0, 20)] = "Cached"
svc._on_raw_message(bytes([0xC0, 20])); settle(0.4)
check("cached name: no request", len(br.name_requests()) == 1)
tok = svc._dump_gate.begin()
svc._on_raw_message(bytes([0xC0, 21])); settle(0.4)
check("never injects into a bulk dump (gate active)", len(br.name_requests()) == 1)
svc._dump_gate.end(tok)
svc.stop()

# ---------------------------------------------------------------- poll on changes
for flag in (True, False):
    svc, br = make(); settle(0.3)
    base = len(br.perf_requests())      # the loop's initial query
    svc._poll_on_changes = flag
    svc._state_mode = 1; svc._last_bank_id = None; svc._have_bank_context = False   # undecodable PC
    for pc in (1, 2, 3):
        svc._on_raw_message(bytes([0xC0, pc]))
    settle(0.7)
    n = len(br.perf_requests()) - base
    check(f"poll-on-changes={flag}: undecodable PC burst -> {'ONE coalesced' if flag else 'no'} query", n == (1 if flag else 0))
    svc.stop()

# a bank-storage push (0x38) wakes the loop regardless of the flag
svc, br = make(); settle(0.3)
base = len(br.perf_requests()); svc._poll_on_changes = False
svc._on_raw_message(bytes([0xF0, 0x42, 0x30, 0x68, 0x38, 0x00, 0x00] + [0] * 23 + [0xF7])); settle(0.7)
check("bank-digest push refreshes even with poll-on-changes off", len(br.perf_requests()) - base == 1)
svc.stop()

# ---------------------------------------------------------------- proactive polling
svc, br = make(); settle(0.3)
base = len(br.perf_requests())
settle(1.3)
check("proactive OFF: loop parks (no periodic queries)", len(br.perf_requests()) == base)
svc.apply_midi_settings(True, 1, True)
settle(3.0)
n = len(br.perf_requests()) - base
check("proactive ON @1s: repeats on the interval (got %d in 3s)" % n, 2 <= n <= 5)
svc.apply_midi_settings(False, 1, True); settle(0.3); mark = len(br.perf_requests()); settle(1.6)
check("proactive turned OFF: stops", len(br.perf_requests()) == mark)
svc.stop()

# ---------------------------------------------------------------- shutdown
svc, br = make(); settle(0.3)
svc.apply_midi_settings(True, 1, True); settle(0.3)
svc._stream_names.clear()
svc.stop()
mark = len(br.perf_requests()); settle(1.6)
check("stop() retires the loop and cancels debounces", len(br.perf_requests()) == mark)

print("ALL PASS" if not fails else "FAILED: " + ", ".join(fails))
sys.exit(1 if fails else 0)
