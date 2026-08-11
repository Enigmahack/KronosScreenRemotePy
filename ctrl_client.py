"""
CtrlClient — persistent TCP connection to the Kronos control port (7374).

Commands are queued and sent in a background thread.  TOUCH_MOVE commands are
coalesced so only the latest pending position is sent (same logic as the C# version
using Interlocked.Exchange).

STATE polling rides this same persistent connection (query_state()) instead of
opening a fresh one-shot connection every poll tick — the daemon's control-port
dispatcher (screenremote.c's process_ctrl_cmd()) answers STATE identically
whether it arrives over a one-shot connection or an established CTRL_PERSIST
session, so a poll every ~1s was paying a full TCP connect/close cycle for no
reason (see api.md section 6). query_state() falls back to the one-shot query()
if the persistent session doesn't reply in time — e.g. mid-reconnect, or the
daemon revoked ownership (api.md section 13) — so a transient hiccup degrades
gracefully instead of reporting the daemon unreachable.
"""
from __future__ import annotations
import logging
import queue
import socket
import threading
import time
from typing import Callable, List, Optional

log = logging.getLogger(__name__)

CTRL_PORT = 7374
_PERSIST_HEADER = b"CTRL_PERSIST\n"

# Sentinel for "flush the pending TOUCH_MOVE"
_FLUSH_MOVE = object()

# Marker for a queued command that must NOT establish a connection — see
# send_existing_only. Queued as (_ONLY_IF_CONNECTED, cmd).
_ONLY_IF_CONNECTED = object()


class CtrlClient:
    def __init__(self):
        self._host: Optional[str] = None
        self._port: int = CTRL_PORT
        self._queue: queue.Queue = queue.Queue()
        self._pending_move: Optional[str] = None
        self._pending_move_lock = threading.Lock()
        self._sock: Optional[socket.socket] = None
        self._sock_lock = threading.Lock()
        self._line_listeners: List[Callable[[bytes], None]] = []
        self._listeners_lock = threading.Lock()
        self._thread = threading.Thread(target=self._send_loop, daemon=True, name="CtrlClient")
        self._thread.start()

    # ── Public API ─────────────────────────────────────────────────────────────

    def send(self, host: str, port: int, cmd: str):
        self._host = host
        self._port = port

        if cmd.startswith("TOUCH_MOVE "):
            with self._pending_move_lock:
                self._pending_move = cmd
            self._queue.put(_FLUSH_MOVE)
        else:
            # Flush any pending move first to preserve ordering
            with self._pending_move_lock:
                pm, self._pending_move = self._pending_move, None
            if pm is not None:
                self._queue.put(pm)
            self._queue.put(cmd)

    def send_existing_only(self, host: str, port: int, cmd: str) -> bool:
        """Queue `cmd` to be sent only if a persistent session is established.

        Returns False immediately when there is visibly no session, so a caller
        that merely wants to piggy-back on an open one can fall back at once
        instead of waiting out its timeout. A True return is not a delivery
        guarantee: the session can still be gone by the time the send loop gets
        there, in which case the command is dropped rather than triggering a
        connect. Goes through the same queue as send() so the socket keeps a
        single writer and command ordering is preserved.
        """
        self._host = host
        self._port = port
        with self._sock_lock:
            if self._sock is None:
                return False
        self._queue.put((_ONLY_IF_CONNECTED, cmd))
        return True

    def reset(self):
        """Drop the persistent connection (e.g. on host change or reconnect)."""
        with self._sock_lock:
            s, self._sock = self._sock, None
        if s:
            try:
                s.close()
            except Exception:
                pass

    def query(self, host: str, port: int, cmd: str, timeout_ms: int = 2000) -> Optional[str]:
        """
        Send a command on a short-lived connection and return the trimmed response.
        Does NOT use CTRL_PERSIST — the server handles it as a one-shot command.
        """
        try:
            with socket.create_connection((host, port), timeout=timeout_ms / 1000) as s:
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                s.sendall((cmd + "\n").encode("ascii"))
                s.settimeout(timeout_ms / 1000)
                data = s.recv(256)
                return data.decode("ascii", errors="replace").strip() if data else None
        except Exception:
            return None

    def query_state(self, host: str, port: int, timeout_ms: int = 800) -> Optional[str]:
        """STATE query piggy-backed on the persistent connection, matched by the
        reply's 'MODE=' prefix (unique among control-port replies). Avoids a
        fresh TCP connect/close every poll tick.

        Rides an *existing* persistent session only — never establishes one,
        enforced by send_existing_only all the way down to the send loop rather
        than by a check the send could race past. Otherwise a poll that lands
        after _disconnect()'s reset() (which races the _poll_in_progress guard)
        would open a fresh CTRL_PERSIST session on the daemon that nothing ever
        tears down, and a poll during a dead control port (daemon down / still
        booting) would burn a 2s connect timeout every tick. Falls back to the
        one-shot query() whenever there's no session to ride or it doesn't reply
        in time, so both cases still fail (or succeed) in line with the previous
        one-shot-only behavior."""
        result: list = [None]
        done = threading.Event()

        def on_line(line: bytes):
            if result[0] is None and line.startswith(b"MODE="):
                result[0] = line.decode("ascii", errors="replace")
                done.set()

        self.add_line_listener(on_line)
        try:
            # send_existing_only, not send: the check-then-send below is racy,
            # and a plain send() would have _send_one open a fresh CTRL_PERSIST
            # session on a miss — the exact orphan-session and 2s-connect-stall
            # behaviour this method exists to avoid.
            if not self.send_existing_only(host, port, "STATE"):
                return self.query(host, port, "STATE", timeout_ms=timeout_ms)
            done.wait(timeout_ms / 1000)
        finally:
            self.remove_line_listener(on_line)

        if result[0] is not None:
            return result[0]
        return self.query(host, port, "STATE", timeout_ms=timeout_ms)

    def query_multi(self, host: str, port: int, cmd: str, timeout_ms: int = 5000) -> Optional[str]:
        """
        Send a command and read a multi-line response terminated by 'OK\\n'.
        Used for SYSINFO which returns many key=value lines before the OK sentinel.
        """
        try:
            with socket.create_connection((host, port), timeout=timeout_ms / 1000) as s:
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                s.sendall((cmd + "\n").encode("ascii"))
                s.settimeout(timeout_ms / 1000)
                buf = b""
                while True:
                    try:
                        chunk = s.recv(4096)
                    except socket.timeout:
                        break
                    if not chunk:
                        break
                    buf += chunk
                    if buf.rstrip().endswith(b"OK") or b"\nOK\n" in buf:
                        break
                    if len(buf) > 65536:
                        break
                return buf.decode("ascii", errors="replace").strip() if buf else None
        except Exception:
            return None

    # ── Reply-line listeners (thread-safe) ──────────────────────────────────────

    def add_line_listener(self, cb: Callable[[bytes], None]):
        with self._listeners_lock:
            self._line_listeners.append(cb)

    def remove_line_listener(self, cb: Callable[[bytes], None]):
        with self._listeners_lock:
            if cb in self._line_listeners:
                self._line_listeners.remove(cb)

    def _dispatch_line(self, line: bytes):
        with self._listeners_lock:
            listeners = list(self._line_listeners)
        for cb in listeners:
            try:
                cb(line)
            except Exception:
                pass

    # ── Background send loop ───────────────────────────────────────────────────

    def _send_loop(self):
        while True:
            item = self._queue.get()

            if item is _FLUSH_MOVE:
                with self._pending_move_lock:
                    cmd, self._pending_move = self._pending_move, None
                if cmd is None:
                    continue
                self._send_one(cmd)
            elif isinstance(item, tuple) and item[0] is _ONLY_IF_CONNECTED:
                self._send_one(item[1], allow_connect=False)
            else:
                self._send_one(item)

    def _send_one(self, cmd: str, allow_connect: bool = True):
        if not self._host:
            return
        data = (cmd + "\n").encode("ascii")

        # Try existing socket first
        with self._sock_lock:
            sock = self._sock

        if sock is not None:
            try:
                sock.sendall(data)
                return
            except OSError:
                self._drop_socket(sock)

        if not allow_connect:
            return

        # Need a new persistent connection
        sock = self._connect_persistent()
        if sock is None:
            return
        try:
            sock.sendall(data)
        except OSError:
            self._drop_socket(sock)

    def _connect_persistent(self) -> Optional[socket.socket]:
        if not self._host:
            return None
        try:
            s = socket.create_connection((self._host, self._port), timeout=2.0)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.sendall(_PERSIST_HEADER)
            with self._sock_lock:
                self._sock = s
            threading.Thread(target=self._drain_loop, args=(s,), daemon=True,
                             name="CtrlDrain").start()
            return s
        except Exception as e:
            log.warning("persistent connect failed: %s", e)
            return None

    def _drain_loop(self, sock: socket.socket):
        """Line-buffer replies so the server's send buffer never fills, and
        dispatch each complete line to registered listeners (e.g. query_state's
        MODE= matcher). Lines nobody is waiting for ('OK\\n'/'ERR\\n' from
        fire-and-forget commands) are simply dispatched to no listener."""
        buf = bytearray()
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    line = bytes(buf[:nl])
                    del buf[:nl + 1]
                    self._dispatch_line(line)
        except Exception:
            pass
        finally:
            self._drop_socket(sock)

    def _drop_socket(self, sock: socket.socket):
        with self._sock_lock:
            if self._sock is sock:
                self._sock = None
        try:
            sock.close()
        except Exception:
            pass


# Module-level singleton — mirrors the C# static class pattern
_instance: Optional[CtrlClient] = None
_instance_lock = threading.Lock()


def get() -> CtrlClient:
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = CtrlClient()
    return _instance


def play_macro(host: str, port: int, steps: List[str], step_delay_ms: int):
    """Send a macro's steps in order, pausing step_delay_ms between them.

    Blocking — callers run it on a background thread. Shared by the main
    window's keybind trigger and the Settings macro editor's Play button, which
    previously carried identical copies differing only in where they read the
    host and port from.
    """
    if not host or not steps:
        return
    client = get()
    for i, step in enumerate(steps):
        if i:
            time.sleep(step_delay_ms / 1000.0)
        client.send(host, port, step)
