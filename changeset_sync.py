r"""
Changeset build + sync/commit execution — the capstone of the Local Library
subsystem: the push half of the Sync/Commit pipeline, wiring together
local_library_store, library_pull_pipeline, dependency_scanner, and
session_dependency_clipboard into the one thing a user actually clicks.

Port of Core/LocalLibrary/ChangesetBuilder.cs (BuildAsync), Core/LocalLibrary/
ChangesetModel.cs (ChangesetPlan), and Core/LocalLibrary/SyncPipeline.cs
(PushAsync/CommitChangesAsync/SyncLibraryAsync).

BuildAsync's four steps, ported as build_changeset():
  1. Dependency-completeness gate — REFUSE outright (no plan assembled at all)
     if the session clipboard still has unresolved placements.
  2. Conflict pre-scan — one get_live_digest per distinct bank touched by a
     dirty-or-pending-delete entry, compared against index.bank_digest_baseline.
     A changed bank excludes every entry in it (flagged Conflicted, same
     marker library_pull_pipeline uses) instead of silently overwriting a
     possible concurrent front-panel edit. NON-OBVIOUS INVARIANT: this is a
     SECOND digest check, not a repeat of the pull pipeline's — real time has
     passed since the object was last pulled or edited, and a bank can drift
     between edit-time and push-time even though it looked clean at the last
     pull. Skipping this and trusting the stale baseline is exactly the bug
     this step exists to catch.
  3. Defense-in-depth referential check — every surviving dirty Combi/Set
     List is walked via dependency_scanner against `resolver`; ANY missing
     reference REFUSEs the WHOLE plan (not just that entry) — mirrors
     ChangesetBuilder.cs's own `if (plan.IsRefusable) return` gate placement,
     which runs before any Writes are assembled at all.
  4. Assemble ChangesetEntry writes from the surviving dirty-or-pending-delete
     set.

Design differences from the C# source (deliberate, matching the porting-gap
discipline every other module in this subsystem already documents):
  * No BankTypeChanges / whole-bank HD-1<->EXi reformat (func 0x7C), and no
    Step 3.5 live Program-bank-type re-verification query. Both depend on
    C# plumbing this Python port doesn't have yet (RequestProgramBankTypesAsync,
    KronosBanks.ProgramBankTypeBitIndex, ProgramFormatConverter) — out of
    scope here, same as merge_cache.py scoping out RefKind/Site placement
    patching until its own dependency exists. A future task can add it once
    that plumbing lands.
  * LivePc is not carried at all. C#'s ChangesetPlan keeps LivePc permanently
    empty (requirement 17: no live 0x43 anywhere in the push pipeline) — a
    field that can never hold anything isn't worth porting.
  * `conflicted` is a field ON ChangesetPlan (a list of index keys), not a
    second value in a tuple return from build_changeset the way C#'s
    BuildAsync returns `(ChangesetPlan, List<ObjLoc>)`. Functionally
    identical, simpler for a single dataclass return.
  * execute_changeset's `write_to_hardware(obj_type, bank, number, body) ->
    bool` is a deliberately simplified transport boundary compared to C#'s
    Librarian.ArmPlanAsync/ApplyMoveAsync (four separate MoveExecutor methods:
    backup_objects/bank_digest/write_object/store_bank) — same "the real
    wiring is the caller's job" precedent GetLiveDigest/GetBankObjects already
    set in library_pull_pipeline.py. A real integration's write_to_hardware is
    expected to internally do the backup + a final staleness re-check + the
    0x73 write + 0x76 Store (e.g. via librarian_model.py's WriteOp/apply_move
    plumbing, the same backup -> staleness-gate -> write -> store sequence
    the existing move/swap tool already uses) — this module only needs to
    know whether the whole per-slot write succeeded, not its internal steps.

Two entry points, confirmed distinct from the C# source (not guessed):
  commit_changes — push only. Skips pulling.
  sync_library   — pull, THEN push, in one call. NON-OBVIOUS INVARIANT (see
    SyncPipeline.cs's own comment on SyncLibraryAsync): pull runs first so
    build_changeset's digest re-check sees the freshest possible baseline,
    minimizing spurious conflicts. Push-then-pull was considered and
    rejected in the C# source: it would push against a possibly-stale
    baseline and then immediately re-pull over data it just wrote.

Neither build_changeset nor execute_changeset calls index.save() — same
"caller's job" convention library_pull_pipeline.pull() already established.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

import dependency_scanner as depscan
from librarian_model import ObjLoc
from librarian_sysex import OBJ_COMBI, OBJ_PROGRAM, OBJ_SET_LIST
from library_pull_pipeline import GetBankObjects, GetLiveDigest, PullResult
import library_pull_pipeline as pull_pipeline
from local_library_store import BlobStore, LocalLibraryIndex
from session_dependency_clipboard import SessionDependencyClipboard

WriteToHardware = Callable[[int, int, int, bytes], bool]


def _parse_key(key: str) -> Tuple[int, int, int]:
    obj_type, bank, number = key.split(":")
    return int(obj_type), int(bank), int(number)


# ── Plan shape ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ChangesetEntry:
    """One slot this changeset will write. Port of one element of C#
    ChangesetPlan.Writes (paired with its PreImages entry, which this port
    doesn't need — the pre-image/backup step lives inside the injected
    write_to_hardware, see module docstring)."""
    key: str            # LocalLibraryIndex.key(obj_type, bank, number)
    obj_type: int
    bank: int
    number: int
    body: bytes          # content to write — the entry's current_hash's blob
    version: int
    is_erase: bool = False   # True = a pending_delete entry's blank-body write


@dataclass
class ChangesetPlan:
    entries: List[ChangesetEntry] = field(default_factory=list)
    local_only_deletes: List[str] = field(default_factory=list)   # never had a hardware baseline — remove locally only
    conflicted: List[str] = field(default_factory=list)           # keys excluded by the digest pre-scan
    warnings: List[str] = field(default_factory=list)

    @property
    def is_refusable(self) -> bool:
        return any(w.startswith("REFUSE:") for w in self.warnings)


@dataclass(frozen=True)
class SyncResult:
    written: int = 0     # dirty edits successfully pushed (excludes erasures)
    erased: int = 0       # pending-delete entries successfully blanked on hardware
    deleted: int = 0      # local-only deletes removed (never existed on hardware)
    failed: int = 0       # entries where write_to_hardware returned False


RecordSuccess = Callable[[ChangesetEntry], None]


# ── Build (mostly pure — get_live_digest/resolver are the only I/O) ─────────


def build_changeset(index: LocalLibraryIndex, blobs: BlobStore,
                     clipboard: SessionDependencyClipboard,
                     get_live_digest: GetLiveDigest,
                     resolver: depscan.Resolver) -> ChangesetPlan:
    plan = ChangesetPlan()

    # Step 1: dependency-completeness gate.
    if clipboard.has_unresolved():
        plan.warnings.append(
            f"REFUSE: {len(clipboard.pending)} dependency(ies) still pending "
            "in the session clipboard — place them before pushing")
        return plan

    # Delete supersedes edit: pull pending-delete entries out of the dirty set
    # up front so an erase is never ALSO queued as a normal edit write.
    pending_delete_keys = [k for k, e in index.entries.items() if e.pending_delete]
    dirty_keys = [k for k, e in index.entries.items() if e.is_dirty and not e.pending_delete]

    if not dirty_keys and not pending_delete_keys:
        plan.warnings.append("CHECK: nothing to push — no local changes are pending")
        return plan

    # Step 2: conflict pre-scan, one get_live_digest per distinct bank touched
    # by ANYTHING we might push. See module docstring for why this re-checks
    # rather than trusting the already-recorded baseline.
    touched_bank_keys = {
        LocalLibraryIndex.bank_key(*_parse_key(k)[:2])
        for k in dirty_keys + pending_delete_keys
    }
    excluded_banks = set()
    for bank_key in touched_bank_keys:
        fresh_hex = get_live_digest(bank_key) or ""
        baseline_hex = index.bank_digest_baseline.get(bank_key)
        changed = baseline_hex is None or fresh_hex != baseline_hex
        if changed:
            excluded_banks.add(bank_key)

    def _sift(keys: List[str]) -> List[str]:
        surviving: List[str] = []
        for k in keys:
            obj_type, bank, number = _parse_key(k)
            if LocalLibraryIndex.bank_key(obj_type, bank) in excluded_banks:
                index.mark_conflicted(obj_type, bank, number, True)
                plan.conflicted.append(k)
            else:
                surviving.append(k)
        return surviving

    surviving = _sift(dirty_keys)
    surviving_deletes = _sift(pending_delete_keys)

    # Step 3: defense-in-depth referential check over surviving EDITS only
    # (an entry about to be erased has no "own references must resolve"
    # concern — its content is going away). ANY missing reference REFUSEs the
    # WHOLE plan, matching ChangesetBuilder.cs's own gate placement.
    for k in surviving:
        obj_type, bank, number = _parse_key(k)
        if obj_type not in (OBJ_COMBI, OBJ_SET_LIST):
            continue
        entry = index.entries[k]
        body = blobs.get(entry.current_hash)
        if body is None:
            continue
        loc = ObjLoc(obj_type, bank, number)
        for missing in depscan.scan(resolver, obj_type, body):
            plan.warnings.append(
                f"REFUSE: {loc.label()} references {missing.ref.label()} "
                f"({missing.ref_kind}), which does not exist locally")

    if plan.is_refusable:
        return plan

    # Step 4: assemble the plan.
    for k in surviving:
        obj_type, bank, number = _parse_key(k)
        entry = index.entries[k]
        body = blobs.get(entry.current_hash)
        if body is None:
            continue
        plan.entries.append(ChangesetEntry(k, obj_type, bank, number, body,
                                            entry.version, is_erase=False))

    for k in surviving_deletes:
        obj_type, bank, number = _parse_key(k)
        entry = index.entries[k]
        if entry.baseline_hash == LocalLibraryIndex.NO_BASELINE_SENTINEL:
            # Never existed on hardware — nothing to erase there, just drop it locally.
            plan.local_only_deletes.append(k)
            continue
        # entry.current_hash is already the blank-template body — staged by
        # blank_template_store.erase() at "mark for delete" time, not derived here.
        body = blobs.get(entry.current_hash)
        if body is None:
            continue
        plan.entries.append(ChangesetEntry(k, obj_type, bank, number, body,
                                            entry.version, is_erase=True))

    if not plan.entries and not plan.local_only_deletes:
        plan.warnings.append("CHECK: every pending change conflicted or was rejected — nothing left to push")

    return plan


# ── Execute ──────────────────────────────────────────────────────────────────


def execute_changeset(plan: ChangesetPlan, index: LocalLibraryIndex,
                       write_to_hardware: WriteToHardware,
                       record_success: Optional[RecordSuccess] = None) -> SyncResult:
    """Port of SyncPipeline.PushAsync's execution half (RecordPushSuccesses).
    A refusable plan writes nothing (mirrors PushAsync's own
    `if (plan.IsRefusable) return` before ever touching hardware). Each entry
    is independent: one write_to_hardware failure does not abort the rest of
    the batch — matches RecordPushSuccesses only ever advancing the objects
    that actually wrote successfully, never all-or-nothing."""
    if plan.is_refusable:
        return SyncResult()

    written = 0
    erased = 0
    failed = 0
    for entry in plan.entries:
        ok = write_to_hardware(entry.obj_type, entry.bank, entry.number, entry.body)
        if not ok:
            failed += 1
            continue
        if entry.is_erase:
            erased += 1
        else:
            written += 1
        if record_success is not None:
            record_success(entry)
        else:
            idx_entry = index.get(entry.obj_type, entry.bank, entry.number)
            if idx_entry is not None:
                idx_entry.baseline_hash = idx_entry.current_hash
                idx_entry.conflicted = False
                if entry.is_erase:
                    idx_entry.pending_delete = False

    deleted = 0
    for k in plan.local_only_deletes:
        obj_type, bank, number = _parse_key(k)
        index.delete(obj_type, bank, number)
        deleted += 1

    return SyncResult(written=written, erased=erased, deleted=deleted, failed=failed)


# ── Entry points ─────────────────────────────────────────────────────────────


def commit_changes(index: LocalLibraryIndex, blobs: BlobStore,
                    clipboard: SessionDependencyClipboard,
                    get_live_digest: GetLiveDigest, resolver: depscan.Resolver,
                    write_to_hardware: WriteToHardware,
                    record_success: Optional[RecordSuccess] = None,
                    ) -> Tuple[ChangesetPlan, SyncResult]:
    """Push-only — port of SyncPipeline.CommitChangesAsync. Deliberately
    skips pulling; pushes straight against whatever bank-digest baseline is
    already on record."""
    plan = build_changeset(index, blobs, clipboard, get_live_digest, resolver)
    result = execute_changeset(plan, index, write_to_hardware, record_success)
    return plan, result


def sync_library(index: LocalLibraryIndex, blobs: BlobStore,
                  clipboard: SessionDependencyClipboard,
                  get_live_digest: GetLiveDigest, get_bank_objects: GetBankObjects,
                  resolver: depscan.Resolver, write_to_hardware: WriteToHardware,
                  record_success: Optional[RecordSuccess] = None,
                  full: bool = False, progress=None,
                  ) -> Tuple[PullResult, ChangesetPlan, SyncResult]:
    """Pull, then push — port of SyncPipeline.SyncLibraryAsync. See module
    docstring's pull-vs-commit invariant note for why the ordering is pull
    THEN push, not the reverse."""
    pull_result = pull_pipeline.pull(index, blobs, get_live_digest, get_bank_objects,
                                     full=full, progress=progress)
    plan, result = commit_changes(index, blobs, clipboard, get_live_digest, resolver,
                                  write_to_hardware, record_success)
    return pull_result, plan, result


# ── Self-test (python changeset_sync.py) ────────────────────────────────────


def _selftest() -> None:
    import pathlib
    import shutil
    import sys
    import tempfile
    from datetime import datetime, timezone

    from local_library_store import LocalIndexEntry
    from librarian_sysex import func33_to_obj_bank, set_combi_timbre_ref

    fails: List[str] = []

    def check(name: str, cond: bool) -> None:
        if not cond:
            fails.append(name)

    def now() -> str:
        return datetime.now(timezone.utc).isoformat()

    root = pathlib.Path(tempfile.gettempdir()) / "kronos_selftest_changeset_sync"
    if root.exists():
        shutil.rmtree(root)
    try:
        blobs = BlobStore(root)

        # ── (1) build refuses when the clipboard has unresolved dependencies ──
        idx1 = LocalLibraryIndex(root / "s1")
        old_hash = blobs.put(b"OLD-PROGRAM-BODY")
        new_hash = blobs.put(b"NEW-PROGRAM-BODY")
        idx1.set_entry(OBJ_PROGRAM, 0x00, 3, LocalIndexEntry(
            version=1, baseline_hash=old_hash, current_hash=new_hash,
            display_name="P3", created_utc=now(), modified_utc=now()))

        from session_dependency_clipboard import SessionDependencyEntry
        clip_blocked = SessionDependencyClipboard()
        clip_blocked.add(SessionDependencyEntry(
            missing_ref=ObjLoc(OBJ_COMBI, 0x40, 5), ref_kind="setlist_slot", site=0,
            required_by=ObjLoc(OBJ_SET_LIST, 0, 1)))

        plan1 = build_changeset(idx1, blobs, clip_blocked,
                                 get_live_digest=lambda bk: "irrelevant",
                                 resolver=lambda t, b, n: True)
        check("clipboard-gate-refuses", plan1.is_refusable)
        check("clipboard-gate-no-entries", plan1.entries == [])

        # ── (2) clean dirty entry, no conflicts -> builds + executes, baseline advances ──
        idx2 = LocalLibraryIndex(root / "s2")
        idx2.set_entry(OBJ_PROGRAM, 0x00, 3, LocalIndexEntry(
            version=1, baseline_hash=old_hash, current_hash=new_hash,
            display_name="P3", created_utc=now(), modified_utc=now()))
        idx2.set_bank_digest_baseline(OBJ_PROGRAM, 0x00, "bankdigest-v1")
        clip_empty = SessionDependencyClipboard()

        plan2 = build_changeset(idx2, blobs, clip_empty,
                                 get_live_digest=lambda bk: "bankdigest-v1",
                                 resolver=lambda t, b, n: True)
        check("clean-not-refusable", not plan2.is_refusable)
        check("clean-one-entry", len(plan2.entries) == 1 and plan2.entries[0].key == "0:0:3")
        check("clean-no-conflicts", plan2.conflicted == [])

        hw_log2: List[Tuple[int, int, int, bytes]] = []

        def write_ok(obj_type, bank, number, body):
            hw_log2.append((obj_type, bank, number, body))
            return True

        result2 = execute_changeset(plan2, idx2, write_ok)
        check("clean-written-1", result2.written == 1 and result2.failed == 0)
        check("clean-hw-got-new-body", hw_log2 == [(OBJ_PROGRAM, 0x00, 3, b"NEW-PROGRAM-BODY")])
        entry2_after = idx2.get(OBJ_PROGRAM, 0x00, 3)
        check("clean-baseline-advanced",
              entry2_after is not None and entry2_after.baseline_hash == new_hash and not entry2_after.is_dirty)

        # ── (3) dirty entry whose bank digest changed since baseline -> conflicted, excluded ──
        idx3 = LocalLibraryIndex(root / "s3")
        idx3.set_entry(OBJ_PROGRAM, 0x00, 5, LocalIndexEntry(
            version=1, baseline_hash=old_hash, current_hash=new_hash,
            display_name="P5", created_utc=now(), modified_utc=now()))
        idx3.set_bank_digest_baseline(OBJ_PROGRAM, 0x00, "bankdigest-v1")

        plan3 = build_changeset(idx3, blobs, clip_empty,
                                 get_live_digest=lambda bk: "bankdigest-v2-changed",
                                 resolver=lambda t, b, n: True)
        check("stale-bank-not-refusable-but-empty", not plan3.is_refusable)
        check("stale-bank-no-entries", plan3.entries == [])
        check("stale-bank-conflicted", plan3.conflicted == ["0:0:5"])
        check("stale-bank-index-marked", idx3.get(OBJ_PROGRAM, 0x00, 5).conflicted is True)

        hw_log3: List[Tuple[int, int, int, bytes]] = []
        result3 = execute_changeset(plan3, idx3, lambda *a: (hw_log3.append(a) or True))
        check("stale-bank-nothing-written", result3.written == 0 and hw_log3 == [])

        # ── (4) dirty Combi with an unresolvable reference -> whole plan REFUSEd ──
        idx4 = LocalLibraryIndex(root / "s4")
        fb_present = func33_to_obj_bank(1, 0)     # I-A program bank (obj-dump encoding)
        fb_missing = func33_to_obj_bank(1, 18)    # U-A program bank (obj-dump encoding)
        combi_body = bytearray(7810)
        set_combi_timbre_ref(combi_body, 0, 0, 7)     # func33 bank 0  -> Program I-A:007 (present)
        set_combi_timbre_ref(combi_body, 1, 18, 3)    # func33 bank 18 -> Program U-A:003 (missing)
        old_combi_hash = blobs.put(b"OLD-COMBI-BODY-PADDED..........")
        new_combi_hash = blobs.put(bytes(combi_body))
        idx4.set_entry(OBJ_COMBI, 0x00, 0, LocalIndexEntry(
            version=1, baseline_hash=old_combi_hash, current_hash=new_combi_hash,
            display_name="C0", created_utc=now(), modified_utc=now()))
        idx4.set_bank_digest_baseline(OBJ_COMBI, 0x00, "combidigest-v1")

        present = {(OBJ_PROGRAM, fb_present, 7), (OBJ_PROGRAM, fb_present, 0)}   # unset timbres default to (fb_present, 0)
        plan4 = build_changeset(idx4, blobs, clip_empty,
                                 get_live_digest=lambda bk: "combidigest-v1",
                                 resolver=lambda t, b, n: (t, b, n) in present)
        check("missing-ref-refuses-whole-plan", plan4.is_refusable)
        check("missing-ref-no-entries", plan4.entries == [])
        check("missing-ref-warning-mentions-refuse",
              any(w.startswith("REFUSE:") and "does not exist locally" in w for w in plan4.warnings))

        # ── (5) pending_delete entry executes, clears pending_delete, advances baseline ──
        idx5 = LocalLibraryIndex(root / "s5")
        existing_hash = blobs.put(b"EXISTING-PROGRAM-ON-HW..")
        blank_hash = blobs.put(b"BLANK-INIT-PROGRAM......")
        idx5.set_entry(OBJ_PROGRAM, 0x00, 9, LocalIndexEntry(
            version=1, baseline_hash=existing_hash, current_hash=blank_hash,
            display_name="P9", created_utc=now(), modified_utc=now(), pending_delete=True))
        idx5.set_bank_digest_baseline(OBJ_PROGRAM, 0x00, "bankdigest-erase-v1")

        plan5 = build_changeset(idx5, blobs, clip_empty,
                                 get_live_digest=lambda bk: "bankdigest-erase-v1",
                                 resolver=lambda t, b, n: True)
        check("erase-not-refusable", not plan5.is_refusable)
        check("erase-one-entry-flagged", len(plan5.entries) == 1 and plan5.entries[0].is_erase)

        result5 = execute_changeset(plan5, idx5, lambda *a: True)
        check("erase-written-as-erased", result5.erased == 1 and result5.written == 0)
        entry5_after = idx5.get(OBJ_PROGRAM, 0x00, 9)
        check("erase-pending-delete-cleared", entry5_after is not None and entry5_after.pending_delete is False)
        check("erase-baseline-advanced",
              entry5_after is not None and entry5_after.baseline_hash == blank_hash and not entry5_after.is_dirty)

        # ── (5b) local-only delete (never had a hardware baseline) -> removed, no hw write ──
        idx5b = LocalLibraryIndex(root / "s5b")
        local_only_hash = blobs.put(b"LOCAL-ONLY-NEVER-PUSHED.")
        idx5b.set_entry(OBJ_COMBI, 0x40, 0, LocalIndexEntry(
            version=0, baseline_hash=LocalLibraryIndex.NO_BASELINE_SENTINEL, current_hash=local_only_hash,
            display_name="Local Only", created_utc=now(), modified_utc=now(), pending_delete=True))
        idx5b.set_bank_digest_baseline(OBJ_COMBI, 0x40, "combibankdigest-v1")
        plan5b = build_changeset(idx5b, blobs, clip_empty,
                                 get_live_digest=lambda bk: "combibankdigest-v1",
                                 resolver=lambda t, b, n: True)
        check("local-only-delete-no-hw-entries", plan5b.entries == [])
        check("local-only-delete-listed", plan5b.local_only_deletes == ["1:64:0"])
        hw_log5b: List = []
        result5b = execute_changeset(plan5b, idx5b, lambda *a: (hw_log5b.append(a) or True))
        check("local-only-delete-no-hw-call", hw_log5b == [])
        check("local-only-delete-removed", idx5b.get(OBJ_COMBI, 0x40, 0) is None)
        check("local-only-delete-count", result5b.deleted == 1)

        # ── (6) write_to_hardware False for one entry doesn't block others, doesn't advance it ──
        idx6 = LocalLibraryIndex(root / "s6")
        good_old, good_new = blobs.put(b"GOOD-OLD"), blobs.put(b"GOOD-NEW")
        bad_old, bad_new = blobs.put(b"BAD--OLD"), blobs.put(b"BAD--NEW")
        idx6.set_entry(OBJ_PROGRAM, 0x00, 1, LocalIndexEntry(
            version=1, baseline_hash=good_old, current_hash=good_new,
            display_name="Good", created_utc=now(), modified_utc=now()))
        idx6.set_entry(OBJ_PROGRAM, 0x00, 2, LocalIndexEntry(
            version=1, baseline_hash=bad_old, current_hash=bad_new,
            display_name="Bad", created_utc=now(), modified_utc=now()))
        idx6.set_bank_digest_baseline(OBJ_PROGRAM, 0x00, "bankdigest-mixed")

        plan6 = build_changeset(idx6, blobs, clip_empty,
                                get_live_digest=lambda bk: "bankdigest-mixed",
                                resolver=lambda t, b, n: True)
        check("mixed-two-entries", len(plan6.entries) == 2)

        def write_fails_for_2(obj_type, bank, number, body):
            return number != 2

        result6 = execute_changeset(plan6, idx6, write_fails_for_2)
        check("mixed-one-written-one-failed", result6.written == 1 and result6.failed == 1)
        good_after = idx6.get(OBJ_PROGRAM, 0x00, 1)
        bad_after = idx6.get(OBJ_PROGRAM, 0x00, 2)
        check("mixed-good-baseline-advanced", good_after is not None and good_after.baseline_hash == good_new)
        check("mixed-bad-baseline-not-advanced",
              bad_after is not None and bad_after.baseline_hash == bad_old and bad_after.is_dirty)

        # ── commit_changes / sync_library thin wrappers ──
        idx7 = LocalLibraryIndex(root / "s7")
        idx7.set_entry(OBJ_PROGRAM, 0x00, 4, LocalIndexEntry(
            version=1, baseline_hash=old_hash, current_hash=new_hash,
            display_name="P4", created_utc=now(), modified_utc=now()))
        idx7.set_bank_digest_baseline(OBJ_PROGRAM, 0x00, "bankdigest-v1")
        plan7, result7 = commit_changes(idx7, blobs, clip_empty,
                                        get_live_digest=lambda bk: "bankdigest-v1",
                                        resolver=lambda t, b, n: True,
                                        write_to_hardware=lambda *a: True)
        check("commit-changes-written", result7.written == 1 and not plan7.is_refusable)

        idx8 = LocalLibraryIndex(root / "s8")
        idx8.set_entry(OBJ_PROGRAM, 0x00, 6, LocalIndexEntry(
            version=1, baseline_hash=old_hash, current_hash=new_hash,
            display_name="P6", created_utc=now(), modified_utc=now()))

        def get_live_digest8(bk):
            return "bankdigest-v1"

        def get_bank_objects8(obj_type, bank):
            return {}

        pull_r, plan8, result8 = sync_library(idx8, blobs, clip_empty,
                                              get_live_digest=get_live_digest8,
                                              get_bank_objects=get_bank_objects8,
                                              resolver=lambda t, b, n: True,
                                              write_to_hardware=lambda *a: True,
                                              full=True)
        check("sync-library-pulled", isinstance(pull_r, PullResult))
        check("sync-library-pushed", result8.written == 1 and not plan8.is_refusable)

    finally:
        if root.exists():
            shutil.rmtree(root)

    if fails:
        print("FAIL:", ", ".join(fails))
        sys.exit(1)
    print("changeset_sync self-test: OK")


if __name__ == "__main__":
    _selftest()
