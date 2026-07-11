"""
MidiBridgeClient — connects to the daemon's MIDI bridge (TCP port 9875, see
KronosScreenRemoteDaemon/docs/api.md section 8). Port of the Windows client's
Networking/MidiStreamMonitor.cs (MidiStreamParser + the reconnecting reader) and
the send half of Networking/TcpMidiTransport.cs — but simplified: the bridge is
documented as bidirectional (section 8.3), so unlike the C# app (which sends via
the daemon's ctrl-port MIDI_SEND and only reads from the bridge), this client
both sends and receives on the single port-9875 connection.

Raw-message listeners (add_raw_listener/add_activity_listener/add_aborted_listener)
are called SYNCHRONOUSLY on the network thread, exactly like the C# events — this
is what lets SysExDumpCollector observe every completed SysEx with no Qt event-loop
dependency. UI code must marshal to the GUI thread itself (QTimer.singleShot(0, ...),
matching the convention already used by ctrl_client/_connect_bg in this codebase).
"""
from __future__ import annotations

import socket
import threading
import time
from typing import Callable, List, Optional

from PySide6.QtCore import QThread, Signal

MIDI_BRIDGE_PORT = 9875


class MidiStreamParser:
    """Stateful MIDI byte stream parser with running-status support.

    Handles channel messages, SysEx, and system common/real-time. Suppresses
    MIDI clock (0xF8), active sensing (0xFE), and undefined bytes (0xF9, 0xFD).
    """

    _IDLE, _NEED_DATA, _SYSEX = range(3)

    def __init__(self,
                 on_message: Callable[[bytes], None],
                 on_activity: Optional[Callable[[], None]] = None,
                 on_aborted: Optional[Callable[[int, int], None]] = None):
        self._on_message = on_message
        self._on_activity = on_activity
        self._on_aborted = on_aborted
        self._state = self._IDLE
        self._status = 0
        self._data_needed = 0
        self._data_buf = [0, 0]
        self._data_count = 0
        self._sysex = bytearray()

    def reset(self):
        self._state = self._IDLE
        self._status = 0
        self._data_needed = 0
        self._data_count = 0
        self._sysex.clear()

    def feed(self, data: bytes):
        for b in data:
            self._process(b)

    def _process(self, b: int):
        if b >= 0xF8:
            if b in (0xFA, 0xFB, 0xFC, 0xFF):
                self._on_message(bytes([b]))
            return  # suppress clock/undefined/active-sensing

        if self._state == self._SYSEX:
            if b == 0xF7:
                self._sysex.append(0xF7)
                self._on_message(bytes(self._sysex))
                self._sysex.clear()
                self._state = self._IDLE
            elif b & 0x80:
                if self._on_aborted:
                    self._on_aborted(len(self._sysex), b)
                self._sysex.clear()
                self._state = self._IDLE
                self._process_status(b)
            else:
                self._sysex.append(b)
                if self._on_activity and (len(self._sysex) & 0x1FF) == 0:
                    self._on_activity()  # pulse every 512 bytes
            return

        if b == 0xF0:
            self._sysex.clear()
            self._sysex.append(0xF0)
            self._state = self._SYSEX
            if self._on_activity:
                self._on_activity()
            return

        if b & 0x80:
            self._process_status(b)
            return

        # Data byte — requires active status
        if self._state == self._IDLE:
            return

        self._data_buf[self._data_count] = b
        self._data_count += 1
        if self._data_count < self._data_needed:
            return

        msg = bytes([self._status] + self._data_buf[:self._data_needed])
        self._data_count = 0
        # Running status: stay in NEED_DATA with the same status/data_needed
        self._on_message(msg)

    def _process_status(self, b: int):
        self._status = b
        self._data_count = 0
        self._data_needed = self._data_bytes_for(b)

        if self._data_needed == 0:
            self._on_message(bytes([b]))
            if b >= 0xF0:  # system common clears running status
                self._state = self._IDLE
        else:
            self._state = self._NEED_DATA

    @staticmethod
    def _data_bytes_for(status: int) -> int:
        hi = status & 0xF0
        if hi in (0x80, 0x90, 0xA0, 0xB0, 0xE0):
            return 2
        if hi in (0xC0, 0xD0):
            return 1
        if status in (0xF1, 0xF3):
            return 1  # MTC quarter-frame, song select
        if status == 0xF2:
            return 2  # song position pointer
        return 0


class MidiBridgeClient(QThread):
    """Reconnecting TCP client for the MIDI bridge (port 9875).

    `message_received`/`sysex_activity_signal`/`connection_changed` are Qt
    signals for UI consumption (auto-marshaled to the connecting/GUI thread by
    Qt's queued cross-thread connections). `add_raw_listener` etc. register
    plain callbacks invoked synchronously on the network thread — for the dump
    collector, which cannot rely on a Qt event loop running on its own thread.
    """

    message_received = Signal(bytes)
    connection_changed = Signal(bool)

    def __init__(self, host: str, port: int = MIDI_BRIDGE_PORT, parent=None):
        super().__init__(parent)
        self._host = host
        self._port = port
        self._stop = False
        self._sock: Optional[socket.socket] = None
        self._sock_lock = threading.Lock()

        self._raw_listeners: List[Callable[[bytes], None]] = []
        self._activity_listeners: List[Callable[[], None]] = []
        self._aborted_listeners: List[Callable[[int, int], None]] = []
        self._listeners_lock = threading.Lock()

    # ── Listener registration (thread-safe) ─────────────────────────────────

    def add_raw_listener(self, cb: Callable[[bytes], None]):
        with self._listeners_lock:
            self._raw_listeners.append(cb)

    def remove_raw_listener(self, cb: Callable[[bytes], None]):
        with self._listeners_lock:
            if cb in self._raw_listeners:
                self._raw_listeners.remove(cb)

    def add_activity_listener(self, cb: Callable[[], None]):
        with self._listeners_lock:
            self._activity_listeners.append(cb)

    def remove_activity_listener(self, cb: Callable[[], None]):
        with self._listeners_lock:
            if cb in self._activity_listeners:
                self._activity_listeners.remove(cb)

    def add_aborted_listener(self, cb: Callable[[int, int], None]):
        with self._listeners_lock:
            self._aborted_listeners.append(cb)

    def remove_aborted_listener(self, cb: Callable[[int, int], None]):
        with self._listeners_lock:
            if cb in self._aborted_listeners:
                self._aborted_listeners.remove(cb)

    # ── Sending ──────────────────────────────────────────────────────────────

    def send_bytes(self, data: bytes) -> bool:
        """Inject raw MIDI/SysEx bytes into the Kronos (daemon splits >4096B writes)."""
        with self._sock_lock:
            sock = self._sock
        if sock is None:
            return False
        try:
            for i in range(0, len(data), 4096):
                sock.sendall(data[i:i + 4096])
            return True
        except OSError:
            return False

    @property
    def is_connected(self) -> bool:
        with self._sock_lock:
            return self._sock is not None

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def stop(self):
        self._stop = True
        with self._sock_lock:
            sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def dispose(self):
        self.stop()
        self.wait(3000)

    def run(self):
        retry_s = 2.0
        while not self._stop:
            try:
                sock = socket.create_connection((self._host, self._port), timeout=5.0)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                with self._sock_lock:
                    self._sock = sock
                self.connection_changed.emit(True)
                retry_s = 2.0
                self._read_loop(sock)
            except OSError:
                pass
            finally:
                with self._sock_lock:
                    if self._sock is not None:
                        try:
                            self._sock.close()
                        except OSError:
                            pass
                        self._sock = None
                self.connection_changed.emit(False)

            if self._stop:
                return
            for _ in range(int(retry_s * 10)):
                if self._stop:
                    return
                time.sleep(0.1)
            retry_s = min(retry_s * 2, 30.0)

    def _read_loop(self, sock: socket.socket):
        parser = MidiStreamParser(
            on_message=self._on_parsed_message,
            on_activity=self._on_activity,
            on_aborted=self._on_aborted,
        )
        sock.settimeout(1.0)
        while not self._stop:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            if not chunk:
                return
            parser.feed(chunk)

    # ── Fan-out (called on the network thread) ──────────────────────────────

    def _on_parsed_message(self, msg: bytes):
        with self._listeners_lock:
            listeners = list(self._raw_listeners)
        for cb in listeners:
            try:
                cb(msg)
            except Exception:
                pass
        self.message_received.emit(msg)

    def _on_activity(self):
        with self._listeners_lock:
            listeners = list(self._activity_listeners)
        for cb in listeners:
            try:
                cb()
            except Exception:
                pass

    def _on_aborted(self, nbytes: int, interrupt: int):
        with self._listeners_lock:
            listeners = list(self._aborted_listeners)
        for cb in listeners:
            try:
                cb(nbytes, interrupt)
            except Exception:
                pass
