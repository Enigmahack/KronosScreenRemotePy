"""SysExDumpCollector / SysExService.dump_object must only accept the object that was asked for: a stray or late dump of the
same object type from another bank or slot (seen live: an HD-1-sized Program taken as the answer for an EXi slot) is ignored.
Run from the repo root: python tests/test_dump_match.py"""
import os, sys, tempfile, threading, time
os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_dumpmatch_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import Data.nautilus_sysex as nsx
from Core.sysex_service import SysExService
from Tools.sysex_dump_collector import SysExDumpCollector

OBJ_PROGRAM = 0x01
fails = []


def check(name, cond):
    if not cond:
        fails.append(name)
    print(("ok   " if cond else "FAIL ") + name)


class FakeBridge:
    is_connected = True

    def __init__(self, script):
        self.listeners, self.activity, self.script, self.sent = [], [], script, []

    def add_raw_listener(self, cb): self.listeners.append(cb)
    def remove_raw_listener(self, cb): self.listeners.remove(cb) if cb in self.listeners else None
    def add_activity_listener(self, cb): self.activity.append(cb)
    def remove_activity_listener(self, cb): self.activity.remove(cb) if cb in self.activity else None

    def send_bytes(self, data):
        self.sent.append(bytes(data))
        msgs = list(self.script)
        def run():
            for m in msgs:
                time.sleep(0.05)
                for cb in list(self.activity): cb()
                for cb in list(self.listeners): cb(m)
        threading.Thread(target=run, daemon=True).start()
        return True


def dump(bank, index, size, fill):
    return nsx.object_dump_write(OBJ_PROGRAM, bank, index, 6, bytes([fill]) * size)


def service(script):
    svc = SysExService()
    br = FakeBridge(script)
    svc._bridge = br
    svc._dump = SysExDumpCollector(br, codec=nsx)
    svc._codec = nsx
    svc._is_available = True
    svc._cache_key = "fake"
    return svc


right = dump(0x4A, 31, 4954, 0x11)
stray_bank = dump(0x4C, 31, 3700, 0x22)        # HD-1 bank, same slot
stray_slot = dump(0x4A, 30, 4954, 0x33)        # same bank, other slot

for name, script in (("stray-bank-first", [stray_bank, right]), ("stray-slot-first", [stray_slot, right]),
                     ("stray-both-first", [stray_bank, stray_slot, right])):
    d = service(script).dump_object_parsed(OBJ_PROGRAM, 0x4A, 31, no_response_ms=2000)
    check(name, d is not None and d.bank == 0x4A and d.index == 31 and len(d.body) == 4954 and d.body[0] == 0x11)

d = service([stray_bank]).dump_object_parsed(OBJ_PROGRAM, 0x4A, 31, no_response_ms=1000)
check("only-a-stray-gives-none", d is None)
d = service([right]).dump_object_parsed(OBJ_PROGRAM, 0x4A, 31, no_response_ms=2000)
check("plain-reply-still-works", d is not None and d.body[0] == 0x11)

print("FAIL: " + ", ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
