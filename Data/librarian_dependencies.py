r"""
The Librarian's read-only dependency VIEWS — the "Object Dependencies" panel rows and the
"More Info…" popup text. Port of Views/LibrarianShellViewModel.Dependencies.cs (the Local / PCG /
Merge collectors, the staged-gap rows, the per-row child descriptions). Nothing here mutates the
library; it all answers "what does this object need, and is that need met here?".

Pure logic on injected lookups (no Qt, no globals), so it is unit-testable and the window only
supplies callables:

  LocalSource   — Keyboard Library:   walk(loc) / available(loc) / display_name(loc)
  PcgSource     — a loaded PCG:        get(loc) -> (name, wire_body) | None
  MergeSource   — the Merge Window:    try_get(hash) / unresolved_sites()

Differences from C# (deliberate, documented):
  * Programs' Drum Kit / Wave Sequence oscillator-zone references are not shown — Python's library
    has no Drum Kit / Wave Sequence object type yet (CLAUDE.md follow-up).
  * Sample-bank NAME resolution works; C#'s never matches because its row key carries a 'Bucket|'
    prefix its own lookups don't strip (user decision 2026-10-02: port the intended behavior).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from Data.librarian_model import ObjLoc
from Data.librarian_sysex import OBJ_COMBI, OBJ_PROGRAM, OBJ_SET_LIST
import Tools.dependency_scanner as depscan
import Tools.sample_reference_walker as samples

ROM_ALWAYS_AVAILABLE = "ROM bank, always available on the instrument"
INIT_PLACEHOLDER_SUFFIX = "(INIT placeholder)"
NOT_STAGED_CHILDREN = "Not staged - what this object itself references can't be known until it's found."

from Data.object_types import REGISTRY as _OBJECT_TYPES
_TYPE_NAMES = {t: d.display_name for t, d in _OBJECT_TYPES.items()}


def type_name(obj_type: int) -> str:
    return _TYPE_NAMES.get(obj_type, f"Object {obj_type:02X}")


@dataclass
class DependencyRow:
    """One row of the panel. `describe_children` is LAZY (a fresh re-walk when "More Info…" is
    opened), so it reflects edits made since the panel was populated and building a long panel
    never pays for detail nobody asks to see."""
    description: str
    parent_info: str = ""
    describe_children: Callable[[], List[str]] = field(default=lambda: [])
    sample_bucket: Optional[str] = None
    missing_ref: Optional[ObjLoc] = None

    @property
    def is_missing(self) -> bool:
        return self.missing_ref is not None


@dataclass(frozen=True)
class MissingDependency:
    """One reference site with nothing local behind it. `site` is carried (not just the kind)
    because resolving it later means patching THAT byte site inside `required_by`."""
    missing: ObjLoc
    ref_kind: str
    site: int
    required_by: ObjLoc


# ── lookups the window supplies ───────────────────────────────────────────────────────────────

@dataclass
class LocalSource:
    walk: Callable[[ObjLoc], Optional[Tuple[List[depscan.ObjectRef], List[samples.SampleDependencyRow]]]]
    available: Callable[[ObjLoc], bool]          # present AND not pending-delete
    display_name: Callable[[ObjLoc], str]
    exs_index: Optional[object] = None           # Tools.exs_option_index.ExsOptionIndex | None


@dataclass
class PcgSource:
    get: Callable[[ObjLoc], Optional[Tuple[str, bytes]]]   # (name, wire body)


@dataclass
class MergeSource:
    try_get: Callable[[str], Optional[object]]             # content_hash -> MergeEntry
    unresolved_sites: Callable[[], Iterable[object]]       # every MergeRefSite with no resolved hash


# ── text helpers ──────────────────────────────────────────────────────────────────────────────

def _name_or_unnamed(name: str) -> str:
    return name if name else "(unnamed)"


def describe_rom_dependency(loc: ObjLoc) -> str:
    return f"{type_name(loc.obj_type)}: {loc.label()} - {ROM_ALWAYS_AVAILABLE}"


def describe_dependency(loc: ObjLoc, name: str, found: bool, where_missing: str) -> str:
    if found:
        return f"{type_name(loc.obj_type)}: {loc.label()} - {_name_or_unnamed(name)}"
    return f"{type_name(loc.obj_type)}: {loc.label()} - not found {where_missing}"


def describe_parent(parent: ObjLoc, parent_name: str, via: str) -> str:
    return f"{type_name(parent.obj_type)}: {parent.label()} - {_name_or_unnamed(parent_name)} (via {via})"


def resolved_from_local_library(where_missing: str) -> str:
    return f"(not {where_missing}; already in your Keyboard Library at this address)"


def gap_row_text(type_nm: str, label: str, name: str, count: int) -> str:
    return (f"{type_nm} {label}{f'  “{name}”' if name else ''} - "
            f"needed by {count} object{'' if count == 1 else 's'}")


def program_name_is_init(name: str) -> bool:
    from Objects.object_body import program_name_is_init as f
    return f(name)


def resolve_sample_description(row: samples.SampleDependencyRow, exs_index) -> str:
    """Appends a friendly bank name from the EXs catalog when one is known. Never changes the
    row's bucket/colour (fixed at classification time from the PCG bytes alone). The catalog
    identifies a PRODUCT; a hit is not proof the pack is installed on the instrument."""
    if exs_index is None:
        return row.description
    bare = row.key.split("|", 1)[1] if "|" in row.key else row.key
    name = None
    if row.bucket == samples.EXS and bare.startswith("exs") and bare[3:].isdigit():
        name = exs_index.name_for_exs_number(int(bare[3:]))
    elif row.bucket == samples.USER:
        name = exs_index.name_for_uuid_hex(bare)
    return row.description if name is None else f"{row.description} - {name}"


def _add_sample_rows(walked: List[samples.SampleDependencyRow], parent_info: str, sample_seen: Set[str],
                     rows: List[DependencyRow], exs_index) -> None:
    """Display-only: a sample bank can never be found/resolved locally, so no recursion, no
    'missing' tracking — one flat row per distinct bank, deduped across one panel population."""
    for r in walked:
        if r.key in sample_seen:
            continue
        sample_seen.add(r.key)
        rows.append(DependencyRow(resolve_sample_description(r, exs_index), parent_info, sample_bucket=r.bucket))


# ── Keyboard Library ──────────────────────────────────────────────────────────────────────────

def collect_local_deps(loc: ObjLoc, src: LocalSource, seen: Set[ObjLoc], sample_seen: Set[str],
                       rows: List[DependencyRow],
                       missing: Optional[List[MissingDependency]] = None) -> None:
    walk = src.walk(loc)
    if walk is None:
        return
    refs, sample_rows = walk
    parent_name = src.display_name(loc)
    _add_sample_rows(sample_rows, describe_parent(loc, parent_name, "sample"), sample_seen, rows, src.exs_index)
    for r in refs:
        ref_loc = r.ref
        if ref_loc in seen:
            continue
        seen.add(ref_loc)
        parent_info = describe_parent(loc, parent_name, r.ref_kind)
        # A ROM (GM/g) reference is shown but never as missing — it resolves on the instrument
        # whatever the library holds, and nothing can be pulled or placed to "fix" it.
        if depscan.is_always_available(ref_loc):
            rows.append(DependencyRow(describe_rom_dependency(ref_loc), parent_info))
            continue
        found = src.available(ref_loc)
        name = src.display_name(ref_loc) if found else ""
        if found and ref_loc.obj_type == OBJ_PROGRAM and program_name_is_init(name):
            # Satisfies the reference technically but is a placeholder, not the sound the
            # referrer expects.
            desc = f"{type_name(ref_loc.obj_type)}: {ref_loc.label()} - {name} {INIT_PLACEHOLDER_SUFFIX}"
        else:
            desc = describe_dependency(ref_loc, name, found, "locally")
        rows.append(DependencyRow(
            desc, parent_info,
            # "(references nothing)" would be the wrong answer for a missing object: nothing local
            # HAS it, so what it references is unknowable, not empty.
            (lambda rl=ref_loc: describe_local_children(rl, src)) if found
            else (lambda: [NOT_STAGED_CHILDREN]),
            missing_ref=None if found else ref_loc))
        if not found and missing is not None:
            missing.append(MissingDependency(ref_loc, r.ref_kind, r.site, loc))
        if found:
            collect_local_deps(ref_loc, src, seen, sample_seen, rows, missing)


def describe_local_children(loc: ObjLoc, src: LocalSource) -> List[str]:
    """One level of a LOCAL object's own outgoing references, each annotated with its site — the
    'More Info' popup's References section. Not the transitive walk the panel itself does."""
    walk = src.walk(loc)
    if walk is None:
        return []
    refs, sample_rows = walk
    lines = [resolve_sample_description(r, src.exs_index) for r in sample_rows]
    for r in refs:
        found = src.available(r.ref)
        if depscan.is_always_available(r.ref):
            desc = describe_rom_dependency(r.ref)
        else:
            desc = describe_dependency(r.ref, src.display_name(r.ref) if found else "", found, "locally")
        lines.append(f"{desc} (via {r.ref_kind})")
    return lines


def describe_gap_or_local(ref_loc: ObjLoc, parent_info: str, where_missing: str, src: LocalSource) -> DependencyRow:
    """A reference the loaded PCG / Merge Window can't satisfy — but the reference is an ADDRESS, so
    if Keyboard Library already holds something there the Kronos resolves it on load and there is
    nothing to go find: an ordinary row naming what's there. Only an uncovered address is a gap."""
    if src.available(ref_loc):
        local_name = src.display_name(ref_loc)
        return DependencyRow(
            f"{type_name(ref_loc.obj_type)}: {ref_loc.label()} - {_name_or_unnamed(local_name)} "
            f"{resolved_from_local_library(where_missing)}",
            parent_info, lambda: describe_local_children(ref_loc, src))
    return DependencyRow(describe_dependency(ref_loc, "", False, where_missing), parent_info,
                         lambda: [NOT_STAGED_CHILDREN], missing_ref=ref_loc)


# ── Loaded PCG ────────────────────────────────────────────────────────────────────────────────

def collect_pcg_deps(loc: ObjLoc, pcg: PcgSource, src: LocalSource, seen: Set[ObjLoc],
                     sample_seen: Set[str], rows: List[DependencyRow]) -> None:
    entry = pcg.get(loc)
    if entry is None:
        return
    name, body = entry
    _add_sample_rows(samples.walk(loc.obj_type, body), describe_parent(loc, name, "sample"),
                     sample_seen, rows, src.exs_index)
    for r in depscan.walk_display_references(loc.obj_type, body):
        ref_loc = r.ref
        if ref_loc in seen:
            continue
        seen.add(ref_loc)
        parent_info = describe_parent(loc, name, r.ref_kind)
        if depscan.is_always_available(ref_loc):
            rows.append(DependencyRow(describe_rom_dependency(ref_loc), parent_info))
            continue
        dep = pcg.get(ref_loc)
        if dep is not None:
            rows.append(DependencyRow(describe_dependency(ref_loc, dep[0], True, "in this PCG"),
                                      parent_info, lambda rl=ref_loc: describe_pcg_children(rl, pcg, src)))
            collect_pcg_deps(ref_loc, pcg, src, seen, sample_seen, rows)
            continue
        # Absent from the PCG is NOT the same as missing — see describe_gap_or_local.
        rows.append(describe_gap_or_local(ref_loc, parent_info, "in this PCG", src))


def describe_pcg_children(loc: ObjLoc, pcg: PcgSource, src: LocalSource) -> List[str]:
    entry = pcg.get(loc)
    if entry is None:
        return []
    _name, body = entry
    lines = [resolve_sample_description(r, src.exs_index) for r in samples.walk(loc.obj_type, body)]
    for r in depscan.walk_display_references(loc.obj_type, body):
        dep = pcg.get(r.ref)
        desc = (describe_rom_dependency(r.ref) if depscan.is_always_available(r.ref)
                else describe_dependency(r.ref, dep[0] if dep else "", dep is not None, "in this PCG"))
        lines.append(f"{desc} (via {r.ref_kind})")
    return lines


# ── Merge Window ──────────────────────────────────────────────────────────────────────────────

def collect_merge_deps(entry, merge: MergeSource, src: LocalSource, seen: Set[str],
                       sample_seen: Set[str], rows: List[DependencyRow]) -> None:
    """Merge entries are keyed by content hash, not address — RefSites already carry the resolved
    dependency (or the original PCG address for a still-unresolved gap), so no byte-decoding is
    needed here. SAMPLE refs are read straight off entry.body (RefSites carries OBJECT refs only)."""
    parent_name = entry.display_name or "(unnamed)"
    _add_sample_rows(samples.walk(entry.obj_type, entry.body),
                     f"{type_name(entry.obj_type)}: {parent_name} (via sample, staged - not yet placed)",
                     sample_seen, rows, src.exs_index)
    for site in entry.ref_sites:
        dep = merge.try_get(site.resolved_content_hash) if site.resolved_content_hash else None
        target = ObjLoc(*site.target_address)
        key = site.resolved_content_hash or target.label()
        if key in seen:
            continue
        seen.add(key)
        parent_info = (f"{type_name(entry.obj_type)}: {parent_name} "
                       f"(via {site.ref_kind}, staged - not yet placed)")
        if dep is not None:
            # No real address yet (the Merge Window is bag-based) — the name is all there is.
            rows.append(DependencyRow(f"{type_name(dep.obj_type)}: {dep.display_name or '(unnamed)'}",
                                      parent_info, lambda d=dep: describe_merge_children(d, merge, src)))
            collect_merge_deps(dep, merge, src, seen, sample_seen, rows)
            continue
        rows.append(describe_gap_or_local(target, parent_info, "in any loaded PCG", src))


def describe_merge_children(entry, merge: MergeSource, src: LocalSource) -> List[str]:
    lines = [resolve_sample_description(r, src.exs_index) for r in samples.walk(entry.obj_type, entry.body)]
    for site in entry.ref_sites:
        dep = merge.try_get(site.resolved_content_hash) if site.resolved_content_hash else None
        if dep is not None:
            desc = f"{type_name(dep.obj_type)}: {dep.display_name or '(unnamed)'}"
        else:
            t = ObjLoc(*site.target_address)
            desc = f"{type_name(t.obj_type)}: {t.label()} - not found in any loaded PCG"
        lines.append(f"{desc} (via {site.ref_kind})")
    return lines


def build_merge_gap_rows(merge: MergeSource, src: LocalSource,
                         missing_name: Callable[[ObjLoc], str]) -> List[DependencyRow]:
    """The panel's red section: one row per missing ADDRESS (not per reference site), always first,
    independent of any selection — the pre-Commit checklist must not vanish on a stray click.
    An address Keyboard Library already covers resolves on the instrument, so it isn't listed."""
    groups: Dict[ObjLoc, list] = {}
    for site in merge.unresolved_sites():
        groups.setdefault(ObjLoc(*site.target_address), []).append(site)
    rows: List[DependencyRow] = []
    for target in sorted(groups, key=lambda l: (l.obj_type, l.bank, l.number)):
        if src.available(target):
            continue
        sites = groups[target]
        referrers = []
        for s in sites:
            owner = merge.try_get(s.owner_hash)
            owner_name = owner.display_name if owner is not None and owner.display_name else "(unnamed)"
            referrers.append(f"{type_name(owner.obj_type if owner is not None else target.obj_type)}: "
                             f"{owner_name} (via {s.ref_kind})")
        rows.append(DependencyRow(
            gap_row_text(type_name(target.obj_type), target.label(), missing_name(target), len(sites)),
            "; ".join(referrers), lambda: [NOT_STAGED_CHILDREN], missing_ref=target))
    return rows


# ── Self-test (python -m Data.librarian_dependencies) ─────────────────────────────────────────

def _selftest() -> None:
    import sys
    from types import SimpleNamespace as NS
    from Data.librarian_sysex import func33_to_obj_bank, set_combi_timbre_ref
    from Data.pcg_file import WIRE_SIZE_HD1
    from Objects.object_body import write_program_name
    from Tools.exs_option_index import ExsOptionIndex

    fails: List[str] = []

    def check(name: str, cond: bool) -> None:
        if not cond:
            fails.append(name)

    def prog_loc(func33_bank: int, n: int) -> ObjLoc:
        return ObjLoc(OBJ_PROGRAM, func33_to_obj_bank(1, func33_bank), n)

    # a Combi whose timbres point at: A=I-A:007 (present, named), B=I-A:008 (missing),
    # C=GM:005 (ROM), D=I-A:009 (present but an INIT placeholder)
    combi = bytearray(7810)
    set_combi_timbre_ref(combi, 0, 0, 7)
    set_combi_timbre_ref(combi, 1, 0, 8)
    set_combi_timbre_ref(combi, 2, 16, 5)      # func33 bank 16 -> a read-only ROM bank
    set_combi_timbre_ref(combi, 3, 0, 9)
    for t in range(4, 16):                      # untouched timbres default to I-A:000 — point them at A
        set_combi_timbre_ref(combi, t, 0, 7)
    combi_loc = ObjLoc(OBJ_COMBI, 0, 1)
    A, B, C, D = prog_loc(0, 7), prog_loc(0, 8), prog_loc(16, 5), prog_loc(0, 9)

    present = {A: "Warm Pad", D: "Init Program", combi_loc: "My Combi"}
    pending_delete: set = set()
    bodies: Dict[ObjLoc, bytes] = {combi_loc: bytes(combi)}

    # Program A references EXs10 (named in the catalog) and a Drum Track pointing at B (missing)
    progA = bytearray(write_program_name(bytes(WIRE_SIZE_HD1), "Warm Pad"))
    type_off = 2774
    progA[type_off] = 1
    progA[type_off + 1:type_off + 16] = samples.KORG_MS_PREFIX
    progA[type_off + 16] = 11 << 1             # legacy bank 11 -> EXs10
    progA[1295] |= 0x10                         # Drum Track On
    progA[2688], progA[2689] = 8, 0             # -> func33 bank 0, number 8 == B
    bodies[A] = bytes(progA)

    def walk_fn(loc: ObjLoc):
        body = bodies.get(loc)
        if body is None:
            return None
        return list(depscan.walk_display_references(loc.obj_type, body)), samples.walk(loc.obj_type, body)

    idx = ExsOptionIndex.from_catalog(
        '{"10": "EXs10\\nWest Coast Drums\\n10\\n2,11,EXs10 West Coast Drums\\n"}')
    src = LocalSource(
        walk=walk_fn,
        available=lambda l: l in present and l not in pending_delete,
        display_name=lambda l: present.get(l, ""),
        exs_index=idx)

    rows: List[DependencyRow] = []
    missing: List[MissingDependency] = []
    collect_local_deps(combi_loc, src, set(), set(), rows, missing)
    desc = [r.description for r in rows]
    check("found-row", any("Program" in d and "Warm Pad" in d for d in desc))
    check("rom-row-present", any(ROM_ALWAYS_AVAILABLE in d for d in desc))
    check("rom-row-not-missing", not any(ROM_ALWAYS_AVAILABLE in r.description and r.is_missing for r in rows))
    check("init-placeholder-flagged", any(INIT_PLACEHOLDER_SUFFIX in d for d in desc))
    check("missing-row-red", any(r.is_missing and r.missing_ref == B for r in rows))
    # depth-first, like C#: timbre 1 (A) is visited first and ITS drum track reaches B before the
    # Combi's own timbre 2 does — so B is recorded once, against A
    check("missing-recorded-once", [m.missing for m in missing] == [B]
          and missing[0].required_by == A and missing[0].ref_kind == "drum track")
    check("missing-children-text", [r for r in rows if r.is_missing][0].describe_children() == [NOT_STAGED_CHILDREN])
    check("sample-row-named", any(r.sample_bucket == samples.EXS and "EXs10 - West Coast Drums" in r.description
                                  for r in rows))
    check("sample-row-not-missing", all(not r.is_missing for r in rows if r.sample_bucket))
    # B is reported once even though both A's drum track and the Combi's timbre 2 point at it
    check("drum-track-dedupes-with-timbre", sum(1 for r in rows if "not found" in r.description) == 1)
    check("parent-info-names-site", any("(via timbre 1)" in r.parent_info for r in rows)
          and any("(via drum track)" in r.parent_info for r in rows))

    # More Info children: one level, annotated with the site
    kids = [r for r in rows if "Warm Pad" in r.description][0].describe_children()
    check("children-have-sample-and-drum-track",
          any("EXs10 - West Coast Drums" in k for k in kids) and any("(via drum track)" in k for k in kids))

    # a dependency marked pending-delete is NOT 'available' (Commit is about to remove it)
    pending_delete.add(A)
    rows2: List[DependencyRow] = []
    collect_local_deps(combi_loc, src, set(), set(), rows2)
    check("pending-delete-reads-missing", any(r.is_missing and r.missing_ref == A for r in rows2))
    pending_delete.clear()

    # name resolution: off without an index (bare label); the prefixed row key is stripped before lookup
    bare = samples.SampleDependencyRow("EXs10", "Exs|exs10", 1, samples.EXS)
    check("no-index-bare", resolve_sample_description(bare, None) == "EXs10")
    check("index-resolves-prefixed-key", resolve_sample_description(bare, idx) == "EXs10 - West Coast Drums")
    unknown = samples.SampleDependencyRow("EXs999", "Exs|exs999", 1, samples.EXS)
    check("unknown-number-untouched", resolve_sample_description(unknown, idx) == "EXs999")

    # PCG source: a Combi in the PCG whose Program A is in the PCG; B is not (but IS local -> not red)
    pcg_objs = {combi_loc: ("PCG Combi", bytes(combi)), A: ("PCG Warm Pad", bytes(progA))}
    pcg = PcgSource(get=lambda l: pcg_objs.get(l))
    present[B] = "Local B"
    prow: List[DependencyRow] = []
    collect_pcg_deps(combi_loc, pcg, src, set(), set(), prow)
    pd = [r.description for r in prow]
    check("pcg-found-in-pcg", any("PCG Warm Pad" in d for d in pd))
    check("pcg-absent-but-local-not-red",
          any("Local B" in d and "already in your Keyboard Library" in d for d in pd)
          and not any(r.is_missing and r.missing_ref == B for r in prow))
    del present[B]
    prow2: List[DependencyRow] = []
    collect_pcg_deps(combi_loc, pcg, src, set(), set(), prow2)
    check("pcg-absent-and-not-local-is-red", any(r.is_missing and r.missing_ref == B for r in prow2))

    # Merge gap rows: grouped per ADDRESS, locally-covered addresses omitted, sorted, counted
    sites = [NS(owner_hash="h1", target_address=(B.obj_type, B.bank, B.number), resolved_content_hash=None, ref_kind="timbre 2"),
             NS(owner_hash="h2", target_address=(B.obj_type, B.bank, B.number), resolved_content_hash=None, ref_kind="timbre 4"),
             NS(owner_hash="h1", target_address=(A.obj_type, A.bank, A.number), resolved_content_hash=None, ref_kind="timbre 1")]
    owners = {"h1": NS(obj_type=OBJ_COMBI, display_name="Combi One", body=b"", ref_sites=[]),
              "h2": NS(obj_type=OBJ_COMBI, display_name="", body=b"", ref_sites=[])}
    merge = MergeSource(try_get=owners.get, unresolved_sites=lambda: sites)
    gaps = build_merge_gap_rows(merge, src, lambda l: "From PCG" if l == B else "")
    check("gap-covered-address-omitted", all(r.missing_ref != A for r in gaps))   # A is present locally
    check("gap-one-row-per-address", len(gaps) == 1 and gaps[0].missing_ref == B)
    check("gap-count-and-name", "needed by 2 objects" in gaps[0].description and "“From PCG”" in gaps[0].description)
    check("gap-referrers", "Combi One (via timbre 2)" in gaps[0].parent_info
          and "(unnamed) (via timbre 4)" in gaps[0].parent_info)
    check("gap-singular", "needed by 1 object" in gap_row_text("Program", "X", "", 1)
          and "objects" not in gap_row_text("Program", "X", "", 1))

    if fails:
        print("FAIL:", ", ".join(fails))
        sys.exit(1)
    print("librarian_dependencies self-test: OK")


if __name__ == "__main__":
    _selftest()
