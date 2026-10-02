"""
SysExDumpCollector — port of Networking/SysExDumpCollector.cs.

Collects SysEx Object Dump (func 0x73) replies off the live MIDI bridge stream.
The bridge has no per-message size cap (unlike the daemon's SYSEX ctrl command,
which is capped at 64 KB and returns only one message), so this is how a large
object (a Set List is ~79 KB) or a multi-object bank dump is retrieved.

Blocking: every method here must be called from a worker thread, never the GUI
thread. The timing constants (idle/no_response/stall/batch pacing) are copied
verbatim from the Windows client — they were empirically tuned against real
hardware and are load-bearing (a tighter batch pacing corrupts a request on the
Kronos MIDI-in and pops a "MIDI Receiving Error" dialog on the unit).

`codec` selects Kronos (4-byte Exclusive Header) vs Nautilus (6-byte) wire
framing — port of C#'s IKorgSysExCodec pairing (SysExDumpCollector(transport,
codec)). Python has no interface type, so a "codec" is just a module (or
anything duck-typed the same way) exposing object_dump_request/
dump_bank_request/parse_object_dump/parse_reply with matching signatures —
Data.librarian_sysex (default, Kronos) and Data.nautilus_sysex are the two
that exist. Defaulting the constructor arg to the Kronos module keeps every
pre-existing caller (there were none passing a codec before device-family
awareness existed) unchanged.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, List, Optional, Set, Tuple

import Data.librarian_sysex as _kronos_codec
from Core.kronos_sysex import bytes_to_hex, hex_to_bytes
from Core.midi_bridge import MidiBridgeClient


class SysExDumpCollector:
    def __init__(self, bridge: MidiBridgeClient, codec=_kronos_codec):
        self._bridge = bridge
        self._codec = codec
        self._gate = threading.Lock()

    def object_dump_request(self, obj: int, bank: int, index: int) -> str:
        """Codec-aware 0x72 Object Dump Request, hex-string form (for collect())."""
        return bytes_to_hex(self._codec.object_dump_request(obj, bank, index))

    def dump_bank_request(self, obj: int, bank: int) -> str:
        """Codec-aware 0x77 Dump Bank Request, hex-string form (for collect())."""
        return bytes_to_hex(self._codec.dump_bank_request(obj, bank))

    def collect(self, request_hex: str, expect_obj: int, expected_count: Optional[int],
                idle_ms: int = 600, no_response_ms: int = 4000,
                stall_ms: int = 4000, overall_ms: int = 60000) -> List[bytes]:
        with self._gate:
            results: List[bytes] = []
            results_lock = threading.Lock()
            last_match = [0.0]
            last_activity = [0.0]
            activity = [False]
            reject_code = [-1]

            def on_msg(m: bytes):
                dump0 = self._codec.parse_object_dump(m)
                if dump0 is not None and dump0.obj == expect_obj:
                    with results_lock:
                        results.append(m)
                    now = time.monotonic()
                    last_match[0] = now
                    last_activity[0] = now
                    return
                rc = self._codec.parse_reply(m)
                if rc is not None:
                    reject_code[0] = rc  # 4 = "target object not found"

            def on_activity():
                activity[0] = True
                last_activity[0] = time.monotonic()

            self._bridge.add_raw_listener(on_msg)
            self._bridge.add_activity_listener(on_activity)
            try:
                req_bytes = hex_to_bytes(request_hex)
                if req_bytes is None or not self._bridge.send_bytes(req_bytes):
                    return []

                start = time.monotonic()
                while True:
                    time.sleep(0.05)
                    now = time.monotonic()
                    with results_lock:
                        c = len(results)

                    if expected_count is not None and c >= expected_count:
                        break
                    if c > 0 and last_match[0] and (now - last_match[0]) * 1000 > idle_ms:
                        break
                    if c == 0 and reject_code[0] >= 0:
                        break
                    if not activity[0]:
                        if (now - start) * 1000 > no_response_ms:
                            break
                    elif c == 0 and last_activity[0] and (now - last_activity[0]) * 1000 > stall_ms:
                        break
                    if (now - start) * 1000 > overall_ms:
                        break
            finally:
                self._bridge.remove_raw_listener(on_msg)
                self._bridge.remove_activity_listener(on_activity)

            with results_lock:
                return list(results)

    def collect_per_object_names(
            self, obj: int, bank: int, count: int,
            progress: Optional[Callable[[int], None]] = None,
            cancel_event: Optional[threading.Event] = None,
            batch_size: int = 32, batch_idle_ms: int = 350,
            batch_max_ms: int = 2500, max_passes: int = 3) -> Tuple[Set[int], bool]:
        """Pull a whole bank's names one object at a time via func 0x72 — the only
        path that works for USER-writable banks (func 0x77's whole-bank enum is
        preset-only and rejects every USER bank with Reply code 4).
        """
        if cancel_event is None:
            cancel_event = threading.Event()

        with self._gate:
            replied: Set[int] = set()
            replied_lock = threading.Lock()
            last_reply = [0.0]

            def on_msg(m: bytes):
                dump0 = self._codec.parse_object_dump(m)
                if dump0 is not None and dump0.obj == obj and dump0.bank == bank:
                    idx = dump0.index
                    with replied_lock:
                        added = idx not in replied
                        replied.add(idx)
                    last_reply[0] = time.monotonic()
                    if added and progress:
                        with replied_lock:
                            c = len(replied)
                        progress(c)

            self._bridge.add_raw_listener(on_msg)
            converged = False
            try:
                for _pass in range(max_passes):
                    if cancel_event.is_set():
                        break

                    with replied_lock:
                        missing = [i for i in range(count) if i not in replied]
                    if not missing:
                        converged = True
                        break

                    with replied_lock:
                        before = len(replied)

                    for start in range(0, len(missing), batch_size):
                        if cancel_event.is_set():
                            break
                        end = min(start + batch_size, len(missing))
                        batch_bytes = b"".join(
                            self._codec.object_dump_request(obj, bank, missing[j])
                            for j in range(start, end))
                        if batch_bytes:
                            self._bridge.send_bytes(batch_bytes)

                        last_reply[0] = 0.0
                        t0 = time.monotonic()
                        while not cancel_event.is_set():
                            time.sleep(0.025)
                            lr = last_reply[0]
                            if lr and (time.monotonic() - lr) * 1000 > batch_idle_ms:
                                break
                            if (time.monotonic() - t0) * 1000 > batch_max_ms:
                                break

                        # Absent-bank early-out, only after one full generous wait.
                        if _pass == 0 and start == 0:
                            with replied_lock:
                                got = len(replied)
                            if got == 0:
                                converged = True
                                break
                    if converged:
                        break

                    with replied_lock:
                        after = len(replied)
                    if after == before:
                        converged = True  # a full pass added nothing -> done
                        break
            finally:
                self._bridge.remove_raw_listener(on_msg)

            with replied_lock:
                return (set(replied), converged)
