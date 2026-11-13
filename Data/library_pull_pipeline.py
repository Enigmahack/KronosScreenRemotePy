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
                    May return a BankObjects (dict subclass) whose `missing`
                    lists slots that did not answer (and `skipped`, slots a
                    cancel never asked); those are kept per slot in
                    index.incomplete_banks and re-fetched / held back on
                    their own while the bank's baseline advances. May also
                    accept an optional `slots=` keyword (a list of slot
                    numbers): pull() then re-asks only a bank's unread slots
                    (detected with accepts_slots; a callable without it gets
                    the whole bank). Real wiring: sysex_dump_collector.SysExDumpCollector,
                    bulk func-0x77 request with a per-slot func-0x73 fallback
                    for USER banks -- entirely the integration point's call,
                    not this module's.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

from Data.librarian_sysex import OBJ_COMBI, OBJ_DRUM_KIT, OBJ_PROGRAM, OBJ_SET_LIST, OBJ_WAVE_SEQ
import Data.object_types as object_types
from Data.local_library_store import BlobStore, LocalIndexEntry, LocalLibraryIndex
from Tools.setlist_data import MAX_COUNT
import Core.kronos_sysex as ksx

_OBJ_TYPE_DISPLAY = {t: d.display_name for t, d in object_types.REGISTRY.items()}


def _bank_display_label(obj_type: int, bank: int) -> str:
    if obj_type == OBJ_PROGRAM:
        return ksx.program_label(bank)
    if obj_type == OBJ_COMBI:
        return ksx.combi_label(bank)
    if obj_type in (OBJ_DRUM_KIT, OBJ_WAVE_SEQ):
        return object_types.label_for(obj_type, bank)
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
    OBJ_DRUM_KIT: list(object_types.REGISTRY[OBJ_DRUM_KIT].editable_banks),    # INT + U-A..U-GG (GM is browse-only)
    OBJ_WAVE_SEQ: list(object_types.REGISTRY[OBJ_WAVE_SEQ].editable_banks),    # INT + U-A..U-GG
}

SLOT_COUNT: Dict[int, int] = {
    OBJ_PROGRAM: 128,
    OBJ_COMBI: 128,
    OBJ_SET_LIST: MAX_COUNT,
    # Deliberately NO Drum Kit / Wave Sequence entry: their slot count differs PER BANK (INT vs User),
    # and a single type-wide number would expose nonexistent slots as legal write destinations.
    # Use slot_count_for(obj_type, bank).
}


def slot_count_for(obj_type: int, bank: int) -> int:
    """Slots in one specific bank (ObjectTypeRegistry.SlotCount(bank))."""
    return object_types.slot_count(obj_type, bank)


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
    #: Non-None only when the sweep gave up before doing any work because the
    #: instrument answered nothing at all (see NO_REPLY_GIVE_UP). Carries the
    #: user-facing explanation; every normal outcome leaves it None.
    aborted: Optional[str] = None
    #: Display labels of the banks with slots that did not answer THIS pull and are still
    #: retried (fewer than UNREADABLE_AFTER failures). What did arrive is applied and the bank's
    #: baseline advances; the unread slots are kept in index.incomplete_banks, which alone makes
    #: the next pull re-fetch them (only them) and holds back edits to them.
    incomplete_banks: Tuple[str, ...] = ()
    #: Slots behind `incomplete_banks` (failed this pull, still retried).
    missing_slots: int = 0
    #: Slot labels ("Program I-A slot 5") that became unreadable with this pull. Reported once;
    #: afterwards they only show in the window's standing line.
    newly_unreadable: Tuple[str, ...] = ()
    #: Previously unread slots that answered this pull.
    recovered_slots: int = 0

    @property
    def incomplete_warning(self) -> Optional[str]:
        """User-facing CHECK line for slots that did not answer and are still retried; None when
        there are none (slots already marked unreadable stay quiet)."""
        if not self.incomplete_banks and not self.newly_unreadable:
            return None
        parts = []
        if self.incomplete_banks:
            names = ", ".join(self.incomplete_banks[:6]) + (
                f" and {len(self.incomplete_banks) - 6} more" if len(self.incomplete_banks) > 6 else "")
            parts.append(
                f"CHECK: {self.missing_slots} slot(s) did not answer, in {len(self.incomplete_banks)} bank(s) "
                f"({names}). Those slots were not refreshed; local changes to those slots are held back "
                f"(not pushed) and they are asked for again on the next Sync.")
        if self.newly_unreadable:
            names = ", ".join(self.newly_unreadable[:6]) + (
                f" and {len(self.newly_unreadable) - 6} more" if len(self.newly_unreadable) > 6 else "")
            parts.append(
                f"CHECK: {len(self.newly_unreadable)} slot(s) did not answer {LocalLibraryIndex.UNREADABLE_AFTER} "
                f"pulls in a row and are now marked unreadable ({names}). They are no longer asked for on every "
                "Sync - only when their bank changes or you choose Re-check unreadable slots.")
        return "\n".join(parts)


class BankObjects(dict):
    """What a GetBankObjects callable returns: {number: (version, body)} for every slot that
    answered, plus `missing`, the slot numbers that were asked for and did NOT answer (timeout or
    a reply that failed to parse), and `skipped`, the slots never asked because a cancel stopped
    the bank part-way (they are remembered as unread but do not count as failed attempts).

    A plain dict is still accepted everywhere and means "complete" (every absent number is an
    empty slot) - the duck-typed fakes the self-tests use rely on that."""

    def __init__(self, data=(), missing=(), skipped=()) -> None:
        super().__init__(data)
        self.missing: Tuple[int, ...] = tuple(missing)
        self.skipped: Tuple[int, ...] = tuple(skipped)


def missing_slots_of(objs) -> Tuple[int, ...]:
    """The `missing` slot numbers of a GetBankObjects result (empty for a plain dict)."""
    return tuple(getattr(objs, "missing", ()) or ())


def skipped_slots_of(objs) -> Tuple[int, ...]:
    """The `skipped` (cancel) slot numbers of a GetBankObjects result (empty for a plain dict)."""
    return tuple(getattr(objs, "skipped", ()) or ())


def accepts_slots(fn) -> bool:
    """True when a GetBankObjects callable takes the optional `slots=` subset keyword (a named
    `slots` parameter or **kwargs). Older duck-typed callables take only (obj_type, bank)."""
    import inspect
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return "slots" in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def slot_label(bank_key: str, number: int) -> str:
    """'Program I-A slot 5' for one slot of a "type:bank" key."""
    return f"{bank_key_label(bank_key)} slot {number}"


#: Consecutive unanswered digest requests tolerated at the START of the sweep before it
#: gives up (LibraryPullPipeline.NoReplyGiveUp). Program INT A is the first bank all_banks()
#: yields and always answers on a live instrument, so silence there means the instrument is
#: not talking at all (most commonly SysEx switched off in GLOBAL > MIDI) — without this the
#: failure was invisible: every request times out instead of erroring.
NO_REPLY_GIVE_UP = 4

SYSEX_OFF_FIX = ("On the instrument: GLOBAL > MIDI, and check every MIDI Filter box (Program Change, "
                 "Bank Change, Combi Change, After Touch, Control Change and Exclusive).")
REFUSE_NO_INSTRUMENT_REPLY = ("REFUSE: the instrument answered no SysEx requests, so nothing could be "
                              "pulled. " + SYSEX_OFF_FIX)


def all_banks() -> List[BankRef]:
    """Every editable bank across all three registry object types, in a
    stable order (Program, then Combi, then Set List) -- mirrors
    LibraryPullPlanner.AllBanks."""
    return [BankRef(obj_type, bank)
            for obj_type in object_types.TYPE_ORDER
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
    silent_run = 0
    for b in all_banks():
        if cancel is not None and cancel():
            # Nothing has been written yet; report an empty, cancelled pull
            # rather than a plan built from a partial digest scan.
            return PullResult(0, 0, 0, cancelled=True)
        d = get_live_digest(b.bank_key)
        if d:
            fresh[b.bank_key] = d
            silent_run = 0
        else:
            no_digest.append(b.bank_key)
            # Only while NOTHING has answered yet: a run of nulls LATER in the sweep is
            # normal (a whole object type the unit gives no digest for). Returning here
            # leaves every persisted baseline untouched.
            if not fresh:
                silent_run += 1
                if silent_run >= NO_REPLY_GIVE_UP:
                    return PullResult(0, 0, 0, aborted=REFUSE_NO_INSTRUMENT_REPLY)

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
    # Unread slots are tracked per slot in index.incomplete_banks, independent of the digest
    # baseline: a bank whose digest changed (or a full pull) is read whole - which also retries
    # its unreadable slots - and a bank whose digest did NOT change but that still has retryable
    # unread slots gets those slots, and only those, asked again. The record, not a stale
    # baseline, drives that re-fetch, so nothing that re-baselines a bank (this pull, a push's
    # post-Store step, Resolve Conflicts) can make an unread slot look pulled.
    whole = {b.bank_key for b in plan.banks_to_fetch}
    work: List[Tuple[BankRef, Optional[List[int]]]] = []
    for b in all_banks():
        if b.bank_key in whole:
            work.append((b, None))
        else:
            retry = index.retryable_slots(b.bank_key)
            if retry:
                work.append((b, retry))
    return _fetch_banks(index, blobs, work, fresh, get_bank_objects, progress, cancel)


def recheck_unread(index: LocalLibraryIndex, blobs: BlobStore, get_bank_objects: GetBankObjects,
                   progress: Optional[Callable[[str], None]] = None,
                   cancel: Optional[Callable[[], bool]] = None) -> PullResult:
    """Ask again for every unread slot in index.incomplete_banks, unreadable ones included (the
    Librarian's "Re-check unreadable slots"). No digest sweep and no baseline change: only the
    record and the slots that answer are touched. A slot that fails again keeps counting (an
    unreadable slot stays unreadable)."""
    work = [(b, index.incomplete_slots(b.bank_key)) for b in all_banks()
            if index.incomplete_slots(b.bank_key)]
    return _fetch_banks(index, blobs, work, {}, get_bank_objects, progress, cancel)


def _apply_objects(index: LocalLibraryIndex, blobs: BlobStore, bank_ref: BankRef, objs) -> Tuple[int, int]:
    """Reconcile one fetch's answered slots into the index. A locally-dirty entry is NEVER
    overwritten; it is flagged Conflicted only when THIS slot's incoming content differs from its
    baseline. Returns (fetched, conflicts)."""
    fetched = conflicts = 0
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
    return fetched, conflicts


def _fetch_banks(index: LocalLibraryIndex, blobs: BlobStore,
                 work: List[Tuple[BankRef, Optional[List[int]]]], fresh: Dict[str, str],
                 get_bank_objects: GetBankObjects, progress: Optional[Callable[[str], None]],
                 cancel: Optional[Callable[[], bool]]) -> PullResult:
    """Fetch each (bank, slots) of `work` (slots None = the whole bank), apply what answered,
    fold the outcome into the unread-slot record, and advance the digest baseline of every
    bank read whole and not cut short by a cancel."""
    subset_ok = accepts_slots(get_bank_objects)
    cancelled = False
    fetched = conflicts = banks_done = missing_total = recovered = 0
    incomplete: List[str] = []
    newly_unreadable: List[str] = []
    limit = LocalLibraryIndex.UNREADABLE_AFTER
    for bank_ref, slots in work:
        if cancel is not None and cancel():
            cancelled = True
            break
        banks_done += 1
        key = bank_ref.bank_key
        display = _OBJ_TYPE_DISPLAY.get(bank_ref.obj_type, "")
        bank_label = _bank_display_label(bank_ref.obj_type, bank_ref.bank)
        name = f"{display} {bank_label}".strip()
        asked: Optional[List[int]] = slots if (slots is not None and subset_ok) else None
        if progress is not None:
            progress(f"Bulk-dumping {name}..." if asked is None
                     else f"Re-reading {len(asked)} unread slot(s) of {name}...")
        objs = (get_bank_objects(bank_ref.obj_type, bank_ref.bank, slots=asked) if asked is not None
                else get_bank_objects(bank_ref.obj_type, bank_ref.bank))
        if objs is None:
            objs = {}
        # Not `or {}`: a bank whose every slot timed out is an EMPTY BankObjects, falsy, and
        # replacing it would drop its `missing` list - exactly the case that must not look pulled.
        missing = set(missing_slots_of(objs))
        skipped = set(skipped_slots_of(objs))
        if missing and not skipped and cancel is not None and cancel():
            # An older callable that reports a cancel's unasked slots as `missing`: do not count
            # them as failures (a cancel is not a failed read).
            skipped, missing = missing, set()

        f, c = _apply_objects(index, blobs, bank_ref, objs)
        fetched += f
        conflicts += c

        fresh_hex = fresh.get(key)
        # A bank read whole that gave no digest AND answered no slot at all is one the instrument
        # does not serve (the NoDigest case): keep pinning it with the sentinel and record nothing,
        # or every lazy Sync would re-sweep it slot by slot.
        served = asked is not None or bool(fresh_hex) or bool(objs)
        if served:
            before = set(index.incomplete_slots(key))
            newly = index.record_slot_fetch(bank_ref.obj_type, bank_ref.bank, asked,
                                            failed=missing, skipped=skipped)
            rec = index.incomplete_banks.get(key, {})
            recovered += len(before - set(rec))
            retry_failed = [n for n in missing if rec.get(n, limit) < limit]
            if retry_failed:
                incomplete.append(_bank_report_label(bank_ref))
                missing_total += len(retry_failed)
            newly_unreadable.extend(slot_label(key, n) for n in newly)
        if skipped:
            # Cut short by a cancel inside the bank: its unasked slots are recorded above (unread,
            # held back, re-fetched), but the bank is not a finished read - no baseline advance.
            cancelled = True
            break
        if asked is None and fresh_hex is not None:
            # The baseline now reflects what this library holds for every slot that answered;
            # the record above keeps the ones that did not.
            index.set_bank_digest_baseline(bank_ref.obj_type, bank_ref.bank, fresh_hex)

    return PullResult(banks_done, fetched, conflicts, cancelled=cancelled,
                      incomplete_banks=tuple(incomplete), missing_slots=missing_total,
                      newly_unreadable=tuple(newly_unreadable), recovered_slots=recovered)


def _bank_report_label(bank_ref: BankRef) -> str:
    display = _OBJ_TYPE_DISPLAY.get(bank_ref.obj_type, "")
    label = _bank_display_label(bank_ref.obj_type, bank_ref.bank)
    return f"{display} {label}".strip() if label else (display or bank_ref.bank_key)


def bank_key_label(bank_key: str) -> str:
    """Display label ("Program U-A") of a "type:bank" index bank key."""
    try:
        obj_type, bank = (int(x) for x in bank_key.split(":"))
    except ValueError:
        return bank_key
    return _bank_report_label(BankRef(obj_type, bank))


def unread_slots_text(incomplete: Dict[str, Dict[int, int]], entry_keys: List[str], limit: int = 12) -> str:
    """'Program I-A slot 3, Program I-A slot 5 (unreadable)' for the given "type:bank:number"
    index keys, each tagged unreadable when the record says so."""
    parts = []
    for k in sorted(dict.fromkeys(entry_keys), key=lambda k: tuple(int(x) for x in k.split(":"))):
        obj_type, bank, number = (int(x) for x in k.split(":"))
        bank_key = LocalLibraryIndex.bank_key(obj_type, bank)
        fails = incomplete.get(bank_key, {}).get(number)
        tag = " (unreadable)" if fails is not None and fails >= LocalLibraryIndex.UNREADABLE_AFTER else ""
        parts.append(slot_label(bank_key, number) + tag)
    if len(parts) > limit:
        parts = parts[:limit] + [f"and {len(parts) - limit} more"]
    return ", ".join(parts)


def incomplete_banks_text(incomplete: Dict[str, Dict[int, int]], keys: Optional[List[str]] = None) -> str:
    """'Program U-A (3 unread slots), Combi I-B (128 unread slots)' for the given incomplete-bank
    record (LocalLibraryIndex.incomplete_banks), restricted to `keys` when given."""
    chosen = [k for k in (keys if keys is not None else incomplete) if k in incomplete]
    parts = [f"{bank_key_label(k)} ({len(incomplete[k])} unread slot{'s' if len(incomplete[k]) != 1 else ''})"
             for k in sorted(chosen, key=lambda k: tuple(int(x) for x in k.split(":")))]
    return ", ".join(parts)


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


        # ── silent instrument: the sweep gives up after NO_REPLY_GIVE_UP unanswered banks,
        #    touches no baseline, and reports aborted (not a hang / not "complete") ──
        asked: List[str] = []
        idx_silent = LocalLibraryIndex(root / "silent")
        idx_silent.set_bank_digest_baseline(OBJ_PROGRAM, prog_a_bank, "keep-me")
        r_silent = pull(idx_silent, blobs, lambda bk: (asked.append(bk) or None),
                        lambda t, b: {}, full=True)
        check("silent-aborted", r_silent.aborted is not None and "SysEx" in r_silent.aborted)
        check("silent-gave-up-early", len(asked) == NO_REPLY_GIVE_UP)
        check("silent-baseline-untouched",
              idx_silent.bank_digest_baseline == {LocalLibraryIndex.bank_key(OBJ_PROGRAM, prog_a_bank): "keep-me"})
        check("silent-nothing-fetched", r_silent.objects_fetched == 0 and not r_silent.cancelled)

        # ── incomplete banks: what arrived is applied, the baseline ADVANCES (it reflects the slots
        #    that answered), the unread slots are recorded per slot, and the next lazy pull asks for
        #    those slots only - the record, not the digest, drives that ──
        idx_inc = LocalLibraryIndex(root / "incomplete")
        blobs_inc = BlobStore(root / "incomplete")
        inc_digest = {b.bank_key: f"d-{b.bank_key}" for b in all_banks()}
        prog_b_key = BankRef(OBJ_PROGRAM, prog_b_bank).bank_key
        timeouts = {(OBJ_PROGRAM, prog_a_bank): {1, 2}, (OBJ_PROGRAM, prog_b_bank): set(range(128))}
        calls: List[Tuple[int, int, Optional[Tuple[int, ...]]]] = []

        def gbo_inc(obj_type, bank, slots=None):
            calls.append((obj_type, bank, None if slots is None else tuple(slots)))
            want = range(128) if slots is None else slots
            if (obj_type, bank) in timeouts:
                dead = timeouts[(obj_type, bank)]
                # every slot answers with a body unless it times out; an EMPTY (falsy) BankObjects
                # (all timed out) must still count as incomplete
                return BankObjects({n: (1, make_body(f"Inc-{bank}-{n}")) for n in want if n not in dead},
                                   missing=[n for n in want if n in dead])
            return {}                      # plain dict: complete
        r_inc = pull(idx_inc, blobs_inc, lambda bk: inc_digest.get(bk), gbo_inc, full=True)
        check("incomplete-applied", idx_inc.get(OBJ_PROGRAM, prog_a_bank, 0) is not None)
        check("incomplete-two-banks", len(r_inc.incomplete_banks) == 2)
        check("incomplete-missing-count", r_inc.missing_slots == 2 + 128)
        check("incomplete-baseline-advanced",
              idx_inc.bank_digest_baseline.get(prog_a_key) == inc_digest[prog_a_key]
              and idx_inc.bank_digest_baseline.get(prog_b_key) == inc_digest[prog_b_key])
        check("plain-dict-complete",
              idx_inc.bank_digest_baseline.get(BankRef(OBJ_COMBI, combi_bank).bank_key)
              == inc_digest[BankRef(OBJ_COMBI, combi_bank).bank_key])
        check("incomplete-warning", r_inc.incomplete_warning is not None
              and "CHECK:" in r_inc.incomplete_warning)
        check("incomplete-persisted-record",
              idx_inc.incomplete_slots(prog_a_key) == [1, 2]
              and idx_inc.incomplete_slots(prog_b_key) == list(range(128))
              and BankRef(OBJ_COMBI, combi_bank).bank_key not in idx_inc.incomplete_banks)
        idx_inc.save()
        check("incomplete-survives-restart",
              LocalLibraryIndex(root / "incomplete").load().incomplete_slots(prog_a_key) == [1, 2])
        # The digest is unchanged (the baseline advanced): the record alone forces a re-fetch of ONLY
        # the unread slots - the original bug was a lazy Sync never asking for them again.
        calls.clear()
        r_inc2 = pull(idx_inc, blobs_inc, lambda bk: inc_digest.get(bk), gbo_inc, full=False)
        check("incomplete-refetched-lazily", r_inc2.banks_checked == 2)
        check("incomplete-targeted", sorted(calls) == sorted([(OBJ_PROGRAM, prog_a_bank, (1, 2)),
                                                              (OBJ_PROGRAM, prog_b_bank, tuple(range(128)))]))
        check("incomplete-second-strike", idx_inc.incomplete_banks[prog_a_key] == {1: 2, 2: 2})
        timeouts[(OBJ_PROGRAM, prog_b_bank)] = {5}   # partly answered now: answered slots leave the record
        r_inc2b = pull(idx_inc, blobs_inc, lambda bk: inc_digest.get(bk), gbo_inc, full=False)
        check("incomplete-record-shrinks", r_inc2b.banks_checked == 2
              and idx_inc.incomplete_slots(prog_b_key) == [5] and r_inc2b.recovered_slots == 127)
        # the 3rd consecutive failure: slots 1, 2 of bank A (and 5 of B) become unreadable, reported once
        check("unreadable-after-3", idx_inc.unreadable_slots(prog_a_key) == [1, 2]
              and idx_inc.unreadable_slots(prog_b_key) == [5]
              and len(r_inc2b.newly_unreadable) == 3 and "unreadable" in (r_inc2b.incomplete_warning or "")
              and not r_inc2b.incomplete_banks)
        # ...and from then on are quiet: not asked for, no warning, nothing to do on a lazy pull
        calls.clear()
        r_quiet = pull(idx_inc, blobs_inc, lambda bk: inc_digest.get(bk), gbo_inc, full=False)
        check("unreadable-quiet", r_quiet.banks_checked == 0 and calls == [] and r_quiet.incomplete_warning is None)
        # a digest change of the bank reads it whole, which retries the unreadable slots (still dead: quiet)
        inc_digest[prog_a_key] = "d-changed"
        calls.clear()
        r_dc = pull(idx_inc, blobs_inc, lambda bk: inc_digest.get(bk), gbo_inc, full=False)
        check("unreadable-digest-change-retries", calls == [(OBJ_PROGRAM, prog_a_bank, None)]
              and r_dc.incomplete_warning is None and idx_inc.unreadable_slots(prog_a_key) == [1, 2]
              and idx_inc.bank_digest_baseline.get(prog_a_key) == "d-changed")
        # Re-check asks only for the unread slots, no digest sweep, and a slot that answers recovers
        timeouts[(OBJ_PROGRAM, prog_a_bank)] = {2}
        calls.clear()
        r_rc = recheck_unread(idx_inc, blobs_inc, gbo_inc)
        check("recheck-targeted", sorted(calls) == sorted([(OBJ_PROGRAM, prog_a_bank, (1, 2)),
                                                           (OBJ_PROGRAM, prog_b_bank, (5,))]))
        check("recheck-result", r_rc.recovered_slots == 1 and idx_inc.incomplete_slots(prog_a_key) == [2]
              and idx_inc.unreadable_slots(prog_a_key) == [2] and r_rc.incomplete_warning is None
              and idx_inc.bank_digest_baseline.get(prog_a_key) == "d-changed")
        # a subset fetch where every asked slot answers never clears slots it did not ask for
        idx_inc.incomplete_banks[prog_b_key] = {5: 1, 6: 3}
        timeouts[(OBJ_PROGRAM, prog_b_bank)] = set()
        calls.clear()
        pull(idx_inc, blobs_inc, lambda bk: inc_digest.get(bk), gbo_inc, full=False)
        check("subset-keeps-unreadable", (OBJ_PROGRAM, prog_b_bank, (5,)) in calls
              and idx_inc.incomplete_banks.get(prog_b_key) == {6: 3})
        timeouts.clear()                   # the instrument answers everything now
        recheck_unread(idx_inc, blobs_inc, gbo_inc)
        check("incomplete-cleared", idx_inc.incomplete_banks == {})
        r_inc4 = pull(idx_inc, blobs_inc, lambda bk: inc_digest.get(bk), gbo_inc, full=False)
        check("complete-then-skipped", r_inc4.banks_checked == 0)

        # an older two-argument callable (no slots=) still works: the bank is read whole
        idx_old = LocalLibraryIndex(root / "oldcb")
        idx_old.set_incomplete_bank(OBJ_PROGRAM, prog_a_bank, [3])
        for b in all_banks():
            idx_old.set_bank_digest_baseline(b.obj_type, b.bank, inc_digest[b.bank_key])
        old_calls: List[Tuple[int, int]] = []
        r_old = pull(idx_old, BlobStore(root / "oldcb"), lambda bk: inc_digest.get(bk),
                     lambda t, b: (old_calls.append((t, b)) or {3: (1, make_body("Old3"))}), full=False)
        check("old-callable-whole-bank", old_calls == [(OBJ_PROGRAM, prog_a_bank)] and r_old.banks_checked == 1
              and idx_old.incomplete_banks == {} and idx_old.get(OBJ_PROGRAM, prog_a_bank, 3) is not None)
        check("accepts-slots", accepts_slots(gbo_inc) and not accepts_slots(lambda t, b: {})
              and accepts_slots(lambda t, b, **k: {}))

        # a bank with NO digest that answers no slot keeps the NoDigest pin (not re-swept forever)
        idx_nd = LocalLibraryIndex(root / "nodigest")
        nd_digest = dict(inc_digest)
        nd_digest[prog_b_key] = None
        r_nd = pull(idx_nd, BlobStore(root / "nodigest"), lambda bk: nd_digest.get(bk),
                    lambda t, b: BankObjects({}, missing=range(128)) if (t, b) == (OBJ_PROGRAM, prog_b_bank) else {},
                    full=True)
        check("nodigest-silent-pinned", idx_nd.bank_digest_baseline.get(prog_b_key) == ""
              and not r_nd.incomplete_banks and not idx_nd.incomplete_banks)

        # a cancel inside a bank (holes) stops the sweep, advances no baseline, and STILL remembers the
        # unread slots - without counting the never-asked ones as failed attempts
        idx_c = LocalLibraryIndex(root / "cancel")
        flag = {"stop": False}

        def gbo_cancel(obj_type, bank, slots=None):
            flag["stop"] = True
            return BankObjects({0: (1, make_body("C0"))}, missing=[1], skipped=range(2, 128))
        r_c = pull(idx_c, BlobStore(root / "cancel"), lambda bk: inc_digest.get(bk), gbo_cancel,
                   full=True, cancel=lambda: flag["stop"])
        rec_c = next(iter(idx_c.incomplete_banks.values()), {})
        check("cancel-mid-bank", r_c.cancelled and not idx_c.bank_digest_baseline
              and len(idx_c.incomplete_banks) == 1 and sorted(rec_c) == list(range(1, 128))
              and rec_c[1] == 1 and all(rec_c[n] == 0 for n in range(2, 128)))
        # an older callable reporting the cancel's slots as `missing` gets the same no-count treatment
        idx_c2 = LocalLibraryIndex(root / "cancel2")
        flag["stop"] = False

        def gbo_cancel_old(obj_type, bank):
            flag["stop"] = True
            return BankObjects({0: (1, make_body("C0"))}, missing=range(1, 128))
        pull(idx_c2, BlobStore(root / "cancel2"), lambda bk: inc_digest.get(bk), gbo_cancel_old,
             full=True, cancel=lambda: flag["stop"])
        rec_c2 = next(iter(idx_c2.incomplete_banks.values()), {})
        check("cancel-old-callable-no-count", len(rec_c2) == 127 and set(rec_c2.values()) == {0}
              and not idx_c2.bank_digest_baseline)
    finally:
        if root.exists():
            shutil.rmtree(root)

    if fails:
        print("FAIL:", ", ".join(fails))
        sys.exit(1)
    print("library_pull_pipeline self-test: OK")


if __name__ == "__main__":
    _selftest()
