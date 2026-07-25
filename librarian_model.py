r"""
Librarian model — catalog, dependency graph, and coherent-move planning.

This is the brain of the librarian. It is deliberately split so the risky part
is testable off-hardware:

  * LibraryCatalog   — holds dumped Combi (obj 0x01) and Set List (obj 0x0D)
                       bodies and answers "who references location L?".
  * plan_move()      — PURE. Given a catalog + the two objects being swapped,
                       produces a MovePlan: the exact 0x73 writes, the banks to
                       Store, the reference patches, and human-readable preview
                       lines. Touches no hardware.
  * arm_plan()       — captures a bank-digest baseline for the staleness gate.
  * apply_move()     — executes a plan against a MoveExecutor (backup -> digest
                       re-check/abort -> writes -> Store -> optional live 0x43),
                       the only part that talks to the instrument.

Move semantics are SWAP (like PCG Tools): references to src follow to dst and
references to dst follow back to src. A program "moved" into an unused slot is a
swap with that slot's InitProgram (program slots are never truly empty).

Referrer scope (v1, per plan): Combi timbres + Set List slots. Song Timbre Sets
(obj 0x02) are intentionally out of scope.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import librarian_sysex as lsx
from librarian_sysex import (
    OBJ_COMBI, OBJ_PROGRAM, OBJ_SET_LIST, ObjectDump,
    iter_combi_timbre_refs, iter_setlist_slot_refs,
    obj_bank_to_func33, set_combi_timbre_ref, set_setlist_slot_ref,
)
import kronos_sysex as ksx


# ── Value types ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ObjLoc:
    """A movable object location, addressed in object-dump (header) encoding."""
    obj_type: int   # OBJ_PROGRAM (0x00) or OBJ_COMBI (0x01)
    bank: int       # object-dump bank byte
    number: int     # index within bank (0-127)

    def label(self) -> str:
        if self.obj_type == OBJ_PROGRAM:
            return f"{ksx.program_label(self.bank)}:{self.number:03d}"
        return f"{ksx.combi_label(self.bank)}:{self.number:03d}"


@dataclass(frozen=True)
class ReferrerSite:
    """One reference *site* pointing at a movable object."""
    kind: str          # 'combi_timbre' | 'setlist_slot'
    ref_obj: int       # 0x01 (combi) or 0x0D (set list)
    ref_bank: int      # object-dump bank of the REFERRING object (0 for set list)
    ref_index: int     # combi index, or set-list number
    site: int          # timbre 0-15, or slot 0-127
    cur_bank: int      # func33 bank currently stored at the site
    cur_number: int

    def describe(self) -> str:
        where = (f"Combi {ksx.combi_label(self.ref_bank)}:{self.ref_index:03d} timbre {self.site + 1}"
                 if self.kind == 'combi_timbre'
                 else f"Set List {self.ref_index:03d} slot {self.site + 1}")
        return where


@dataclass
class WriteOp:
    """A single re-addressed 0x73 Object Dump write (volatile until Stored)."""
    obj: int
    bank: int
    index: int
    version: int
    body: bytes
    note: str = ""


@dataclass
class MovePlan:
    src: ObjLoc
    dst: ObjLoc
    writes: List[WriteOp]                   # patched objects to write (0x73)
    pre_images: List[WriteOp]              # ORIGINAL objects (for backup/restore)
    stores: List[Tuple[int, int]]          # (obj, bank) to Store, deduped
    referrers: List[ReferrerSite]          # all sites that get rewritten
    preview: List[str]                     # human-readable dry-run lines
    warnings: List[str]
    live_pc: List[bytes] = field(default_factory=list)   # optional 0x43 dual-write
    digest_baseline: Dict[Tuple[int, int], bytes] = field(default_factory=dict)

    @property
    def is_refusable(self) -> bool:
        return any(w.startswith("REFUSE:") for w in self.warnings)


# ── Catalog / dependency graph ───────────────────────────────────────────────


# Object-dump banks that a program can NEVER be moved into (read-only).
_READONLY_PROGRAM_BANKS = set([0x10] + list(range(0x11, 0x1B)))  # GM, g(1)..g(d)


class LibraryCatalog:
    """Reverse index over dumped Combis and Set Lists."""

    def __init__(self) -> None:
        self.combis: Dict[Tuple[int, int], ObjectDump] = {}   # (bank, index) -> dump
        self.setlists: Dict[int, ObjectDump] = {}             # number -> dump

    # -- population -----------------------------------------------------------
    def add_combi(self, dump: ObjectDump) -> None:
        if dump.obj != OBJ_COMBI:
            raise ValueError("not a combi dump")
        self.combis[(dump.bank, dump.index)] = dump

    def add_setlist(self, dump: ObjectDump) -> None:
        if dump.obj != OBJ_SET_LIST:
            raise ValueError("not a set-list dump")
        self.setlists[dump.index] = dump

    # -- queries --------------------------------------------------------------
    def referrers_of(self, loc: ObjLoc) -> List[ReferrerSite]:
        """Every reference site that currently points at `loc`."""
        out: List[ReferrerSite] = []
        # func33 type: program refs use type 1, combi refs use type 0 — and the
        # set-list slot `type` field uses the same 0=combi/1=prog convention.
        ref_type = 1 if loc.obj_type == OBJ_PROGRAM else 0
        want_bank = obj_bank_to_func33(ref_type, loc.bank)
        if want_bank < 0:
            return out

        if loc.obj_type == OBJ_PROGRAM:
            for (bank, index), dump in self.combis.items():
                for t, fbank, num in iter_combi_timbre_refs(dump.body):
                    if fbank == want_bank and num == loc.number:
                        out.append(ReferrerSite('combi_timbre', OBJ_COMBI, bank,
                                                index, t, fbank, num))
        # Set-list slots (both program and combi moves land here, gated on slot type)
        for number, dump in self.setlists.items():
            for s, slot_type, fbank, idx in iter_setlist_slot_refs(dump.body):
                if slot_type == ref_type and fbank == want_bank and idx == loc.number:
                    out.append(ReferrerSite('setlist_slot', OBJ_SET_LIST, 0,
                                            number, s, fbank, idx))
        return out

    def usage_count(self, loc: ObjLoc) -> int:
        return len(self.referrers_of(loc))


class RefIndex:
    """Lightweight reference index — stores only the reference tuples per combi
    and set list (not full bodies). Cheap to build during a library scan and to
    hold/cache, and fast to query for usage counts and referrer discovery. Full
    object bodies are re-dumped on demand (only for the few objects a move
    actually rewrites), which also closes the body-staleness window.

    Query results are identical to LibraryCatalog.referrers_of on the same data
    (verified in the self-test)."""

    def __init__(self) -> None:
        # (bank, index) -> [(func33_bank, number)] * 16
        self.combi_refs: Dict[Tuple[int, int], List[Tuple[int, int]]] = {}
        # number -> [(slot, type, func33_bank, index)]
        self.setlist_refs: Dict[int, List[Tuple[int, int, int, int]]] = {}
        # (obj, bank) -> SHA-1 storage digest captured AT SCAN TIME. Used to
        # detect that a bank changed since the scan — which means the index may
        # have MISSED a newly-created referrer (a discovery hole the per-move
        # digest gate cannot see, because it only checks banks it already
        # decided to touch). stale_banks() re-checks these before a move.
        self.scan_digests: Dict[Tuple[int, int], bytes] = {}

    def add_combi(self, dump: ObjectDump) -> None:
        self.combi_refs[(dump.bank, dump.index)] = [
            (b, n) for _t, b, n in iter_combi_timbre_refs(dump.body)]

    def add_setlist(self, dump: ObjectDump) -> None:
        self.setlist_refs[dump.index] = [
            (s, t, b, i) for s, t, b, i in iter_setlist_slot_refs(dump.body)]

    def referrers_of(self, loc: ObjLoc) -> List[ReferrerSite]:
        out: List[ReferrerSite] = []
        ref_type = 1 if loc.obj_type == OBJ_PROGRAM else 0
        want_bank = obj_bank_to_func33(ref_type, loc.bank)
        if want_bank < 0:
            return out
        if loc.obj_type == OBJ_PROGRAM:
            for (bank, index), refs in self.combi_refs.items():
                for t, (fbank, num) in enumerate(refs):
                    if fbank == want_bank and num == loc.number:
                        out.append(ReferrerSite('combi_timbre', OBJ_COMBI, bank,
                                                index, t, fbank, num))
        for number, slots in self.setlist_refs.items():
            for s, slot_type, fbank, idx in slots:
                if slot_type == ref_type and fbank == want_bank and idx == loc.number:
                    out.append(ReferrerSite('setlist_slot', OBJ_SET_LIST, 0,
                                            number, s, fbank, idx))
        return out

    def usage_count(self, loc: ObjLoc) -> int:
        return len(self.referrers_of(loc))

    def referrer_object_ids(self, loc: ObjLoc):
        """Distinct (obj, bank, index) of every object that references loc."""
        return {(r.ref_obj, r.ref_bank, r.ref_index) for r in self.referrers_of(loc)}

    def record_digest(self, obj: int, bank: int, sha1: Optional[bytes]) -> None:
        if sha1:
            self.scan_digests[(obj, bank)] = sha1

    def stale_banks(self, reader) -> List[Tuple[int, int]]:
        """Re-read the scan-time digests via reader(obj, bank) -> sha1|None and
        return the (obj, bank) whose storage changed since the scan. A non-empty
        result means the index is stale for referrer DISCOVERY — re-scan before
        moving, or a newly-created referrer will be left dangling."""
        stale: List[Tuple[int, int]] = []
        for (obj, bank), base in self.scan_digests.items():
            cur = reader(obj, bank)
            if cur is not None and cur != base:
                stale.append((obj, bank))
        return stale


# ── Move planning (pure) ─────────────────────────────────────────────────────


def plan_move(catalog: LibraryCatalog, src: ObjLoc, src_dump: ObjectDump,
              dst: ObjLoc, dst_dump: ObjectDump,
              active: Optional[ObjLoc] = None) -> MovePlan:
    """Compute a coherent swap(src, dst). Pure — no hardware access.

    src_dump / dst_dump are the freshly dumped bodies of the two objects being
    swapped (for a combi move these are usually already in the catalog).
    """
    warnings: List[str] = []
    preview: List[str] = []

    if src.obj_type != dst.obj_type:
        warnings.append("REFUSE: cannot move between different object types "
                        "(program vs combi)")
    if src.obj_type == OBJ_PROGRAM and dst.bank in _READONLY_PROGRAM_BANKS:
        warnings.append(f"REFUSE: destination {dst.label()} is a read-only "
                        "(GM/g) program bank")
    if src == dst:
        warnings.append("REFUSE: source and destination are the same location")

    ref_type = 1 if src.obj_type == OBJ_PROGRAM else 0

    # New reference targets after the swap.
    dst_func33 = obj_bank_to_func33(ref_type, dst.bank)
    src_func33 = obj_bank_to_func33(ref_type, src.bank)

    # Collect referrers: those pointing at src retarget to dst, and vice-versa.
    src_referrers = catalog.referrers_of(src)   # -> point to dst
    dst_referrers = catalog.referrers_of(dst)   # -> point back to src
    all_referrers = src_referrers + dst_referrers

    # Group patches by the referring object so each object is written once.
    # key -> list of (site, kind, new_func33, new_number)
    grouped: Dict[Tuple[int, int, int], List[Tuple[int, str, int, int]]] = {}
    for r in src_referrers:
        grouped.setdefault((r.ref_obj, r.ref_bank, r.ref_index), []).append(
            (r.site, r.kind, dst_func33, dst.number))
    for r in dst_referrers:
        grouped.setdefault((r.ref_obj, r.ref_bank, r.ref_index), []).append(
            (r.site, r.kind, src_func33, src.number))

    writes: List[WriteOp] = []
    pre_images: List[WriteOp] = []

    # (1) The two swapped objects. Patched write goes to the OTHER location; the
    #     pre-image records each object at its ORIGINAL location for restore.
    writes.append(WriteOp(src.obj_type, dst.bank, dst.number, src_dump.version,
                          src_dump.body, note=f"{src.label()} -> {dst.label()}"))
    writes.append(WriteOp(dst.obj_type, src.bank, src.number, dst_dump.version,
                          dst_dump.body, note=f"{dst.label()} -> {src.label()}"))
    pre_images.append(WriteOp(src.obj_type, src.bank, src.number, src_dump.version,
                              src_dump.body, note="original"))
    pre_images.append(WriteOp(dst.obj_type, dst.bank, dst.number, dst_dump.version,
                              dst_dump.body, note="original"))

    # (2) Patched referrer objects (pre-image = the unpatched original body).
    for (ref_obj, ref_bank, ref_index), patches in grouped.items():
        base_dump = (catalog.combis.get((ref_bank, ref_index)) if ref_obj == OBJ_COMBI
                     else catalog.setlists.get(ref_index))
        if base_dump is None:
            warnings.append(f"REFUSE: referring object missing from catalog "
                            f"(obj {ref_obj:02X} bank {ref_bank:02X} idx {ref_index}) "
                            "— re-scan before moving")
            continue
        pre_images.append(WriteOp(ref_obj, ref_bank, ref_index, base_dump.version,
                                  base_dump.body, note="original"))
        body = bytearray(base_dump.body)
        for site, kind, new_func33, new_number in patches:
            if kind == 'combi_timbre':
                set_combi_timbre_ref(body, site, new_func33, new_number)
            else:  # setlist_slot — preserve type/color/transpose bits
                set_setlist_slot_ref(body, site, new_func33, new_number, type_=None)
        writes.append(WriteOp(ref_obj, ref_bank, ref_index, base_dump.version,
                              bytes(body),
                              note=f"fix {len(patches)} ref(s)"))

    # (3) Banks to Store (deduped). Set lists all live under obj 0x0D bank 0.
    stores: List[Tuple[int, int]] = []
    for w in writes:
        key = (w.obj, w.bank)
        if key not in stores:
            stores.append(key)

    # (4) Optional live dual-write: if the currently-loaded performance is a
    #     combi we are patching, mirror the change into its edit buffer with
    #     0x43 so it is audible without a reload. (Set-list active case: N/A.)
    live_pc: List[bytes] = []
    if active is not None and active.obj_type == OBJ_COMBI:
        for r in all_referrers:
            if (r.kind == 'combi_timbre' and r.ref_obj == OBJ_COMBI
                    and r.ref_bank == active.bank and r.ref_index == active.number):
                is_src = (r.cur_bank == obj_bank_to_func33(ref_type, src.bank)
                          and r.cur_number == src.number)
                new_bank = dst_func33 if is_src else src_func33
                new_num = dst.number if is_src else src.number
                live_pc.append(lsx.combi_timbre_bank_pc(r.site, new_bank))
                live_pc.append(lsx.combi_timbre_number_pc(r.site, new_num))

    # (5) Program bank-type reminder — we cannot know HD-1/EXi from the dump
    #     header alone; enforce at apply-time via the Reply code.
    if src.obj_type == OBJ_PROGRAM and src.bank != dst.bank:
        warnings.append("CHECK: program move across banks — destination bank "
                        "must be the same type (HD-1/EXi) or the write is "
                        "rejected (Reply 64).")

    # Preview lines
    preview.append(f"SWAP  {src.label()}  <->  {dst.label()}  "
                   f"({'programs' if src.obj_type == OBJ_PROGRAM else 'combis'})")
    preview.append(f"  references to rewrite: {len(all_referrers)}")
    for r in all_referrers:
        tgt = dst.label() if r in src_referrers else src.label()
        preview.append(f"    - {r.describe()}  ->  {tgt}")
    preview.append(f"  objects to write (0x73): {len(writes)}")
    preview.append("  banks to Store (0x76): " +
                   ", ".join(_store_label(o, b) for o, b in stores))
    if live_pc:
        preview.append(f"  live edit-buffer preview (0x43): "
                       f"{len(live_pc)} message(s) to active combi")

    return MovePlan(src, dst, writes, pre_images, stores, all_referrers, preview,
                    warnings, live_pc)


def _store_label(obj: int, bank: int) -> str:
    if obj == OBJ_PROGRAM:
        return f"Prog {ksx.program_label(bank)}"
    if obj == OBJ_COMBI:
        return f"Combi {ksx.combi_label(bank)}"
    if obj == OBJ_SET_LIST:
        return "Set Lists"
    return f"obj{obj:02X}:bank{bank:02X}"


# ── Execution ────────────────────────────────────────────────────────────────


class MoveExecutor:
    """Interface the real SysExService satisfies. Split out so apply_move() is
    testable with a fake. All methods either succeed or raise."""

    def backup_objects(self, ops: List[WriteOp], path: str) -> None: ...     # noqa: E704
    def bank_digest(self, obj: int, bank: int) -> Optional[bytes]: ...       # noqa: E704
    def write_object(self, op: WriteOp) -> int: ...   # returns Reply code (0 ok)
    def store_bank(self, obj: int, bank: int) -> int: ...  # returns Reply code
    def send_raw(self, data: bytes) -> None: ...            # noqa: E704


@dataclass
class ApplyResult:
    ok: bool
    steps: List[str]
    aborted_reason: Optional[str] = None


def arm_plan(plan: MovePlan, ex: MoveExecutor) -> None:
    """Capture the storage digest of every affected bank — the staleness baseline
    that apply_move() re-checks immediately before Storing."""
    for obj, bank in plan.stores:
        d = ex.bank_digest(obj, bank)
        if d is not None:
            plan.digest_baseline[(obj, bank)] = d


def apply_move(plan: MovePlan, ex: MoveExecutor, backup_dir: str,
               stamp: str, progress: Optional[Callable[[str], None]] = None,
               do_live: bool = True) -> ApplyResult:
    """Execute a coherent move. Order: backup -> staleness re-check -> writes ->
    Store -> optional live 0x43. Aborts (before any Store) on a stale digest.
    `stamp` is an externally supplied timestamp string (scripts have no clock)."""
    steps: List[str] = []

    def note(msg: str) -> None:
        steps.append(msg)
        if progress:
            progress(msg)

    if plan.is_refusable:
        return ApplyResult(False, steps, "; ".join(plan.warnings))

    # 1. Backup the pre-image of every object we are about to overwrite, to a
    #    single .syx (restore = replay these 0x73s + Store the same banks). This
    #    is precise and cheap; it is correct provided Store preserves untouched
    #    slots — exactly what the Step-1 hardware spike verifies before any move.
    backup_path = f"{backup_dir}/{stamp}_move_{plan.src.label()}_{plan.dst.label()}.syx"
    backup_path = backup_path.replace(":", "").replace(" ", "")
    note(f"backup {len(plan.pre_images)} pre-image object(s) -> {backup_path}")
    ex.backup_objects(plan.pre_images, backup_path)

    # 2. Staleness gate — re-read digests, abort if any changed since arm_plan().
    for (obj, bank), baseline in plan.digest_baseline.items():
        cur = ex.bank_digest(obj, bank)
        if cur is not None and cur != baseline:
            return ApplyResult(False, steps,
                               f"ABORT: {_store_label(obj, bank)} changed since "
                               "preview (edited at the panel?) — nothing was Stored")
    note("staleness gate passed")

    # 3. Send all object writes (volatile).
    for w in plan.writes:
        rc = ex.write_object(w)
        note(f"write 0x73 {_store_label(w.obj, w.bank)} idx {w.index} "
             f"({w.note}) -> Reply {rc}")
        if rc != 0:
            return ApplyResult(False, steps,
                               f"ABORT: write rejected (Reply {rc}) for "
                               f"{_store_label(w.obj, w.bank)} idx {w.index} — "
                               "nothing Stored; replay backups to be safe")

    # 4. Commit each affected bank.
    for obj, bank in plan.stores:
        rc = ex.store_bank(obj, bank)
        note(f"Store 0x76 {_store_label(obj, bank)} -> Reply {rc}")
        if rc != 0:
            return ApplyResult(False, steps,
                               f"ABORT: Store rejected (Reply {rc}) for "
                               f"{_store_label(obj, bank)} — replay backups")

    # 5. Optional live edit-buffer dual-write (audible-now, non-persisting).
    if do_live and plan.live_pc:
        for msg in plan.live_pc:
            ex.send_raw(msg)
        note(f"live 0x43 x{len(plan.live_pc)} to active combi")

    return ApplyResult(True, steps)


# ── Self-test (python librarian_model.py) ────────────────────────────────────


def _mk_combi(bank: int, index: int, timbre_refs) -> ObjectDump:
    body = bytearray(7810)
    for t, (fbank, num) in enumerate(timbre_refs):
        set_combi_timbre_ref(body, t, fbank, num)
    return ObjectDump(OBJ_COMBI, bank, index, 3, bytes(body))


def _mk_setlist(number: int, slot_refs) -> ObjectDump:
    body = bytearray(69416)
    for s, (typ, fbank, idx) in slot_refs:
        set_setlist_slot_ref(body, s, fbank, idx, type_=typ)
    return ObjectDump(OBJ_SET_LIST, 0, number, 0, bytes(body))


class _FakeExec(MoveExecutor):
    def __init__(self, digests):
        self._digests = dict(digests)
        self.log: List[str] = []

    def backup_objects(self, ops, path):
        self.log.append(f"backup {len(ops)} objs")

    def bank_digest(self, obj, bank):
        return self._digests.get((obj, bank))

    def write_object(self, op):
        self.log.append(f"write {op.obj:02X}:{op.bank:02X}#{op.index}")
        return 0

    def store_bank(self, obj, bank):
        self.log.append(f"store {obj:02X}:{bank:02X}")
        return 0

    def send_raw(self, data):
        self.log.append("raw")


def _selftest() -> None:
    import sys
    fails: List[str] = []

    def check(name, cond):
        if not cond:
            fails.append(name)

    cat = LibraryCatalog()
    # Program I-A:007 lives at obj bank 0x00, func33 bank 0. Move it to U-A:005
    # (obj bank 0x40, func33 bank 18).
    src = ObjLoc(OBJ_PROGRAM, 0x00, 7)
    dst = ObjLoc(OBJ_PROGRAM, 0x40, 5)
    fb_src = obj_bank_to_func33(1, 0x00)   # 0
    fb_dst = obj_bank_to_func33(1, 0x40)   # 18

    # Combi I-A:000 timbre 3 uses the src program; timbre 5 uses dst program.
    combi = _mk_combi(0x00, 0, [(0, 0)] * 16)
    cbody = bytearray(combi.body)
    set_combi_timbre_ref(cbody, 3, fb_src, 7)     # -> src
    set_combi_timbre_ref(cbody, 5, fb_dst, 5)     # -> dst
    combi = ObjectDump(OBJ_COMBI, 0x00, 0, 3, bytes(cbody))
    cat.add_combi(combi)

    # Set list 000 slot 2 (program type=1) references src program.
    sl = _mk_setlist(0, [(2, (1, fb_src, 7))])
    cat.add_setlist(sl)

    check("usage-src", cat.usage_count(src) == 2)   # combi timbre3 + setlist slot2
    check("usage-dst", cat.usage_count(dst) == 1)   # combi timbre5

    # RefIndex must agree with LibraryCatalog on the same data.
    ri = RefIndex()
    ri.add_combi(combi)
    ri.add_setlist(sl)
    check("refindex-usage-src", ri.usage_count(src) == 2)
    check("refindex-usage-dst", ri.usage_count(dst) == 1)
    check("refindex-ids-src",
          ri.referrer_object_ids(src) == {(OBJ_COMBI, 0x00, 0), (OBJ_SET_LIST, 0, 0)})
    check("refindex-agree",
          {(r.ref_obj, r.ref_bank, r.ref_index, r.site) for r in ri.referrers_of(src)}
          == {(r.ref_obj, r.ref_bank, r.ref_index, r.site) for r in cat.referrers_of(src)})

    # Freshness gate: a changed scan-time digest must be reported stale.
    ri.record_digest(OBJ_COMBI, 0x00, b"AAAA")
    ri.record_digest(OBJ_SET_LIST, 0, b"BBBB")
    fresh_reader = {(OBJ_COMBI, 0x00): b"AAAA", (OBJ_SET_LIST, 0): b"BBBB"}
    check("stale-none", ri.stale_banks(lambda o, b: fresh_reader.get((o, b))) == [])
    changed_reader = {(OBJ_COMBI, 0x00): b"ZZZZ", (OBJ_SET_LIST, 0): b"BBBB"}
    check("stale-detect",
          ri.stale_banks(lambda o, b: changed_reader.get((o, b))) == [(OBJ_COMBI, 0x00)])
    # A None digest (unsupported/timeout) must NOT be treated as stale.
    check("stale-none-on-missing",
          ri.stale_banks(lambda o, b: None) == [])

    # Freshly-dumped bodies of the two programs being swapped (content irrelevant).
    src_dump = ObjectDump(OBJ_PROGRAM, 0x00, 7, 5, bytes([0xAA] * 100))
    dst_dump = ObjectDump(OBJ_PROGRAM, 0x40, 5, 5, bytes([0xBB] * 100))

    plan = plan_move(cat, src, src_dump, dst, dst_dump)
    check("not-refusable", not plan.is_refusable)
    check("referrers", len(plan.referrers) == 3)

    # Writes: 2 swapped programs + 1 combi (both its sites) + 1 set list = 4.
    check("write-count", len(plan.writes) == 4)
    check("preimage-count", len(plan.pre_images) == 4)
    # Pre-images record ORIGINAL locations (src at src.bank, not dst.bank).
    src_pre = next(w for w in plan.pre_images
                   if w.obj == OBJ_PROGRAM and w.bank == 0x00 and w.index == 7)
    check("preimage-src-loc", src_pre.body == src_dump.body)

    # Verify the combi write actually retargets timbre3->dst and timbre5->src.
    combi_write = next(w for w in plan.writes if w.obj == OBJ_COMBI)
    b3, n3 = lsx.combi_timbre_ref(combi_write.body, 3)
    b5, n5 = lsx.combi_timbre_ref(combi_write.body, 5)
    check("t3-retarget", b3 == fb_dst and n3 == 5)
    check("t5-retarget", b5 == fb_src and n5 == 7)

    # Verify set-list slot 2 now points at dst.
    sl_write = next(w for w in plan.writes if w.obj == OBJ_SET_LIST)
    st, sb, si = lsx.setlist_slot_ref(sl_write.body, 2)
    check("sl-retarget", st == 1 and sb == fb_dst and si == 5)

    # Stores: prog I-A, prog U-A, combi I-A, set lists = 4 distinct.
    check("store-count", len(plan.stores) == 4)

    # Refuse a read-only destination.
    bad = plan_move(cat, src, src_dump, ObjLoc(OBJ_PROGRAM, 0x10, 0),
                    ObjectDump(OBJ_PROGRAM, 0x10, 0, 5, b""))
    check("refuse-readonly", bad.is_refusable)

    # Execution: staleness gate passes when digests match, aborts when they don't.
    digests = {s: bytes([i]) for i, s in enumerate(plan.stores)}
    ex = _FakeExec(digests)
    arm_plan(plan, ex)
    res = apply_move(plan, ex, "/tmp", "20260717-000000")
    check("apply-ok", res.ok)
    check("apply-order",
          ex.log[0].startswith("backup") and "store" in ex.log[-1])

    ex2 = _FakeExec(dict(digests))
    arm_plan(plan, ex2)
    # Someone edits one bank between arm and apply.
    changed = plan.stores[0]
    ex2._digests[changed] = b"\xff\xff"
    res2 = apply_move(plan, ex2, "/tmp", "20260717-000001")
    check("apply-abort-stale", (not res2.ok) and "changed since preview" in (res2.aborted_reason or ""))
    check("apply-no-store-on-abort", not any(s.startswith("store") for s in ex2.log))

    if fails:
        print("FAIL:", ", ".join(fails))
        sys.exit(1)
    print("librarian_model self-test: OK")


if __name__ == "__main__":
    _selftest()
