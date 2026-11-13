"""StreamReceiver latest-frame coalescing, stop-before-run, and stale ping / SYSINFO results against a fake daemon."""
import os, socket, struct, sys, threading, time, zlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])

from Core.stream_receiver import StreamReceiver

fails = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


W, H = 64, 48


def rect(x0, y0, w, h, fill, enc=0):
    data = bytes([fill]) * (w * h)
    if enc == 2:
        data = zlib.compress(data)
    body = bytes([enc]) + struct.pack("<HHHH", x0, y0, w, h) + data
    return struct.pack("<I", len(body)) + body


def fake_daemon(rects, sent_all: threading.Event, release: threading.Event):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def run():
        conn, _ = srv.accept()
        conn.recv(4096)
        # status OK, w, h, fmt=INDEX8, bpp=8, enc, flags, palette
        conn.sendall(b"KSCR\x00" + struct.pack("<HH", W, H) + bytes([0, 8, 0, 0]) + bytes(768))
        for r in rects:
            conn.sendall(r)
        sent_all.set()
        release.wait(10)
        conn.close()
        srv.close()

    threading.Thread(target=run, daemon=True).start()
    return srv.getsockname()[1]


# ── Coalescing: GUI thread blocked while 200 rects arrive -> one delivery, newest canvas ──
rects = [rect(0, 0, W, H, 1)]                                     # full frame
rects += [rect((i * 3) % (W - 8), (i * 5) % (H - 8), 8, 8, (i % 250) + 2, enc=2 if i % 2 else 0)
          for i in range(200)]
expected = bytearray([1]) * (W * H)
for i in range(200):
    x0, y0, fill = (i * 3) % (W - 8), (i * 5) % (H - 8), (i % 250) + 2
    for row in range(8):
        d = (y0 + row) * W + x0
        expected[d:d + 8] = bytes([fill]) * 8

sent_all, release = threading.Event(), threading.Event()
rx = StreamReceiver("127.0.0.1", fake_daemon(rects, sent_all, release), False, 5, "u", "p")
rx.connect_to_host()
frames = []
rx.frame_received.connect(lambda b: frames.append(b))
rx.start()
sent_all.wait(5)
# Wait for the worker to have consumed every rect (no event processing meanwhile).
deadline = time.monotonic() + 5
while time.monotonic() < deadline:
    with rx._frame_lock:
        if rx._canvas is not None and bytes(rx._canvas) == bytes(expected):
            break
    time.sleep(0.01)
check("no frame delivered while the GUI thread is busy", frames == [])
app.processEvents()
check("200+ rects collapse into a single delivery", len(frames) == 1)
check("the delivered frame is the fully composited newest canvas", frames and frames[-1] == bytes(expected))
check("delivered frame is an immutable snapshot (bytes)", frames and isinstance(frames[-1], bytes))
app.processEvents()
check("no further deliveries queued", len(frames) == 1)

# A later rect after delivery posts exactly one new delivery.
with rx._frame_lock:
    pending = rx._frame_pending
check("pending flag cleared after delivery", pending is False)
release.set()
rx.dispose()
app.processEvents()

# ── stop() before the worker reaches run() must stick (no run-time reset) ─────────────
sent_all2, release2 = threading.Event(), threading.Event()
rx2 = StreamReceiver("127.0.0.1", fake_daemon([rect(0, 0, W, H, 7)], sent_all2, release2), False, 5, "u", "p")
rx2.connect_to_host()
lost = []
rx2.disconnected.connect(lambda: lost.append(1))
rx2.start()
rx2.stop()
check("stopped receiver exits", rx2.wait(3000))
app.processEvents()
check("a requested stop is not reported as a lost connection", lost == [])
release2.set()

# ── stale ping result after a session change is ignored ───────────────────────────────
import tempfile
from unittest.mock import Mock, patch
DATA = tempfile.TemporaryDirectory()
os.environ["KRONOS_DATA_DIR"] = DATA.name
import Views.main_window as ui
from Models.app_settings import AppSettings
with patch.object(ui.Models.storage, "save_settings"), patch.object(ui, "_setup_logging"):
    w = ui.MainWindow(AppSettings(midi_monitor_enabled=False))
    w._ctrl = Mock()
    w._host = "A"
    w._receiver = object()
    gen = w._session_generation
    w._ping_label.setText("⇄ —")
    w._ping_result(12.0, gen, w._receiver, "A")
    check("current-session ping updates the footer", w._ping_label.text() == "⇄ 12ms")
    stale_rx = w._receiver
    w._session_generation += 1
    w._receiver = object()
    w._ping_inflight = True
    w._ping_result(99.0, gen, stale_rx, "A")
    check("stale ping does not repaint the footer", w._ping_label.text() == "⇄ 12ms")
    check("stale ping does not clear the new session's in-flight flag", w._ping_inflight is True)
    w._receiver = None
    w._ping_result(5.0, w._session_generation, None, "A")
    check("ping after disconnect is ignored", w._ping_label.text() == "⇄ 12ms")

    # ── Perf window: a SYSINFO reply for an earlier host is dropped ──────────────────
    from Views.perf_window import PerformanceWindow
    pw = PerformanceWindow("", 7374, None)
    pw.update_host("A", 7374)
    old_token = pw._poll_token
    pw.update_host("", 7374)
    pw._poll_busy = True
    pw._apply("CPU_PCT=50\nUPTIME=10\nOK", old_token)
    check("stale SYSINFO reply does not show Connected", "Not connected" in pw._status_lbl.text())
    check("stale SYSINFO reply still frees the poll slot", pw._poll_busy is False)
    pw.update_host("A", 7374)
    pw._apply("CPU_PCT=50\nUPTIME=10\nOK", pw._poll_token)
    check("current SYSINFO reply is applied", "Connected" in pw._status_lbl.text()
          and "Not" not in pw._status_lbl.text())
    pw.close()
    pw.deleteLater()

    # ── Per-open dialogs and context menus are deleted after use ──────────────────────
    from PySide6.QtCore import QEvent, QPoint
    from PySide6.QtWidgets import QDialog, QMenu
    from Views.about_dialog import AboutDialog
    from Views.help_window import HelpWindow

    def flush():
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()

    def count(cls):
        return len(w.findChildren(cls))

    class _NoExecMenu(QMenu):
        def exec(self, *a):
            return None

    with patch.object(QDialog, "exec", lambda self: 0), patch.object(ui, "QMenu", _NoExecMenu), \
            patch("Core.ctrl_client.CtrlClient.query", lambda *a, **k: None, create=True):
        w._ctrl_port = 7374
        flush()
        base_menus, base_dialogs = count(QMenu), count(QDialog)
        for _ in range(3):
            w._toggle_help()
            w._open_about()
            w._open_notify_log()
            w._open_settings()
            w._show_frame_context_menu(QPoint(0, 0))
            w._show_status_context_menu(QPoint(0, 0))
            w._show_kbd_context_menu(QPoint(0, 0))
            w._show_fps_context_menu(QPoint(0, 0))
            w._show_mode_context_menu(QPoint(0, 0))
            w._show_notify_context_menu(QPoint(0, 0))
            w._show_vu_device_menu([])
        flush()
        check("no Help/About/Settings/notification dialogs accumulate", count(QDialog) == base_dialogs
              and not w.findChildren(AboutDialog) and not w.findChildren(HelpWindow))
        check("no context menus accumulate", count(QMenu) == base_menus)
        check("shared Scaling Quality actions survive the frame menu's deletion",
              w._act_scale_sharp.text() != "")

    # ── Switching VU device does not block on the old capture's stream close ─────────
    from PySide6.QtCore import QThread, Signal

    class _SlowCapture(QThread):
        levels_updated = Signal(float, float)
        capture_failed = Signal(str)

        def __init__(self, device, parent):
            super().__init__(parent)
            self._running = True

        def run(self):
            while self._running:
                self.msleep(10)
            self.msleep(600)          # a slow WASAPI close

    with patch("Rendering.vu_meter.AudioCapture", _SlowCapture):
        w._start_audio_capture("x")
        time.sleep(0.05)
        old = w._audio_capture
        t0 = time.monotonic()
        w._stop_audio_capture()
        took = time.monotonic() - t0
        check("stopping a capture does not wait for its stream close", took < 0.3)
        check("retired capture is kept alive and unparented", old in w._retired_audio and old.parent() is None)
        check("retired capture finishes", old.wait(3000))
        deadline = time.monotonic() + 2
        while old in w._retired_audio and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        check("retired capture is released once finished", old not in w._retired_audio)

    w._shutting_down = True
    w.close()
    w.deleteLater()
    app.processEvents()

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
