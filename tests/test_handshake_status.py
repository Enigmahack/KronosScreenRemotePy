"""Stream handshake failure statuses (docs/api.md 3.4) against a fake daemon, plus the SYSINFO display helpers."""
import os, socket, sys, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])

from Core.stream_receiver import StreamReceiver, StreamVersionError, version_failure_message
from Views.perf_window import unit_summary, cpu_summary

fails = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def fake_daemon(reply: bytes):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def run():
        conn, _ = srv.accept()
        conn.recv(4096)               # the hello
        conn.sendall(reply)
        conn.close()
        srv.close()

    threading.Thread(target=run, daemon=True).start()
    return srv.getsockname()[1]


def attempt(reply: bytes):
    rx = StreamReceiver("127.0.0.1", fake_daemon(reply), False, 5, "u", "p")
    try:
        rx.connect_to_host()
        return None
    except Exception as e:
        return e


e = attempt(b"KSCR\x04\x04\x05")      # VERSION_MISMATCH, daemon accepts 4-5: this client (3) is too old
check("0x04 raises StreamVersionError", isinstance(e, StreamVersionError))
check("0x04 carries ver_min/ver_max", isinstance(e, StreamVersionError) and (e.ver_min, e.ver_max) == (4, 5))
check("0x04 with ver_min > 3 says the client is too old", isinstance(e, StreamVersionError) and "client is too old" in str(e))

e = attempt(b"KSCR\x04\x02\x02")      # daemon only speaks v2: the daemon is too old
check("0x04 with ver_max < 3 says the daemon is too old", isinstance(e, StreamVersionError) and "daemon is too old" in str(e))

e = attempt(b"KSCR\x03\x02\x03")
check("0x03 raises StreamVersionError", isinstance(e, StreamVersionError) and e.status == 0x03)
check("0x03 message names the display-format cause", isinstance(e, StreamVersionError) and "display format" in str(e))

e = attempt(b"KSCR\x04")              # truncated: no version bytes
check("truncated 0x04 still raises cleanly", isinstance(e, StreamVersionError) and e.ver_min is None)
check("message without versions has no range text", "accepts stream versions" not in version_failure_message(4, None, None))

e = attempt(b"KSCR\x01")
check("0x01 stays an auth failure (PermissionError)", isinstance(e, PermissionError))
e = attempt(b"KSCR\x02")
check("0x02 stays user-not-found (ConnectionError, not a version error)",
      isinstance(e, ConnectionError) and not isinstance(e, StreamVersionError))

kv = {"MODEL": "NAUTILUS_AT", "BOARD_VENDOR": "ASRock", "BOARD_NAME": "N3160TM-ITX-K", "BIOS_VERSION": "L0.07E",
      "CPU_COUNT": "4", "CPU_CORES": "4", "CPU_THREADS_PER_CORE": "1", "CPU_DAEMON_MASK": "0x8", "CPU_RT_MASK": "0xf"}
check("unit summary", unit_summary(kv) == "NAUTILUS_AT - ASRock N3160TM-ITX-K, BIOS L0.07E")
check("cpu summary", cpu_summary(kv) == "4 CPUs / 4 cores / 1 per core, daemon on 0x8, RT on 0xf")
check("older daemon: no fields -> dashes", unit_summary({}) == "—" and cpu_summary({}) == "—")
check("UNKNOWN board is hidden", unit_summary({"MODEL": "KRONOS2", "BOARD_VENDOR": "UNKNOWN", "BOARD_NAME": "UNKNOWN"}) == "KRONOS2")

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
