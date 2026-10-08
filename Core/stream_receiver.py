"""
StreamReceiver — connects to the Kronos/Nautilus stream port (7373), performs
the KSCR v3 handshake, and delivers frames to the GUI thread via Qt signals.

Protocol v3 supports both Kronos (8bpp indexed color) and Nautilus (RGB565).

Pull mode: client sends 0xFF per frame; server responds with frame.
Change mode: server sends frames whenever the display changes.

Authentication: FTP username and password must be provided. These are configured
in the Kronos/Nautilus UI and must match exactly for authentication to succeed.
"""
from __future__ import annotations
import logging
import select
import socket
import struct
import time
import zlib
from typing import List, Optional

from PySide6.QtCore import QThread, Signal

from Models.models import PaletteEntry

log = logging.getLogger(__name__)

STREAM_PORT = 7373
_MAGIC      = b"KSCR"
_MODE_PULL  = 0x01
_MODE_CHANGE = 0x02

# Sanity bounds on the handshake's declared frame size, so a corrupt/desynced
# stream can't make us allocate on its say-so before we've validated anything.
_MAX_DIMENSION = 8192


_STATUS_FORMAT_NEEDS_NEWER_VERSION = 0x03
_STATUS_VERSION_MISMATCH = 0x04
_HELLO_VERSION = 0x03


class StreamVersionError(ConnectionError):
    """Handshake status 0x03 / 0x04: the daemon and this client don't share a stream protocol version."""

    def __init__(self, status: int, ver_min: Optional[int], ver_max: Optional[int]):
        self.status, self.ver_min, self.ver_max = status, ver_min, ver_max
        super().__init__(version_failure_message(status, ver_min, ver_max))


def version_failure_message(status: int, ver_min: Optional[int], ver_max: Optional[int]) -> str:
    rng = "" if ver_min is None or ver_max is None else f" (the daemon accepts stream versions {ver_min}-{ver_max}; this client speaks {_HELLO_VERSION})"
    if ver_max is not None and ver_max < _HELLO_VERSION:
        return f"The instrument daemon is too old for this client - update the ScreenRemote daemon{rng}."
    if ver_min is not None and ver_min > _HELLO_VERSION:
        return f"This client is too old for the instrument daemon - update the client{rng}."
    if status == _STATUS_FORMAT_NEEDS_NEWER_VERSION:
        return f"This unit's display format needs a newer stream version than this client sent{rng}."
    return f"The daemon rejected this client's stream protocol version{rng}."


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
        self.stream_fmt = 0  # 0 = INDEX8, 1 = RGB565
        self.bytes_per_pixel = 1  # Updated after handshake

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
            # API requires username length 1-64 bytes. Empty credentials are not allowed.
            if not self._username:
                raise ConnectionError("Username is required for authentication")
            u_bytes = self._username.encode('ascii', errors='replace')[:64]
            p_bytes = self._password.encode('ascii', errors='replace')[:128]
            hello = (_MAGIC
                     + bytes([_HELLO_VERSION, self._mode, self._fps,
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
                raise PermissionError("FTP authentication rejected by instrument daemon (bad credentials or locked "
                                      "account; a daemon older than 3.0.2 also answers a v3 hello this way).")
            if status == 0x02:
                raise ConnectionError("Instrument could not look up credentials — user not found.")
            if status in (_STATUS_FORMAT_NEEDS_NEWER_VERSION, _STATUS_VERSION_MISMATCH):
                # Both are followed by ver_min/ver_max (docs/api.md 3.4) so the client can say which side is too old.
                vers = _recv_all(s, 2)
                raise StreamVersionError(status, vers[0] if vers else None, vers[1] if vers else None)
            if status != 0x00:
                raise ConnectionError(f"Handshake rejected by daemon (status 0x{status:02X})")

            # V3 response: w(2) + h(2) + fmt(1) + bpp(1) + enc(1) + flags(1) = 8 bytes
            # If INDEX8 format, followed by palette(256*3)
            # (V2 response was: w(2) + h(2) + palette(256*3), always included)
            payload = _recv_all(s, 8)
            if payload is None:
                raise ConnectionError("Handshake payload truncated")
            width  = payload[0] | (payload[1] << 8)
            height = payload[2] | (payload[3] << 8)
            stream_fmt = payload[4]  # 0 = INDEX8, 1 = RGB565
            # These size every buffer below and the QImage the GUI builds from
            # each frame, so reject nonsense here rather than allocating on it.
            if not (0 < width <= _MAX_DIMENSION and 0 < height <= _MAX_DIMENSION):
                raise ConnectionError(
                    f"Daemon reported an unusable frame size ({width}x{height})")
            self.width, self.height = width, height
            self.stream_fmt = stream_fmt
            self.bytes_per_pixel = 1 if stream_fmt == 0 else 2

            # Read palette if INDEX8 format
            pal: list[PaletteEntry] = []
            if stream_fmt == 0:  # INDEX8
                pal_payload = _recv_all(s, 256 * 3)
                if pal_payload is None:
                    raise ConnectionError("Palette payload truncated")
                for i in range(256):
                    o = i * 3
                    pal.append(PaletteEntry(pal_payload[o], pal_payload[o + 1], pal_payload[o + 2]))
            else:
                # RGB565 format (Nautilus) - no palette needed
                log.debug("RGB565 format detected - no palette needed (2 bytes/pixel)")
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
        """v3 frame envelope (docs/api.md §4.5): every frame is one self-describing
        rect — payload_len(4) [len], then enc(1) x0(2) y0(2) w(2) h(2) data(...).
        There is no separate "full frame" wire format in v3; a full frame is just
        the rect (0,0,width,height) like any other. Rects accumulate into a
        persistent canvas (master_frame) and the whole canvas is emitted after
        each one, mirroring StreamReceiver.cs's V3FrameDecoder.ApplyRect.
        """
        self._stop = False
        log.debug("run() started, mode=%s, format=%s, bpp=%d", self._mode,
                  "RGB565" if self.stream_fmt == 1 else "INDEX8", self.bytes_per_pixel)
        interval    = (1.0 / self._fps) if self._mode == _MODE_PULL and self._fps > 0 else 0.0
        hdr_buf     = bytearray(4)
        frame_size  = self.width * self.height * self.bytes_per_pixel
        master_frame = bytearray(frame_size)   # persistent reconstructed canvas
        # Generous bound on a rect payload — a raw uncompressed full frame plus
        # PackBits' worst-case ~1/128 expansion, with headroom (mirrors
        # StreamReceiver.cs's MaxRectPayload).
        max_payload  = 9 + frame_size + 16 * 1024
        payload_buf  = bytearray(max_payload)
        decode_buf   = bytearray(frame_size)   # scratch for packbits/zlib-decoded rect data
        first_pull_request = True

        try:
            while not self._stop:
                if self._mode == _MODE_PULL:
                    # 0xFF = full frame (first request only), 0xFE = delta since the
                    # last frame this client received (every later poll).
                    try:
                        self._sock.sendall(b"\xff" if first_pull_request else b"\xfe")
                    except OSError as e:
                        log.info("stream ended: pull sendall failed: %s", e)
                        break
                    first_pull_request = False
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

                if length < 9 or length > max_payload:
                    log.warning("stream ended: invalid v3 frame length %d (max %d)",
                                length, max_payload)
                    break

                payload = memoryview(payload_buf)[:length]
                if not _recv_into(self._sock, payload, length):
                    log.info("stream ended: rect payload recv failed")
                    break

                enc = payload[0]
                x0  = payload[1] | (payload[2] << 8)
                y0  = payload[3] | (payload[4] << 8)
                w   = payload[5] | (payload[6] << 8)
                h   = payload[7] | (payload[8] << 8)

                if w == 0 or h == 0:
                    # Explicit "nothing changed" reply (0xFE poll only) — canvas
                    # untouched, nothing new to emit.
                    if self._mode == _MODE_PULL and interval > 0:
                        time.sleep(interval)
                    continue

                if x0 + w > self.width or y0 + h > self.height:
                    log.warning("stream ended: v3 rect out of bounds "
                                "(x0=%d y0=%d w=%d h=%d frame=%dx%d)",
                                x0, y0, w, h, self.width, self.height)
                    break

                data     = payload[9:]
                data_len = length - 9
                decoded_len = w * h * self.bytes_per_pixel

                if enc == 0:  # raw
                    if data_len != decoded_len:
                        log.warning("stream ended: raw rect length mismatch "
                                    "(got %d, want %d)", data_len, decoded_len)
                        break
                    decoded = data
                elif enc == 1:  # PackBits
                    dview = memoryview(decode_buf)[:decoded_len]
                    got = _packbits_expand(data, data_len, decode_buf, 0, decoded_len)
                    if got != decoded_len:
                        log.warning("stream ended: packbits expand produced %d bytes, "
                                    "expected %d", got, decoded_len)
                        break
                    decoded = dview
                elif enc == 2:  # zlib / RFC 1950 deflate
                    try:
                        out = zlib.decompress(bytes(data))
                    except zlib.error as e:
                        log.warning("stream ended: zlib inflate failed: %s", e)
                        break
                    if len(out) != decoded_len:
                        log.warning("stream ended: zlib rect length mismatch "
                                    "(got %d, want %d)", len(out), decoded_len)
                        break
                    decoded = out
                else:
                    log.warning("stream ended: unknown rect encoding %d", enc)
                    break

                # Composite row-by-row: the rect's own width may be narrower than
                # the canvas, so a single contiguous copy would misalign rows.
                src_pitch = w * self.bytes_per_pixel
                dst_pitch = self.width * self.bytes_per_pixel
                for row in range(h):
                    s = row * src_pitch
                    d = (y0 + row) * dst_pitch + x0 * self.bytes_per_pixel
                    master_frame[d:d + src_pitch] = decoded[s:s + src_pitch]

                self.frame_received.emit(bytes(master_frame))

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
        if not self.isRunning() and self._sock is not None:
            self._sock.close()


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
