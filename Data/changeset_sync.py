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
     this step exists to catch. Entries for a SLOT a pull could not read
     (index.incomplete_banks, per slot) are taken out BEFORE this step into plan.incomplete:
     its live content is unknown, so neither "Conflicted" nor a push is right (see
     build_changeset's allow_incomplete). Other slots of that bank are unaffected.
  3. Defense-in-depth referential check — every surviving dirty Combi/Set
     List is walked via dependency_scanner against `resolver`; ANY missing
     reference REFUSEs the WHOLE plan (not just that entry) — mirrors
     ChangesetBuilder.cs's own `if (plan.IsRefusable) return` gate placement,
     which runs before any Writes are assembled at all.
  4. Assemble ChangesetEntry writes from the surviving dirty-or-pending-delete
     set.

Design differences from the C# source (deliberate, matching the porting-gap
discipline every other module in this subsystem already documents):
  * BankTypeChanges / whole-bank HD-1<->EXi reformat (func 0x7C) + Step 3.5
    live Program-bank-type re-verification — previously deferred here, now
    ported, confirmed against ChangesetBuilder.cs/ChangesetModel.cs/
    LibrarianModel.cs's ApplyMoveAsync line by line:
      - A bank-type MISMATCH by itself (a surviving Program write whose body
        length doesn't match the bank's CURRENT live HD-1/EXi format) is
        NEVER auto-resolved by queuing a reformat. ChangesetBuilder.cs's own
        Step 3.5 only ever REFUSEs that whole bank (one REFUSE per bank, not
        per Program — issue 3b) when it finds this. Ported as
        get_live_bank_type below.
      - A whole-bank func-0x7C reformat is queued ONLY when the caller has a
        separately staged, INTENTIONAL conversion on record for that bank
        (C#'s `cache.PendingBankTypeChange(bank)` — some other, out-of-scope
        UI flow the user drives deliberately, e.g. "convert this bank to
        EXi"). Ported as pending_bank_type_change below; entirely independent
        of get_live_bank_type — queuing it does not require or imply a
        verified mismatch, and it works even when get_live_bank_type is None
        (0x7C is a no-op on the instrument if the bank is already that type).
      - A bank with a staged conversion is EXCLUDED from the Step 3.5
        mismatch check (ChangesetBuilder.cs: `if (plan.BankTypeChanges.Any(x
        => x.Bank == bank)) continue;`) — the queued 0x7C will make the
        format match before these writes land, so comparing against the
        CURRENT (pre-reformat) live type would always look like a mismatch.
      - Execution order matters and is NOT the independent-per-entry
        semantics execute_changeset otherwise uses for plan.entries:
        LibrarianModel.cs's ApplyMoveAsync issues every BankTypeChange right
        after the staleness gate but BEFORE any object write (0x7C erases the
        whole bank, so every slot must still be about to be rewritten), and a
        rejected 0x7C ABORTS THE WHOLE PUSH — not just that bank. Ported
        exactly: execute_changeset below runs write_bank_type_change calls
        first, and any single False aborts the entire call (no entries are
        written at all), mirroring that all-or-nothing gate.
      - get_live_bank_type/pending_bank_type_change/write_bank_type_change all
        take a plain Program obj-dump bank int, not the "obj_type:bank"
        string GetLiveDigest uses — bank-type is a Program-only concept with
        no obj_type axis, matching librarian_model.py's plan_batch_move's own
        pre-existing bank_type_of convention (same underlying HD-1/EXi
        concept, checked at placement time instead of push time) rather than
        introducing a second, incompatible calling convention for the same
        idea.
  * LivePc is not carried at all. C#'s ChangesetPlan keeps LivePc permanently
    empty (requirement 17: no live 0x43 anywhere in the push pipeline) — a
    field that can never hold anything isn't worth porting.
  * `conflicted` is a field ON ChangesetPlan (a list of index keys), not a
    second value in a tuple return from build_changeset the way C#'s
    BuildAsync returns `(ChangesetPlan, List<ObjLoc>)`. Functionally
    identical, simpler for a single dataclass return.
  * The production integration supplies prepare_hardware (backups and the
    pre-mutation staleness gate), volatile write_to_hardware, and store_bank.
    A rejected write stops the burst before any Store; baselines advance only
    for banks actually Stored. Cancellation is honored before mutation, never
    halfway through a burst that may already have erased an entire bank.
    Legacy callers without store_bank supply an already-committing per-slot
    callback instead.

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

import Tools.dependency_scanner as depscan
from Core.kronos_sysex import program_label
from Data.librarian_model import ObjLoc
from Data.librarian_sysex import OBJ_COMBI, OBJ_DRUM_KIT, OBJ_PROGRAM, OBJ_SET_LIST, OBJ_WAVE_SEQ
from Data.library_pull_pipeline import GetBankObjects, GetLiveDigest, PullResult
import Data.library_pull_pipeline as pull_pipeline
from Data.local_library_store import BlobStore, LocalLibraryIndex
from Data.pcg_file import WIRE_SIZE_EXI, WIRE_SIZE_HD1, body_family, program_body_kind, wire_body_is_exi
from Commands.session_dependency_clipboard import SessionDependencyClipboard

WriteToHardware = Callable[[int, int, int, bytes], bool]

# Program-only bank-type plumbing (see module docstring's "BankTypeChanges" bullet for the
# confirmed-from-source semantics of each). All three take a plain object-dump bank int, not a
# LocalLibraryIndex bank_key string — HD-1/EXi has no obj_type axis, matching
# librarian_model.py's pre-existing plan_batch_move bank_type_of convention.
GetLiveBankType = Callable[[int], Optional[bool]]          # bank -> True=EXi/False=HD-1/None=unverifiable
PendingBankTypeChange = Callable[[int], Optional[bool]]    # bank -> staged target is_exi, or None if untouched
WriteBankTypeChange = Callable[[int, bool], bool]          # (bank, to_exi) -> True on success (func 0x7C)


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
    # Keys held back because a pull could not read THEIR slot (index.incomplete_banks): NOT
    # conflicted (a timeout, not a front-panel change) and NOT pushed until the slot answers,
    # unless the caller passed allow_incomplete after an explicit confirm.
    incomplete: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    # Whole-bank HD-1<->EXi reformats (func 0x7C) to issue before this plan's Program entry
    # writes — (bank, to_exi) pairs, port of ChangesetModel.cs's BankTypeChanges. Distinct from
    # `entries`: this is its own operation type, executed via write_bank_type_change, not
    # write_to_hardware. See module docstring for when this gets populated (an explicitly staged
    # pending_bank_type_change) vs when a mismatch instead REFUSEs (get_live_bank_type).
    bank_type_changes: List[Tuple[int, bool]] = field(default_factory=list)

    @property
    def is_refusable(self) -> bool:
        return any(w.startswith("REFUSE:") for w in self.warnings)

    @property
    def banks(self) -> List[Tuple[int, int]]:
        return list(dict.fromkeys((e.obj_type, e.bank) for e in self.entries))


# Referenced objects are persisted before their referrers: Programs point at Drum Kits / Wave Sequences, Combis at
# Programs, Set Lists at Programs and Combis. A failed Store stops the sweep, so a Combi bank is never Stored
# while a Program bank it depends on was not.
_STORE_RANK = {OBJ_WAVE_SEQ: 0, OBJ_DRUM_KIT: 1, OBJ_PROGRAM: 2, OBJ_COMBI: 3, OBJ_SET_LIST: 4}


def store_order(banks: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """`banks` in dependency order (stable within a type)."""
    return sorted(banks, key=lambda b: _STORE_RANK.get(b[0], 5))


def _add_bank_type_change(plan: ChangesetPlan, bank: int, is_exi: bool) -> None:
    """Port of ChangesetModel.cs's ChangesetPlan.AddBankTypeChange — idempotent per bank (a
    second call for the same bank, even with a different target, is a no-op; matches the C#
    source's own dedup-by-bank-only behavior)."""
    if not any(b == bank for b, _ in plan.bank_type_changes):
        plan.bank_type_changes.append((bank, is_exi))


@dataclass(frozen=True)
class SyncResult:
    written: int = 0     # dirty edits successfully pushed (excludes erasures)
    erased: int = 0       # pending-delete entries successfully blanked on hardware
    deleted: int = 0      # local-only deletes removed (never existed on hardware)
    failed: int = 0       # entries where write_to_hardware returned False
    reformatted: int = 0  # whole-bank func-0x7C type changes successfully applied
    #: Production cancellation is honored before mutation. Legacy callbacks that
    #: commit each slot themselves may stop between slots; their committed entries
    #: remain recorded. Never cancel an in-flight reformat/write/Store transaction.
    cancelled: bool = False
    completed_bank_type_changes: Tuple[int, ...] = ()
    committed_banks: Tuple[Tuple[int, int], ...] = ()


RecordSuccess = Callable[[ChangesetEntry], None]


# ── Build (mostly pure — get_live_digest/resolver are the only I/O) ─────────


def build_changeset(index: LocalLibraryIndex, blobs: BlobStore,
                     clipboard: SessionDependencyClipboard,
                     get_live_digest: GetLiveDigest,
                     resolver: depscan.Resolver,
                     get_live_bank_type: Optional[GetLiveBankType] = None,
                     pending_bank_type_change: Optional[PendingBankTypeChange] = None,
                     force_destructive_write: bool = False,
                     allow_dangling: bool = False,
                     device_family: Optional[str] = None,
                     allow_incomplete: bool = False,
                     ) -> ChangesetPlan:
    """allow_incomplete: the user explicitly confirmed writing into slots a pull could not read
    (index.incomplete_banks). Without it - and force_destructive_write does NOT imply it - a
    pending change targeting such a slot goes to plan.incomplete with a CHECK: it is neither
    flagged Conflicted (a timeout, not a front-panel change) nor pushed. Changes to the bank's
    other slots are planned normally.

    device_family ("kronos"/"nautilus"): a Program or Combi in the other family's format is refused, never
    written; a Kronos body sent to a Nautilus is the right size for some banks but lays its parameters out differently.

    allow_dangling: the user explicitly accepted leaving references unresolved ("Continue
    anyway" / "Overwrite anyway"), so steps 1 and 3 report them as CHECKs instead of refusing.

    force_destructive_write (Settings > Librarian): the Local Library is the source of
    truth, so step 2's conflict pre-scan is skipped outright rather than run and ignored —
    its digest round trip exists only to decide what to exclude, and flagging a conflict
    here would mark objects this very push is about to write. The gates that survive it
    (steps 3 / 3.5 and the per-write staleness check in the caller's write_to_hardware)
    guard against writes the Kronos would MANGLE, a different question from who wins a
    disagreement."""
    plan = ChangesetPlan()

    # Step 1: dependency-completeness gate.
    if clipboard.has_unresolved():
        if not allow_dangling:
            plan.warnings.append(
                f"REFUSE: {len(clipboard.pending)} dependency(ies) still pending "
                "in the session clipboard — place them before pushing")
            return plan
        plan.warnings.append(
            f"CHECK: {len(clipboard.pending)} dependency(ies) still pending in the session "
            "clipboard — their references are left dangling")

    # Delete supersedes edit: pull pending-delete entries out of the dirty set
    # up front so an erase is never ALSO queued as a normal edit write.
    pending_delete_keys = [k for k, e in index.entries.items() if e.pending_delete]
    dirty_keys = [k for k, e in index.entries.items() if e.is_dirty and not e.pending_delete]

    # An entry the pull just flagged Conflicted must not be written: the pull advances the
    # bank digest baseline, so the pre-scan below would otherwise see the bank as unchanged
    # and overwrite the front-panel change. "Resolve Conflicts" clears the flag.
    if not force_destructive_write:
        def _split_conflicted(keys: List[str]) -> List[str]:
            keep: List[str] = []
            for k in keys:
                if index.entries[k].conflicted:
                    plan.conflicted.append(k)
                else:
                    keep.append(k)
            return keep
        dirty_keys = _split_conflicted(dirty_keys)
        pending_delete_keys = _split_conflicted(pending_delete_keys)

    # Unread slots (index.incomplete_banks): a pull asked for these slots and they did not answer,
    # so their live content has never been read - an edit there cannot be checked against it, and
    # (being unreadable) the slot cannot be backed up or verified after the write either. Held back
    # PER SLOT, before the pre-scan (no digest request, no flag change) and regardless of
    # force_destructive_write. Every other slot of the same bank goes through the normal pre-scan:
    # the pull advanced the bank's baseline to what the answered slots reflect.
    if not allow_incomplete and index.incomplete_banks:
        def _split_incomplete(keys: List[str]) -> List[str]:
            keep: List[str] = []
            for k in keys:
                if index.is_slot_unread(*_parse_key(k)):
                    plan.incomplete.append(k)
                else:
                    keep.append(k)
            return keep
        dirty_keys = _split_incomplete(dirty_keys)
        pending_delete_keys = _split_incomplete(pending_delete_keys)
        if plan.incomplete:
            unreadable = [k for k in plan.incomplete
                          if index.incomplete_banks.get(LocalLibraryIndex.bank_key(*_parse_key(k)[:2]), {})
                          .get(_parse_key(k)[2], 0) >= LocalLibraryIndex.UNREADABLE_AFTER]
            retry = [k for k in plan.incomplete if k not in unreadable]
            if retry:
                plan.warnings.append(
                    f"CHECK: {len(retry)} pending change(s) NOT pushed - unread slot, re-sync first: "
                    f"{pull_pipeline.unread_slots_text(index.incomplete_banks, retry)} did not answer the "
                    "last pull, so this library has not read what is there. Run Sync Library again; they "
                    "are pushed once the slot answers.")
            if unreadable:
                plan.warnings.append(
                    f"CHECK: {len(unreadable)} pending change(s) NOT pushed - unreadable slot: "
                    f"{pull_pipeline.unread_slots_text(index.incomplete_banks, unreadable)} never answered, so "
                    "it cannot be backed up or verified. Use Re-check unreadable slots; once it answers the "
                    "change is pushed.")

    if not dirty_keys and not pending_delete_keys:
        if plan.incomplete:
            return plan
        if plan.conflicted:
            plan.warnings.append("CHECK: only conflicted entries are pending — resolve the conflicts first")
            return plan
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
    if not force_destructive_write:
        for bank_key in touched_bank_keys:
            fresh_hex = get_live_digest(bank_key) or ""
            baseline_hex = index.bank_digest_baseline.get(bank_key)
            changed = baseline_hex is None or fresh_hex != baseline_hex
            if changed:
                excluded_banks.add(bank_key)
    else:
        # A Conflicted flag left by an earlier pull/push would outlive this push forever —
        # nothing downstream clears it once the pre-scan no longer runs, and the object it
        # marks is about to be written. Clear it so the conflict banner stays honest.
        for k in dirty_keys + pending_delete_keys:
            obj_type, bank, number = _parse_key(k)
            index.mark_conflicted(obj_type, bank, number, False)

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
    being_erased = {_parse_key(k) for k in surviving_deletes}

    def push_resolver(obj_type: int, bank: int, number: int) -> bool:
        return (obj_type, bank, number) not in being_erased and resolver(obj_type, bank, number)

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
        for missing in depscan.scan(push_resolver, obj_type, body):
            r = missing.ref
            # Accepting dangling references never covers a target this same push erases.
            waived = allow_dangling and (r.obj_type, r.bank, r.number) not in being_erased
            plan.warnings.append(
                f"{'CHECK' if waived else 'REFUSE'}: {loc.label()} references "
                f"{missing.ref.label()} ({missing.ref_kind}), which does not exist locally")

    # Step 3.4: instrument-family gate over surviving edits.
    if device_family is not None:
        for k in surviving:
            obj_type, bank, number = _parse_key(k)
            body = blobs.get(index.entries[k].current_hash)
            fam = body_family(obj_type, len(body)) if body is not None else None
            if fam is not None and fam != device_family:
                plan.warnings.append(
                    f"REFUSE: {ObjLoc(obj_type, bank, number).label()} is in the {fam.title()} format, but the "
                    f"connected instrument is a {device_family.title()} - convert it first (open the PCG while "
                    "connected, or use Convert to Nautilus...)")

    # Step 3.5a: whole-bank type changes (requirement 4, port of ChangesetBuilder.cs's own
    # BankTypeChanges assembly) — a Program bank the caller has an INTENTIONAL, already-staged
    # HD-1/EXi conversion on record for (pending_bank_type_change), AND which this push is
    # actually writing to, gets a func 0x7C queued before its writes. Filtered to banks with
    # surviving writes so a bank we're NOT rewriting this push is never erased. Independent of
    # get_live_bank_type below — queuing this never depends on a verified mismatch, and it's
    # safe even when the bank is already that type (0x7C is then a no-op on the instrument).
    if pending_bank_type_change is not None:
        for k in surviving:
            obj_type, bank, number = _parse_key(k)
            if obj_type != OBJ_PROGRAM:
                continue
            target_is_exi = pending_bank_type_change(bank)
            if target_is_exi is not None:
                _add_bank_type_change(plan, bank, target_is_exi)
        staged_targets = dict(plan.bank_type_changes)
        for k in surviving + surviving_deletes:
            obj_type, bank, number = _parse_key(k)
            if obj_type == OBJ_PROGRAM and bank in staged_targets:
                body = blobs.get(index.entries[k].current_hash)
                if body is not None and program_body_kind(len(body)) is not staged_targets[bank]:
                    plan.warnings.append(
                        f"REFUSE: {ObjLoc(obj_type, bank, number).label()} does not match the "
                        "staged bank format — replace it with content in the target format before reformatting")

    # Step 3.5b: Program EXi/HD-1 bank-type re-verification — a fresh live query per distinct
    # surviving, NOT-already-staged Program bank, run right before any hardware write, to catch
    # the user having flipped a bank's type at the front panel since these edits were made
    # (port of ChangesetBuilder.cs's own Step 3.5; see module docstring for why this can't just
    # trust librarian_model.py's placement-time plan_batch_move check). NON-OBVIOUS INVARIANT,
    # confirmed from source: a mismatch here is NEVER auto-resolved into a reformat — it's an
    # unconditional REFUSE of the whole bank (one REFUSE per bank, not per Program). The only way
    # to make a genuinely-reformatted-content push succeed is staging pending_bank_type_change
    # above (a deliberate, separate user action), which is why staged banks are skipped here —
    # the queued 0x7C will make the format match before these writes land, so comparing against
    # the CURRENT (pre-reformat) live type would always look like a mismatch.
    if get_live_bank_type is not None:
        staged_banks = {b for b, _ in plan.bank_type_changes}
        checked_banks = set()
        for k in surviving:
            obj_type, bank, number = _parse_key(k)
            if obj_type != OBJ_PROGRAM or bank in staged_banks or bank in checked_banks:
                continue
            checked_banks.add(bank)
            live_is_exi = get_live_bank_type(bank)
            if live_is_exi is None:
                continue   # unverifiable (query failed / bank not covered) — non-blocking, matches C#
            mismatch = False
            for k2 in surviving:
                obj_type2, bank2, number2 = _parse_key(k2)
                if obj_type2 != OBJ_PROGRAM or bank2 != bank:
                    continue
                entry2 = index.entries[k2]
                body2 = blobs.get(entry2.current_hash)
                if body2 is not None and wire_body_is_exi(len(body2)) != live_is_exi:
                    mismatch = True
                    break
            if mismatch:
                plan.warnings.append(
                    f"REFUSE: {program_label(bank)} is currently formatted as "
                    f"{'EXi' if live_is_exi else 'HD-1'} on the instrument, but this push has at "
                    "least one edit in the other format — stage an explicit bank-type conversion "
                    "first, or revert the edit to match")

    if plan.is_refusable:
        return plan

    # Step 4: assemble the plan.
    #
    # These are the only two blob reads whose bytes reach the instrument, so
    # they go through get_verified: a blob that no longer hashes to the key it
    # is stored under is damaged, and writing it would corrupt the slot. A
    # damaged blob REFUSEs the whole plan rather than being skipped quietly —
    # silently declining to push an edit the user explicitly asked for would
    # leave them believing it landed.
    def _body_for_hardware(entry, k: str) -> Optional[bytes]:
        body = blobs.get_verified(entry.current_hash)
        if body is None and blobs.exists(entry.current_hash):
            obj_type, bank, number = _parse_key(k)
            plan.warnings.append(
                f"REFUSE: {ObjLoc(obj_type, bank, number).label()}'s stored content is "
                f"damaged (blob {entry.current_hash[:8]} no longer matches its hash) — "
                "re-pull it from hardware or from the source PCG before pushing")
        return body

    for k in surviving:
        obj_type, bank, number = _parse_key(k)
        entry = index.entries[k]
        body = _body_for_hardware(entry, k)
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
        body = _body_for_hardware(entry, k)
        if body is None:
            continue
        plan.entries.append(ChangesetEntry(k, obj_type, bank, number, body,
                                            entry.version, is_erase=True))

    if plan.is_refusable:
        return plan

    if not plan.entries and not plan.local_only_deletes and (plan.conflicted or not plan.incomplete):
        plan.warnings.append("CHECK: every pending change conflicted or was rejected — nothing left to push")

    return plan


# ── Execute ──────────────────────────────────────────────────────────────────


def execute_changeset(plan: ChangesetPlan, index: LocalLibraryIndex,
                       write_to_hardware: WriteToHardware,
                       write_bank_type_change: Optional[WriteBankTypeChange] = None,
                       record_success: Optional[RecordSuccess] = None,
                       cancel: Optional[Callable[[], bool]] = None,
                       prepare_hardware: Optional[Callable[[ChangesetPlan], bool]] = None,
                       store_bank: Optional[Callable[[int, int], bool]] = None) -> SyncResult:
    """Back up/check, reformat, write the full burst, then Store each bank once.

    Production callbacks split volatile writes from persistence. Never publish
    a successful local baseline for volatile data or a rejected Store. Once
    mutation begins, cancellation cannot leave an erased bank unfilled.
    Without store_bank, legacy callbacks commit individual objects themselves.
    """
    if plan.is_refusable:
        return SyncResult()
    if cancel is not None and cancel():
        return SyncResult(cancelled=True)
    if any((OBJ_PROGRAM, bank) not in plan.banks for bank, _ in plan.bank_type_changes):
        plan.warnings.append("REFUSE: bank reformat has no refill writes.")
        return SyncResult(failed=len(plan.entries))
    if plan.entries and prepare_hardware is not None and not prepare_hardware(plan):
        return SyncResult(failed=len(plan.entries),
                          cancelled=cancel is not None and cancel())
    if cancel is not None and cancel():
        return SyncResult(cancelled=True)

    reformatted = 0
    if plan.bank_type_changes:
        if write_bank_type_change is None:
            # A plan that needs a reformat but was given no way to perform one is a caller
            # wiring bug — refuse to touch hardware at all rather than silently write
            # mismatched-format bodies into a bank that was never actually reformatted.
            return SyncResult(failed=len(plan.entries))
        for bank, to_exi in plan.bank_type_changes:
            if not write_bank_type_change(bank, to_exi):
                plan.warnings.append("CHECK: bank reformat failed; restore pre-image backups before relying on the bank.")
                return SyncResult(failed=len(plan.entries), reformatted=reformatted)
            reformatted += 1

    transactional = store_bank is not None or bool(plan.bank_type_changes)
    written = 0
    erased = 0
    failed = 0
    cancelled = False
    pending: List[ChangesetEntry] = []

    def record(entry: ChangesetEntry) -> None:
        nonlocal written, erased
        if entry.is_erase:
            erased += 1
        else:
            written += 1
        if record_success is not None:
            record_success(entry)
        else:
            idx_entry = index.get(entry.obj_type, entry.bank, entry.number)
            if idx_entry is not None:
                idx_entry.baseline_hash = BlobStore.compute_hash(entry.body)
                idx_entry.conflicted = False
                if entry.is_erase:
                    idx_entry.pending_delete = False

    for entry in (plan.entries if not transactional
                  else sorted(plan.entries, key=lambda e: _STORE_RANK.get(e.obj_type, 5))):
        if not transactional and cancel is not None and cancel():
            cancelled = True
            break
        ok = write_to_hardware(entry.obj_type, entry.bank, entry.number, entry.body)
        if not ok:
            failed += 1
            if transactional:
                plan.warnings.append(
                    "CHECK: object write failed; no banks were Stored. Volatile bank data may be partial — "
                    "restore pre-image backups before a panel Store or retry.")
                return SyncResult(failed=len(plan.entries), reformatted=reformatted)
            continue
        if transactional:
            pending.append(entry)
        else:
            record(entry)

    committed_banks = set()
    if transactional:
        for obj_type, bank in store_order(plan.banks):
            if store_bank is not None and not store_bank(obj_type, bank):
                failed = len(pending) - written - erased
                plan.warnings.append(
                    f"CHECK: Store failed for bank {obj_type}:{bank}; uncommitted edits remain pending. "
                    "Restore pre-image backups before relying on the affected banks.")
                break
            committed_banks.add((obj_type, bank))
            for entry in pending:
                if (entry.obj_type, entry.bank) == (obj_type, bank):
                    record(entry)

    deleted = 0
    if not cancelled and not failed:
        # Local-only deletes touch no hardware, but they are still part of "this
        # plan was applied". Skipping them on cancel keeps the plan replayable in
        # full by the next Commit instead of half-consumed.
        for k in plan.local_only_deletes:
            obj_type, bank, number = _parse_key(k)
            index.delete(obj_type, bank, number)
            deleted += 1

    return SyncResult(written=written, erased=erased, deleted=deleted, failed=failed,
                       reformatted=reformatted, cancelled=cancelled,
                       completed_bank_type_changes=tuple(
                           bank for bank, _ in plan.bank_type_changes
                           if (OBJ_PROGRAM, bank) in committed_banks),
                       committed_banks=tuple(b for b in plan.banks if b in committed_banks))


# ── Entry points ─────────────────────────────────────────────────────────────


def commit_changes(index: LocalLibraryIndex, blobs: BlobStore,
                    clipboard: SessionDependencyClipboard,
                    get_live_digest: GetLiveDigest, resolver: depscan.Resolver,
                    write_to_hardware: WriteToHardware,
                    record_success: Optional[RecordSuccess] = None,
                    get_live_bank_type: Optional[GetLiveBankType] = None,
                    pending_bank_type_change: Optional[PendingBankTypeChange] = None,
                    write_bank_type_change: Optional[WriteBankTypeChange] = None,
                    cancel: Optional[Callable[[], bool]] = None,
                    force_destructive_write: bool = False,
                    allow_dangling: bool = False,
                    prepare_hardware: Optional[Callable[[ChangesetPlan], bool]] = None,
                    store_bank: Optional[Callable[[int, int], bool]] = None,
                    device_family: Optional[str] = None,
                    allow_incomplete: bool = False,
                    ) -> Tuple[ChangesetPlan, SyncResult]:
    """Push-only — port of SyncPipeline.CommitChangesAsync. Deliberately
    skips pulling; pushes straight against whatever bank-digest baseline is
    already on record. `cancel` is forwarded to execute_changeset (see there)."""
    plan = build_changeset(index, blobs, clipboard, get_live_digest, resolver,
                            get_live_bank_type, pending_bank_type_change,
                            force_destructive_write, allow_dangling, device_family,
                            allow_incomplete=allow_incomplete)
    result = execute_changeset(plan, index, write_to_hardware, write_bank_type_change,
                                record_success, cancel=cancel,
                                prepare_hardware=prepare_hardware, store_bank=store_bank)
    return plan, result


def sync_library(index: LocalLibraryIndex, blobs: BlobStore,
                  clipboard: SessionDependencyClipboard,
                  get_live_digest: GetLiveDigest, get_bank_objects: GetBankObjects,
                  resolver: depscan.Resolver, write_to_hardware: WriteToHardware,
                  record_success: Optional[RecordSuccess] = None,
                  full: bool = False, progress=None,
                  get_live_bank_type: Optional[GetLiveBankType] = None,
                  pending_bank_type_change: Optional[PendingBankTypeChange] = None,
                  write_bank_type_change: Optional[WriteBankTypeChange] = None,
                  cancel: Optional[Callable[[], bool]] = None,
                  force_destructive_write: bool = False,
                  allow_dangling: bool = False,
                  prepare_hardware: Optional[Callable[[ChangesetPlan], bool]] = None,
                  store_bank: Optional[Callable[[int, int], bool]] = None,
                  device_family: Optional[str] = None,
                  allow_incomplete: bool = False,
                  ) -> Tuple[PullResult, ChangesetPlan, SyncResult]:
    """Pull, then push — port of SyncPipeline.SyncLibraryAsync. See module
    docstring's pull-vs-commit invariant note for why the ordering is pull
    THEN push, not the reverse.

    A cancelled pull skips the push entirely: the push half decides what to
    write from the baselines the pull half just established, so pushing against
    a half-refreshed picture of the instrument is exactly the state this
    pipeline's ordering exists to avoid."""
    pull_result = pull_pipeline.pull(index, blobs, get_live_digest, get_bank_objects,
                                     full=full, progress=progress, cancel=cancel)
    if pull_result.cancelled:
        return pull_result, ChangesetPlan(), SyncResult(cancelled=True)
    if pull_result.aborted:
        return pull_result, ChangesetPlan(warnings=[pull_result.aborted]), SyncResult()
    plan, result = commit_changes(index, blobs, clipboard, get_live_digest, resolver,
                                  write_to_hardware, record_success,
                                  get_live_bank_type, pending_bank_type_change,
                                  write_bank_type_change, cancel=cancel,
                                  force_destructive_write=force_destructive_write,
                                  allow_dangling=allow_dangling,
                                  prepare_hardware=prepare_hardware, store_bank=store_bank,
                                  device_family=device_family, allow_incomplete=allow_incomplete)
    return pull_result, plan, result


# ── Self-test (python changeset_sync.py) ────────────────────────────────────


def _selftest() -> None:
    import pathlib
    import shutil
    import sys
    import tempfile
    from datetime import datetime, timezone

    from Data.local_library_store import LocalIndexEntry
    from Data.librarian_sysex import func33_to_obj_bank, set_combi_timbre_ref

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

        from Commands.session_dependency_clipboard import SessionDependencyEntry
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

        # ── (3b) force_destructive_write: same stale bank is NOT excluded, no digest is
        #    queried, and a leftover Conflicted flag is cleared ──
        idx3b = LocalLibraryIndex(root / "s3b")
        idx3b.set_entry(OBJ_PROGRAM, 0x00, 5, LocalIndexEntry(
            version=1, baseline_hash=old_hash, current_hash=new_hash,
            display_name="P5", created_utc=now(), modified_utc=now(), conflicted=True))
        idx3b.set_bank_digest_baseline(OBJ_PROGRAM, 0x00, "bankdigest-v1")
        asked: List[str] = []
        plan3b = build_changeset(idx3b, blobs, clip_empty,
                                  get_live_digest=lambda bk: (asked.append(bk) or "bankdigest-v2-changed"),
                                  resolver=lambda t, b, n: True, force_destructive_write=True)
        check("force-writes-stale-bank", len(plan3b.entries) == 1 and plan3b.conflicted == [])
        check("force-skips-digest-prescan", asked == [])
        check("force-clears-conflict-flag", idx3b.get(OBJ_PROGRAM, 0x00, 5).conflicted is False)
        check("force-not-refusable", not plan3b.is_refusable)

        # force does NOT bypass the dependency gate
        plan3c = build_changeset(idx1, blobs, clip_blocked, get_live_digest=lambda bk: "x",
                                  resolver=lambda t, b, n: True, force_destructive_write=True)
        check("force-still-refuses-pending-deps", plan3c.is_refusable and plan3c.entries == [])

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

        # explicit acceptance ("Continue anyway" / "Overwrite anyway"): same plan, but it writes
        plan4b = build_changeset(idx4, blobs, clip_empty,
                                  get_live_digest=lambda bk: "combidigest-v1",
                                  resolver=lambda t, b, n: (t, b, n) in present, allow_dangling=True)
        check("allow-dangling-missing-ref-writes", not plan4b.is_refusable and len(plan4b.entries) == 1)
        check("allow-dangling-missing-ref-is-check",
              any(w.startswith("CHECK:") and "does not exist locally" in w for w in plan4b.warnings))
        # a Kronos-format Combi is refused for a Nautilus (and the reverse), never written
        plan4c = build_changeset(idx4, blobs, clip_empty, get_live_digest=lambda bk: "combidigest-v1",
                                  resolver=lambda t, b, n: True, device_family="nautilus")
        check("wrong-family-refused", plan4c.is_refusable
              and any("Kronos format" in w and "Nautilus" in w for w in plan4c.warnings))
        plan4d = build_changeset(idx4, blobs, clip_empty, get_live_digest=lambda bk: "combidigest-v1",
                                  resolver=lambda t, b, n: True, device_family="kronos")
        check("right-family-writes", not plan4d.is_refusable and len(plan4d.entries) == 1)
        plan4e = build_changeset(idx4, blobs, clip_empty, get_live_digest=lambda bk: "combidigest-v1",
                                  resolver=lambda t, b, n: True)
        check("no-family-no-gate", not plan4e.is_refusable)
        plan3d = build_changeset(idx1, blobs, clip_blocked, get_live_digest=lambda bk: "x",
                                  resolver=lambda t, b, n: True, allow_dangling=True)
        check("allow-dangling-pending-deps-not-refused", not plan3d.is_refusable)

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

        # Legacy callbacks already commit each slot independently. Production
        # volatile callbacks instead withhold every Store on a rejected write.
        result6 = execute_changeset(plan6, idx6, write_fails_for_2)
        check("mixed-one-written-one-failed", result6.written == 1 and result6.failed == 1)
        good_after = idx6.get(OBJ_PROGRAM, 0x00, 1)
        bad_after = idx6.get(OBJ_PROGRAM, 0x00, 2)
        check("mixed-good-baseline-advanced", good_after is not None and good_after.baseline_hash == good_new)
        check("mixed-bad-baseline-not-advanced",
              bad_after is not None and bad_after.baseline_hash == bad_old and bad_after.is_dirty)

        # ── (7) is_exi matches live bank type -> builds/executes normally, no reformat ──
        idx7bt = LocalLibraryIndex(root / "s7bt")
        exi_hash = blobs.put(bytes(WIRE_SIZE_EXI))   # 4960 zero bytes -- EXi-shaped
        idx7bt.set_entry(OBJ_PROGRAM, 0x02, 0, LocalIndexEntry(
            version=1, baseline_hash=old_hash, current_hash=exi_hash,
            display_name="P-I-C-0", created_utc=now(), modified_utc=now()))
        idx7bt.set_bank_digest_baseline(OBJ_PROGRAM, 0x02, "bankdigest-ic-v1")

        plan7bt = build_changeset(idx7bt, blobs, clip_empty,
                                   get_live_digest=lambda bk: "bankdigest-ic-v1",
                                   resolver=lambda t, b, n: True,
                                   get_live_bank_type=lambda bank: True if bank == 0x02 else None,
                                   pending_bank_type_change=lambda bank: None)
        check("banktype-match-not-refusable", not plan7bt.is_refusable)
        check("banktype-match-no-reformat", plan7bt.bank_type_changes == [])
        check("banktype-match-one-entry", len(plan7bt.entries) == 1)

        result7bt = execute_changeset(plan7bt, idx7bt, lambda *a: True)
        check("banktype-match-written", result7bt.written == 1 and result7bt.reformatted == 0)

        # ── (8) is_exi mismatches live bank type, nothing staged -> whole bank REFUSEd ──
        idx8bt = LocalLibraryIndex(root / "s8bt")
        idx8bt.set_entry(OBJ_PROGRAM, 0x03, 0, LocalIndexEntry(
            version=1, baseline_hash=old_hash, current_hash=exi_hash,   # EXi-shaped body...
            display_name="P-I-D-0", created_utc=now(), modified_utc=now()))
        idx8bt.set_bank_digest_baseline(OBJ_PROGRAM, 0x03, "bankdigest-id-v1")

        plan8bt = build_changeset(idx8bt, blobs, clip_empty,
                                   get_live_digest=lambda bk: "bankdigest-id-v1",
                                   resolver=lambda t, b, n: True,
                                   get_live_bank_type=lambda bank: False if bank == 0x03 else None,  # ...but bank is live HD-1
                                   pending_bank_type_change=lambda bank: None)
        check("banktype-mismatch-refuses", plan8bt.is_refusable)
        check("banktype-mismatch-no-entries", plan8bt.entries == [])
        check("banktype-mismatch-no-reformat-queued", plan8bt.bank_type_changes == [])
        check("banktype-mismatch-warning",
              any(w.startswith("REFUSE:") and "currently formatted as HD-1" in w for w in plan8bt.warnings))

        hw_log8bt: List = []
        result8bt = execute_changeset(plan8bt, idx8bt, lambda *a: (hw_log8bt.append(a) or True))
        check("banktype-mismatch-nothing-written", result8bt.written == 0 and hw_log8bt == [])

        # ── (9) mismatch WITH an intentional staged conversion -> reformat queued (not REFUSEd),
        #     issued before the entry write, using pcg_file's real HD-1 truncation for the body ──
        from Data.pcg_file import PCG_SLOT_SIZE, PcgObjectEntry, WIRE_SIZE_HD1, wire_body_from_pcg_entry

        pcg_raw = bytearray(PCG_SLOT_SIZE)
        pcg_raw[0:8] = b"HD1PROG9"
        pcg_entry = PcgObjectEntry(OBJ_PROGRAM, None, 0, bytes(pcg_raw), "HD1PROG9", is_exi=False)
        hd1_wire_body = wire_body_from_pcg_entry(OBJ_PROGRAM, pcg_entry)   # ground truth: pcg_file's own conversion
        check("banktype-hd1-wire-is-truncation",
              hd1_wire_body is not None and len(hd1_wire_body) == WIRE_SIZE_HD1
              and hd1_wire_body == bytes(pcg_raw[:WIRE_SIZE_HD1]))

        idx9bt = LocalLibraryIndex(root / "s9bt")
        hd1_hash = blobs.put(hd1_wire_body)
        idx9bt.set_entry(OBJ_PROGRAM, 0x04, 0, LocalIndexEntry(
            version=1, baseline_hash=old_hash, current_hash=hd1_hash,   # already converted to the target (HD-1) format
            display_name="P-I-E-0", created_utc=now(), modified_utc=now()))
        idx9bt.set_bank_digest_baseline(OBJ_PROGRAM, 0x04, "bankdigest-ie-v1")

        plan9bt = build_changeset(idx9bt, blobs, clip_empty,
                                   get_live_digest=lambda bk: "bankdigest-ie-v1",
                                   resolver=lambda t, b, n: True,
                                   get_live_bank_type=lambda bank: True if bank == 0x04 else None,   # still EXi live
                                   pending_bank_type_change=lambda bank: False if bank == 0x04 else None)  # staged -> HD-1
        check("banktype-staged-not-refusable", not plan9bt.is_refusable)
        check("banktype-staged-reformat-queued", plan9bt.bank_type_changes == [(0x04, False)])
        check("banktype-staged-one-entry",
              len(plan9bt.entries) == 1 and plan9bt.entries[0].body == hd1_wire_body)

        call_order: List[Tuple[str, tuple]] = []

        def write_bank_type_change9(bank, to_exi):
            call_order.append(("reformat", (bank, to_exi)))
            return True

        def write_to_hardware9(obj_type, bank, number, body):
            call_order.append(("write", (obj_type, bank, number, body)))
            return True

        result9bt = execute_changeset(plan9bt, idx9bt, write_to_hardware9, write_bank_type_change9)
        check("banktype-staged-reformatted-count", result9bt.reformatted == 1 and result9bt.written == 1)
        check("banktype-staged-reformat-before-write",
              len(call_order) == 2 and call_order[0][0] == "reformat" and call_order[1][0] == "write")
        check("banktype-staged-reformat-args", call_order[0][1] == (0x04, False))
        check("banktype-staged-write-body-matches-pcg-conversion", call_order[1][1][3] == hd1_wire_body)

        # ── (9b) a rejected reformat aborts the WHOLE call -- no entry write is even attempted ──
        idx9b = LocalLibraryIndex(root / "s9b")
        idx9b.set_entry(OBJ_PROGRAM, 0x05, 0, LocalIndexEntry(
            version=1, baseline_hash=old_hash, current_hash=hd1_hash,
            display_name="P-I-F-0", created_utc=now(), modified_utc=now()))
        idx9b.set_bank_digest_baseline(OBJ_PROGRAM, 0x05, "bankdigest-if-v1")
        plan9b = build_changeset(idx9b, blobs, clip_empty,
                                  get_live_digest=lambda bk: "bankdigest-if-v1",
                                  resolver=lambda t, b, n: True,
                                  pending_bank_type_change=lambda bank: False if bank == 0x05 else None)
        check("banktype-abort-setup-queued", plan9b.bank_type_changes == [(0x05, False)])

        hw_log9b: List = []
        result9b = execute_changeset(plan9b, idx9b, lambda *a: (hw_log9b.append(a) or True),
                                      lambda bank, to_exi: False)   # reformat rejected
        check("banktype-abort-no-entry-write", hw_log9b == [])
        check("banktype-abort-failed-count", result9b.written == 0 and result9b.failed == 1)

        # Production callbacks: whole burst, one Store, baseline only after Store.
        def transaction_index():
            idx = LocalLibraryIndex(root / "transaction")
            for n, old, new in ((1, good_old, good_new), (2, bad_old, bad_new)):
                idx.set_entry(OBJ_PROGRAM, 0, n, LocalIndexEntry(
                    version=1, baseline_hash=old, current_hash=new))
            return idx

        tx = transaction_index()
        tx_events = []
        tx_cancelled = [False]

        def tx_write(obj_type, bank, number, body):
            tx_events.append(("write", number))
            tx_cancelled[0] = True
            return True

        def tx_store(obj_type, bank):
            check("transaction-baseline-waits-for-store", all(e.is_dirty for e in tx.entries.values()))
            tx_events.append(("store", bank))
            return True

        tx_result = execute_changeset(
            plan6, tx, tx_write, cancel=lambda: tx_cancelled[0], store_bank=tx_store)
        check("transaction-cancel-during-burst-finishes",
              tx_result.written == 2 and not tx_result.cancelled)
        check("transaction-one-store-after-all-writes",
              tx_events == [("write", 1), ("write", 2), ("store", 0)])
        check("transaction-records-committed-bank", tx_result.committed_banks == ((OBJ_PROGRAM, 0),))
        check("transaction-baselines-advance-after-store",
              all(not e.is_dirty for e in tx.entries.values()))

        tx = transaction_index()
        tx_events.clear()
        tx_result = execute_changeset(
            plan6, tx, lambda *args: tx_events.append("write") or True,
            cancel=lambda: True, store_bank=lambda *args: tx_events.append("store") or True)
        check("transaction-cancel-before-mutation-does-nothing",
              tx_result.cancelled and tx_events == [])

        tx = transaction_index()
        tx_events.clear()
        tx_result = execute_changeset(
            plan6, tx, write_fails_for_2,
            store_bank=lambda *args: tx_events.append("store") or True)
        check("transaction-failed-write-never-stores",
              tx_result.written == 0 and tx_result.failed == 2 and tx_events == [])
        check("transaction-failed-write-leaves-all-baselines",
              all(e.is_dirty for e in tx.entries.values()))

        tx = transaction_index()
        tx_result = execute_changeset(plan6, tx, lambda *args: True, store_bank=lambda *args: False)
        check("transaction-failed-store-not-recorded",
              tx_result.written == 0 and tx_result.failed == 2
              and all(e.is_dirty for e in tx.entries.values()))

        conversion_events = []
        conversion_cancelled = [False]

        def convert_then_cancel(bank, to_exi):
            conversion_events.append("reformat")
            conversion_cancelled[0] = True
            return True

        conversion_result = execute_changeset(
            plan9bt, idx9bt,
            lambda *args: conversion_events.append("write") or True,
            convert_then_cancel, cancel=lambda: conversion_cancelled[0],
            store_bank=lambda *args: conversion_events.append("store") or True)
        check("transaction-conversion-cancel-cannot-abandon-refill",
              conversion_events == ["reformat", "write", "store"]
              and conversion_result.written == 1 and not conversion_result.cancelled
              and conversion_result.completed_bank_type_changes == (0x04,))

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

        # ── (10) a pull timeout in a bank with a dirty entry: NOT conflicted, NOT pushed, a
        #    distinct "incomplete bank" CHECK; a later sync that reads every slot clears it and
        #    pushes. The live digest differs from the old baseline - exactly what used to turn
        #    the timeout into "N conflicted". ──
        idx10 = LocalLibraryIndex(root / "s10")
        idx10.set_entry(OBJ_PROGRAM, 0x00, 6, LocalIndexEntry(
            version=1, baseline_hash=old_hash, current_hash=new_hash,
            display_name="P6", created_utc=now(), modified_utc=now()))
        # an edit to ANOTHER slot of the same bank, which answers: a dead slot must not block it
        old7, new7 = blobs.put(b"OLD-7"), blobs.put(b"NEW-7")
        idx10.set_entry(OBJ_PROGRAM, 0x00, 7, LocalIndexEntry(
            version=1, baseline_hash=old7, current_hash=new7,
            display_name="P7", created_utc=now(), modified_utc=now()))
        idx10.set_bank_digest_baseline(OBJ_PROGRAM, 0x00, "bankdigest-old")
        timeout10 = {"on": True}
        writes10: List[Tuple[int, int, int]] = []

        def digest10(bk):
            return "bankdigest-new" if bk == LocalLibraryIndex.bank_key(OBJ_PROGRAM, 0x00) else f"d-{bk}"

        def objects10(obj_type, bank, slots=None):
            if (obj_type, bank) != (OBJ_PROGRAM, 0x00):
                return {}
            full_bank = {0: (1, b"OTHER-SLOT"), 6: (1, b"OLD-PROGRAM-BODY"), 7: (1, b"OLD-7")}
            want = set(full_bank) | {9} if slots is None else set(slots)
            dead = {6, 9} if timeout10["on"] else set()
            return pull_pipeline.BankObjects({n: v for n, v in full_bank.items() if n in want and n not in dead},
                                             missing=sorted(want & dead))

        def write10(t, b, n, body):
            writes10.append((t, b, n))
            return True

        pull10, plan10, res10 = sync_library(idx10, blobs, clip_empty, get_live_digest=digest10,
                                             get_bank_objects=objects10, resolver=lambda t, b, n: True,
                                             write_to_hardware=write10)
        k10 = LocalLibraryIndex.key(OBJ_PROGRAM, 0x00, 6)
        check("incomplete-not-conflicted", plan10.conflicted == [] and not idx10.get(OBJ_PROGRAM, 0x00, 6).conflicted)
        check("incomplete-held-back", plan10.incomplete == [k10] and writes10 == [(OBJ_PROGRAM, 0x00, 7)]
              and res10.written == 1)
        check("dead-slot-does-not-block-bank", not idx10.get(OBJ_PROGRAM, 0x00, 7).is_dirty
              and idx10.bank_digest_baseline.get(LocalLibraryIndex.bank_key(OBJ_PROGRAM, 0x00)) == "bankdigest-new")
        check("incomplete-warning", any(w.startswith("CHECK:") and "unread slot" in w and "slot 6" in w
                                        for w in plan10.warnings) and not plan10.is_refusable)
        check("incomplete-recorded", idx10.incomplete_slots(LocalLibraryIndex.bank_key(OBJ_PROGRAM, 0x00)) == [6, 9])
        writes10.clear()

        # force_destructive_write alone does not write into the incomplete bank...
        plan10f, res10f = commit_changes(idx10, blobs, clip_empty, get_live_digest=digest10,
                                         resolver=lambda t, b, n: True, write_to_hardware=write10,
                                         force_destructive_write=True)
        check("incomplete-force-still-held", plan10f.incomplete == [k10] and writes10 == [])
        # ...only an explicit allow_incomplete (the UI's confirm naming the unread slots) does.
        idx10b = LocalLibraryIndex(root / "s10b")
        idx10b.entries = {k: LocalIndexEntry.from_dict(e.to_dict()) for k, e in idx10.entries.items()}
        idx10b.incomplete_banks = dict(idx10.incomplete_banks)
        plan10a, res10a = commit_changes(idx10b, blobs, clip_empty, get_live_digest=digest10,
                                         resolver=lambda t, b, n: True, write_to_hardware=write10,
                                         force_destructive_write=True, allow_incomplete=True)
        check("incomplete-allowed-pushes", plan10a.incomplete == [] and res10a.written == 1
              and writes10 == [(OBJ_PROGRAM, 0x00, 6)])
        writes10.clear()

        # Every slot answers now: the record clears and the edit goes out.
        timeout10["on"] = False
        pull10c, plan10c, res10c = sync_library(idx10, blobs, clip_empty, get_live_digest=digest10,
                                                get_bank_objects=objects10, resolver=lambda t, b, n: True,
                                                write_to_hardware=write10)
        check("incomplete-cleared-by-complete-pull", idx10.incomplete_banks == {}
              and not pull10c.incomplete_banks)
        check("incomplete-then-pushed", plan10c.incomplete == [] and plan10c.conflicted == []
              and res10c.written == 1 and writes10 == [(OBJ_PROGRAM, 0x00, 6)])

        # ── (11) an unreadable slot (3 strikes): its edit and a pending delete on it stay held with an
        #    "unreadable" CHECK; a pending delete on a readable slot of the same bank goes out ──
        idx11 = LocalLibraryIndex(root / "s11")
        for n in (4, 5):
            idx11.set_entry(OBJ_PROGRAM, 0x00, n, LocalIndexEntry(
                version=1, baseline_hash=old_hash, current_hash=new_hash,
                display_name=f"P{n}", created_utc=now(), modified_utc=now(), pending_delete=(n == 5)))
        idx11.set_entry(OBJ_PROGRAM, 0x00, 8, LocalIndexEntry(
            version=1, baseline_hash=old_hash, current_hash=new_hash,
            display_name="P8", created_utc=now(), modified_utc=now(), pending_delete=True))
        idx11.set_bank_digest_baseline(OBJ_PROGRAM, 0x00, "d11")
        idx11.incomplete_banks = {LocalLibraryIndex.bank_key(OBJ_PROGRAM, 0x00): {4: 3, 5: 3}}
        writes11: List[Tuple[int, int, int]] = []
        plan11, res11 = commit_changes(idx11, blobs, clip_empty, get_live_digest=lambda bk: "d11",
                                       resolver=lambda t, b, n: True,
                                       write_to_hardware=lambda t, b, n, body: writes11.append((t, b, n)) or True)
        check("unreadable-held", sorted(plan11.incomplete) == [LocalLibraryIndex.key(OBJ_PROGRAM, 0x00, 4),
                                                              LocalLibraryIndex.key(OBJ_PROGRAM, 0x00, 5)])
        check("unreadable-warning", any("unreadable slot" in w and "slot 4 (unreadable)" in w for w in plan11.warnings))
        check("unreadable-other-slot-erased", writes11 == [(OBJ_PROGRAM, 0x00, 8)] and res11.erased == 1)

    finally:
        if root.exists():
            shutil.rmtree(root)

    if fails:
        print("FAIL:", ", ".join(fails))
        sys.exit(1)
    print("changeset_sync self-test: OK")


if __name__ == "__main__":
    _selftest()
