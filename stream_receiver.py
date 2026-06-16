"""
StreamReceiver — connects to the Kronos stream port (7373), performs the KSCR
handshake, and delivers 8bpp palette-indexed frames to the GUI thread via Qt signals.

Pull mode: client sends 0xFF per frame; server responds with frame.
Change mode: server sends frames whenever the display changes.
"""
from __future__ import annotations
import socket
import struct
import time
from typing import List, Optional

from PySide6.QtCore import QThread, Signal

from models import PaletteEntry


STREAM_PORT = 7373
_MAGIC      = b"KSCR"
_MODE_PULL  = 0x01
_MODE_CHANGE = 0x02


class StreamReceiver(QThread):
    frame_received = Signal(bytes)   # raw 8bpp frame bytes
    disconnected   = Signal()

    def __init__(self, host: str, port: int, pull_mode: bool, fps: int,
                 username: str = "", password: str = "", parent=None):
        super().__init__(parent)
        self._host      = host
        self._port      = port
        self._mode      = _MODE_PULL if pull_mode else _MODE_CHANGE
        self._fps       = min(max(fps, 1), 15)
        self._username  = username
        self._password  = password
        self._sock: Optional[socket.socket] = None
        self._stop      = False

        self.width   = 800
        self.height  = 600
        self.palette: List[PaletteEntry] = []

    # ── Public API ─────────────────────────────────────────────────────────────

    def connect_to_host(self):
        """Blocking handshake with 10-second timeout. Raises on failure."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 512 * 1024)
        # Keepalive prevents silent idle-connection drops (OS/firewall timeouts)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        try:
            if hasattr(socket, 'TCP_KEEPIDLE'):   # Linux / macOS
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
            if hasattr(socket, 'TCP_KEEPINTVL'):
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)
            if hasattr(socket, 'TCP_KEEPCNT'):
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
            if hasattr(socket, 'SIO_KEEPALIVE_VALS'):  # Windows
                s.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 30_000, 5_000))
        except OSError:
            pass  # platform may not support all options
        s.settimeout(10.0)
        try:
            print(f"[stream] connecting to {self._host}:{self._port} mode={self._mode} fps={self._fps}")
            s.connect((self._host, self._port))
            u_bytes = self._username.encode('ascii', errors='replace')[:64]
            p_bytes = self._password.encode('ascii', errors='replace')[:128]
            hello = (_MAGIC
                     + bytes([0x02, self._mode, self._fps,
                               len(u_bytes), len(p_bytes)])
                     + u_bytes + p_bytes)
            print(f"[stream] sending hello: {hello!r}")
            s.sendall(hello)
            print("[stream] hello sent, waiting for response...")

            # 5-byte header: KSCR magic + 1-byte status
            hdr = _recv_all(s, 5)
            print(f"[stream] hdr recv: {hdr!r}")
            if hdr is None or hdr[:4] != _MAGIC:
                raise ConnectionError("Invalid response from daemon")
            status = hdr[4]
            if status == 0x01:
                raise PermissionError("FTP authentication rejected by Kronos daemon.")
            if status == 0x02:
                raise ConnectionError("Kronos could not look up credentials — user not found.")
            if status != 0x00:
                raise ConnectionError(f"Handshake rejected by daemon (status 0x{status:02X})")

            print("[stream] status OK, reading 772-byte payload...")
            # Remaining payload: w(2) + h(2) + palette(256*3)
            payload = _recv_all(s, 2 + 2 + 256 * 3)
            print(f"[stream] payload recv: {len(payload) if payload is not None else None} bytes")
            if payload is None:
                raise ConnectionError("Handshake payload truncated")
            self.width  = payload[0] | (payload[1] << 8)
            self.height = payload[2] | (payload[3] << 8)

            pal: list[PaletteEntry] = []
            for i in range(256):
                o = 4 + i * 3
                pal.append(PaletteEntry(payload[o], payload[o + 1], payload[o + 2]))
            self.palette = pal

            print(f"[stream] handshake complete: {self.width}x{self.height}")
            s.settimeout(None)   # switch to blocking for the recv loop
            self._sock = s
        except Exception:
            try:
                s.close()
            except Exception:
                pass
            raise

    def stop(self):
        self._stop = True
        try:
            if self._sock:
                self._sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass

    # ── QThread entry point ────────────────────────────────────────────────────

    def run(self):
        self._stop = False
        print(f"[stream] QThread run() started, mode={self._mode}")
        interval    = (1.0 / self._fps) if self._mode == _MODE_PULL and self._fps > 0 else 0.0
        hdr_buf     = bytearray(4)
        sub_hdr     = bytearray(4)
        frame_size  = self.width * self.height
        master_frame = bytearray(frame_size)  # persistent reconstructed frame
        rle_scratch  = bytearray(frame_size)  # receive scratch for RLE payload (daemon ensures < frame_size)

        try:
            while not self._stop:
                if self._mode == _MODE_PULL:
                    try:
                        self._sock.sendall(b"\xff")
                    except OSError as e:
                        print(f"[stream] BREAK: pull sendall failed: {e}")
                        break
                    if not _poll(self._sock, 5.0):
                        print("[stream] BREAK: pull poll timed out (no frame in 5s)")
                        break
                else:
                    if not _poll(self._sock, 5.0):
                        continue   # idle gap is normal in change mode

                if not _recv_into(self._sock, hdr_buf, 4):
                    print("[stream] BREAK: hdr recv failed (connection closed?)")
                    break
                length = struct.unpack_from("<I", hdr_buf)[0]
                print(f"[stream] pkt length={length} frame_size={frame_size}")

                if length == frame_size:
                    # Full frame — receive into master_frame, emit immutable copy.
                    if not _recv_into(self._sock, master_frame, frame_size):
                        print(f"[stream] BREAK: full frame recv failed mid-transfer")
                        break
                    self.frame_received.emit(bytes(master_frame))
                elif 4 < length < frame_size:
                    # Dirty rect with PackBits RLE — decode into master_frame, emit copy.
                    if not _recv_into(self._sock, sub_hdr, 4):
                        print("[stream] BREAK: sub_hdr recv failed")
                        break
                    first_row = sub_hdr[0] | (sub_hdr[1] << 8)
                    row_count = sub_hdr[2] | (sub_hdr[3] << 8)
                    rle_bytes = length - 4
                    raw_bytes = row_count * self.width
                    if raw_bytes > frame_size or first_row + row_count > self.height:
                        print(f"[stream] BREAK: dirty rect overflow raw={raw_bytes} frame={frame_size} r0={first_row} rc={row_count} h={self.height}")
                        break
                    rle_view = memoryview(rle_scratch)[:rle_bytes]
                    if not _recv_into(self._sock, rle_view, rle_bytes):
                        print("[stream] BREAK: rle payload recv failed")
                        break
                    off = first_row * self.width
                    got = _packbits_expand(rle_view, rle_bytes, master_frame, off, raw_bytes)
                    if got != raw_bytes:
                        print(f"[stream] BREAK: packbits expand got={got} expected={raw_bytes}")
                        break
                    self.frame_received.emit(bytes(master_frame))
                else:
                    # Unknown packet — drain and skip.
                    print(f"[stream] unknown pkt length={length} — draining")
                    data = _recv_all(self._sock, length)
                    if data is None:
                        print("[stream] BREAK: unknown pkt drain failed")
                        break
                    self.frame_received.emit(data)

                if self._mode == _MODE_PULL and interval > 0:
                    time.sleep(interval)

        except Exception as exc:
            import traceback
            print(f"[stream] EXCEPTION in run(): {exc}")
            traceback.print_exc()
        finally:
            print(f"[stream] run() exiting, _stop={self._stop}")
            try:
                self._sock.close()
            except Exception:
                pass
            if not self._stop:
                self.disconnected.emit()

    def dispose(self):
        self.stop()
        self.wait(3000)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _packbits_expand(src: bytes | bytearray | memoryview, src_len: int,
                     dst: bytearray, dst_offset: int, dst_len: int) -> int:
    """PackBits decoder (Apple/TIFF variant). Returns bytes written to dst."""
    src_view = memoryview(src) if not isinstance(src, memoryview) else src
    si = 0
    di = 0
    while si < src_len and di < dst_len:
        n = src_view[si]; si += 1
        if n < 128:
            count = n + 1
            dst[dst_offset + di: dst_offset + di + count] = src_view[si: si + count]
            si += count; di += count
        elif n != 128:
            count = 257 - n
            b = bytes([src_view[si]]); si += 1
            dst[dst_offset + di: dst_offset + di + count] = b * count
            di += count
        # n == 128: NOP — skip
    return di


def _recv_all(sock: socket.socket, n: int) -> Optional[bytes]:
    buf = bytearray(n)
    if _recv_into(sock, buf, n):
        return bytes(buf)
    return None


def _recv_into(sock: socket.socket, buf: bytearray | memoryview, n: int) -> bool:
    view = memoryview(buf)
    got = 0
    while got < n:
        try:
            r = sock.recv_into(view[got:], n - got)
        except OSError:
            return False
        if r == 0:
            return False
        got += r
    return True


def _poll(sock: socket.socket, timeout_sec: float) -> bool:
    import select
    try:
        r, _, _ = select.select([sock], [], [], timeout_sec)
        return bool(r)
    except OSError:
        return False
