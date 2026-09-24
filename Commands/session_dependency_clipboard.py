r"""
Session dependency clipboard — port of Core/LocalLibrary/SessionDependencyClipboard.cs.

Tracks dependencies an incoming placement (Combi/Set List) refers to that are NOT
present locally yet — surfaced so the user can place them before Commit/Sync will
validate clean (the dependency-completeness gate, requirement 14). Confirmed from
the C# source and its one production caller
(ViewModels/LibrarianShellViewModel.cs:22 `readonly SessionDependencyClipboard
_sessionClipboard = new();`) plus ChangesetBuilder.BuildAsync's actual gate logic
(Core/LocalLibrary/ChangesetBuilder.cs:26-29):

    if (sessionClip.Pending.Count > 0)
    {
        plan.Warnings.Add(AppMessages.Librarian.Sync.RefusePendingDependencies(...));
        return (plan, conflicted);   // REFUSES outright — no writes assembled at all
    }

So this is exactly the earlier gap analysis's guess, now CONFIRMED (not guessed):
ChangesetBuilder.BuildAsync's very first step refuses to build any changeset at all
while this clipboard is non-empty. `has_unresolved()` below is the query a future
Python ChangesetBuilder port will call as that same gate.

Genuinely session-only (mirrors the C# original exactly): in-memory ONLY, nothing
here is persisted to local_library_store's index/oplog/blob store — deliberately
distinct from that persisted pending-change history and from any persisted batch
clipboard. Closing the app loses anything still unplaced here (accepted, per the
C# class comment's own "see the plan's flagged tensions").

Each entry's fields are what let a future ResolvePendingDependencies actually
REPAIR the reference later, not just display it (per the C# comment):
  * missing_ref  — the (type, bank, number) address that was referenced but not
                    found locally.
  * ref_kind     — 'combi_timbre' | 'setlist_slot' (matches ReferrerSite.kind in
                    librarian_model.py).
  * site         — the numeric reference-site index within required_by's body
                    (timbre slot / set-list slot) that needs repatching.
  * required_by  — the (type, bank, number) address of the object holding the
                    dangling reference.
  * expected_content_hash — sha1 hex digest (BlobStore.compute_hash) of the
                    content the missing object is expected to have, if known
                    (e.g. from a loaded .pcg or a Merge Window pull); None for a
                    true gap this mechanism can never auto-repair (mirrors the
                    C# comment: "null for a true gap... not something this
                    mechanism can ever auto-repair").
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from Data.librarian_model import ObjLoc


@dataclass(frozen=True)
class SessionDependencyEntry:
    missing_ref: ObjLoc
    ref_kind: str
    site: int
    required_by: ObjLoc
    expected_content_hash: Optional[str] = None


class SessionDependencyClipboard:
    """In-memory-only tracker of unresolved placements for the current editing
    session. Mirrors SessionDependencyClipboard.cs's four operations exactly —
    Add (de-duped), Resolve (by address), Remove (exact entry), Clear (all)."""

    def __init__(self) -> None:
        self._entries: List[SessionDependencyEntry] = []

    @property
    def pending(self) -> List[SessionDependencyEntry]:
        return list(self._entries)

    def add(self, entry: SessionDependencyEntry) -> None:
        """De-duped append — mirrors C# Add's `if (!_entries.Contains(entry))`."""
        if entry not in self._entries:
            self._entries.append(entry)

    def resolve(self, missing_ref: ObjLoc) -> None:
        """Called once the missing object has actually been placed locally AT
        EXACTLY missing_ref's own address — clears every pending entry
        referencing it, regardless of which placement originally flagged it.
        NOT what a future ResolvePendingDependencies uses (that repoints by
        content hash, which can resolve an entry whose dependency landed at a
        DIFFERENT address than missing_ref) — this is the narrower "landed at
        the exact original address" case, same split as the C# original."""
        self._entries = [e for e in self._entries if e.missing_ref != missing_ref]

    def remove(self, entry: SessionDependencyEntry) -> None:
        """Exact-entry removal — for a future ResolvePendingDependencies, which
        repatches one entry at a time and must remove only THAT entry, not
        every entry that happens to share an address (two different referrers
        can each be tracked against the same expected content hash from two
        different original addresses)."""
        try:
            self._entries.remove(entry)
        except ValueError:
            pass

    def clear(self) -> None:
        self._entries.clear()

    def has_unresolved(self) -> bool:
        """The gate a future ChangesetBuilder port calls: True while ANY
        placement is still unresolved, mirroring ChangesetBuilder.BuildAsync's
        `sessionClip.Pending.Count > 0` refusal check exactly."""
        return len(self._entries) > 0


# ── Self-test (python session_dependency_clipboard.py) ──────────────────────

def _selftest() -> None:
    import sys

    fails: list = []

    def check(name, cond):
        if not cond:
            fails.append(name)

    clip = SessionDependencyClipboard()
    check("empty-not-unresolved", not clip.has_unresolved())
    check("empty-pending", clip.pending == [])

    missing = ObjLoc(0x01, 0x40, 5)     # OBJ_COMBI at U-A:005 — never placed locally
    referrer = ObjLoc(0x0D, 0, 3)       # Set List 003 referencing it

    entry = SessionDependencyEntry(
        missing_ref=missing, ref_kind="setlist_slot", site=12,
        required_by=referrer, expected_content_hash=None,
    )
    clip.add(entry)
    check("has-unresolved-after-add", clip.has_unresolved())
    check("pending-count-1", len(clip.pending) == 1)

    # de-dup: adding the exact same entry again is a no-op
    clip.add(SessionDependencyEntry(
        missing_ref=missing, ref_kind="setlist_slot", site=12,
        required_by=referrer, expected_content_hash=None,
    ))
    check("add-dedup", len(clip.pending) == 1)

    # a distinct entry (different site) against the same missing_ref DOES get added
    entry2 = SessionDependencyEntry(
        missing_ref=missing, ref_kind="combi_timbre", site=0,
        required_by=ObjLoc(0x01, 0x40, 6), expected_content_hash="deadbeef",
    )
    clip.add(entry2)
    check("pending-count-2", len(clip.pending) == 2)

    # resolve() clears every entry sharing missing_ref, regardless of site/referrer
    clip.resolve(missing)
    check("resolve-clears-all-matching", not clip.has_unresolved())
    check("resolve-empty", clip.pending == [])

    # remove() targets exactly one entry, leaving others with the same missing_ref intact
    clip.add(entry)
    clip.add(entry2)
    clip.remove(entry)
    check("remove-exact-only", clip.pending == [entry2])

    clip.clear()
    check("clear-empties", not clip.has_unresolved() and clip.pending == [])

    if fails:
        print("FAIL:", ", ".join(fails))
        sys.exit(1)
    print("session_dependency_clipboard self-test: OK")


if __name__ == "__main__":
    _selftest()
