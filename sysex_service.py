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
import storage
from midi_bridge import MidiBridgeClient
from setlist_data import SetListData, SetListSyncResult, MAX_COUNT
from sysex_dump_collector import SysExDumpCollector


class SysExService(QObject):
    performance_changed = Signal(str)   # human-readable "BANK:NNN Name"
    mode_changed = Signal(int)          # STATE-equivalent mode (1-7)
    available_changed = Signal(bool)    # SysEx capability probe result

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bridge: Optional[MidiBridgeClient] = None
        self._dump: Optional[SysExDumpCollector] = None
        self._cache_key = ""
        self._dumping = False

        self._state_mode = 0
        self._bank_msb = 0
        self._bank_lsb = 0
        self._have_bank_context = False
        self._last_bank_id: Optional[ksx.BankId] = None

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
        self._bridge.start()
        self._dump = SysExDumpCollector(self._bridge)

        threading.Thread(target=self._probe, daemon=True, name="SysExProbe").start()

    def stop(self):
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
        if bridge is None or not bridge.is_connected:
            return None
        result: List[Optional[bytes]] = [None]
        done = threading.Event()

        def on_msg(m: bytes):
            if (len(m) >= 5 and m[0] == 0xF0 and m[1] == 0x42 and (m[2] & 0xF0) == 0x30
                    and m[3] == 0x68 and m[4] == expect_func and result[0] is None):
                result[0] = m
                done.set()

        bridge.add_raw_listener(on_msg)
        try:
            req = ksx.hex_to_bytes(request_hex)
            if req is None or not bridge.send_bytes(req):
                return None
            done.wait(timeout_s)
            return result[0]
        finally:
            bridge.remove_raw_listener(on_msg)

    def _on_connection_changed(self, connected: bool):
        if not connected:
            self._is_available = False
            self.available_changed.emit(False)

    # ── Manual refresh (func 0x32 + 0x74) ───────────────────────────────────

    def refresh_now(self):
        threading.Thread(target=self._refresh_worker, daemon=True, name="SysExRefresh").start()

    def _refresh_worker(self):
        if self._dumping:
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

        self._dumping = True
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
            self._dumping = False
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
        self._dumping = True
        try:
            return self._dump_one_set_list(dump, number)
        finally:
            self._dumping = False
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
        self._dumping = True
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
            self._dumping = False
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
