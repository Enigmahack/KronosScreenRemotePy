r"""
Linear undo for the Librarian's LOCAL (pre-Commit) state — port of
Core/LocalLibrary/LibrarianUndo.cs.

The answer to "I dragged a whole bank out of the Merge Window by accident and
had to start over."

Capture model (observational, not per-call-site): LocalLibraryIndex raises
slot_mutating before every index write/removal and MergeCache raises
mutating before every staging change, so an action only has to open a scope
(Begin) and EVERY local edit it performs — however deep — lands in the step
automatically. First-prior-per-slot wins, because one user action
legitimately touches the same slot twice (ToggleDelete does Discard then
SetPendingDelete).

A scope pushes its step iff it captured something, with no explicit Commit:
"captured" means the mutation already happened, so a partially-completed
action stays recoverable instead of being discarded as a failure.

Deliberately NOT undone: the persisted displaced-occupant clipboard
(batch_clipboard.BatchClipboard). Undo restores the occupant to its slot,
which makes the clipboard copy redundant — but that clipboard IS the safety
net, and undo removing entries from it could delete a copy some later action
put there. Undo never deletes a safety copy.

Also NOT undoable: anything already pushed to hardware. Sync/Commit clears
the stack (see LibrarianShellWindow) — a local rollback across a hardware
write isn't representable here. Clear History is likewise excluded: it
deletes oplog.jsonl outright.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from local_library_store import LocalIndexEntry, LocalLibraryIndex


@dataclass
class LocalSlotSnapshot:
    """One local slot's state BEFORE an undoable action touched it.
    Entry == None means the slot didn't exist at all (a placement into a
    previously-empty slot), which undo restores by removing the index entry
    again. Holds the entry reference as-is (dataclass, not frozen-copied
    field-by-field) so Version/BaselineHash/CurrentHash/DisplayName/
    Conflicted/PendingDelete come back exactly as they were."""
    obj_type: int
    bank: int
    number: int
    entry: Optional[LocalIndexEntry]


@dataclass
class LibrarianUndoStep:
    """One undoable user action, as the state it needs to be rolled back TO
    (not as a reverse operation). Snapshot-of-what-was-touched, not
    full-library: a placement into a bank captures only that bank's affected
    slots, so a step costs a handful of small index records plus — only when
    the Merge Window actually changed — one merge staging snapshot."""

    description: str
    local_slots: List[LocalSlotSnapshot] = field(default_factory=list)
    merge_snapshot: Optional[dict] = None      # lazily captured, only if the merge cache mutated
    session_dependencies: List[object] = field(default_factory=list)
    pending_bank_type_change: Optional[tuple] = None  # (bank, prior_is_exi_or_None)

    @property
    def captured_nothing(self) -> bool:
        """A step that captured nothing means the action mutated nothing
        (e.g. a placement REFUSEd by the orphan gate before writing) — never
        pushed onto the stack, so Ctrl+Z can't consume a no-op step and look
        broken."""
        return (not self.local_slots and self.merge_snapshot is None
                and self.pending_bank_type_change is None)


class LibrarianUndoRecorder:
    """One undo stack for one Librarian window session. Bounded (MaxSteps) —
    enough to walk back a run of accidental drops; a step can hold a merge
    staging snapshot (bodies), and the oldest steps are the least likely to
    be wanted."""

    MAX_STEPS = 20

    def __init__(self, index: LocalLibraryIndex,
                 snapshot_merge: Callable[[], Optional[dict]],
                 restore_merge: Callable[[Optional[dict]], None],
                 read_session_deps: Callable[[], List[object]],
                 restore_session_deps: Callable[[List[object]], None],
                 pending_bank_type_change: Callable[[int], Optional[bool]],
                 set_pending_bank_type_change: Callable[[int, Optional[bool]], None]):
        self._index = index
        self._snapshot_merge = snapshot_merge
        self._restore_merge = restore_merge
        self._read_session_deps = read_session_deps
        self._restore_session_deps = restore_session_deps
        self._pending_bank_type_change = pending_bank_type_change
        self._set_pending_bank_type_change = set_pending_bank_type_change

        self._steps: List[LibrarianUndoStep] = []
        self._active: Optional[_Capture] = None
        self._restoring = False
        self.on_changed: Optional[Callable[[], None]] = None

    # ── Public API ──────────────────────────────────────────────────────────

    @property
    def can_undo(self) -> bool:
        return len(self._steps) > 0

    @property
    def depth(self) -> int:
        return len(self._steps)

    @property
    def top_description(self) -> Optional[str]:
        return self._steps[-1].description if self._steps else None

    def begin(self, description: str) -> "_Capture | None":
        """Open one undoable scope. Returns None when a capture is already
        active (nested Begin — the outer scope owns the step, so this does
        nothing), matching LibrarianUndoRecorder.Begin's NoOpScope."""
        if self._active is not None:
            return None
        capture = _Capture(self, description, self._read_session_deps())
        self._active = capture
        return capture

    def capture_pending_bank_type_change(self, bank: int) -> None:
        """Called by PlaceMergeBankWithTypeChange immediately before it stages
        a whole-bank HD-1/EXi reformat — event-driven capture can't see this
        one (it's index metadata, not a slot write)."""
        if self._restoring:
            return
        if self._active is not None:
            self._active.capture_bank_type(bank, self._pending_bank_type_change(bank))

    def undo(self) -> Optional[str]:
        """Rolls the most recent step back and returns its description (None
        if the stack was empty — nothing is mutated in that case). Restores,
        in order: local slots, the pending bank-type-change intent, the Merge
        Window's staged contents, and the pending-dependency list. Capture is
        suppressed throughout — an undo is never itself an undoable step."""
        if not self._steps:
            return None
        step = self._steps.pop()

        self._restoring = True
        try:
            if step.local_slots:
                self._restore_slots(step.local_slots, f"Undid: {step.description}")
            if step.pending_bank_type_change is not None:
                bank, prior = step.pending_bank_type_change
                self._set_pending_bank_type_change(bank, prior)
            if step.merge_snapshot is not None:
                self._restore_merge(step.merge_snapshot)
            self._restore_session_deps(step.session_dependencies)
        finally:
            self._restoring = False

        if self.on_changed:
            self.on_changed()
        return step.description

    def clear(self) -> None:
        """Called after a successful Sync/Commit: every step below describes
        local state that has now been written to hardware, and this stack
        can't roll a hardware write back."""
        if not self._steps:
            return
        self._steps.clear()
        if self.on_changed:
            self.on_changed()

    # ── Event hooks (called by LocalLibraryIndex / MergeCache wrappers) ────

    def on_slot_mutating(self, obj_type: int, bank: int, number: int,
                         prior: Optional[LocalIndexEntry]) -> None:
        if self._restoring:
            return
        if self._active is not None:
            self._active.capture_slot(obj_type, bank, number, prior)

    def on_merge_mutating(self) -> None:
        if self._restoring:
            return
        if self._active is not None:
            self._active.capture_merge(self._snapshot_merge)

    # ── Internals ──────────────────────────────────────────────────────────

    def _end(self, capture: "_Capture") -> None:
        if self._active is not capture:
            return
        self._active = None
        step = capture.build()
        if step.captured_nothing:
            return
        self._steps.append(step)
        if len(self._steps) > self.MAX_STEPS:
            self._steps.pop(0)
        if self.on_changed:
            self.on_changed()

    def _restore_slots(self, slots: List[LocalSlotSnapshot], description: str) -> None:
        """Puts slots back exactly as captured — a snapshot whose entry is None
        means the slot didn't exist then, so it's removed again. Appends ONE
        "Undo" op-log entry so index.json stays a valid fold of oplog.jsonl
        and the rollback is auditable history (mirrors
        LocalLibraryCache.RestoreSlots)."""
        now = _now_iso()
        targets = []
        for s in slots:
            key = LocalLibraryIndex.key(s.obj_type, s.bank, s.number)
            if s.entry is not None:
                self._index.set_entry(s.obj_type, s.bank, s.number, s.entry)
                targets.append({"obj_type": s.obj_type, "bank": s.bank,
                                "number": s.number, "result_hash": s.entry.current_hash})
            else:
                self._index.delete(s.obj_type, s.bank, s.number)
                targets.append({"obj_type": s.obj_type, "bank": s.bank,
                                "number": s.number,
                                "result_hash": LocalLibraryIndex.DELETED_TOMBSTONE})
        if not targets:
            return
        try:
            from local_library_store import OpLog
            OpLog().append({
                "id": str(uuid4()), "timestamp_utc": now, "op_kind": "Undo",
                "targets": targets, "description": description,
                "sync_batch_id": None, "synced_at_utc": None,
            })
        except Exception:
            pass
        self._index.save()


class _Capture:
    """Accumulates one user action's prior states. First-prior-per-slot wins
    (a single action can write the same slot more than once)."""

    def __init__(self, owner: LibrarianUndoRecorder, description: str,
                 session_deps: List[object]):
        self._owner = owner
        self._description = description
        self._session_deps = list(session_deps)
        self._slots: Dict[str, LocalSlotSnapshot] = {}
        self._merge: Optional[dict] = None
        self._bank_type: Optional[tuple] = None
        self._disposed = False

    def capture_slot(self, obj_type: int, bank: int, number: int,
                     prior: Optional[LocalIndexEntry]) -> None:
        key = LocalLibraryIndex.key(obj_type, bank, number)
        if key not in self._slots:
            self._slots[key] = LocalSlotSnapshot(obj_type, bank, number, prior)

    def capture_merge(self, snapshot_fn: Callable[[], Optional[dict]]) -> None:
        if self._merge is None:
            self._merge = snapshot_fn()

    def capture_bank_type(self, bank: int, prior: Optional[bool]) -> None:
        if self._bank_type is None:
            self._bank_type = (bank, prior)

    def build(self) -> LibrarianUndoStep:
        return LibrarianUndoStep(
            description=self._description,
            local_slots=list(self._slots.values()),
            merge_snapshot=self._merge,
            session_dependencies=self._session_deps,
            pending_bank_type_change=self._bank_type,
        )

    def dispose(self) -> None:
        """Ends this capture scope — the owner pushes the step iff it
        captured something (mirrors LibrarianUndoRecorder.End on Dispose)."""
        if self._disposed:
            return
        self._disposed = True
        self._owner._end(self)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def uuid4():
    import uuid
    return str(uuid.uuid4())


# ── Self-test (python librarian_undo.py) ────────────────────────────────────


def _selftest() -> None:
    import sys
    import tempfile
    import pathlib

    fails: List[str] = []

    def check(name: str, cond: bool) -> None:
        if not cond:
            fails.append(name)

    root = pathlib.Path(tempfile.mkdtemp(prefix="kronos_undo_selftest"))
    idx = LocalLibraryIndex(root)

    # An undoable placement: capture prior (None) for a fresh slot, then write.
    rec = LibrarianUndoRecorder(
        idx,
        snapshot_merge=lambda: None,
        restore_merge=lambda s: None,
        read_session_deps=lambda: [],
        restore_session_deps=lambda s: None,
        pending_bank_type_change=lambda b: None,
        set_pending_bank_type_change=lambda b, v: None,
    )
    changed_log: List[bool] = []
    rec.on_changed = lambda: changed_log.append(True)

    check("undo-empty-at-start", not rec.can_undo and rec.undo() is None)

    scope = rec.begin("Placed test object")
    check("scope-opened", scope is not None)
    # Simulate LocalLibraryCache.RecordEdits raising SlotMutating before the write.
    rec.on_slot_mutating(0x00, 0x40, 5, idx.get(0x00, 0x40, 5))   # prior None (empty slot)
    from local_library_store import LocalIndexEntry
    idx.set_entry(0x00, 0x40, 5, LocalIndexEntry(
        version=1, baseline_hash="", current_hash="h1", display_name="New",
        created_utc="t0", modified_utc="t0"))
    idx.save()
    if scope is not None:
        scope.dispose()

    check("undo-available-after-scope", rec.can_undo)
    check("undo-top-description", rec.top_description == "Placed test object")

    # Undo restores the slot to non-existence.
    desc = rec.undo()
    check("undo-returns-description", desc == "Placed test object")
    check("slot-removed-by-undo", idx.get(0x00, 0x40, 5) is None)
    check("undo-stack-empty-again", not rec.can_undo)

    # A no-op scope (nothing captured) pushes nothing.
    scope2 = rec.begin("Refused placement")
    if scope2 is not None:
        scope2.dispose()
    check("no-op-scope-pushes-nothing", not rec.can_undo)

    # First-prior-wins: same slot touched twice in one action keeps the FIRST prior.
    scope3 = rec.begin("Double touch")
    first_prior = idx.get(0x00, 0x40, 5)   # None
    rec.on_slot_mutating(0x00, 0x40, 5, first_prior)
    idx.set_entry(0x00, 0x40, 5, LocalIndexEntry(
        version=1, baseline_hash="", current_hash="h1", display_name="A",
        created_utc="t0", modified_utc="t0"))
    idx.save()
    rec.on_slot_mutating(0x00, 0x40, 5, idx.get(0x00, 0x40, 5))   # second touch, prior now exists
    idx.set_entry(0x00, 0x40, 5, LocalIndexEntry(
        version=1, baseline_hash="", current_hash="h2", display_name="B",
        created_utc="t0", modified_utc="t0"))
    idx.save()
    if scope3 is not None:
        scope3.dispose()

    check("first-prior-wins-depth", rec.depth == 1)
    rec.undo()
    check("first-prior-restored-none", idx.get(0x00, 0x40, 5) is None)

    # Restore of an existing entry brings the WHOLE entry back (baseline etc).
    scope4 = rec.begin("Edit existing")
    existing = LocalIndexEntry(
        version=2, baseline_hash="base1", current_hash="base1", display_name="Old",
        created_utc="t1", modified_utc="t1", conflicted=False,
        has_resolved_dependencies=False, is_exi=False)
    idx.set_entry(0x00, 0x40, 5, existing)
    idx.save()
    rec.on_slot_mutating(0x00, 0x40, 5, existing)
    idx.set_entry(0x00, 0x40, 5, LocalIndexEntry(
        version=2, baseline_hash="base1", current_hash="newh", display_name="Edited",
        created_utc="t1", modified_utc="t2", conflicted=False,
        has_resolved_dependencies=True, is_exi=True))
    idx.save()
    if scope4 is not None:
        scope4.dispose()

    rec.undo()
    restored = idx.get(0x00, 0x40, 5)
    check("undo-restores-whole-entry", restored is not None
          and restored.baseline_hash == "base1" and restored.current_hash == "base1"
          and restored.display_name == "Old" and restored.is_exi is False)

    # Clear empties the stack.
    scope5 = rec.begin("Place again")
    rec.on_slot_mutating(0x00, 0x40, 6, None)
    idx.set_entry(0x00, 0x40, 6, LocalIndexEntry(
        version=1, baseline_hash="", current_hash="hx", display_name="X",
        created_utc="t0", modified_utc="t0"))
    idx.save()
    if scope5 is not None:
        scope5.dispose()
    check("undo-available-before-clear", rec.can_undo)
    rec.clear()
    check("clear-empties-stack", not rec.can_undo)

    if fails:
        print("librarian_undo self-test FAIL:", ", ".join(fails))
        sys.exit(1)
    print("librarian_undo self-test: OK")


if __name__ == "__main__":
    _selftest()
