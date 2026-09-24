r"""
Library Pull Pipeline — decides which local-library banks are stale against
real hardware, and reconciles freshly-dumped objects into the local index
without ever clobbering an unsaved local edit.

Port of Core/LocalLibrary/LibraryPullPlanner.cs (AllBanks/PlanPull) and
Core/LocalLibrary/LibraryPullPipeline.cs (PullAsync), against this repo's
local_library_store.py (LocalLibraryIndex/BlobStore/LocalIndexEntry, landed
in the previous commit) instead of the C# LocalLibraryCache facade.

Design differences from the C# source (deliberate, matching the "porting-gap
analysis" discipline local_library_store.py's own module docstring already
follows):

  * String bank keys, not tuple keys. LocalLibraryIndex.bank_digest_baseline
    is already keyed "obj_type:bank" (LocalLibraryIndex.bank_key) throughout
    this Python port — PlanPull here operates on the SAME string keys rather
    than introducing a parallel (ObjType, Bank) tuple-keyed dict as the C#
    source does. One key format for the whole subsystem, not two.
  * No bulk-vs-per-slot fallback logic in this module. LibraryPullPipeline.cs
    inlines the decision "bulk reply came back empty -> re-sweep per-slot"
    because DumpBankBulkAsync IS the transport call there. This port keeps
    transport (sysex_dump_collector.py's collector, bulk request + per-slot
    fallback) entirely behind the injected `get_bank_objects` callable, which
    the caller wires up at the real integration point — that decision doesn't
    belong in a module this self-test exercises with zero hardware.
  * Per-object conflict check, not per-bank. LibraryPullPipeline.cs flags
    EVERY locally-dirty object in a changed bank as Conflicted, even ones
    whose own content is byte-identical to another slot's change in the same
    bank digest. Since bank digests cover the whole bank, that means a dirty
    Program in U-A:005 gets flagged just because U-A:012 changed on hardware.
    This port only marks Conflicted when the INCOMING pulled body for that
    exact slot differs from the entry's own baseline_hash — a strictly
    narrower, more accurate signal, and the one explicitly asked for in this
    task's invariant. A dirty object whose own slot didn't actually change
    keeps its edit, un-flagged.

The invariant this whole module exists to protect (same one LocalIndexEntry.
is_dirty was built for): a locally-dirty object (current_hash != baseline_hash)
must NEVER be overwritten by a pull. If its own incoming pulled content also
differs from its baseline, that's a genuine conflict -> flag Conflicted and
leave both hashes untouched. Otherwise it's a plain refresh: write the new
body via BlobStore.put + LocalLibraryIndex.set_entry, baseline=current=new
hash.

Public composition points for the (later) changeset/sync task:

  BankRef(obj_type, bank).bank_key          -- "obj_type:bank" string
  all_banks() -> List[BankRef]              -- every editable bank, all 3 obj types
  plan_pull(persisted, fresh, full) -> PullPlan   -- PURE, no I/O
  pull(index, blobs, get_live_digest, get_bank_objects, full=False, progress=None)
      -> PullResult

  GetLiveDigest   = Callable[[str], Optional[str]]
                    bank_key -> live SHA-1 hex digest, or None (timeout/unsupported).
                    Real wiring: sysex_service.SysExService.bank_digest(obj, bank),
                    hex-encoded, called once per BankRef.bank_key.
  GetBankObjects  = Callable[[int, int], Dict[int, Tuple[int, bytes]]]
                    (obj_type, bank) -> {number: (version, body)} for every
                    occupied slot in that bank (absent number = empty slot).
                    Real wiring: sysex_dump_collector.SysExDumpCollector,
                    bulk func-0x77 request with a per-slot func-0x73 fallback
                    for USER banks -- entirely the integration point's call,
                    not this module's.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

from Data.librarian_sysex import OBJ_COMBI, OBJ_PROGRAM, OBJ_SET_LIST
from Data.local_library_store import BlobStore, LocalIndexEntry, LocalLibraryIndex
from Tools.setlist_data import MAX_COUNT
import Core.kronos_sysex as ksx

_OBJ_TYPE_DISPLAY = {OBJ_PROGRAM: "Program", OBJ_COMBI: "Combi", OBJ_SET_LIST: "Set List"}


def _bank_display_label(obj_type: int, bank: int) -> str:
    if obj_type == OBJ_PROGRAM:
        return ksx.program_label(bank)
    if obj_type == OBJ_COMBI:
        return ksx.combi_label(bank)
    return ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Bank enumeration (port of ObjectTypeRegistry.EditableBanks/SlotCount) ────
# GM/g program banks (0x10..0x1A) are excluded, same as the C# registry's own
# scoping note: read-only factory content browsing is future scope, not v1.

EDITABLE_BANKS: Dict[int, List[int]] = {
    # Program INT is I-A..I-F — SIX internal banks, not seven: object-dump bank
    # 0x06 ("I-G") is not a real Program bank (see kronos_sysex.func33_to_obj_bank
    # and ObjectTypeRegistry.ProgramDescriptor.EditableBanks' own comment). Listing
    # it here cost twice: every Sync Library swept its 128 slots individually (no
    # bank digest / bulk dump) and returned nothing, AND it stayed a legal auto-fill
    # destination — a write aimed at a bank that does not exist. Combi I-G is
    # unaffected (Combi really does have seven internal banks).
    OBJ_PROGRAM: list(range(0x00, 0x06)) + list(range(0x40, 0x4E)),   # I-A..I-F, U-A..U-GG (20)
    OBJ_COMBI: list(range(0x00, 0x07)) + list(range(0x40, 0x47)),     # I-A..I-G, U-A..U-G  (14)
    OBJ_SET_LIST: [0],                                                # flat 128-slot pseudo-bank
}

SLOT_COUNT: Dict[int, int] = {
    OBJ_PROGRAM: 128,
    OBJ_COMBI: 128,
    OBJ_SET_LIST: MAX_COUNT,
}


@dataclass(frozen=True)
class BankRef:
    obj_type: int
    bank: int

    @property
    def bank_key(self) -> str:
        return LocalLibraryIndex.bank_key(self.obj_type, self.bank)


@dataclass(frozen=True)
class PullPlan:
    banks_to_fetch: List[BankRef]
    first_run: bool


@dataclass(frozen=True)
class PullResult:
    banks_checked: int
    objects_fetched: int
    conflicts: int
    #: True when `cancel` asked the sweep to stop before it had visited every
    #: bank. Whatever WAS pulled is fully applied and its bank baselines are
    #: advanced — a cancelled pull is a short pull, never a corrupt one — but the
    #: caller must not report it as a completed sync.
    cancelled: bool = False


def all_banks() -> List[BankRef]:
    """Every editable bank across all three registry object types, in a
    stable order (Program, then Combi, then Set List) -- mirrors
    LibraryPullPlanner.AllBanks."""
    return [BankRef(obj_type, bank)
            for obj_type in (OBJ_PROGRAM, OBJ_COMBI, OBJ_SET_LIST)
            for bank in EDITABLE_BANKS[obj_type]]


def plan_pull(persisted: Dict[str, str], fresh: Dict[str, str], full: bool) -> PullPlan:
    """PURE. A bank with no persisted baseline (never pulled) is always
    "changed" -- same "unknown = needs work" convention the rest of this app's
    ledgers use (mirrors LibraryPullPlanner.PlanPull exactly, modulo the
    string-vs-tuple key difference noted in the module docstring)."""
    first_run = len(persisted) == 0
    banks = all_banks()
    if full:
        return PullPlan(banks, first_run)

    def changed(b: BankRef) -> bool:
        baseline = persisted.get(b.bank_key)
        cur = fresh.get(b.bank_key)
        return baseline is None or cur is None or cur != baseline

    return PullPlan([b for b in banks if changed(b)], first_run)


GetLiveDigest = Callable[[str], Optional[str]]
GetBankObjects = Callable[[int, int], Dict[int, Tuple[int, bytes]]]


def _extract_display_name(body: bytes) -> str:
    """Every Program/Combi/Set-List body has a 24-byte ASCII name field at
    offset 0 -- same simplification pcg_file.py's _read_record_name already
    makes (see its own comment): reading the field directly here is
    equivalent to the C# port's ProgramBody.ReadName/CombiBody.ReadName/
    SetListBody(...)?.Name dispatch for this module's purposes, without
    porting three body-model classes this module has no other use for."""
    return ksx._ascii_trim(body, 0, 24)


def pull(index: LocalLibraryIndex, blobs: BlobStore,
         get_live_digest: GetLiveDigest, get_bank_objects: GetBankObjects,
         full: bool = False, progress: Optional[Callable[[str], None]] = None,
         cancel: Optional[Callable[[], bool]] = None,
         ) -> PullResult:
    """Digest every registry bank, diff via plan_pull (lazy by default, or
    force everything when full=True), pull each changed bank's objects, and
    reconcile against the local index -- a locally-dirty object is NEVER
    overwritten; if its own pulled content also differs from its baseline
    it is flagged Conflicted instead (see module docstring's "Design
    differences" note for how this differs from LibraryPullPipeline.cs's
    bank-wide conflict check). Advances bank_digest_baseline for every bank
    actually pulled -- does NOT call index.save(); that's the caller's job
    (mirrors LocalLibraryCache.Save being an explicit, separate call in the
    C# pipeline too).

    `cancel`, when given, is polled between banks; returning True stops the
    sweep at the next bank boundary and sets PullResult.cancelled. A full sync
    is minutes of instrument round-trips, so the caller (the Librarian window)
    needs a way to abandon one when its window closes -- without that, the
    sweep kept talking to the instrument, and its progress callback kept firing
    into a deleted window. Bank granularity, not object granularity: a
    half-applied bank whose digest baseline had already advanced would look
    fully pulled on the next sync."""
    cancelled = False
    persisted = dict(index.bank_digest_baseline)
    fresh: Dict[str, str] = {}
    no_digest: List[str] = []
    for b in all_banks():
        if cancel is not None and cancel():
            # Nothing has been written yet; report an empty, cancelled pull
            # rather than a plan built from a partial digest scan.
            return PullResult(0, 0, 0, cancelled=True)
        d = get_live_digest(b.bank_key)
        if d:
            fresh[b.bank_key] = d
        else:
            no_digest.append(b.bank_key)

    # A bank the instrument never answers a digest request for still needs a PERSISTED
    # baseline, or it is "changed" forever: plan_pull treats a missing fresh OR missing
    # persisted digest as changed, so without this the bank was re-swept in full on EVERY
    # lazy Sync Library — 128 slots, and (since a bank that won't answer a digest generally
    # won't answer a bulk dump either) 128 individual dump round-trips. Mirrors
    # LibraryPullPipeline.cs's NoDigest sentinel: the empty string can never collide with a
    # real digest (always 40 hex chars), and a bank pinned this way is only re-fetched by an
    # explicit full pull, which bypasses the changed check entirely. Only when at least one
    # bank DID answer, though: if none did, the instrument is unreachable rather than quiet
    # about one bank, and overwriting every good baseline with the sentinel would silently
    # mark the whole library up to date.
    if fresh and no_digest:
        for key in no_digest:
            fresh[key] = ""   # NoDigest sentinel (LocalLibraryIndex.NO_BASELINE_SENTINEL)

    plan = plan_pull(persisted, fresh, full)

    fetched = 0
    conflicts = 0
    banks_done = 0
    for bank_ref in plan.banks_to_fetch:
        if cancel is not None and cancel():
            cancelled = True
            break
        banks_done += 1
        if progress is not None:
            display = _OBJ_TYPE_DISPLAY.get(bank_ref.obj_type, "")
            bank_label = _bank_display_label(bank_ref.obj_type, bank_ref.bank)
            suffix = f" {bank_label}" if bank_label else ""
            progress(f"Bulk-dumping {display}{suffix}...")
        objs = get_bank_objects(bank_ref.obj_type, bank_ref.bank) or {}

        for number, (version, body) in objs.items():
            entry = index.get(bank_ref.obj_type, bank_ref.bank, number)

            if entry is None or not entry.is_dirty:
                content_hash = blobs.put(body)
                now = _now_iso()
                index.set_entry(bank_ref.obj_type, bank_ref.bank, number, LocalIndexEntry(
                    version=version,
                    baseline_hash=content_hash,
                    current_hash=content_hash,
                    display_name=_extract_display_name(body),
                    created_utc=entry.created_utc if entry is not None else now,
                    modified_utc=now,
                    conflicted=False,
                    has_resolved_dependencies=entry.has_resolved_dependencies if entry is not None else True,
                    is_exi=entry.is_exi if entry is not None else True,
                    pending_delete=False,
                ))
                fetched += 1
            else:
                # Dirty: never overwrite. Conflict only if THIS slot's incoming
                # content actually differs from what it was edited against.
                incoming_hash = BlobStore.compute_hash(body)
                if incoming_hash != entry.baseline_hash:
                    index.mark_conflicted(bank_ref.obj_type, bank_ref.bank, number, True)
                    conflicts += 1

        fresh_hex = fresh.get(bank_ref.bank_key)
        if fresh_hex is not None:
            index.set_bank_digest_baseline(bank_ref.obj_type, bank_ref.bank, fresh_hex)

    return PullResult(banks_done, fetched, conflicts, cancelled=cancelled)


# ── Self-test (python library_pull_pipeline.py) ─────────────────────────────

def _selftest() -> None:
    import hashlib
    import shutil
    import sys
    import tempfile
    import pathlib

    fails: List[str] = []

    def check(name, cond):
        if not cond:
            fails.append(name)

    root = pathlib.Path(tempfile.gettempdir()) / "kronos_selftest_pull_pipeline"
    if root.exists():
        shutil.rmtree(root)
    try:
        blobs = BlobStore(root)
        idx = LocalLibraryIndex(root)

        prog_a_bank, prog_b_bank = 0x00, 0x01
        combi_bank = 0x00

        def make_body(tag: str) -> bytes:
            return (tag.encode("ascii") + b"\x00" * 24)[:24] + hashlib.sha1(tag.encode()).digest()

        # fake "hardware": bank -> {number: (version, body)}
        hw_state = {
            (OBJ_PROGRAM, prog_a_bank): {0: (1, make_body("ProgA0-v1"))},
            (OBJ_COMBI, combi_bank): {0: (1, make_body("CombiA0-v1"))},
        }
        # Every editable bank gets a stable "v1" digest so a full pull sets a
        # complete baseline and a subsequent lazy pull sees nothing changed
        # (not just the two banks this test actually populates with objects).
        hw_digest_version: Dict[Tuple[int, int], int] = {b: 1 for b in
                                                           ((r.obj_type, r.bank) for r in all_banks())}

        def get_live_digest(bank_key: str) -> Optional[str]:
            parts = bank_key.split(":")
            key = (int(parts[0]), int(parts[1]))
            v = hw_digest_version.get(key)
            return None if v is None else f"digest-{bank_key}-v{v}"

        def get_bank_objects(obj_type: int, bank: int):
            return hw_state.get((obj_type, bank), {})

        # ── (1) empty index + full pull populates it and sets digest baseline ──
        result1 = pull(idx, blobs, get_live_digest, get_bank_objects, full=True)
        check("full-pull-banks-checked", result1.banks_checked == len(all_banks()))
        check("full-pull-fetched-2", result1.objects_fetched == 2)
        check("full-pull-no-conflicts", result1.conflicts == 0)

        prog_entry = idx.get(OBJ_PROGRAM, prog_a_bank, 0)
        combi_entry = idx.get(OBJ_COMBI, combi_bank, 0)
        check("prog-entry-written", prog_entry is not None and not prog_entry.is_dirty)
        check("combi-entry-written", combi_entry is not None and not combi_entry.is_dirty)
        check("prog-display-name", prog_entry is not None and prog_entry.display_name.startswith("ProgA0-v1"))
        prog_a_key = BankRef(OBJ_PROGRAM, prog_a_bank).bank_key
        check("digest-baseline-set",
              idx.bank_digest_baseline.get(prog_a_key) == f"digest-{prog_a_key}-v1")

        # ── (2) lazy pull, nothing changed on hardware -> bank skipped ──
        result2 = pull(idx, blobs, get_live_digest, get_bank_objects, full=False)
        check("lazy-unchanged-no-fetch", result2.objects_fetched == 0)
        check("lazy-unchanged-no-banks", result2.banks_checked == 0)

        # ── (3) lazy pull, digest changed -> bank re-pulled ──
        hw_digest_version[(OBJ_PROGRAM, prog_a_bank)] = 2
        hw_state[(OBJ_PROGRAM, prog_a_bank)] = {0: (2, make_body("ProgA0-v2"))}
        result3 = pull(idx, blobs, get_live_digest, get_bank_objects, full=False)
        check("lazy-changed-banks-checked-1", result3.banks_checked == 1)
        check("lazy-changed-fetched-1", result3.objects_fetched == 1)
        prog_entry_v2 = idx.get(OBJ_PROGRAM, prog_a_bank, 0)
        check("lazy-changed-entry-updated",
              prog_entry_v2 is not None and prog_entry_v2.version == 2 and not prog_entry_v2.is_dirty)
        check("lazy-changed-digest-advanced",
              idx.bank_digest_baseline.get(prog_a_key) == f"digest-{prog_a_key}-v2")

        # ── (4) locally-dirty object whose bank ALSO changed -> Conflicted, NOT overwritten ──
        dirty_body = make_body("ProgA0-LOCAL-EDIT")
        dirty_hash = blobs.put(dirty_body)
        prog_entry_v2.current_hash = dirty_hash   # simulate a local edit: current != baseline now
        check("precondition-is-dirty", idx.get(OBJ_PROGRAM, prog_a_bank, 0).is_dirty)

        hw_digest_version[(OBJ_PROGRAM, prog_a_bank)] = 3
        hw_state[(OBJ_PROGRAM, prog_a_bank)] = {0: (3, make_body("ProgA0-v3-FROM-HW"))}
        result4 = pull(idx, blobs, get_live_digest, get_bank_objects, full=False)
        check("conflict-pull-fetched-none", result4.objects_fetched == 0)
        check("conflict-pull-conflicts-1", result4.conflicts == 1)

        conflicted_entry = idx.get(OBJ_PROGRAM, prog_a_bank, 0)
        check("conflict-flagged", conflicted_entry is not None and conflicted_entry.conflicted is True)
        check("conflict-current-hash-unchanged",
              conflicted_entry is not None and conflicted_entry.current_hash == dirty_hash)
        check("conflict-baseline-unchanged",
              conflicted_entry is not None and conflicted_entry.baseline_hash != blobs.compute_hash(make_body("ProgA0-v3-FROM-HW")))

        # ── dirty object whose own slot did NOT change (bank digest changed for
        #    a DIFFERENT slot) keeps its edit silently, no conflict flag ──
        idx2 = LocalLibraryIndex(root)
        blobs2 = BlobStore(root)
        base_body = make_body("Untouched-baseline")
        base_hash = blobs2.put(base_body)
        edited_hash = blobs2.put(make_body("Untouched-LOCAL-EDIT"))
        idx2.set_entry(OBJ_COMBI, combi_bank, 5, LocalIndexEntry(
            version=1, baseline_hash=base_hash, current_hash=edited_hash,
            display_name="Untouched", created_utc=_now_iso(), modified_utc=_now_iso()))
        idx2.set_bank_digest_baseline(OBJ_COMBI, combi_bank, "digest-combia-v1")

        def get_bank_objects2(obj_type, bank):
            if (obj_type, bank) == (OBJ_COMBI, combi_bank):
                return {5: (1, base_body)}   # same content as this slot's own baseline
            return {}

        result5 = pull(idx2, blobs2,
                        lambda bk: "digest-combia-v2" if bk == BankRef(OBJ_COMBI, combi_bank).bank_key else None,
                        get_bank_objects2, full=False)
        check("untouched-slot-no-conflict", result5.conflicts == 0)
        untouched_entry = idx2.get(OBJ_COMBI, combi_bank, 5)
        check("untouched-slot-preserved",
              untouched_entry is not None and untouched_entry.current_hash == edited_hash
              and not untouched_entry.conflicted)

    finally:
        if root.exists():
            shutil.rmtree(root)

    if fails:
        print("FAIL:", ", ".join(fails))
        sys.exit(1)
    print("library_pull_pipeline self-test: OK")


if __name__ == "__main__":
    _selftest()
