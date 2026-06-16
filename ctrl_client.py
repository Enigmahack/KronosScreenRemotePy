"""
CtrlClient — persistent TCP connection to the Kronos control port (7374).

Commands are queued and sent in a background thread.  TOUCH_MOVE commands are
coalesced so only the latest pending position is sent (same logic as the C# version
using Interlocked.Exchange).

STATE queries use their own short-lived connection (no CTRL_PERSIST prefix) so
they don't block the command queue.
"""
from __future__ import annotations
import queue
import socket
import threading
from typing import Optional

CTRL_PORT = 7374
_PERSIST_HEADER = b"CTRL_PERSIST\n"

# Sentinel for "flush the pending TOUCH_MOVE"
_FLUSH_MOVE = object()


class CtrlClient:
    def __init__(self):
        self._host: Optional[str] = None
        self._port: int = CTRL_PORT
        self._queue: queue.Queue = queue.Queue()
        self._pending_move: Optional[str] = None
        self._pending_move_lock = threading.Lock()
        self._sock: Optional[socket.socket] = None
        self._sock_lock = threading.Lock()
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

    # ── Background send loop ───────────────────────────────────────────────────

    def _send_loop(self):
        while True:
            item = self._queue.get()

            if item is _FLUSH_MOVE:
                with self._pending_move_lock:
                    cmd, self._pending_move = self._pending_move, None
                if cmd is None:
                    continue
            else:
                cmd = item

            self._send_one(cmd)

    def _send_one(self, cmd: str):
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
            print(f"[ctrl] persistent connect failed: {e}")
            return None

    def _drain_loop(self, sock: socket.socket):
        """Discard 'OK\\n' responses so the server's send buffer never fills."""
        buf = bytearray(256)
        try:
            while True:
                n = sock.recv_into(buf)
                if n == 0:
                    break
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


def get() -> CtrlClient:
    global _instance
    if _instance is None:
        _instance = CtrlClient()
    return _instance
