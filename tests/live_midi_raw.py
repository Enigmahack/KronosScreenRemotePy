"""Live: print a histogram of every incoming MIDI message (status/CC#) for N seconds — to find out
which CC# a hardware control actually transmits.  usage: live_midi_raw.py <host> [seconds]"""
import os, sys, time, tempfile, collections
host = sys.argv[1]; secs = int(sys.argv[2]) if len(sys.argv) > 2 else 20
os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_liveraw_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtCore import QCoreApplication
app = QCoreApplication([])
from Core.sysex_service import SysExService
svc = SysExService(); hist = collections.Counter(); ranges = {}
svc.start(host)
orig = svc._on_raw_message
def tap(raw):
    if raw and raw[0] != 0xF0:
        st = raw[0] & 0xF0
        key = f"CC#{raw[1]}" if st == 0xB0 else {0xC0: "ProgramChange", 0x90: "NoteOn", 0x80: "NoteOff",
                                                  0xE0: "PitchBend", 0xD0: "ChanPressure"}.get(st, hex(st))
        hist[key] += 1
        if st == 0xB0 and len(raw) > 2:
            lo, hi = ranges.get(key, (127, 0)); ranges[key] = (min(lo, raw[2]), max(hi, raw[2]))
    elif raw:
        hist[f"SysEx func 0x{raw[4]:02X}" if len(raw) > 4 else "SysEx"] += 1
    orig(raw)
svc._bridge.remove_raw_listener(svc._on_raw_message); svc._bridge.add_raw_listener(tap)
print(f"listening {secs}s ...", flush=True)
end = time.time() + secs
while time.time() < end:
    app.processEvents(); time.sleep(0.02)
for k, v in hist.most_common():
    print(f"  {k:<22} x{v}", ("range %s" % (ranges[k],)) if k in ranges else "")
svc.stop()
