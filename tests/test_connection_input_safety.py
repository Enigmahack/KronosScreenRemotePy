"""Headless regressions for connection/input lifetime; no hardware traffic."""
import os
import pathlib
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
DATA = tempfile.TemporaryDirectory(prefix="connection-test-", dir=ROOT / "tests")
os.environ["KRONOS_DATA_DIR"] = DATA.name

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QThread, Signal
from PySide6.QtGui import QCloseEvent, QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication
from Core.ctrl_client import CtrlClient
from Core.device_family import DeviceFamily
from Core.stream_receiver import StreamReceiver
from Models.app_settings import AppSettings, MacroDef
from Views import main_window as ui

APP = QApplication.instance() or QApplication([])


class Socket:
    def __init__(self):
        self.sent = []
        self.closed = False

    def sendall(self, data):
        if self.closed:
            raise OSError("closed")
        self.sent.append(data)

    def shutdown(self, *_):
        pass

    def close(self):
        self.closed = True

    def setsockopt(self, *_):
        pass

    def settimeout(self, *_):
        pass


def client():
    with patch.object(CtrlClient, "_send_loop"), patch.object(CtrlClient, "_keeper_loop"):
        c = CtrlClient()
        c._thread.join(2)
        c._keeper.join(2)
    c.send("A", 7374, "STATE")
    c._queue.get_nowait()
    return c


class ControlSafety(unittest.TestCase):
    def test_single_flight_keeper_and_send(self):
        c = client()
        entered, release = threading.Event(), threading.Event()
        s = Socket()

        def connect(*_args, **_kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return s

        with patch("Core.ctrl_client.socket.create_connection", side_effect=connect) as dial, \
                patch.object(c, "_drain_loop"):
            keeper = threading.Thread(target=c._connect_persistent)
            writer = threading.Thread(target=c._send_one, args=("BTN 1",))
            keeper.start()
            self.assertTrue(entered.wait(3))
            writer.start()
            release.set()
            keeper.join(3)
            writer.join(3)
        self.assertFalse(keeper.is_alive() or writer.is_alive())
        self.assertEqual(dial.call_count, 1)
        self.assertEqual(s.sent, [b"CTRL_PERSIST\n", b"BTN 1\n"])
        c.stop_persistent()

    def test_late_connect_is_closed_after_reset_stop_and_swap(self):
        for action in ("reset", "stop", "swap"):
            with self.subTest(action=action):
                c = client()
                entered, release = threading.Event(), threading.Event()
                s = Socket()

                def connect(*_args, **_kwargs):
                    entered.set()
                    self.assertTrue(release.wait(3))
                    return s

                with patch("Core.ctrl_client.socket.create_connection", side_effect=connect), \
                        patch.object(c, "_drain_loop"):
                    t = threading.Thread(target=c._connect_persistent)
                    t.start()
                    self.assertTrue(entered.wait(3))
                    if action == "reset":
                        c.reset()
                    elif action == "stop":
                        c.stop_persistent()
                    else:
                        c.set_endpoint("B", 9000)
                    release.set()
                    t.join(3)
                self.assertFalse(t.is_alive())
                self.assertTrue(s.closed)
                self.assertFalse(c.is_persistent_connected)
                c.stop_persistent()

    def test_queued_old_commands_and_moves_do_not_cross_endpoint(self):
        c = client()
        old, new = Socket(), Socket()
        c._sock = old
        c.send("A", 7374, "TOUCH_MOVE 100 100")
        c.send("A", 7374, "BTN 1")
        c.send("B", 9000, "BTN 2")
        with patch("Core.ctrl_client.socket.create_connection", return_value=new) as dial, \
                patch.object(c, "_drain_loop"):
            while not c._queue.empty():
                c._send_item(c._queue.get_nowait())
        self.assertTrue(old.closed)
        self.assertEqual(new.sent, [b"CTRL_PERSIST\n", b"BTN 2\n"])
        self.assertEqual(dial.call_args.args[0], ("B", 9000))
        self.assertFalse(c.send_existing_only("A", 7374, "STATE"))
        self.assertEqual(c._host, "B")
        c.stop_persistent()
        c.send("B", 9000, "BTN 3")
        self.assertTrue(c._queue.empty())

    def test_old_macro_cannot_retarget_new_session(self):
        import Core.ctrl_client as ctrl
        c = client()
        with patch.object(ctrl, "get", return_value=c):
            def swap(_seconds):
                c.set_endpoint("B", 9000)

            with patch.object(ctrl.time, "sleep", side_effect=swap):
                ctrl.play_macro("A", 7374, ["KEY 30 1", "KEY 30 0"], 10)
        self.assertEqual(c._host, "B")
        with patch.object(c, "_send_one") as send:
            while not c._queue.empty():
                c._send_item(c._queue.get_nowait())
        send.assert_not_called()
        c.stop_persistent()

    def test_move_coalescing_cannot_overtake_up_and_next_down(self):
        c = client()
        s = Socket()
        c._sock = s
        for cmd in ("TOUCH_MOVE 1 1", "TOUCH_MOVE 2 2", "TOUCH_UP 2 2",
                    "TOUCH_DOWN 3 3", "TOUCH_MOVE 4 4", "TOUCH_MOVE 5 5"):
            c.send("A", 7374, cmd)
        while not c._queue.empty():
            c._send_item(c._queue.get_nowait())
        self.assertEqual(s.sent, [b"TOUCH_MOVE 2 2\n", b"TOUCH_UP 2 2\n",
                                  b"TOUCH_DOWN 3 3\n", b"TOUCH_MOVE 5 5\n"])
        c.stop_persistent()

    def test_dispose_unstarted_receiver_closes_socket(self):
        rx = StreamReceiver("A", 7373, True, 15, "user", "pass")
        rx._sock = Socket()
        rx.dispose()
        self.assertTrue(rx._sock.closed)


class WindowSafety(unittest.TestCase):
    def setUp(self):
        self.saver = patch.object(ui.Models.storage, "save_settings")
        self.saver.start()
        with patch.object(ui, "_setup_logging"):
            self.w = ui.MainWindow(AppSettings(midi_monitor_enabled=False))
        self.w._ctrl = Mock()
        self.w._host = self.w._settings.kronos_host = "A"

    def tearDown(self):
        APP.removeEventFilter(self.w)
        self.w._receiver = None
        self.w._shutting_down = True
        self.w.close()
        self.w.deleteLater()
        ui.Models.storage.on_write_failure = None
        APP.processEvents()
        self.saver.stop()

    def test_drag_off_release_and_ungrab_release_once(self):
        f = self.w._frame_w
        f._frame_rect = QRectF(0, 0, 800, 600)
        f._is_connected = True
        down, up = [], []
        f.touch_down.connect(lambda x, y: down.append((x, y)))
        f.touch_up.connect(lambda x, y: up.append((x, y)))

        def mouse(kind, x, button):
            return QMouseEvent(kind, QPointF(x, 100), QPointF(x, 100), button,
                               Qt.LeftButton, Qt.NoModifier)

        for ending in ("outside", "leave", "ungrab"):
            with self.subTest(ending=ending):
                before = len(up)
                f.mousePressEvent(mouse(QEvent.MouseButtonPress, 100, Qt.LeftButton))
                f.mouseMoveEvent(mouse(QEvent.MouseMove, 120, Qt.NoButton))
                if ending == "outside":
                    f.mouseReleaseEvent(mouse(QEvent.MouseButtonRelease, 810, Qt.LeftButton))
                elif ending == "leave":
                    f.leaveEvent(QEvent(QEvent.Leave))
                else:
                    APP.sendEvent(f, QEvent(QEvent.UngrabMouse))
                f.mouseReleaseEvent(mouse(QEvent.MouseButtonRelease, 810, Qt.LeftButton))
                self.assertEqual(len(up), before + 1)
                self.assertFalse(f._drag_active)
        self.assertEqual(len(down), 3)

    def test_captured_function_keys_remain_local(self):
        w = self.w
        w._kbd_capture = True
        for key, expected in ((Qt.Key_F3, "BTN 1"), (Qt.Key_F4, "BTN 2")):
            event = QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier)
            self.assertFalse(w.eventFilter(w, event))
            w.keyPressEvent(event)
            self.assertEqual(w._ctrl.send.call_args.args[-1], expected)
            self.assertTrue(w.eventFilter(w, QKeyEvent(QEvent.KeyRelease, key, Qt.NoModifier)))
        with patch.object(w, "_toggle_help") as help_action:
            e = QKeyEvent(QEvent.KeyPress, Qt.Key_F1, Qt.NoModifier)
            self.assertFalse(w.eventFilter(w, e))
            w.keyPressEvent(e)
            help_action.assert_called_once()
        self.assertFalse(any(call.args[-1].startswith("KEY") for call in w._ctrl.send.call_args_list))

    def test_captured_macro_runs_without_forwarding_trigger(self):
        w = self.w
        w._kbd_capture = True
        macro = MacroDef(trigger_key=int(Qt.Key_B), trigger_mods=Qt.ControlModifier.value,
                         steps=["KEY 30 1", "KEY 30 0"])
        w._settings.macros = [macro]
        played = threading.Event()
        with patch.object(w, "_play_macro", side_effect=lambda _: played.set()) as play:
            e = QKeyEvent(QEvent.KeyPress, Qt.Key_B, Qt.ControlModifier)
            self.assertTrue(w.eventFilter(w, e))
            self.assertTrue(played.wait(3))
            self.assertTrue(w.eventFilter(w, QKeyEvent(QEvent.KeyRelease, Qt.Key_B, Qt.ControlModifier)))
            play.assert_called_once_with(macro)
        w._ctrl.send.assert_not_called()

    def test_reserved_ctrl_shortcuts_precede_macros_and_do_not_forward_key_up(self):
        w = self.w
        w._kbd_capture = True
        for key, method in ((Qt.Key_V, "_paste_clipboard_to_kronos"),
                            (Qt.Key_K, "_open_command_palette"), (Qt.Key_S, "_save_screenshot")):
            with self.subTest(key=key):
                w._settings.macros = [MacroDef(
                    trigger_key=int(key), trigger_mods=Qt.ControlModifier.value,
                    steps=["KEY 30 1", "KEY 30 0"])]
                with patch.object(w, method) as local, patch.object(w, "_play_macro") as macro:
                    event = QKeyEvent(QEvent.KeyPress, key, Qt.ControlModifier)
                    self.assertFalse(w.eventFilter(w, event))
                    w.keyPressEvent(event)
                    local.assert_called_once()
                    macro.assert_not_called()
                    self.assertTrue(w.eventFilter(w, QKeyEvent(QEvent.KeyRelease, key, Qt.ControlModifier)))
        w._ctrl.send.assert_not_called()

    def test_settings_and_import_reconnect_changed_endpoints(self):
        for imported in (False, True):
            with self.subTest(imported=imported):
                w = self.w
                w._host, w._ctrl_port, w._stream_port = "A", 7374, 7373
                w._pull_mode, w._fps = True, 15
                w._receiver = Mock(_username="", _password="")
                new = AppSettings(kronos_host="B", ctrl_port=9000, stream_port=9001,
                                  pull_mode=False, max_fps=4, midi_monitor_enabled=False)
                with patch.object(w, "_connect_async") as connect:
                    if imported:
                        with patch.object(ui.QFileDialog, "getOpenFileName", return_value=("fake.json", "")), \
                                patch.object(ui.Models.storage, "import_settings", return_value=new), \
                                patch.object(ui.QMessageBox, "information"):
                            w._import_settings()
                    else:
                        w._settings = new
                        w._apply_settings_side_effects(False, new.screensaver_timeout, False, False, False)
                    connect.assert_called_once()
                self.assertIsNone(w._receiver)
                w._ctrl.stop_persistent.assert_called()
                w._ctrl.set_endpoint.assert_called_with("B", 9000)
                self.assertEqual((w._host, w._stream_port, w._pull_mode, w._fps), ("B", 9001, False, 4))

    def test_stale_model_and_calibration_worker_callbacks_are_ignored(self):
        w = self.w
        for command in ("MODEL", "CAL_GET"):
            with self.subTest(command=command):
                entered, release, posted = threading.Event(), threading.Event(), threading.Event()
                callbacks = []
                w._receiver = object()
                w._host = "A"

                def query(*_args, **_kwargs):
                    entered.set()
                    self.assertTrue(release.wait(3))
                    return "FAMILY=KRONOS MODEL=KRONOS2 FB_BPP=8" if command == "MODEL" else "CAL NONE"

                def post(*args):
                    callbacks.append(args[-1])
                    posted.set()

                w._ctrl.query.side_effect = query
                with patch.object(ui.QTimer, "singleShot", side_effect=post):
                    (w._fetch_device_family if command == "MODEL" else w._fetch_unit_calibration)()
                    self.assertTrue(entered.wait(3))
                    w._session_generation += 1
                    w._receiver = object()
                    w._host = "B"
                    w._is_nautilus = True
                    w._device_family = DeviceFamily.NAUTILUS
                    original = w._frame_w._cal_mesh
                    w._frame_w._cal_dirty = True
                    release.set()
                    self.assertTrue(posted.wait(3))
                callbacks.pop()()
                self.assertEqual(w._device_family, DeviceFamily.NAUTILUS)
                self.assertTrue(w._is_nautilus)
                self.assertIs(w._frame_w._cal_mesh, original)
                self.assertTrue(w._frame_w._cal_dirty)
                w._frame_w._cal_dirty = False

    def test_reply_guards_check_receiver_endpoint_and_shutdown_independently(self):
        w = self.w
        old_receiver = w._receiver = object()
        generation = w._session_generation
        for change in ("receiver", "endpoint", "shutdown"):
            with self.subTest(change=change):
                w._receiver = old_receiver
                w._host = "A"
                w._shutting_down = False
                if change == "receiver":
                    w._receiver = object()
                elif change == "endpoint":
                    w._host = "B"
                else:
                    w._shutting_down = True
                with patch.object(w, "_apply_device_family_ui") as apply, \
                        patch.object(w, "_sync_cal_grid_checks") as grid:
                    original = w._frame_w._cal_mesh
                    w._apply_device_family("FAMILY=KRONOS", generation, old_receiver, ("A", 7374))
                    w._apply_unit_calibration("CAL NONE", generation, old_receiver, ("A", 7374))
                    apply.assert_not_called()
                    grid.assert_not_called()
                    self.assertIs(w._frame_w._cal_mesh, original)
        w._shutting_down = False

    def test_late_receiver_and_auth_failure_cannot_revive_cancelled_session(self):
        w = self.w
        old = w._session_generation
        w._session_generation += 1
        w._connecting = True
        rx = Mock()
        w._apply_new_receiver(rx, old)
        rx.dispose.assert_called_once()
        self.assertIsNone(w._receiver)
        self.assertTrue(w._connecting)
        w._settings.ftp_username = "new-user"
        w._on_connect_failed(Mock(), old, PermissionError("old"), False)
        self.assertEqual(w._settings.ftp_username, "new-user")
        self.assertTrue(w._connecting)

    def test_active_library_transaction_blocks_all_transport_teardown(self):
        w = self.w
        lib = types.SimpleNamespace(hardware_transaction_active=True,
                                    request_shutdown=Mock(return_value=False))
        w._librarian_shell_win = lib
        rx = w._receiver = Mock(_username="", _password="")
        w._sysex_service = Mock(bridge=object())
        with patch.object(ui.QMessageBox, "warning"), patch.object(w, "_connect_async") as connect:
            event = QCloseEvent()
            w.closeEvent(event)
            self.assertFalse(event.isAccepted())
            self.assertFalse(w._shutting_down)
            self.assertFalse(w._disconnect())
            w._trigger_reconnect()
            w._connect_to_recent("B")
            w._settings.kronos_host = "B"
            w._settings.max_fps = 4
            w._settings.midi_monitor_enabled = False
            w._apply_settings_side_effects(False, w._settings.screensaver_timeout, False, False, False)
            connect.assert_not_called()
            self.assertEqual(w._settings.kronos_host, "A")
            self.assertEqual(w._settings.max_fps, 15)
            self.assertTrue(w._settings.midi_monitor_enabled)
            self.assertIs(w._receiver, rx)
            w._ctrl.stop_persistent.assert_not_called()
            w._sysex_service.stop.assert_not_called()
        w._librarian_shell_win = None

    def test_monitor_off_and_close_use_atomic_librarian_shutdown_fence(self):
        w = self.w
        lib = types.SimpleNamespace(hardware_transaction_active=False,
                                    request_shutdown=Mock(return_value=True))
        w._librarian_shell_win = lib
        w._sysex_service = Mock(bridge=object())
        w._settings.midi_monitor_enabled = False
        w._sync_midi_monitor_state()
        lib.request_shutdown.assert_called_once()
        w._sysex_service.stop.assert_called_once()
        lib.request_shutdown.reset_mock()
        w._settings.prompt_before_quitting = False
        with patch.object(ui.QTimer, "singleShot") as deferred:
            w.closeEvent(QCloseEvent())
        lib.request_shutdown.assert_called_once()
        self.assertTrue(w._shutting_down)
        deferred.assert_called_once()
        w._librarian_shell_win = None

    def test_shutdown_fences_before_receiver_and_midi_teardown(self):
        w = self.w
        order = []
        w._librarian_shell_win = types.SimpleNamespace(
            request_shutdown=lambda: order.append("fence") or True)
        w._receiver = Mock()
        w._receiver.stop.side_effect = lambda: order.append("stream")
        w._sysex_service = Mock()
        w._sysex_service.stop.side_effect = lambda: order.append("midi")
        w._shutting_down = True
        w._do_shutdown()
        self.assertEqual(order, ["fence", "stream", "midi"])
        w._librarian_shell_win = None

    def test_auto_reconnect_defers_without_cancelling_active_transaction(self):
        w = self.w
        w._librarian_shell_win = types.SimpleNamespace(
            hardware_transaction_active=True, request_shutdown=Mock())
        with patch.object(ui.QTimer, "singleShot") as later, \
                patch.object(w, "_new_stream_receiver") as factory:
            w._schedule_reconnect(w._session_generation)
        later.assert_called_once()
        factory.assert_not_called()
        w._librarian_shell_win.request_shutdown.assert_not_called()
        w._librarian_shell_win = None

    def test_ping_timeout_units(self):
        for platform, expected in (("darwin", "2000"), ("linux", "2")):
            with self.subTest(platform=platform), patch.object(ui.sys, "platform", platform), \
                    patch.object(ui.subprocess, "run", return_value=types.SimpleNamespace(
                        returncode=0, stdout="64 bytes: time=1.25 ms")) as run:
                self.assertEqual(ui._icmp_ping_subprocess("A", 2000), 1.25)
                self.assertEqual(run.call_args.args[0], ["ping", "-c", "1", "-W", expected, "A"])

    def test_audio_capture_failure_reaches_notification_on_gui_thread(self):
        class FailingCapture(QThread):
            levels_updated = Signal(float, float)
            capture_failed = Signal(str)

            def __init__(self, device, parent):
                super().__init__(parent)

            def run(self):
                self.capture_failed.emit("Audio capture failed: unplugged")

            def stop(self):
                self.wait(3000)

        w = self.w
        before = w._notify_count
        with patch("Rendering.vu_meter.AudioCapture", FailingCapture):
            w._start_audio_capture("unplugged")
            self.assertTrue(w._audio_capture.wait(3000))
            self.assertEqual(w._notify_count, before)
            APP.processEvents()
            self.assertEqual(w._notify_count, before + 1)
            self.assertEqual(w._notify_msgs[-1], "Audio capture failed: unplugged")
            old = w._audio_capture
            w._audio_capture = object()
            old.capture_failed.emit("stale capture failure")
            self.assertEqual(w._notify_count, before + 1)
            w._audio_capture = old
            w._stop_audio_capture()


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        DATA.cleanup()
