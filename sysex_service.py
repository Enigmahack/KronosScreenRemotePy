"""
SysExService — port of Networking/SysExService.cs + ISysExService.cs.

Orchestrates the MIDI bridge transport for one connected Kronos: passively
decodes the live stream (Bank Digest, Object Dump names, Mode Change, Bank
Select + Program Change identity — all zero extra SysEx traffic), persists a
program/combi name cache + a per-bank "already dumped" ledger, and exposes the
user-triggered bulk operations (Sync Names, Set List dump / sweep).

Simplification vs the C# version: this port skips the continuous func-0x33
polling loop and the PullNamesOnChange per-change debounce — the live-stream
decode (mode-change 0x4E, Bank-Select+PC) already gives a flash-free identity
the instant it changes, which covers the common case. `refresh_now()` does a
single on-demand func 0x32 + 0x74 query for callers that want to force a
resync (e.g. right after connecting, before any stream event has arrived).
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Dict, List, Optional, Set, Tuple

from PySide6.QtCore import QObject, QTimer, Signal

import kronos_sysex as ksx
import librarian_sysex as lsx
import storage
from librarian_sysex import ObjectDump, parse_bank_digest, parse_object_dump
from midi_bridge import MidiBridgeClient
from setlist_data import SetListData, SetListSyncResult, MAX_COUNT
from sysex_dump_collector import SysExDumpCollector


class DumpGate:
    """Pauses refresh_now() (this port's one-shot stand-in for the C# func-33
    poll loop) while a bulk dump/write is in flight, so it can't steal one of
    the dump's 0x73/0x24 replies off the shared bridge stream. Port of
    Networking/DumpGate.cs. A plain bool `_dumping` (the prior state here) is
    racy in two ways:
      1. Overlap — two dumps can be in flight at once (Sync Names, a Set List
         sweep, a Librarian write, all triggered independently). A bool lets
         whichever finishes FIRST un-pause the loop while the other is still
         mid-dump. A refcount instead stays paused until the LAST one ends.
      2. Transport switch — if the bridge is torn down and rebuilt mid-dump
         (reconnect), the orphaned old dump's End() must not un-pause the NEW
         generation. Each dump captures an epoch at begin(); end() is a no-op
         once new_generation() has moved the epoch on.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._epoch = 0
        self._depth = 0

    def begin(self) -> int:
        with self._lock:
            self._depth += 1
            return self._epoch

    def end(self, epoch: int) -> None:
        with self._lock:
            if epoch == self._epoch:
                self._depth -= 1

    def new_generation(self) -> None:
        with self._lock:
            self._epoch += 1
            self._depth = 0

    @property
    def active(self) -> bool:
        with self._lock:
            return self._depth > 0


_NO_REPLY = object()  # sentinel: distinct from any real reply value (0, False, b"", ...)


def _await_reply(bridge: Optional[MidiBridgeClient], send: Callable[[], bool],
                  match: Callable[[bytes], Optional[object]], timeout_s: float):
    """Subscribe, send, wait up to timeout_s for the first raw message where
    match() returns non-None, then unsubscribe. Returns the matched value, or
    the _NO_REPLY sentinel on send failure or timeout. Port of the scaffold in
    MidiTransportReplyExtensions.cs — that file exists because a naive generic
    `default(T)` timeout return once yielded reply-code 0 (a genuine success
    code) for a Store Bank that never actually replied. Python's None already
    isn't 0/False, but callers here use `is None`, not truthiness, precisely so
    a real reply of 0/False/b"" is never mistaken for "no reply"."""
    if bridge is None or not bridge.is_connected:
        return _NO_REPLY
    result = [_NO_REPLY]
    done = threading.Event()

    def on_msg(m: bytes):
        if result[0] is _NO_REPLY:
            v = match(m)
            if v is not None:
                result[0] = v
                done.set()

    bridge.add_raw_listener(on_msg)
    try:
        if not send():
            return _NO_REPLY
        done.wait(timeout_s)
        return result[0]
    finally:
        bridge.remove_raw_listener(on_msg)


class SysExService(QObject):
    performance_changed = Signal(str)   # human-readable "BANK:NNN Name"
    mode_changed = Signal(int)          # STATE-equivalent mode (1-7)
    available_changed = Signal(bool)    # SysEx capability probe result
    # Footer MIDI indicators — stable signals the UI connects to ONCE (the
    # bridge is rebuilt on every reconnect; these outlive it).
    rx_activity = Signal()              # a MIDI message was received
    tx_activity = Signal()              # a MIDI message was sent
    link_changed = Signal(bool)         # bridge TCP connection state

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bridge: Optional[MidiBridgeClient] = None
        self._dump: Optional[SysExDumpCollector] = None
        self._cache_key = ""
        self._dump_gate = DumpGate()

        self._state_mode = 0
        self._bank_msb = 0
        self._bank_lsb = 0
        self._have_bank_context = False
        self._last_bank_id: Optional[ksx.BankId] = None
        self._last_rx_emit = 0.0        # throttle rx_activity (coalesce dump floods)

        self._stream_names: Dict[Tuple[int, int, int], str] = {}
        self._dumped_banks: Set[Tuple[int, int]] = set()
        self._names_lock = threading.Lock()
        self._persist_timer: Optional[threading.Timer] = None

        self._performance_display = ""
        self._is_available = False

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self, host: str, port: int = 9875):
        self.stop()

        self._cache_key = host
        self._performance_display = ""
        self._is_available = False
        self._state_mode = 0
        self._bank_msb = self._bank_lsb = 0
        self._have_bank_context = False
        self._last_bank_id = None

        with self._names_lock:
            self._stream_names = {
                (n.type, n.bank, n.number): n.name for n in storage.load_names(host)
            }
            self._dumped_banks = storage.load_dumped_banks(host)

        self._bridge = MidiBridgeClient(host, port)
        self._bridge.add_raw_listener(self._on_raw_message)
        self._bridge.connection_changed.connect(self._on_connection_changed)
        self._bridge.message_sent.connect(lambda _n: self.tx_activity.emit())
        self._bridge.start()
        self._dump = SysExDumpCollector(self._bridge)

        threading.Thread(target=self._probe, daemon=True, name="SysExProbe").start()

    def stop(self):
        # New transport generation: any dump/write still in flight against the
        # outgoing bridge is now orphaned — its later end() must be a no-op so it
        # can't un-pause the next connection's refresh_now() (see DumpGate).
        self._dump_gate.new_generation()
        if self._persist_timer is not None:
            self._persist_timer.cancel()
            self._persist_timer = None
        if self._cache_key:
            self._persist_names()
        if self._bridge is not None:
            self._bridge.remove_raw_listener(self._on_raw_message)
            self._bridge.dispose()
            self._bridge = None
        self._dump = None
        self._is_available = False
        self._performance_display = ""

    @property
    def bridge(self) -> Optional[MidiBridgeClient]:
        return self._bridge

    @property
    def can_dump(self) -> bool:
        return self._dump is not None and self._bridge is not None and self._bridge.is_connected

    @property
    def is_available(self) -> bool:
        return self._is_available

    @property
    def performance_display(self) -> str:
        return self._performance_display

    # ── Probe ────────────────────────────────────────────────────────────────

    def _probe(self):
        bridge = self._bridge
        if bridge is None:
            return
        deadline = time.monotonic() + 5.0
        while not bridge.is_connected and time.monotonic() < deadline:
            time.sleep(0.1)
        if not bridge.is_connected:
            return

        resp = self._query(ksx.mode_request_hex(), 0x42, timeout_s=3.0)
        capable = resp is not None
        self._is_available = capable
        self.available_changed.emit(capable)
        if capable:
            md = ksx.parse_mode_data(resp)
            if md is not None:
                sm = md.to_state_mode()
                if sm > 0:
                    self._state_mode = sm
                    self.mode_changed.emit(sm)

    def _query(self, request_hex: str, expect_func: int, timeout_s: float) -> Optional[bytes]:
        """Blocking request/response over the bridge, matched on the Korg reply
        header F0 42 3g 68 <func>. Used only for the one-shot probe/refresh."""
        bridge = self._bridge
        req = ksx.hex_to_bytes(request_hex)
        if req is None:
            return None

        def match(m: bytes) -> Optional[bytes]:
            if (len(m) >= 5 and m[0] == 0xF0 and m[1] == 0x42 and (m[2] & 0xF0) == 0x30
                    and m[3] == 0x68 and m[4] == expect_func):
                return m
            return None

        result = _await_reply(bridge, lambda: bridge.send_bytes(req), match, timeout_s)
        return None if result is _NO_REPLY else result

    def _on_connection_changed(self, connected: bool):
        self.link_changed.emit(connected)
        if not connected:
            self._is_available = False
            self.available_changed.emit(False)

    # ── Manual refresh (func 0x32 + 0x74) ───────────────────────────────────

    def refresh_now(self):
        threading.Thread(target=self._refresh_worker, daemon=True, name="SysExRefresh").start()

    def _refresh_worker(self):
        # Never inject a probe query into a bulk dump's 0x73/0x24 reply stream.
        if self._dump_gate.active:
            return
        resp = self._query(ksx.perf_id_request_hex(), 0x33, timeout_s=3.0)
        if resp is None:
            self._set_performance_display("")
            return
        info = ksx.parse_performance_id(resp)
        if info is None:
            self._set_performance_display("")
            return

        bid = ksx.from_func33(info.type, info.bank, info.number)
        if bid is not None:
            self._last_bank_id = bid
            with self._names_lock:
                name = self._stream_names.get((bid.type, bid.obj_bank, bid.number))
            display = bid.display if not name else f"{bid.display} {name}"
        else:
            display = info.to_display_string()
        self._set_performance_display(display)

    def _set_performance_display(self, display: str):
        if display != self._performance_display:
            self._performance_display = display
            self.performance_changed.emit(display)

    # ── Live stream decode ───────────────────────────────────────────────────

    def _on_raw_message(self, raw: bytes):
        """Called synchronously on the bridge's network thread."""
        if not raw:
            return
        # Throttle to ~25 Hz so a bank dump doesn't queue thousands of GUI events
        # (the footer flash only needs to be refreshed, not per-message).
        now = time.monotonic()
        if now - self._last_rx_emit > 0.04:
            self._last_rx_emit = now
            self.rx_activity.emit()   # queued → footer RX flash on the GUI thread
        status = raw[0]

        # Bank Digest (func 0x38): pushed on any bank-storage change.
        if (status == 0xF0 and len(raw) >= 7 and raw[1] == 0x42 and (raw[2] & 0xF0) == 0x30
                and raw[3] == 0x68 and raw[4] == 0x38):
            dig_obj, dig_bank = raw[5], raw[6]
            if dig_obj in (0x00, 0x01):
                t = 1 if dig_obj == 0x00 else 0
                with self._names_lock:
                    for k in [k for k in self._stream_names if k[0] == t and k[1] == dig_bank]:
                        del self._stream_names[k]
                    was_dumped = (t, dig_bank) in self._dumped_banks
                    self._dumped_banks.discard((t, dig_bank))
                if was_dumped:
                    storage.save_dumped_banks(self._cache_key, self._snapshot_dumped())
                self._persist_names()
            return

        # Object Dump (func 0x73): passively capture program/combi names.
        if (status == 0xF0 and len(raw) >= 11 and raw[1] == 0x42 and (raw[2] & 0xF0) == 0x30
                and raw[3] == 0x68 and raw[4] == 0x73):
            obj = raw[5]
            if obj in (0x00, 0x13, 0x01, 0x12):
                type_ = 1 if obj in (0x00, 0x13) else 0
                obj_bank = raw[6]
                idx, nm = ksx.parse_name_object_dump(raw)
                if idx >= 0 and nm:
                    key = (type_, obj_bank, idx)
                    with self._names_lock:
                        changed = self._stream_names.get(key) != nm
                        if changed:
                            self._stream_names[key] = nm
                    if changed:
                        self._schedule_persist()
                        if (self._last_bank_id is not None and self._last_bank_id.type == type_
                                and self._last_bank_id.obj_bank == obj_bank
                                and self._last_bank_id.number == idx):
                            self._set_stream_perf_display(self._last_bank_id, nm)
            return

        # Mode Change (func 0x4E): F0 42 3g 68 4E 0m F7.
        if (status == 0xF0 and len(raw) >= 7 and raw[1] == 0x42 and (raw[2] & 0xF0) == 0x30
                and raw[3] == 0x68 and raw[4] == 0x4E):
            md = ksx.SysExModeData(raw[5] & 0x0F, 0, 0, 0)
            sm = md.to_state_mode()
            if sm > 0:
                self._state_mode = sm
                self.mode_changed.emit(sm)
            return

        hi = status & 0xF0

        # Control Change: Bank Select MSB/LSB feeds the next Program Change decode.
        if hi == 0xB0 and len(raw) >= 3:
            cc, val = raw[1] & 0x7F, raw[2] & 0x7F
            if cc == 0:
                self._bank_msb, self._have_bank_context = val, True
            elif cc == 32:
                self._bank_lsb, self._have_bank_context = val, True
            return

        # Program Change: resolve identity from Bank Select + PC, zero extra SysEx.
        if hi == 0xC0 and len(raw) >= 2:
            pc = raw[1] & 0x7F
            bid = ksx.decode_bank(self._state_mode, self._bank_msb, self._bank_lsb, pc) \
                if self._have_bank_context else None

            if bid is None and self._last_bank_id is not None and self._state_mode in (2, 3):
                last = self._last_bank_id
                if last.type == (0 if self._state_mode == 2 else 1):
                    bid = ksx.BankId(last.type, last.label, last.obj_bank, pc)

            if bid is not None:
                self._last_bank_id = bid
                with self._names_lock:
                    name = self._stream_names.get((bid.type, bid.obj_bank, bid.number))
                self._set_stream_perf_display(bid, name)
            return

    def _set_stream_perf_display(self, bid: ksx.BankId, name: Optional[str]):
        display = bid.display if not name else f"{bid.display} {name}"
        self._set_performance_display(display)

    # ── Name cache persistence ──────────────────────────────────────────────

    def _persist_names(self):
        with self._names_lock:
            snapshot = [
                ksx.CachedName(t, b, n, name) for (t, b, n), name in self._stream_names.items()
            ]
        storage.save_names(self._cache_key, snapshot)

    def _schedule_persist(self, delay_s: float = 2.0):
        if self._persist_timer is not None:
            self._persist_timer.cancel()
        self._persist_timer = threading.Timer(delay_s, self._persist_names)
        self._persist_timer.daemon = True
        self._persist_timer.start()

    def _snapshot_dumped(self) -> Set[Tuple[int, int]]:
        with self._names_lock:
            return set(self._dumped_banks)

    def current_name_count(self) -> int:
        with self._names_lock:
            return len(self._stream_names)

    # ── Sync Names (user-triggered bulk name sweep) ─────────────────────────

    def sync_names(self, progress: Optional[Callable[[int, int, int], None]] = None,
                    cancel_event: Optional[threading.Event] = None) -> int:
        """Blocking — call from a worker thread. progress(done, total, names)."""
        if cancel_event is None:
            cancel_event = threading.Event()
        dump = self._dump
        if dump is None or not self.can_dump:
            return self.current_name_count()

        all_banks = ksx.all_name_banks()
        total = len(all_banks)
        with self._names_lock:
            todo = [b for b in all_banks if b not in self._dumped_banks]
        if not todo:
            return self.current_name_count()

        gate_epoch = self._dump_gate.begin()   # pause refresh_now() for the whole sweep
        ledger_dirty = False
        try:
            for type_, obj_bank in todo:
                if cancel_event.is_set():
                    break
                name_obj = ksx.name_object(type_)
                done_ok = False

                if obj_bank < 0x40:
                    req = ksx.dump_bank_request(name_obj, obj_bank)
                    msgs = dump.collect(req, name_obj, expected_count=None,
                                        idle_ms=600, no_response_ms=1200,
                                        stall_ms=3000, overall_ms=30000)
                    done_ok = len(msgs) > 0
                else:
                    def per_obj_progress(_c):
                        with self._names_lock:
                            nd = len(self._dumped_banks)
                        if progress:
                            progress(nd, total, self.current_name_count())

                    replied, converged = dump.collect_per_object_names(
                        name_obj, obj_bank, 128, per_obj_progress, cancel_event)
                    done_ok = converged and len(replied) > 0

                if done_ok:
                    with self._names_lock:
                        self._dumped_banks.add((type_, obj_bank))
                    ledger_dirty = True

                with self._names_lock:
                    now_done = len(self._dumped_banks)
                if progress:
                    progress(now_done, total, self.current_name_count())
        finally:
            self._dump_gate.end(gate_epoch)
            if ledger_dirty:
                storage.save_dumped_banks(self._cache_key, self._snapshot_dumped())
            self._persist_names()
        return self.current_name_count()

    # ── Set List dumps ──────────────────────────────────────────────────────

    def dump_set_list(self, number: int) -> Optional[SetListData]:
        """Blocking — call from a worker thread."""
        dump = self._dump
        if dump is None or not self.can_dump:
            return None
        gate_epoch = self._dump_gate.begin()
        try:
            return self._dump_one_set_list(dump, number)
        finally:
            self._dump_gate.end(gate_epoch)
            self.refresh_now()

    def dump_all_set_lists(self, progress: Optional[Callable[[int, int, int], None]] = None,
                            cancel_event: Optional[threading.Event] = None) -> SetListSyncResult:
        """Blocking — call from a worker thread. progress(done, total, found)."""
        if cancel_event is None:
            cancel_event = threading.Event()
        found: Dict[int, SetListData] = {}
        empty: List[int] = []

        dump = self._dump
        if dump is None or not self.can_dump:
            return SetListSyncResult(found, empty, 0, cancel_event.is_set())

        total = MAX_COUNT
        attempted = 0
        gate_epoch = self._dump_gate.begin()
        try:
            for n in range(total):
                if cancel_event.is_set():
                    break
                attempted += 1
                data = self._dump_one_set_list(dump, n)
                if data is not None:
                    if data.is_empty:
                        empty.append(n)
                    else:
                        found[n] = data
                if progress:
                    progress(attempted, total, len(found))
        finally:
            self._dump_gate.end(gate_epoch)
            self.refresh_now()
        return SetListSyncResult(found, empty, attempted, cancel_event.is_set())

    def _dump_one_set_list(self, dump: SysExDumpCollector, number: int) -> Optional[SetListData]:
        req = ksx.object_dump_request(0x0D, 0, number)
        msgs = dump.collect(req, 0x0D, expected_count=1, no_response_ms=10000)
        if not msgs:
            return None
        return SetListData.from_object_dump(msgs[0])

    # ── Raw send ─────────────────────────────────────────────────────────────

    def send_midi(self, hex_bytes: str) -> bool:
        bridge = self._bridge
        b = ksx.hex_to_bytes(hex_bytes)
        return bridge is not None and b is not None and bridge.send_bytes(b)

    # ── Librarian primitives (satisfy librarian_model.MoveExecutor) ──────────
    # All of these BLOCK on the bridge and must be called from a worker thread.
    # Writes/stores are serialized against their func-0x24 Reply so a rejection
    # (wrong bank type, protected, overflow) is caught before the next step.

    def dump_object(self, obj: int, bank: int, index: int,
                    no_response_ms: int = 10000) -> Optional[bytes]:
        """Fetch one full object (raw func-0x73 message) from a specific slot."""
        dump = self._dump
        if dump is None or not self.can_dump:
            return None
        req = ksx.object_dump_request(obj, bank, index)
        msgs = dump.collect(req, obj, expected_count=1, no_response_ms=no_response_ms)
        return msgs[0] if msgs else None

    def dump_object_parsed(self, obj: int, bank: int, index: int,
                           **kw) -> Optional[ObjectDump]:
        raw = self.dump_object(obj, bank, index, **kw)
        return parse_object_dump(raw) if raw else None

    def _send_expect_reply(self, data: bytes, timeout_s: float) -> Optional[int]:
        """Send raw bytes, wait for the next func-0x24 Reply, return its code
        (0 = OK — a real reply value the sentinel in _await_reply keeps distinct
        from "no reply"). None on timeout / no link."""
        bridge = self._bridge

        def match(m: bytes) -> Optional[int]:
            if (len(m) >= 6 and m[0] == 0xF0 and m[1] == 0x42 and (m[2] & 0xF0) == 0x30
                    and m[3] == 0x68 and m[4] == 0x24):
                return m[5]
            return None

        result = _await_reply(bridge, lambda: bridge is not None and bridge.send_bytes(data),
                               match, timeout_s)
        return None if result is _NO_REPLY else result

    def write_object(self, op, timeout_s: float = 6.0) -> int:
        """Send a re-addressed func-0x73 Object Dump (volatile). Returns the
        Reply code (0 OK); -1 on timeout."""
        msg = lsx.object_dump_write(op.obj, op.bank, op.index, op.version, op.body)
        code = self._send_expect_reply(msg, timeout_s)
        return -1 if code is None else code

    def store_bank(self, obj: int, bank: int, timeout_s: float = 20.0) -> int:
        """Commit a bank to non-volatile storage (func 0x76). Returns Reply code
        (0 OK); -1 on timeout. Longer timeout — flash commit can be slow."""
        msg = lsx.store_bank_request(obj, bank)
        code = self._send_expect_reply(msg, timeout_s)
        return -1 if code is None else code

    def change_program_bank_type(self, bank: int, is_exi: bool, timeout_s: float = 30.0) -> int:
        """Send a func-0x7C Change Program Bank Type (REFORMATS AND ERASES the given Program
        bank if its type actually changes; a no-op reply otherwise). Returns the Reply code
        (0 OK); -1 on timeout. Longer default timeout than store_bank's — a whole-bank
        reformat is a bigger flash operation than committing a bank's already-written
        contents. Backs changeset_sync.py's WriteBankTypeChange (see librarian_shell_window.py's
        _write_bank_type_change adapter)."""
        msg = lsx.change_program_bank_type_request(bank, is_exi)
        code = self._send_expect_reply(msg, timeout_s)
        return -1 if code is None else code

    def bank_digest(self, obj: int, bank: int, timeout_s: float = 5.0) -> Optional[bytes]:
        """Request (func 0x37) and return the 20-byte SHA-1 storage digest for a
        bank (func 0x38 reply), matched on obj+bank. None on timeout."""
        bridge = self._bridge

        def match(m: bytes) -> Optional[bytes]:
            bd = parse_bank_digest(m)
            if bd is not None and bd.obj == obj and bd.bank == bank:
                return bd.sha1
            return None

        result = _await_reply(
            bridge, lambda: bridge is not None and bridge.send_bytes(lsx.bank_digest_request(obj, bank)),
            match, timeout_s)
        return None if result is _NO_REPLY else result

    def backup_objects(self, ops, path: str) -> None:
        """Serialize the given object pre-images to a .syx file as func-0x73
        Object Dumps. Restore = replay this file's messages, then Store the
        affected banks."""
        with open(path, "wb") as f:
            for op in ops:
                f.write(lsx.object_dump_write(op.obj, op.bank, op.index,
                                              op.version, op.body))

    def backup_bank_to_syx(self, obj: int, bank: int, path: str,
                           slot_count: int = 128,
                           progress: Optional[Callable[[int, int, int], None]] = None,
                           cancel_event: Optional[threading.Event] = None) -> int:
        """Full per-slot func-0x72 sweep of one bank into a .syx (for the paranoid
        pre-spike snapshot / an explicit 'Backup bank' action). Note: func 0x77's
        whole-bank enum is preset-only, so USER banks REQUIRE this per-slot path.
        Blocking — worker thread only. Returns number of objects saved."""
        dump = self._dump
        if dump is None or not self.can_dump:
            return 0
        saved = 0
        gate_epoch = self._dump_gate.begin()
        try:
            with open(path, "wb") as f:
                for i in range(slot_count):
                    if cancel_event is not None and cancel_event.is_set():
                        break
                    raw = self.dump_object(obj, bank, i, no_response_ms=4000)
                    if raw:
                        f.write(raw)
                        saved += 1
                    if progress:
                        progress(i + 1, slot_count, saved)
        finally:
            self._dump_gate.end(gate_epoch)
        return saved

    def send_raw(self, data: bytes) -> None:
        """Fire-and-forget raw bytes (e.g. live 0x43 edit-buffer preview)."""
        bridge = self._bridge
        if bridge is not None:
            bridge.send_bytes(data)

    def current_performance_loc(self):
        """Best-effort current performance as a librarian_model.ObjLoc, for the
        live dual-write path. None if unknown."""
        bid = self._last_bank_id
        if bid is None:
            return None
        from librarian_model import ObjLoc
        from librarian_sysex import OBJ_COMBI, OBJ_PROGRAM
        obj_type = OBJ_PROGRAM if bid.type == 1 else OBJ_COMBI
        return ObjLoc(obj_type, bid.obj_bank, bid.number)
