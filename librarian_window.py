"""
Librarian — move Programs/Combis between slots while keeping every Combi timbre
and Set List slot that references them coherent, live over SysEx.

Modeled on setlist_window.py (QDialog + worker threads + Qt Signals). The risky
logic lives in librarian_model / librarian_sysex (both unit-tested off-hardware);
this window is orchestration + safety UI:

  * Scan Library      -> builds a lightweight RefIndex (usage counts + referrer
                         discovery) from the in-use Combis and Set Lists.
  * Preview Move      -> re-dumps the few referrers + src/dst fresh, builds a
                         MovePlan, captures the bank-digest baseline, and shows
                         the exact dry-run (references rewritten, banks Stored).
  * Commit Move       -> backup pre-images -> staleness re-check -> writes ->
                         Store -> optional live 0x43. Gated behind an explicit
                         "Store-Bank spike verified" checkbox (see plan Step 1).

Nothing is written to the instrument until Commit, and Commit refuses to run
until the spike checkbox is ticked for this session.
"""
from __future__ import annotations

import datetime
import os
import threading
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QGroupBox, QHBoxLayout, QLabel, QPlainTextEdit,
    QPushButton, QRadioButton, QSpinBox, QVBoxLayout, QWidget,
)

import kronos_sysex as ksx
import storage
from librarian_model import (
    LibraryCatalog, MovePlan, ObjLoc, RefIndex, _store_label, apply_move, arm_plan,
    plan_move,
)
from librarian_sysex import OBJ_COMBI, OBJ_PROGRAM, OBJ_SET_LIST
from setlist_data import MAX_COUNT
from sysex_service import SysExService
import theme as T

_WIN_BG = T.BG
_WIN_FG = T.TEXT

_SAFETY = (
    "LIVE WRITES to the instrument. A move rewrites Combi/Set List references and "
    "commits whole banks (0x76). Before enabling Commit, verify the Store-Bank "
    "spike on THIS unit (dump a USER bank, write one object, Store, re-dump, "
    "confirm untouched slots survived). Keep 'Enable Exclusive' ON in Global > MIDI."
)


def _program_move_banks() -> List[int]:
    # exclude read-only GM/g(x) banks (0x10-0x1A)
    return list(range(0x00, 0x07)) + list(range(0x40, 0x4E))


def _combi_move_banks() -> List[int]:
    return list(range(0x00, 0x07)) + list(range(0x40, 0x47))


def _backup_dir() -> str:
    base = os.path.dirname(str(storage._path("name_cache.json")))
    d = os.path.join(base, "librarian_backups")
    os.makedirs(d, exist_ok=True)
    return d


class LibrarianWindow(QDialog):
    _scan_progress = Signal(int, int, str)          # done, total, note
    _scan_done = Signal(object, str)                # RefIndex|None, status
    _preview_done = Signal(object, str)             # MovePlan|None, status
    _commit_done = Signal(bool, str)                # ok, status

    def __init__(self, host: str, service: Optional[SysExService], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Librarian — coherent move (Programs / Combis)")
        self.resize(940, 720)
        self.setStyleSheet(f"QDialog {{ background-color: {_WIN_BG}; color: {_WIN_FG}; }}")
        self._host = host
        self._service = service
        self._names: Dict[Tuple[int, int, int], str] = {
            (n.type, n.bank, n.number): n.name for n in storage.load_names(host)
        }
        self._refindex: Optional[RefIndex] = None
        self._plan: Optional[MovePlan] = None
        self._busy = False

        self._build_ui()
        self._scan_progress.connect(self._on_scan_progress)
        self._scan_done.connect(self._on_scan_done)
        self._preview_done.connect(self._on_preview_done)
        self._commit_done.connect(self._on_commit_done)
        self._on_type_changed()
        self._update_usage()
        self._refresh_enable()

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)

        banner = QLabel(_SAFETY)
        banner.setWordWrap(True)
        banner.setStyleSheet("color: #FFD0D0; background: #3A1414; border: 1px solid "
                             "#7A2A2A; border-radius: 4px; padding: 8px; font-size: 12px;")
        root.addWidget(banner)

        # Scan row
        scan_row = QHBoxLayout()
        self._btn_scan = QPushButton("Scan Library")
        self._btn_scan.setToolTip("Dump in-use Combis and Set Lists to build the "
                                  "reference index (usage counts + coherency).")
        self._btn_scan.clicked.connect(self._start_scan)
        scan_row.addWidget(self._btn_scan)
        self._chk_full = QCheckBox("Full scan (all banks/slots)")
        self._chk_full.setToolTip("Sweep every combi slot and all 128 set lists, "
                                  "not just named ones. Slow but complete.")
        scan_row.addWidget(self._chk_full)
        self._btn_scan_cancel = QPushButton("Cancel")
        self._btn_scan_cancel.setEnabled(False)
        self._btn_scan_cancel.clicked.connect(self._cancel_scan)
        scan_row.addWidget(self._btn_scan_cancel)
        self._scan_label = QLabel("Not scanned")
        self._scan_label.setStyleSheet("color: #88AADD;")
        scan_row.addWidget(self._scan_label)
        scan_row.addStretch(1)
        root.addLayout(scan_row)

        # Move controls
        move_box = QGroupBox("Move (swap)")
        move_box.setStyleSheet("QGroupBox { border: 1px solid #333; margin-top: 8px; } "
                               "QGroupBox::title { subcontrol-origin: margin; left: 8px; }")
        mv = QVBoxLayout(move_box)

        type_row = QHBoxLayout()
        self._rb_prog = QRadioButton("Program")
        self._rb_prog.setChecked(True)
        self._rb_combi = QRadioButton("Combi")
        self._rb_prog.toggled.connect(self._on_type_changed)
        type_row.addWidget(QLabel("Object type:"))
        type_row.addWidget(self._rb_prog)
        type_row.addWidget(self._rb_combi)
        type_row.addStretch(1)
        mv.addLayout(type_row)

        # Source
        self._src_bank = QComboBox()
        self._src_num = QSpinBox(); self._src_num.setRange(0, 127)
        self._src_usage = QLabel("")
        self._src_usage.setStyleSheet("color: #9ACD9A;")
        mv.addLayout(self._loc_row("Source", self._src_bank, self._src_num, self._src_usage))

        # Dest
        self._dst_bank = QComboBox()
        self._dst_num = QSpinBox(); self._dst_num.setRange(0, 127)
        self._dst_usage = QLabel("")
        self._dst_usage.setStyleSheet("color: #9ACD9A;")
        mv.addLayout(self._loc_row("Destination", self._dst_bank, self._dst_num, self._dst_usage))

        for w in (self._src_bank, self._dst_bank):
            w.currentIndexChanged.connect(self._update_usage)
        for w in (self._src_num, self._dst_num):
            w.valueChanged.connect(self._update_usage)

        act_row = QHBoxLayout()
        self._btn_preview = QPushButton("Preview Move")
        self._btn_preview.clicked.connect(self._start_preview)
        act_row.addWidget(self._btn_preview)
        self._chk_live = QCheckBox("Live edit-buffer preview (0x43) if active")
        self._chk_live.setChecked(True)
        act_row.addWidget(self._chk_live)
        act_row.addStretch(1)
        mv.addLayout(act_row)

        root.addWidget(move_box)

        # Preview / log
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setStyleSheet("QPlainTextEdit { background:#141414; color:#CFCFCF; "
                                "border:1px solid #333; font-family: Consolas, monospace; "
                                "font-size: 12px; }")
        root.addWidget(self._log, stretch=1)

        # Commit row + spike gate
        commit_row = QHBoxLayout()
        self._chk_spike = QCheckBox("Store-Bank spike verified on this unit")
        self._chk_spike.setToolTip("Required. Confirms 0x76 preserves untouched "
                                   "slots on this instrument (plan Step 1).")
        self._chk_spike.stateChanged.connect(self._refresh_enable)
        commit_row.addWidget(self._chk_spike)
        commit_row.addStretch(1)
        self._btn_commit = QPushButton("Commit Move")
        self._btn_commit.setStyleSheet("QPushButton { background:#4A1F1F; }")
        self._btn_commit.clicked.connect(self._start_commit)
        commit_row.addWidget(self._btn_commit)
        root.addLayout(commit_row)

        hint = QLabel(f"Backups: {_backup_dir()}")
        hint.setStyleSheet("color: #777; font-size: 11px;")
        root.addWidget(hint)

    def _loc_row(self, label: str, bank: QComboBox, num: QSpinBox, usage: QLabel):
        row = QHBoxLayout()
        lb = QLabel(label + ":")
        lb.setFixedWidth(90)
        row.addWidget(lb)
        row.addWidget(QLabel("Bank"))
        bank.setFixedWidth(90)
        row.addWidget(bank)
        row.addWidget(QLabel("No."))
        row.addWidget(num)
        row.addSpacing(12)
        row.addWidget(usage, stretch=1)
        return row

    # ── Object-type / usage ──────────────────────────────────────────────────

    def _cur_type(self) -> int:
        return OBJ_PROGRAM if self._rb_prog.isChecked() else OBJ_COMBI

    def _on_type_changed(self):
        banks = _program_move_banks() if self._cur_type() == OBJ_PROGRAM else _combi_move_banks()
        label = ksx.program_label if self._cur_type() == OBJ_PROGRAM else ksx.combi_label
        for combo in (self._src_bank, self._dst_bank):
            combo.blockSignals(True)
            combo.clear()
            for ob in banks:
                combo.addItem(label(ob), ob)
            combo.blockSignals(False)
        self._update_usage()

    def _sel_loc(self, bank: QComboBox, num: QSpinBox) -> Optional[ObjLoc]:
        ob = bank.currentData()
        if ob is None:
            return None
        return ObjLoc(self._cur_type(), ob, num.value())

    def _name_of(self, loc: ObjLoc) -> str:
        t = 1 if loc.obj_type == OBJ_PROGRAM else 0
        return self._names.get((t, loc.bank, loc.number), "")

    def _update_usage(self):
        for bank, num, lbl in ((self._src_bank, self._src_num, self._src_usage),
                               (self._dst_bank, self._dst_num, self._dst_usage)):
            loc = self._sel_loc(bank, num)
            if loc is None:
                lbl.setText("")
                continue
            name = self._name_of(loc)
            name_part = f"'{name}'" if name else "(name unknown — Sync Names)"
            if self._refindex is not None:
                lbl.setText(f"{name_part}   used by {self._refindex.usage_count(loc)} ref(s)")
            else:
                lbl.setText(f"{name_part}   (scan for usage)")

    # ── Scan ─────────────────────────────────────────────────────────────────

    def _start_scan(self):
        if self._busy or self._service is None or not self._service.can_dump:
            self._log_line("Cannot scan — MIDI monitoring off or not connected.")
            return
        self._busy = True
        self._cancel = threading.Event()
        self._btn_scan.setEnabled(False)
        self._btn_scan_cancel.setEnabled(True)
        self._scan_label.setText("Scanning…")
        threading.Thread(target=self._scan_worker, args=(self._chk_full.isChecked(),),
                         daemon=True, name="LibScan").start()

    def _cancel_scan(self):
        if getattr(self, "_cancel", None) is not None:
            self._cancel.set()

    def _scan_worker(self, full: bool):
        svc = self._service
        ri = RefIndex()
        # Which combis / set lists to index.
        if full:
            combi_ids = [(b, n) for b in _combi_move_banks() for n in range(128)]
            setlist_ids = list(range(MAX_COUNT))
        else:
            combi_ids = [(n.bank, n.number) for n in storage.load_names(self._host)
                         if n.type == 0]
            cached_sl = storage.load_setlists(self._host)
            setlist_ids = sorted(cached_sl.keys()) or list(range(MAX_COUNT))
        total = len(combi_ids) + len(setlist_ids)
        done = 0
        try:
            for (bank, number) in combi_ids:
                if self._cancel.is_set():
                    break
                d = svc.dump_object_parsed(OBJ_COMBI, bank, number, no_response_ms=3000)
                if d is not None:
                    ri.add_combi(d)
                done += 1
                self._scan_progress.emit(done, total, f"combi {ksx.combi_label(bank)}:{number:03d}")
            for number in setlist_ids:
                if self._cancel.is_set():
                    break
                d = svc.dump_object_parsed(OBJ_SET_LIST, 0, number, no_response_ms=6000)
                if d is not None:
                    ri.add_setlist(d)
                done += 1
                self._scan_progress.emit(done, total, f"set list {number:03d}")
            # Capture scan-time digests of every swept bank for the freshness gate
            # (detects a referrer created AFTER the scan — the discovery hole the
            # per-move digest gate cannot see).
            if not self._cancel.is_set():
                for bank in sorted({b for (b, _n) in combi_ids}):
                    ri.record_digest(OBJ_COMBI, bank, svc.bank_digest(OBJ_COMBI, bank))
                ri.record_digest(OBJ_SET_LIST, 0, svc.bank_digest(OBJ_SET_LIST, 0))
        except Exception as e:  # pragma: no cover - defensive
            self._scan_done.emit(None, f"Scan error: {e}")
            return
        status = ("Scan cancelled" if self._cancel.is_set() else
                  f"Indexed {len(ri.combi_refs)} combis, {len(ri.setlist_refs)} set lists")
        self._scan_done.emit(ri, status)

    def _on_scan_progress(self, done: int, total: int, note: str):
        self._scan_label.setText(f"Scanning {done}/{total} — {note}")

    def _on_scan_done(self, ri: Optional[RefIndex], status: str):
        self._busy = False
        self._btn_scan.setEnabled(True)
        self._btn_scan_cancel.setEnabled(False)
        if ri is not None:
            self._refindex = ri
        self._scan_label.setText(status)
        self._log_line(status)
        self._update_usage()
        self._refresh_enable()

    # ── Preview ──────────────────────────────────────────────────────────────

    def _start_preview(self):
        if self._busy:
            return
        if self._refindex is None:
            self._log_line("Scan the library first (needed to find references).")
            return
        if self._service is None or not self._service.can_dump:
            self._log_line("Not connected / MIDI monitoring off.")
            return
        src = self._sel_loc(self._src_bank, self._src_num)
        dst = self._sel_loc(self._dst_bank, self._dst_num)
        if src is None or dst is None:
            return
        self._busy = True
        self._plan = None
        self._btn_preview.setEnabled(False)
        self._log_line(f"\nPreviewing  {src.label()}  <->  {dst.label()} …")
        threading.Thread(target=self._preview_worker, args=(src, dst), daemon=True,
                         name="LibPreview").start()

    def _preview_worker(self, src: ObjLoc, dst: ObjLoc):
        svc = self._service
        try:
            # Freshness gate: if any bank changed since the scan, the index may
            # have missed a newly-created referrer — refuse and force a re-scan.
            stale = self._refindex.stale_banks(svc.bank_digest)
            if stale:
                labels = ", ".join(_store_label(o, b) for o, b in stale)
                self._preview_done.emit(None, f"STALE: {labels} changed since the "
                                        "last scan — re-scan before moving (a new "
                                        "referrer could otherwise be left dangling).")
                return
            ids = self._refindex.referrer_object_ids(src) | self._refindex.referrer_object_ids(dst)
            cat = LibraryCatalog()
            for (obj, bank, index) in ids:
                d = svc.dump_object_parsed(obj, bank, index,
                                           no_response_ms=6000 if obj == OBJ_SET_LIST else 3000)
                if d is None:
                    self._preview_done.emit(None, f"Failed to dump referrer "
                                            f"obj {obj:02X} bank {bank:02X} idx {index}")
                    return
                if obj == OBJ_COMBI:
                    cat.add_combi(d)
                else:
                    cat.add_setlist(d)
            src_dump = svc.dump_object_parsed(src.obj_type, src.bank, src.number)
            dst_dump = svc.dump_object_parsed(dst.obj_type, dst.bank, dst.number)
            if src_dump is None or dst_dump is None:
                self._preview_done.emit(None, "Failed to dump source/destination object.")
                return
            active = svc.current_performance_loc()
            plan = plan_move(cat, src, src_dump, dst, dst_dump, active)
            arm_plan(plan, svc)   # capture the digest baseline now (staleness gate)
        except Exception as e:  # pragma: no cover - defensive
            self._preview_done.emit(None, f"Preview error: {e}")
            return
        self._preview_done.emit(plan, "Preview ready")

    def _on_preview_done(self, plan: Optional[MovePlan], status: str):
        self._busy = False
        self._btn_preview.setEnabled(True)
        if plan is None:
            self._log_line(f"  {status}")
            self._refresh_enable()
            return
        self._plan = plan
        self._log_line("  " + "\n  ".join(plan.preview))
        for w in plan.warnings:
            self._log_line("  ! " + w)
        if not plan.digest_baseline:
            self._log_line("  ! WARNING: no bank-digest baseline captured — "
                           "staleness gate will be skipped.")
        self._log_line("  Preview complete. Review, tick the spike box, then Commit."
                       if not plan.is_refusable else "  This move is REFUSED (see above).")
        self._refresh_enable()

    # ── Commit ───────────────────────────────────────────────────────────────

    def _start_commit(self):
        if self._busy or self._plan is None:
            return
        if not self._chk_spike.isChecked():
            self._log_line("Tick 'Store-Bank spike verified' first.")
            return
        if self._plan.is_refusable:
            self._log_line("Refused plan — cannot commit.")
            return
        if self._service is None or not self._service.can_dump:
            self._log_line("Not connected.")
            return
        self._busy = True
        self._btn_commit.setEnabled(False)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self._log_line("\nCommitting…")
        threading.Thread(target=self._commit_worker, args=(self._plan, stamp),
                         daemon=True, name="LibCommit").start()

    def _commit_worker(self, plan: MovePlan, stamp: str):
        try:
            res = apply_move(plan, self._service, _backup_dir(), stamp,
                             progress=lambda m: self._commit_done.emit(True, "· " + m),
                             do_live=self._chk_live.isChecked())
        except Exception as e:  # pragma: no cover - defensive
            self._commit_done.emit(False, f"Commit crashed: {e}")
            return
        self._commit_done.emit(res.ok, ("DONE — move committed."
                                        if res.ok else f"ABORTED — {res.aborted_reason}"))

    def _on_commit_done(self, ok: bool, status: str):
        self._log_line("  " + status)
        if status.startswith("DONE") or status.startswith("ABORTED") or status.startswith("Commit crashed"):
            self._busy = False
            self._btn_commit.setEnabled(True)
            # references changed — the index is now stale for the touched banks.
            self._log_line("  (Re-scan recommended: references have changed.)")
            self._plan = None
            self._refresh_enable()

    # ── Enable/disable ───────────────────────────────────────────────────────

    def _refresh_enable(self):
        connected = self._service is not None and self._service.can_dump
        self._btn_scan.setEnabled(connected and not self._busy)
        self._btn_preview.setEnabled(connected and not self._busy and self._refindex is not None)
        can_commit = (connected and not self._busy and self._plan is not None
                      and not self._plan.is_refusable and self._chk_spike.isChecked())
        self._btn_commit.setEnabled(bool(can_commit))

    def _log_line(self, text: str):
        self._log.appendPlainText(text)
