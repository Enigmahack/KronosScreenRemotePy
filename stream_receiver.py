"""
StreamReceiver — connects to the Kronos stream port (7373), performs the KSCR
handshake, and delivers 8bpp palette-indexed frames to the GUI thread via Qt signals.

Pull mode: client sends 0xFF per frame; server responds with frame.
Change mode: server sends frames whenever the display changes.
"""
from __future__ import annotations
import logging
import select
import socket
import struct
import time
from typing import List, Optional

from PySide6.QtCore import QThread, Signal

from models import PaletteEntry

log = logging.getLogger(__name__)

STREAM_PORT = 7373
_MAGIC      = b"KSCR"
_MODE_PULL  = 0x01
_MODE_CHANGE = 0x02

# Largest packet we will allocate for. `length` is an unbounded uint32 straight
# off the wire, so a desynced stream can otherwise ask us to allocate up to 4 GiB
# before we have any chance to reject it. Two full frames is far above any legal
# packet (a full frame is width*height; dirty rects are strictly smaller).
_MAX_PACKET = 2 * 1024 * 1024

# Sanity bounds on the handshake's declared frame size, for the same reason.
_MAX_DIMENSION = 8192


class StreamReceiver(QThread):
    frame_received = Signal(bytes)   # raw 8bpp frame bytes
    disconnected   = Signal()

    def __init__(self, host: str, port: int, pull_mode: bool, fps: int,
                 username: str = "", password: str = "", parent=None):
        super().__init__(parent)
        self._host      = host
        self._port      = port
        self._mode      = _MODE_PULL if pull_mode else _MODE_CHANGE
        # Protocol: 1-15, 0 = daemon uses its maximum. Clamp negative garbage from a
        # hand-edited settings file / CLI arg to 0 (daemon max) rather than letting a
        # negative int wrap (mirrors StreamReceiver.cs's Math.Clamp(fps, 0, 15)).
        self._fps       = min(max(fps, 0), 15)
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
            log.debug("connecting to %s:%s mode=%s fps=%s",
                      self._host, self._port, self._mode, self._fps)
            s.connect((self._host, self._port))
            u_bytes = self._username.encode('ascii', errors='replace')[:64]
            p_bytes = self._password.encode('ascii', errors='replace')[:128]
            hello = (_MAGIC
                     + bytes([0x02, self._mode, self._fps,
                               len(u_bytes), len(p_bytes)])
                     + u_bytes + p_bytes)
            # Never log `hello` itself — it carries the FTP username and
            # password in cleartext.
            log.debug("sending hello (%d bytes, user=%d pass=%d)",
                      len(hello), len(u_bytes), len(p_bytes))
            s.sendall(hello)

            # 5-byte header: KSCR magic + 1-byte status
            hdr = _recv_all(s, 5)
            if hdr is None or hdr[:4] != _MAGIC:
                raise ConnectionError("Invalid response from daemon")
            status = hdr[4]
            if status == 0x01:
                raise PermissionError("FTP authentication rejected by Kronos daemon.")
            if status == 0x02:
                raise ConnectionError("Kronos could not look up credentials — user not found.")
            if status != 0x00:
                raise ConnectionError(f"Handshake rejected by daemon (status 0x{status:02X})")

            # Remaining payload: w(2) + h(2) + palette(256*3)
            payload = _recv_all(s, 2 + 2 + 256 * 3)
            if payload is None:
                raise ConnectionError("Handshake payload truncated")
            width  = payload[0] | (payload[1] << 8)
            height = payload[2] | (payload[3] << 8)
            # These size every buffer below and the QImage the GUI builds from
            # each frame, so reject nonsense here rather than allocating on it.
            if not (0 < width <= _MAX_DIMENSION and 0 < height <= _MAX_DIMENSION):
                raise ConnectionError(
                    f"Daemon reported an unusable frame size ({width}x{height})")
            self.width, self.height = width, height

            pal: list[PaletteEntry] = []
            for i in range(256):
                o = 4 + i * 3
                pal.append(PaletteEntry(payload[o], payload[o + 1], payload[o + 2]))
            self.palette = pal

            log.info("handshake complete: %dx%d", self.width, self.height)
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
        log.debug("run() started, mode=%s", self._mode)
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
                        log.info("stream ended: pull sendall failed: %s", e)
                        break
                    if not _poll(self._sock, 5.0):
                        log.info("stream ended: no frame within 5s of a pull request")
                        break
                else:
                    if not _poll(self._sock, 5.0):
                        continue   # idle gap is normal in change mode

                if not _recv_into(self._sock, hdr_buf, 4):
                    log.info("stream ended: header recv failed (connection closed?)")
                    break
                length = struct.unpack_from("<I", hdr_buf)[0]

                if length == frame_size:
                    # Full frame — receive into master_frame, emit immutable copy.
                    if not _recv_into(self._sock, master_frame, frame_size):
                        log.info("stream ended: full frame recv failed mid-transfer")
                        break
                    self.frame_received.emit(bytes(master_frame))
                elif 4 < length < frame_size:
                    # Dirty rect with PackBits RLE — decode into master_frame, emit copy.
                    if not _recv_into(self._sock, sub_hdr, 4):
                        log.info("stream ended: sub-header recv failed")
                        break
                    first_row = sub_hdr[0] | (sub_hdr[1] << 8)
                    row_count = sub_hdr[2] | (sub_hdr[3] << 8)
                    rle_bytes = length - 4
                    raw_bytes = row_count * self.width
                    if raw_bytes > frame_size or first_row + row_count > self.height:
                        log.warning("stream ended: dirty rect out of bounds "
                                    "(raw=%d frame=%d row0=%d rows=%d h=%d)",
                                    raw_bytes, frame_size, first_row, row_count, self.height)
                        break
                    rle_view = memoryview(rle_scratch)[:rle_bytes]
                    if not _recv_into(self._sock, rle_view, rle_bytes):
                        log.info("stream ended: rle payload recv failed")
                        break
                    off = first_row * self.width
                    got = _packbits_expand(rle_view, rle_bytes, master_frame, off, raw_bytes)
                    if got != raw_bytes:
                        log.warning("stream ended: packbits expand produced %d bytes, expected %d",
                                    got, raw_bytes)
                        break
                    self.frame_received.emit(bytes(master_frame))
                elif length <= _MAX_PACKET:
                    # Unknown packet — drain and skip.
                    log.debug("unknown packet length=%d — draining", length)
                    data = _recv_all(self._sock, length)
                    if data is None:
                        log.info("stream ended: unknown packet drain failed")
                        break
                    self.frame_received.emit(data)
                else:
                    # Beyond any legal packet — the stream has desynced, and
                    # draining would mean allocating up to 4 GiB on the
                    # sender's say-so. Drop the connection instead.
                    log.warning("stream ended: packet length %d exceeds the %d-byte limit",
                                length, _MAX_PACKET)
                    break

                if self._mode == _MODE_PULL and interval > 0:
                    time.sleep(interval)

        except Exception:
            log.exception("unhandled error in stream receive loop")
        finally:
            log.debug("run() exiting, _stop=%s", self._stop)
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
    """PackBits decoder (Apple/TIFF variant). Returns bytes written to dst.

    Every run is clamped against both the remaining source and the remaining
    destination before it is copied. `dst` is the caller's persistent frame
    buffer and slice assignment on a bytearray RESIZES it when the two sides
    differ in length, so an over-long run or a truncated payload would other-
    wise silently grow or shrink the frame buffer — after which the next
    full-frame recv_into fails against a wrong-sized buffer and kills the
    stream. Stopping short instead lets the caller's `got != raw_bytes` check
    reject the packet with the buffer still intact.
    """
    src_view = memoryview(src) if not isinstance(src, memoryview) else src
    si = 0
    di = 0
    while si < src_len and di < dst_len:
        n = src_view[si]; si += 1
        if n < 128:
            count = min(n + 1, src_len - si, dst_len - di)
            if count <= 0:
                break
            dst[dst_offset + di: dst_offset + di + count] = src_view[si: si + count]
            si += count; di += count
        elif n != 128:
            if si >= src_len:
                break
            count = min(257 - n, dst_len - di)
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
    try:
        r, _, _ = select.select([sock], [], [], timeout_sec)
        return bool(r)
    except OSError:
        return False
