r"""
Local Library Cut/Copy/Paste clipboard — session-only, in-memory.

Port of ViewModels/LocalLibraryPaneViewModel.cs's `_clipItems`/`ClipboardMode`
fields and its Cut/Copy/PasteIntoSlot/PasteIntoBank/PasteSingle/PasteBatch
methods (lines 48-347 of that file, read in full, not guessed at).

THIS IS NOT Core/BatchMoveModel.cs's `BatchClipboard`/`ClipboardEntry`/
`ClipboardProvenance` classes. That is a completely different, already-ported
thing — a durable, JSON-persisted audit trail of objects displaced by a batch
placement (`Storage.LoadClipboardGlobal`/`SaveClipboardGlobal`, entries never
removed, only marked Pasted) — confirmed by LocalLibraryPaneViewModel.cs's own
doc comment (lines 23-27): "The Cut/Copy clipboard here is a small,
session-only field ... deliberately NOT the persisted BatchClipboard/
ClipboardEntry model in Core/BatchMoveModel.cs. That model exists for a
different, already-solved problem (a durable safety net for occupants
displaced by a batch placement) and stays exactly as-is; pasting still feeds
it via the same displacement path a PCG-pane copy already uses." That
persisted DTO is out of scope for this module (librarian_model.py's own
BatchMovePlan.displaced / DisplacedItem already cover its planning-side
half); this module owns only the UX-facing "what did the user Cut or Copy"
gesture state plan_batch_move's caller is expected to supply.

Confirmed semantics (LocalLibraryPaneViewModel.cs, read in full):

  * Persistence: SESSION-ONLY. `_clipItems`/`Mode` are plain fields on the
    view-model, never written to disk. Nothing like index.json/oplog.jsonl
    (local_library_store.py) or the BatchClipboard DTO backs this — closing
    the app loses it. This module mirrors that: BatchClipboard here is a
    plain in-memory object with no load()/save().

  * Cut vs. Copy mixing: STRUCTURALLY IMPOSSIBLE, not merely disallowed.
    `Cut()`/`Copy()` each unconditionally REPLACE `_clipItems` and set `Mode`
    fresh (lines 211-228) — there is no "add to clipboard" operation, so a
    clipboard can never hold both Cut and Copy items at once. cut()/copy()
    below reproduce this by replacing self._entries and self.mode on every
    call, never merging with what was there before.

  * Cut is capped at exactly ONE item (line 214: `if (locs.Count > 1) { ...
    CutOneAtATime; return; }`). The reason (line 201-205's comment): the only
    correct "move" this app can perform is a true, symmetric swap onto an
    ALREADY-OCCUPIED slot (LocalEditOps.Move, writing both directions) — there
    is no primitive that vacates a source slot, so a multi-item or
    move-to-empty Cut can never be completed correctly. Copy has no such cap.

  * Cut's paste is NOT a batch placement. PasteIntoSlot's single-item branch
    (`_clipItems.Count == 1`) routes Cut through PasteSingle -> LocalEditOps.
    Move -> Librarian.PlanMove (librarian_model.plan_move here): a true
    pairwise BODY SWAP (both slots' bytes exchanged, referrers on both sides
    repointed), and it REFUSES outright if the destination slot is empty
    (lines 296-304: "No move-to-empty here ... swap onto an occupied slot
    instead, or use Copy"). Copy's paste — single item OR many — always goes
    through PlaceObject/PasteBatch -> BatchPlacement(From: null, ...) ->
    BatchLibrarian.PlanBatchMove (librarian_model.plan_batch_move here): the
    source's own slot is never written; only referrers of a FRESH placement
    get resolved (there are none, since a fresh copy has no existing
    referrers pointing at the new slot yet). This module exposes that split
    as two methods: paste_swap_target() (Cut) and paste_targets() (Copy),
    matching two structurally different downstream planning calls — it does
    not (and per the task's own instruction, should not) re-implement either
    plan_move's swap math or plan_batch_move's placement math itself.

  * Staleness: NO content-hash/digest gate exists for this clipboard (unlike
    library_pull_pipeline.py's bank-digest staleness check, or
    librarian_model.py's arm_plan/apply_move digest baseline for a hardware
    move-in-progress). The only check LocalEditOps.Move/PlaceObject perform
    at paste time is a live EXISTENCE re-check: GetObjectDump(src) re-reads
    the cache fresh and returns null if the object was Discarded/Deleted
    since the Cut/Copy, at which point the paste is refused with "not found
    locally" (Move, lines 39-41) — the clipboard is NOT auto-cleared on that
    failure (FinishPaste only clears on success, line 271: `if (ok && cut)
    ClearClipboard();`), so a stale Cut just sits there until the user tries
    again or clears it manually. cut()/copy() below optionally capture a
    content hash (via an injected `dump_of`) purely as an inert diagnostic
    field on ClipboardEntry — paste_targets()/paste_swap_target() never
    consult it, exactly mirroring the confirmed absence of a content-based
    gate; only existence (dump_of(loc) is None) blocks a paste, exactly like
    the C# source.

  * Type mismatch: PasteIntoSlot/PasteIntoBank each refuse the WHOLE paste
    (not a per-item filter) if ANY clip item's ObjType differs from the
    destination's (lines 243, 261: `if (_clipItems.Any(l => l.ObjType !=
    dest.ObjType)) return (false, TypeMismatch);`). paste_targets()/
    paste_swap_target() below reproduce this as an all-or-nothing refusal.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, List, Optional, Tuple

from librarian_model import BatchPlacement, ObjLoc, SequentialFillItem, resolve_sequential_fill
from librarian_sysex import ObjectDump

DumpOf = Callable[[ObjLoc], Optional[ObjectDump]]
BankTypeOf = Callable[[int], Optional[bool]]


class ClipboardMode(Enum):
    """Port of LocalLibraryPaneViewModel.ClipboardMode (None, Cut, Copy)."""
    NONE = "none"
    CUT = "cut"
    COPY = "copy"


@dataclass(frozen=True)
class ClipboardEntry:
    """One clipboard slot — mirrors an element of `_clipItems: List<ObjLoc>`
    (there is no per-item C# DTO; `_clipItems` is a bare list of addresses).

    `content_hash` is a Python-side addition with no C# counterpart: a sha1
    hex digest of the object's body AT CUT/COPY TIME, captured only if a
    `dump_of` callable is supplied. It is never consulted by paste_targets()/
    paste_swap_target() (see module docstring's "Staleness" section for why
    that would NOT match the confirmed C# behavior) — it exists purely so a
    caller wiring up a UI can show an inert "may have changed since you cut
    it" hint if it wants to, without this module inventing a blocking gate
    the real app doesn't have.
    """
    loc: ObjLoc
    content_hash: str = ""


def _content_hash(dump_of: Optional[DumpOf], loc: ObjLoc) -> str:
    if dump_of is None:
        return ""
    dump = dump_of(loc)
    if dump is None:
        return ""
    return hashlib.sha1(dump.body).hexdigest()


class BatchClipboard:
    """Session-only Cut/Copy/Paste clipboard for the Local Library pane. Port
    of LocalLibraryPaneViewModel's `_clipItems`/`Mode` fields plus its
    Cut/Copy/ClearClipboard/PasteIntoSlot/PasteIntoBank methods — see module
    docstring for the confirmed semantics this reproduces.
    """

    def __init__(self) -> None:
        self._entries: List[ClipboardEntry] = []
        self.mode: ClipboardMode = ClipboardMode.NONE

    @property
    def is_empty(self) -> bool:
        """Port of `HasClipboard`'s negation: `Mode != None && _clipItems.Count > 0`."""
        return self.mode == ClipboardMode.NONE or not self._entries

    @property
    def entries(self) -> List[ClipboardEntry]:
        return list(self._entries)

    def label(self) -> str:
        """Port of `ClipboardLabel`."""
        if self.mode == ClipboardMode.CUT:
            return f"Cut: {len(self._entries)} item(s)"
        if self.mode == ClipboardMode.COPY:
            return f"Copy: {len(self._entries)} item(s)"
        return "(nothing cut or copied)"

    # -- Cut / Copy -----------------------------------------------------------

    def cut(self, locs: List[ObjLoc], dump_of: Optional[DumpOf] = None) -> Tuple[bool, str]:
        """Port of Cut(). Empty selection clears the clipboard (matches
        `if (locs.Count == 0) { ClearClipboard(); ...; return; }`); more than
        one item refuses outright (see module docstring)."""
        if not locs:
            self.clear()
            return False, "nothing to cut"
        if len(locs) > 1:
            return False, "cut one item at a time"
        loc = locs[0]
        self._entries = [ClipboardEntry(loc, _content_hash(dump_of, loc))]
        self.mode = ClipboardMode.CUT
        return True, f"Cut: {loc.label()}"

    def copy(self, locs: List[ObjLoc], dump_of: Optional[DumpOf] = None) -> Tuple[bool, str]:
        """Port of Copy(). No item-count cap, unlike cut()."""
        if not locs:
            return False, "nothing to copy"
        self._entries = [ClipboardEntry(l, _content_hash(dump_of, l)) for l in locs]
        self.mode = ClipboardMode.COPY
        if len(locs) == 1:
            return True, f"Copied: {locs[0].label()}"
        return True, f"Copied {len(locs)} item(s)"

    def clear(self) -> None:
        """Port of ClearClipboard()."""
        self._entries = []
        self.mode = ClipboardMode.NONE

    # -- Paste ------------------------------------------------------------

    def paste_targets(self, obj_type: int, dest_bank: int, start_slot: int,
                       dump_of: DumpOf, bank_type_of: Optional[BankTypeOf] = None,
                       ) -> Tuple[List[BatchPlacement], List[Tuple[ClipboardEntry, str]]]:
        """COPY-only. Port of PasteIntoBank (auto-fill from a start slot) and,
        for a single-entry clipboard, also of PasteIntoSlot's single-item
        Copy branch (PlaceObject) — both bottom out at the identical
        `BatchPlacement(From: null, ...)` shape once you are not doing a
        same-slot swap (see module docstring), and placing one item at
        `start_slot + 0` IS placing it exactly at `start_slot` — so this one
        method covers both real call sites, exactly like the C# source's own
        ResolveSequentialFill/PlanBatchMove pairing does.

        Composes with librarian_model.resolve_sequential_fill (placement
        arithmetic — this module does no slot-numbering math of its own) and
        hands its result straight through as `BatchPlacement`s ready for
        `plan_batch_move(catalog, obj_type, placements, dest_occupants, ...)`.

        Refuses the WHOLE paste (empty `placed`) if the clipboard is not in
        Copy mode, or if ANY entry's object type differs from `obj_type` —
        both are all-or-nothing checks in the C# source (see module
        docstring's "Type mismatch" note), not a per-item filter.

        `dump_of` re-resolves each entry's CURRENT body fresh (the only
        staleness check the C# source actually performs, see module
        docstring): an entry `dump_of` can no longer find (discarded/deleted
        since the Copy) is dropped into the returned pending list with a
        "no longer available locally" reason instead of being placed.
        """
        if self.mode != ClipboardMode.COPY:
            return [], [(e, "clipboard is not in Copy mode — Cut pastes via "
                            "paste_swap_target()") for e in self._entries]
        if any(e.loc.obj_type != obj_type for e in self._entries):
            return [], [(e, "type mismatch — clipboard holds an entry of a "
                            "different object type than the paste destination")
                        for e in self._entries]

        items: List[SequentialFillItem] = []
        pending: List[Tuple[ClipboardEntry, str]] = []
        entry_by_origin = {e.loc: e for e in self._entries}
        for e in self._entries:
            dump = dump_of(e.loc)
            if dump is None:
                pending.append((e, f"{e.loc.label()} is no longer available locally"))
                continue
            items.append(SequentialFillItem(e.loc, dump, e.loc.label()))

        placed, still_pending = resolve_sequential_fill(items, obj_type, dest_bank,
                                                        start_slot, bank_type_of)
        for item, reason in still_pending:
            pending.append((entry_by_origin[item.origin], reason))
        return placed, pending

    def paste_swap_target(self, dest: ObjLoc, dump_of: DumpOf,
                          ) -> Tuple[Optional[ObjLoc], Optional[str]]:
        """CUT-only. Port of PasteSingle's Cut branch (LocalEditOps.Move):
        a Cut clipboard always holds exactly one item (enforced by cut()).
        Returns `(src, None)` on success — the caller feeds
        `plan_move(catalog, src, src_dump, dest, dest_dump)` (a TRUE body
        swap, both directions written; this is why Cut cannot go through
        paste_targets()/plan_batch_move at all, see module docstring) — or
        `(None, reason)` on refusal. Does NOT clear the clipboard itself
        (mirrors `FinishPaste`, which clears only after the caller's actual
        apply succeeds) — call clear() yourself once the swap is applied.

        Refuses if: not in Cut mode; the clip item's type differs from
        dest's; source == dest; the source is no longer resolvable locally
        (discarded/deleted since the Cut — the confirmed staleness check,
        see module docstring); or the destination is empty (no primitive
        vacates a source slot, so Cut can only land on an occupied slot).
        """
        if self.mode != ClipboardMode.CUT:
            return None, "paste_swap_target() is Cut-only — Copy pastes via paste_targets()"
        if not self._entries:
            return None, "nothing cut"
        src = self._entries[0].loc
        if src.obj_type != dest.obj_type:
            return None, ("type mismatch — clipboard holds an entry of a different "
                          "object type than the paste destination")
        if src == dest:
            return None, "source and destination are the same location"
        if dump_of(src) is None:
            return None, f"{src.label()} is no longer available locally — Pull first"
        if dump_of(dest) is None:
            return None, (f"cannot cut-paste onto an empty slot ({dest.label()}) — there is "
                          "no way to vacate the source; swap onto an occupied slot, or use Copy")
        return src, None


# ── Self-test (python batch_clipboard.py) ───────────────────────────────────


def _selftest() -> None:
    import sys
    from librarian_sysex import OBJ_COMBI, OBJ_PROGRAM

    fails: List[str] = []

    def check(name, cond):
        if not cond:
            fails.append(name)

    def dump(obj_type, bank, number, tag=b"\x00"):
        return ObjectDump(obj_type, bank, number, 1, tag * 16)

    # A tiny in-memory "cache": locs present in this dict resolve; absent = gone.
    store = {
        ObjLoc(OBJ_COMBI, 0x00, 1): dump(OBJ_COMBI, 0x00, 1, b"\x01"),
        ObjLoc(OBJ_COMBI, 0x00, 2): dump(OBJ_COMBI, 0x00, 2, b"\x02"),
        ObjLoc(OBJ_COMBI, 0x00, 3): dump(OBJ_COMBI, 0x00, 3, b"\x03"),
        ObjLoc(OBJ_COMBI, 0x40, 5): dump(OBJ_COMBI, 0x40, 5, b"\xAA"),   # occupied dest for a swap
        ObjLoc(OBJ_PROGRAM, 0x00, 9): dump(OBJ_PROGRAM, 0x00, 9, b"\x09"),
    }
    dump_of = lambda loc: store.get(loc)

    a = ObjLoc(OBJ_COMBI, 0x00, 1)
    b = ObjLoc(OBJ_COMBI, 0x00, 2)
    c = ObjLoc(OBJ_COMBI, 0x00, 3)

    # ── cut(): single-item cap, empty clears, mode set ──
    clip = BatchClipboard()
    check("initially-empty", clip.is_empty and clip.mode == ClipboardMode.NONE)

    ok, msg = clip.cut([a, b])
    check("cut-multi-refused", not ok and "one item at a time" in msg and clip.is_empty)

    ok, msg = clip.cut([a], dump_of=dump_of)
    check("cut-single-ok", ok and clip.mode == ClipboardMode.CUT
          and [e.loc for e in clip.entries] == [a])
    check("cut-captured-hash", clip.entries[0].content_hash != "")

    ok, msg = clip.cut([])
    check("cut-empty-clears", not ok and clip.is_empty and clip.mode == ClipboardMode.NONE)

    # ── copy(): no cap, and copy()/cut() REPLACE (mixing is structurally impossible) ──
    clip.cut([a], dump_of=dump_of)
    check("precondition-cut-a", clip.mode == ClipboardMode.CUT and [e.loc for e in clip.entries] == [a])

    ok, msg = clip.copy([b, c], dump_of=dump_of)
    check("copy-replaces-cut",
          ok and clip.mode == ClipboardMode.COPY
          and [e.loc for e in clip.entries] == [b, c]
          and a not in [e.loc for e in clip.entries])   # no trace of the earlier Cut item

    ok, msg = clip.copy([])
    check("copy-empty-refused", not ok and clip.mode == ClipboardMode.COPY)   # unlike cut([]), copy([]) does NOT clear

    # ── paste_targets(): Copy mode, multi-item sequential fill ──
    clip.copy([b, c], dump_of=dump_of)
    placed, pending = clip.paste_targets(OBJ_COMBI, 0x40, 0, dump_of)
    check("paste-targets-count", len(placed) == 2 and len(pending) == 0)
    check("paste-targets-slots",
          [p.dst for p in placed] == [ObjLoc(OBJ_COMBI, 0x40, 0), ObjLoc(OBJ_COMBI, 0x40, 1)])
    check("paste-targets-src-none-always", all(p.src is None for p in placed))   # Copy never repoints origin

    # ── paste_targets(): single-item Copy lands exactly at start_slot (== PasteSingle's dest) ──
    clip.copy([a], dump_of=dump_of)
    placed1, pending1 = clip.paste_targets(OBJ_COMBI, 0x40, 7, dump_of)
    check("paste-single-copy-exact-slot",
          len(placed1) == 1 and placed1[0].dst == ObjLoc(OBJ_COMBI, 0x40, 7) and placed1[0].src is None)

    # ── paste_targets(): refused when clipboard is in Cut mode ──
    clip.cut([a], dump_of=dump_of)
    placed_c, pending_c = clip.paste_targets(OBJ_COMBI, 0x40, 0, dump_of)
    check("paste-targets-cut-mode-refused",
          len(placed_c) == 0 and len(pending_c) == 1 and "Cut" in pending_c[0][1])

    # ── paste_targets(): type mismatch refuses the WHOLE paste ──
    clip.copy([a], dump_of=dump_of)
    placed_t, pending_t = clip.paste_targets(OBJ_PROGRAM, 0x40, 0, dump_of)
    check("paste-targets-type-mismatch",
          len(placed_t) == 0 and len(pending_t) == 1 and "type mismatch" in pending_t[0][1])

    # ── paste_targets(): staleness = existence only (no content-hash gate) ──
    missing = ObjLoc(OBJ_COMBI, 0x00, 99)   # never in `store`
    clip.copy([b, missing], dump_of=dump_of)
    placed_s, pending_s = clip.paste_targets(OBJ_COMBI, 0x40, 0, dump_of)
    check("paste-targets-stale-dropped",
          len(placed_s) == 1 and placed_s[0].dst == ObjLoc(OBJ_COMBI, 0x40, 0)
          and len(pending_s) == 1 and pending_s[0][0].loc == missing
          and "no longer available locally" in pending_s[0][1])
    check("paste-targets-not-auto-cleared", clip.mode == ClipboardMode.COPY)   # a failed/partial paste never auto-clears

    # ── paste_swap_target(): Cut onto an occupied slot succeeds ──
    dest_occupied = ObjLoc(OBJ_COMBI, 0x40, 5)
    clip.cut([a], dump_of=dump_of)
    src, err = clip.paste_swap_target(dest_occupied, dump_of)
    check("swap-target-ok", src == a and err is None)

    # ── paste_swap_target(): Cut onto an EMPTY slot refuses (no vacate primitive) ──
    dest_empty = ObjLoc(OBJ_COMBI, 0x40, 6)   # not in `store`
    clip.cut([a], dump_of=dump_of)
    src2, err2 = clip.paste_swap_target(dest_empty, dump_of)
    check("swap-target-empty-refused", src2 is None and "vacate" in (err2 or ""))

    # ── paste_swap_target(): staleness — source deleted after Cut, before Paste ──
    volatile = ObjLoc(OBJ_COMBI, 0x00, 55)
    store[volatile] = dump(OBJ_COMBI, 0x00, 55, b"\x55")
    clip.cut([volatile], dump_of=dump_of)
    del store[volatile]   # simulate a Discard/Delete of the cut item before Paste
    src3, err3 = clip.paste_swap_target(dest_occupied, dump_of)
    check("swap-target-stale-source-refused",
          src3 is None and "no longer available locally" in (err3 or ""))
    check("swap-target-stale-does-not-clear-clipboard", clip.mode == ClipboardMode.CUT)

    # ── paste_swap_target(): Copy mode refuses (Copy never uses the swap path) ──
    clip.copy([a], dump_of=dump_of)
    src4, err4 = clip.paste_swap_target(dest_occupied, dump_of)
    check("swap-target-copy-mode-refused", src4 is None and "Cut-only" in (err4 or ""))

    # ── paste_swap_target(): same source and destination refuses ──
    clip.cut([a], dump_of=dump_of)
    src5, err5 = clip.paste_swap_target(a, dump_of)
    check("swap-target-same-location-refused", src5 is None and "same location" in (err5 or ""))

    # ── paste_swap_target(): type mismatch refuses ──
    clip.cut([a], dump_of=dump_of)
    prog_dest = ObjLoc(OBJ_PROGRAM, 0x00, 9)
    src6, err6 = clip.paste_swap_target(prog_dest, dump_of)
    check("swap-target-type-mismatch-refused", src6 is None and "type mismatch" in (err6 or ""))

    # ── clear() resets both entries and mode ──
    clip.copy([b, c], dump_of=dump_of)
    clip.clear()
    check("clear-resets", clip.is_empty and clip.mode == ClipboardMode.NONE and clip.entries == [])

    if fails:
        print("FAIL:", ", ".join(fails))
        sys.exit(1)
    print("batch_clipboard self-test: OK")


if __name__ == "__main__":
    _selftest()
