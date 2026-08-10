r"""
Dependency scanner — walks a Combi/Set List body's own outgoing references and
reports which of them a caller-supplied resolver can't find.

Port of Core/LocalLibrary/DependencyScanner.cs's ObjectReferenceWalker.Walk +
WalkResolvable + IsAlwaysAvailable + Scan + HasAllDependencies. This module
only answers "what does this body reference" and "which of those references
are missing/always-available", independent of what "resolves" a reference —
the placement-time byte-patching that consumes walk_resolvable_references
lives in merge_cache.MergeCache.resolve_references_for_placement (the
Merge-Window-specific "search Local Library by content hash and repoint"
mechanism, port of MergeCache.ResolveReferencesForPlacement).

The resolver is deliberately NOT one hardcoded source: pass any plain
callable `has(obj_type, bank, number) -> bool`, backed by
local_library_store.LocalLibraryIndex, a loaded pcg_file PCG, the merge
cache, a librarian_model.LibraryCatalog, or a plain dict/set in a test — this
module never imports any of them, so it composes with all three later.

Reference-site layout (Combi timbre bytes, Set List slot bytes) is decoded
via librarian_sysex.iter_combi_timbre_refs/iter_setlist_slot_refs — the same
single source of truth librarian_model.py's LibraryCatalog/RefIndex already
use — nothing here reimplements that byte-level extraction.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterator, List

from librarian_model import ObjLoc, _READONLY_PROGRAM_BANKS
from librarian_sysex import (
    OBJ_COMBI, OBJ_PROGRAM, OBJ_SET_LIST,
    func33_to_obj_bank, iter_combi_timbre_refs, iter_setlist_slot_refs,
)

try:  # mirror the same defensive import librarian_sysex.py already does
    from setlist_data import _NAME_LEN, _SLOT_BASE, _SLOT_SIZE
except Exception:  # pragma: no cover - defensive
    _NAME_LEN, _SLOT_BASE, _SLOT_SIZE = 24, 24, 542

Resolver = Callable[[int, int, int], bool]


@dataclass(frozen=True)
class ObjectRef:
    """One outgoing reference site inside a Combi/Set List body."""
    ref_kind: str    # "timbre N" / "slot N" (1-based site label, matches C# RefKind)
    site: int        # 0-based timbre/slot index
    ref: ObjLoc      # what it points at


def _setlist_slot_is_blank(body: bytes, slot: int) -> bool:
    """A slot's blank NAME (not a zero-valued bank/index) is what marks it
    unused — every unused slot's default zero-valued reference would
    otherwise look like a real (and near-always already-satisfied,
    misleadingly so) dependency on Program/Combi bank 0 slot 0. Mirrors
    SetListSlot.IsEmpty via SetListBody.FromRawBody in the C# source."""
    base = _SLOT_BASE + slot * _SLOT_SIZE
    name = bytes(body[base:base + _NAME_LEN])
    return not name.strip(b"\x00 \t")


def walk_object_references(obj_type: int, body: bytes) -> Iterator[ObjectRef]:
    """Yield every outgoing reference a Combi's timbres or a Set List's slots
    carry. Port of ObjectReferenceWalker.Walk. An INIT/placeholder object
    references nothing meaningful (its timbres/slots still hold the zero
    default, which encodes "nothing assigned" — not a dependency on Program
    I-A:000); blank Set List slots are skipped (see _setlist_slot_is_blank);
    Song refs (slot type 2) are out of scope, same as the C# source's own
    note. Any other object type yields nothing."""
    if obj_type in (OBJ_COMBI, OBJ_SET_LIST):
        from object_body import combi_body_is_init, setlist_body_is_init
        is_init = combi_body_is_init if obj_type == OBJ_COMBI else setlist_body_is_init
        if is_init(body):
            return

    if obj_type == OBJ_COMBI:
        for t, fbank, num in iter_combi_timbre_refs(body):
            obj_bank = func33_to_obj_bank(1, fbank)   # combi timbres always reference Programs
            if obj_bank < 0:
                continue
            yield ObjectRef(f"timbre {t + 1}", t, ObjLoc(OBJ_PROGRAM, obj_bank, num))
        return

    if obj_type != OBJ_SET_LIST:
        return

    for s, slot_type, fbank, idx in iter_setlist_slot_refs(body):
        if slot_type == 2:   # Song ref — out of scope, same as ObjectReferenceWalker.Walk
            continue
        if _setlist_slot_is_blank(body, s):
            continue
        obj_bank = func33_to_obj_bank(slot_type, fbank)   # slot_type: 0=combi,1=program
        if obj_bank < 0:
            continue
        ref_obj_type = OBJ_PROGRAM if slot_type == 1 else OBJ_COMBI
        yield ObjectRef(f"slot {s + 1}", s, ObjLoc(ref_obj_type, obj_bank, idx))


def is_always_available(ref: ObjLoc) -> bool:
    """A reference that can NEVER be missing, because its target isn't part of
    the library at all: the read-only ROM Program banks (GM, g(1)..g(d)).
    Those banks are factory content burned into the instrument — Local
    Library never fetches them, no .pcg file carries them, nothing can ever
    place one — so a Combi timbre or Set List slot pointing at GM:012
    always resolves ON THE INSTRUMENT, however empty the local library is.
    Port of ObjectReferenceWalker.IsAlwaysAvailable — deliberately a
    CLASSIFIER, not a filter inside walk_object_references: display paths
    still want to SHOW these references, they just must never be treated as
    something to resolve, pull, repoint, or block on. walk_resolvable_
    references below is the filtered view every resolution path wants."""
    return ref.obj_type == OBJ_PROGRAM and ref.bank in _READONLY_PROGRAM_BANKS


def walk_resolvable_references(obj_type: int, body: bytes) -> Iterator[ObjectRef]:
    """walk_object_references() minus references nothing can ever resolve
    because they don't need resolving — the shape every RESOLUTION path
    wants (MergeCache.pull_recursive, DependencyScanner.scan/
    has_all_dependencies). Port of ObjectReferenceWalker.WalkResolvable."""
    return (r for r in walk_object_references(obj_type, body) if not is_always_available(r.ref))


def scan(resolver: Resolver, obj_type: int, body: bytes) -> List[ObjectRef]:
    """References `body` makes that `resolver` reports as NOT present. Port
    of DependencyScanner.Scan (its `cache.GetCurrentBody(...) == null` check
    generalized to a plain boolean `resolver` call so it composes with any
    lookup, not just LocalLibraryCache). Walks WalkResolvable, not Walk: a
    reference into a read-only ROM Program bank (GM/g) always resolves ON THE
    INSTRUMENT however empty the local library is, so it must never be
    reported as missing — see is_always_available."""
    return [r for r in walk_resolvable_references(obj_type, body)
            if not resolver(r.ref.obj_type, r.ref.bank, r.ref.number)]


def has_all_dependencies(resolver: Resolver, obj_type: int, body: bytes) -> bool:
    """True if every reference `body` makes resolves via `resolver`. Port of
    DependencyScanner.HasAllDependencies — an index-only existence check
    (the caller's resolver should itself be index-only/cheap; this function
    doesn't care either way). Same WalkResolvable scoping as scan() above."""
    return all(resolver(r.ref.obj_type, r.ref.bank, r.ref.number)
               for r in walk_resolvable_references(obj_type, body))


# ── Self-test (python dependency_scanner.py) ────────────────────────────────


def _selftest() -> None:
    import sys

    fails: List[str] = []

    def check(name: str, cond: bool) -> None:
        if not cond:
            fails.append(name)

    from librarian_sysex import set_combi_timbre_ref, set_setlist_slot_ref

    # ── Combi timbre refs walked + checked against a fake resolver ──
    fb0 = func33_to_obj_bank(1, 0)     # I-A program bank (func33 bank 0)
    fb_u = func33_to_obj_bank(1, 18)   # U-A program bank (func33 bank 18)
    combi = bytearray(7810)
    set_combi_timbre_ref(combi, 0, 0, 7)     # -> Program I-A:007 (present)
    set_combi_timbre_ref(combi, 1, 18, 3)    # -> Program U-A:003 (missing)
    refs = list(walk_object_references(OBJ_COMBI, bytes(combi)))
    check("combi-ref-count", len(refs) == 16)
    check("combi-ref-kind", refs[0].ref_kind == "timbre 1" and refs[1].ref_kind == "timbre 2")
    check("combi-ref-loc0", refs[0].ref == ObjLoc(OBJ_PROGRAM, fb0, 7))
    check("combi-ref-loc1", refs[1].ref == ObjLoc(OBJ_PROGRAM, fb_u, 3))

    present = {(OBJ_PROGRAM, fb0, 7)}
    resolver: Resolver = lambda t, b, n: (t, b, n) in present
    missing = scan(resolver, OBJ_COMBI, bytes(combi))
    check("combi-scan-missing-count", len(missing) == 15)
    check("combi-scan-missing-has-timbre2",
          any(m.ref_kind == "timbre 2" and m.ref == ObjLoc(OBJ_PROGRAM, fb_u, 3) for m in missing))
    check("combi-scan-timbre1-not-missing",
          not any(m.ref_kind == "timbre 1" for m in missing))
    check("combi-has-all-deps-false", not has_all_dependencies(resolver, OBJ_COMBI, bytes(combi)))

    # A resolver that has everything -> no missing refs, has_all_dependencies True.
    full_resolver: Resolver = lambda t, b, n: True
    check("combi-scan-empty-when-resolved", scan(full_resolver, OBJ_COMBI, bytes(combi)) == [])
    check("combi-has-all-deps-true", has_all_dependencies(full_resolver, OBJ_COMBI, bytes(combi)))

    # ── A timbre pointing at a read-only ROM Program bank (GM) is always-available:
    #    walk_resolvable_references drops it, and scan/has_all_dependencies must never
    #    report it as missing even against a resolver that knows nothing (mirrors the C#
    #    "GM references are extremely common... must never block a push" rationale). ──
    combi_gm = bytearray(7810)
    set_combi_timbre_ref(combi_gm, 2, 7, 12)   # func33 bank 7 = GM (see obj_bank_to_func33)
    gm_ref = next(r for r in walk_object_references(OBJ_COMBI, bytes(combi_gm)) if r.site == 2)
    check("gm-ref-is-always-available", is_always_available(gm_ref.ref))
    check("gm-ref-not-in-walk-resolvable",
          not any(r.site == 2 for r in walk_resolvable_references(OBJ_COMBI, bytes(combi_gm))))
    nothing_resolver: Resolver = lambda t, b, n: False
    check("gm-ref-never-reported-missing",
          not any(m.site == 2 for m in scan(nothing_resolver, OBJ_COMBI, bytes(combi_gm))))
    # timbre 0 still defaults to I-A:000 (a real, resolvable address) and IS missing here.
    check("gm-scan-other-timbres-still-checked",
          any(m.site == 0 for m in scan(nothing_resolver, OBJ_COMBI, bytes(combi_gm))))

    # ── Set List slot refs walked + checked, blank slots skipped ──
    sl = bytearray(69416)
    # slot 0: named, references Program I-A:005 (present)
    base0 = _SLOT_BASE + 0 * _SLOT_SIZE
    sl[base0:base0 + 4] = b"ONE "
    set_setlist_slot_ref(sl, 0, func33_bank=0, index=5, type_=1)
    # slot 1: named, references Combi U-A:002 (missing)
    base1 = _SLOT_BASE + 1 * _SLOT_SIZE
    fb_combi_u = func33_to_obj_bank(0, 7)   # U-A combi bank (func33 bank 7)
    sl[base1:base1 + 4] = b"TWO "
    set_setlist_slot_ref(sl, 1, func33_bank=7, index=2, type_=0)
    # slot 2: left blank (default zero bytes, no name) -> must be skipped entirely, even though
    # its raw bytes decode as type=0/bank=0/index=0 (which would otherwise look like a real,
    # already-resolvable reference to Combi I-A:000).
    sl_refs = list(walk_object_references(OBJ_SET_LIST, bytes(sl)))
    check("setlist-ref-count", len(sl_refs) == 2)
    check("setlist-blank-slot-skipped", not any(r.site == 2 for r in sl_refs))
    check("setlist-ref-kinds", {r.ref_kind for r in sl_refs} == {"slot 1", "slot 2"})
    slot0_ref = next(r for r in sl_refs if r.site == 0)
    slot1_ref = next(r for r in sl_refs if r.site == 1)
    check("setlist-ref0", slot0_ref.ref == ObjLoc(OBJ_PROGRAM, fb0, 5))
    check("setlist-ref1", slot1_ref.ref == ObjLoc(OBJ_COMBI, fb_combi_u, 2))

    sl_present = {(OBJ_PROGRAM, fb0, 5)}
    sl_resolver: Resolver = lambda t, b, n: (t, b, n) in sl_present
    sl_missing = scan(sl_resolver, OBJ_SET_LIST, bytes(sl))
    check("setlist-scan-missing-count", len(sl_missing) == 1)
    check("setlist-scan-missing-is-slot2",
          sl_missing[0].ref_kind == "slot 2" and sl_missing[0].ref == ObjLoc(OBJ_COMBI, fb_combi_u, 2))
    check("setlist-has-all-deps-false", not has_all_dependencies(sl_resolver, OBJ_SET_LIST, bytes(sl)))

    # Unsupported object type -> no references, vacuously satisfied.
    check("unsupported-objtype-empty", list(walk_object_references(0x02, bytes(100))) == [])
    check("unsupported-objtype-has-all-deps", has_all_dependencies(resolver, 0x02, bytes(100)))

    if fails:
        print("FAIL:", ", ".join(fails))
        sys.exit(1)
    print("dependency_scanner self-test: OK")


if __name__ == "__main__":
    _selftest()
