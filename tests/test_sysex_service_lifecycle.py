"""SysExService stop()/start() never block the GUI thread, and the name-cache / dumped-bank
files are written in snapshot order (newest wins) and in order with the next start()'s load.

Covers: a real MidiBridgeClient stuck in a slow connect() (stop() returns at once, the thread
is kept alive in _RETIRED and released after it ends, with and without an event loop),
LatestWinsWriter ordering, same-host stop->start with a slow final write, a stale bridge
message after a reconnect to another host, a name captured before the disk caches finished
loading (no partial snapshot replaces the cached list), stop() before the load finished,
and the atomic backup_bank_to_syx. No hardware."""
import os, sys, time, tempfile, threading
os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_sysex_life_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unittest.mock import patch
from PySide6.QtCore import QCoreApplication
app = QCoreApplication([])

import Core.kronos_sysex as ksx
import Core.midi_bridge as mb
import Core.sysex_service as ss
import Models.storage as storage
from Core.sysex_service import LatestWinsWriter, SysExService

fails = []
def check(name, cond):
    if not cond: fails.append(name)
    print(("ok   " if cond else "FAIL ") + name)


def slow_connect(delay):
    def fake(addr, timeout=None):
        time.sleep(delay)
        raise OSError("unreachable (test)")
    return fake


def name_dump(type_, bank, idx, name):
    """A func-0x73 name Object Dump as the instrument sends it (7-bit packed, ASCII)."""
    raw = name.encode().ljust(24, b" ")
    packed = bytearray()
    for i in range(0, len(raw), 7):
        packed.append(0)
        packed.extend(raw[i:i + 7])
    obj = 0x13 if type_ == 1 else 0x12
    return bytes([0xF0, 0x42, 0x30, 0x68, 0x73, obj, bank, idx >> 7, idx & 0x7F, 0]) + bytes(packed) + b"\xF7"


def disk_names(host):
    return {(n.type, n.bank, n.number): n.name for n in storage.load_names(host)}


def flush():
    ss._CACHE_IO.flush(10)


# ------------------------------------------------------------ stop() with a slow connect()
with patch.object(mb.socket, "create_connection", slow_connect(2.0)):
    svc = SysExService()
    svc.start("10.0.0.1")
    time.sleep(0.2)                         # the bridge thread is now inside connect()
    br = svc.bridge
    t0 = time.monotonic(); svc.stop(); dt = time.monotonic() - t0
    check(f"stop() during a slow connect returns at once ({dt*1000:.0f} ms)", dt < 0.15)
    check("the still-running bridge is kept referenced", br in mb._RETIRED and br.isRunning())
    del br
    check("join_retired waits for it to end", mb.join_retired(5.0))
    check("and releases it", not mb._RETIRED)

    # Same again, released by the GUI-thread reaper (event loop running, no join).
    svc.start("10.0.0.1")
    time.sleep(0.2)
    br = svc.bridge
    t0 = time.monotonic(); svc.start("10.0.0.2"); dt = time.monotonic() - t0
    check(f"start() (reconnect) does not wait for the old bridge ({dt*1000:.0f} ms)", dt < 0.15)
    check("old bridge retired", br in mb._RETIRED)
    del br
    end = time.monotonic() + 5
    while time.monotonic() < end and any(t is not svc.bridge for t in mb._RETIRED):
        app.processEvents(); time.sleep(0.02)
    check("reaper releases it once it has ended", not mb._RETIRED)
    svc.stop()
    mb.join_retired(5.0)

# ------------------------------------------------------------ LatestWinsWriter ordering
w = LatestWinsWriter("TestWriter")
out = []
gate = threading.Event()
def job(v, wait=False):
    def run():
        if wait: gate.wait(5)
        out.append(v)
    return run
w.submit("k", 1, job(1, wait=True))          # running (blocked) while the next ones arrive
time.sleep(0.1)
w.submit("k", 3, job(3))
w.submit("k", 2, job(2))                     # older than the queued 3: dropped
gate.set(); w.flush(5)
check("newest snapshot wins, older one never written after it", out == [1, 3])
w.submit("k", 2, job(2)); w.flush(5)
check("a version below the last written is dropped", out == [1, 3])
loaded = []
w.submit("j", 10, job("w10", wait=False)); w.call(lambda: loaded.append(list(out))); w.flush(5)
check("a load queued after a write sees it (FIFO)", loaded and loaded[0][-1] == "w10")

# Service level: two persists race, the slow first one must not land last.
real_save = storage.save_names
calls = []
def slow_first_save(key, names):
    calls.append(len(names))
    if len(calls) == 1:
        time.sleep(0.5)
    real_save(key, names)
svc = SysExService()
svc._cache_key = "order"
with patch.object(storage, "save_names", slow_first_save):
    with svc._names_lock:
        svc._sess.names[(1, 0, 0)] = "A"; svc._sess.dirty_names = True
    svc._persist_names()                     # v1 (one name), written slowly
    time.sleep(0.1)
    with svc._names_lock:
        svc._sess.names[(1, 0, 1)] = "B"; svc._sess.dirty_names = True
    svc._persist_names()                     # v2 (two names)
    flush()
check("disk ends at the newest snapshot", disk_names("order") == {(1, 0, 0): "A", (1, 0, 1): "B"})

# ------------------------------------------------------------ same host: stop -> start
storage.save_names("hA", [ksx.CachedName(1, 0, 0, "OnDisk")])
storage.save_dumped_banks("hA", {(1, 0)})
with patch.object(mb.socket, "create_connection", slow_connect(0.1)):
    svc = SysExService()
    t0 = time.monotonic(); svc.start("hA"); dt = time.monotonic() - t0
    check(f"start() does not read the caches on the calling thread ({dt*1000:.0f} ms)", dt < 0.15)
    flush()
    check("caches loaded in the background", svc._stream_names.get((1, 0, 0)) == "OnDisk"
          and (1, 0) in svc._dumped_banks)
    sessA = svc._sess
    sessA.listener(name_dump(1, 0, 5, "Live5"))
    check("a live name is captured", svc._stream_names.get((1, 0, 5)) == "Live5")

    def slow_save(key, names, _real=real_save):
        time.sleep(0.6)
        _real(key, names)
    with patch.object(storage, "save_names", slow_save):
        t0 = time.monotonic(); svc.stop(); dt = time.monotonic() - t0
        check(f"stop() does not write the cache on the calling thread ({dt*1000:.0f} ms)", dt < 0.15)
        svc.start("hA")                       # reconnect at once, final write still pending
        flush()
    check("reconnect to the same host sees the previous session's final write",
          svc._stream_names.get((1, 0, 5)) == "Live5" and svc._stream_names.get((1, 0, 0)) == "OnDisk")
    sessA.listener(name_dump(1, 0, 6, "Ghost"))
    check("a message from the stopped bridge is ignored", (1, 0, 6) not in svc._stream_names)

    # ---------------------------------------------------- different host
    svc.start("hB")
    flush()
    sessB_prev = svc._sess
    sessA.listener(name_dump(1, 0, 7, "FromA"))
    check("instrument A's late message never lands in B's cache", (1, 0, 7) not in svc._stream_names)
    svc._sess.listener(name_dump(1, 0, 8, "OnB"))
    svc.stop(); flush()
    check("B's final write is keyed under B", disk_names("hB") == {(1, 0, 8): "OnB"})
    check("A's cache untouched by B", (1, 0, 8) not in disk_names("hA") and disk_names("hA").get((1, 0, 5)) == "Live5")

    # ---------------------------------------------------- capture before the load finished
    storage.save_names("hA", storage.load_names("hA") + [ksx.CachedName(1, 1, 0, "StaleBank1")])
    real_load = storage.load_names
    def slow_load(key, _real=real_load):
        time.sleep(0.6)
        return _real(key)
    with patch.object(storage, "load_names", slow_load):
        svc.start("hA")
        svc._sess.listener(name_dump(1, 0, 9, "Early"))
        svc._persist_names()                   # a timed write firing before the merge
        # A Bank Digest for Program bank 1 (obj 0x00 -> type 1) before the merge: drop it.
        svc._sess.listener(bytes([0xF0, 0x42, 0x30, 0x68, 0x38, 0x00, 0x01] + [0] * 23 + [0xF7]))
        flush()
    d = disk_names("hA")
    check("no partial snapshot: old and new names both on disk",
          d.get((1, 0, 0)) == "OnDisk" and d.get((1, 0, 5)) == "Live5" and d.get((1, 0, 9)) == "Early")
    check("a bank invalidated by a digest during the load is dropped from the disk copy",
          (1, 1, 0) not in d and (1, 1, 0) not in svc._stream_names)
    check("merged session has both", svc._stream_names.get((1, 0, 0)) == "OnDisk"
          and svc._stream_names.get((1, 0, 9)) == "Early")

    # ---------------------------------------------------- stop() before the load finished
    storage.save_names("hC", [ksx.CachedName(0, 2, 3, "OldC")])
    with patch.object(storage, "load_names", slow_load):
        svc.start("hC")
        svc._sess.listener(name_dump(0, 2, 4, "NewC"))
        svc.stop()                              # load still pending: it writes once merged
        flush()
    check("stop() before the load: merged result written",
          disk_names("hC") == {(0, 2, 3): "OldC", (0, 2, 4): "NewC"})
    mb.join_retired(5.0)

# ------------------------------------------------------------ atomic backup_bank_to_syx
svc = SysExService()
class _B:
    is_connected = True
svc._bridge = _B(); svc._dump = object()
tmpdir = tempfile.mkdtemp(prefix="kr_backup_")
path = os.path.join(tmpdir, "bank.syx")
with open(path, "wb") as f:
    f.write(b"OLD")
seq = iter([b"\xF0\x01\xF7", RuntimeError("link lost")])
def dump_object(obj, bank, i, **kw):
    v = next(seq)
    if isinstance(v, Exception):
        raise v
    return v
svc.dump_object = dump_object
try:
    svc.backup_bank_to_syx(0, 0x40, path, slot_count=2)
    check("failure propagates", False)
except RuntimeError:
    pass
check("a failed backup leaves the old file and no temp", open(path, "rb").read() == b"OLD"
      and os.listdir(tmpdir) == ["bank.syx"])
svc.dump_object = lambda *a, **k: b"\xF0\x02\xF7"
n = svc.backup_bank_to_syx(0, 0x40, path, slot_count=3)
check("a finished backup replaces it", n == 3 and open(path, "rb").read() == b"\xF0\x02\xF7" * 3
      and os.listdir(tmpdir) == ["bank.syx"])
svc._bridge = None

print("ALL PASS" if not fails else "FAILED: " + ", ".join(fails))
sys.exit(1 if fails else 0)
