r"""
Merge Window staging cache — a content-addressed bag between "things pulled
from a PCG (or from Local Library itself)" and "things placed into Local
Library."

Port of Core/LocalLibrary/MergeCache.cs, Core/LocalLibrary/MergeCachePersistence.cs,
and Core/MergeCacheBehavior.cs.

Deliberately BAG-based, not addressed, exactly like the C# source: two
different PCGs can each have something at the SAME (obj_type, bank, number),
so staging can't use Local Library's own address space without inventing
conflicts that don't need to exist yet. Dedup happens by content hash
(MergeEntry.content_hash); address resolution only happens once, at
placement time (out of scope for this module -- see below).

Design differences from the C# source (deliberate, not oversights):
  * Body storage: MergeEntrySnapshot.Body in C# embeds the full byte[] of
    every staged entry directly in merge_cache.json. This port instead
    reuses local_library_store.BlobStore (the SAME content-addressed store
    Local Library itself uses) -- every entry is already keyed by its SHA-1
    content hash for dedup purposes, so also using that hash as the BLOB
    STORE key means an object's bytes are never written twice (once into the
    blob store, once again into the JSON snapshot), and merge_cache.json
    itself stays small even with many staged objects. Body is looked up from
    the blob store lazily on load().
  * RefKind/Site: the C# MergeRefSite also carries RefKind/Site so
    ResolveReferencesForPlacement can patch the exact bytes
    ObjectReferenceWalker read them from. That placement-time byte-patching
    step is a separate follow-up concern (LocalEditOps-equivalent, not yet
    ported) and isn't ported here -- MergeRefSite in this module only tracks
    target_address + resolved_content_hash. resolve_refs() is deliberately
    generic (returns only (obj_type, bank, number) target addresses, no
    ref-kind/site-offset detail) so it works with ANY reference-walking
    implementation (Combi timbre refs, Set List slot refs, or a test fake)
    without this module importing librarian_sysex or a sibling
    dependency-scanner module that may not exist yet.
  * PullFromPcg/PullFromLocal: the C# MergeCache binds two concrete
    MergePullSource lambdas (one over a loaded PcgLibraryView, one over
    LocalLibraryCache). This port takes resolve_content/resolve_refs as
    plain parameters to pull_recursive() instead, so the SAME method works
    against a PCG, Local Library, or a self-test fake without a hard
    dependency on pcg_file.py or any not-yet-existing local-library-cache
    module.
  * version: C# stamps LibObj.CurrentObjectVersion(objType) ?? 0; there is
    no Python port of that lookup yet, so version defaults to 0 here.

Storage root: {DataDir}/local_library/ -- the SAME root
local_library_store.py resolves (local_library_store.local_library_dir(),
itself storage._data_dir()-based), reused here rather than inventing a
second data directory convention; merge_cache.json and this module's blob
store both live alongside index.json/oplog.jsonl/blobs/.
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional, Set, Tuple

import kronos_sysex as ksx
from local_library_store import BlobStore, local_library_dir

Address = Tuple[int, int, int]   # (obj_type, bank, number)

ResolveContent = Callable[[int, int, int], Optional[bytes]]
ResolveRefs = Callable[[int, bytes], List[Address]]


def _extract_display_name(body: bytes) -> str:
    """Every Program/Combi/Set-List record has a 24-byte ASCII name field at
    offset 0 (same convention pcg_file._read_record_name and the live-dump
    decoders in kronos_sysex already rely on) -- reused here so
    resolve_content's contract can stay a plain Optional[bytes] lookup, with
    no separate "name" channel the source also has to supply."""
    return ksx._ascii_trim(body, 0, 24)


def _normalize_address(entry_or_address) -> Address:
    """Accepts either a plain (obj_type, bank, number) tuple, or an object
    exposing obj_type/bank/number-like attributes -- e.g.
    pcg_file.PcgObjectEntry, whose .bank is a kronos_sysex.BankId (or None
    for Set List) and whose 0-based position is .index. Convenience for
    callers walking PCG-extracted entries directly, without this module
    importing pcg_file itself."""
    if isinstance(entry_or_address, tuple):
        return entry_or_address
    obj_type = entry_or_address.obj_type
    bank = getattr(entry_or_address, "bank", None)
    number = getattr(entry_or_address, "number", None)
    if number is None:
        number = getattr(entry_or_address, "index", 0)
    if bank is not None and hasattr(bank, "obj_bank"):
        bank = bank.obj_bank
    return (obj_type, bank if bank is not None else 0, number)


# ── Staged records ───────────────────────────────────────────────────────────


@dataclass
class MergeOrigin:
    """Where one piece of content in the merge cache was originally pulled
    from -- kept for traceability even after dedup collapses multiple pulls
    of identical content into one MergeEntry. Port of the C# `MergeOrigin`
    record struct."""
    source: str            # PCG filename, or MergeCache.LOCAL_SOURCE_LABEL
    address: Address       # (obj_type, bank, number) it was pulled from


@dataclass
class MergeRefSite:
    """One outgoing reference site inside a Combi/Set-List entry's own body
    (a timbre slot, or a Set-List slot). Port of the C# `MergeRefSite`,
    minus RefKind/Site -- see module docstring for why placement-time byte
    patching is out of scope here. resolved_content_hash is the dependency's
    content hash if pulling it succeeded by the time owner_hash's entry was
    itself pulled; None means it was a gap -- MergeCache.reconcile_gaps can
    still backfill it later if the exact same address is pulled
    successfully afterward (e.g. from a second PCG)."""
    owner_hash: str            # the entry this reference site belongs to
    target_address: Address    # the original reference, for gap reconciliation
    resolved_content_hash: Optional[str] = None


@dataclass
class MergeEntry:
    """One piece of content staged in the Merge Window -- the Merge
    Window's whole point is that IDENTICAL content pulled multiple times
    (same PCG twice, or two different PCGs with a byte-identical Program)
    collapses to exactly one of these, tracked by content_hash. Port of the
    C# `MergeEntry` class."""
    content_hash: str
    obj_type: int
    body: bytes                        # wire format -- same convention BlobStore uses
    version: int = 0
    display_name: str = ""
    is_top_level_pull: bool = False    # the user explicitly pulled this, not just a dependency
    origins: List[MergeOrigin] = field(default_factory=list)
    referenced_by: Set[str] = field(default_factory=set)          # >1 => "shared" in the UI
    ref_sites: List[MergeRefSite] = field(default_factory=list)   # this entry's OWN outgoing refs

    @property
    def has_unresolved_dependencies(self) -> bool:
        return any(s.resolved_content_hash is None for s in self.ref_sites)


# ── Persistence behavior ─────────────────────────────────────────────────────


class MergeCacheBehavior(Enum):
    """Port of MergeCacheBehavior.cs -- settings-tab choice between never
    touching disk (cleared on restart) and surviving a crash/reboot (a plain
    snapshot file, rewritten in full on every mutation)."""
    TEMPORARY_MEMORY = "temporary_memory"   # never touches disk; save()/load() are no-ops
    LOCAL_STORAGE = "local_storage"         # merge_cache.json, full rewrite on every mutation


# ── Merge cache ──────────────────────────────────────────────────────────────


class MergeCache:
    """Bag-based staging cache for objects pulled out of loaded PCG files (or
    Local Library itself), before they're placed into Local Library. Port of
    the C# `MergeCache` class."""

    LOCAL_SOURCE_LABEL = "Local Library"

    def __init__(self, root: Optional[pathlib.Path] = None,
                 behavior: MergeCacheBehavior = MergeCacheBehavior.LOCAL_STORAGE):
        self.root = pathlib.Path(root) if root is not None else local_library_dir()
        self.blobs = BlobStore(self.root)
        self.behavior = behavior

        self._by_hash: Dict[str, MergeEntry] = {}
        # Every RefSite still pointing at a specific address that hasn't
        # resolved yet -- keyed by that address so a LATER pull of the exact
        # same address (e.g. from a second PCG that DOES have it) can
        # retroactively resolve it. See reconcile_gaps().
        self._pending_gap_sites: Dict[Address, List[MergeRefSite]] = {}
        # Where a content hash has already been placed in Local Library this
        # batch -- content_hash -> destination address (mirrors C#'s
        # _placedAddresses, which is keyed by content hash despite the name).
        self._placed_addresses: Dict[str, Address] = {}

        self.load()

    @property
    def entries(self) -> List[MergeEntry]:
        return list(self._by_hash.values())

    # -- persistence ----------------------------------------------------------

    def _path(self) -> pathlib.Path:
        return self.root / "merge_cache.json"

    def save(self) -> None:
        """Full rewrite of merge_cache.json -- no incremental diffing, same
        simplicity as the C# FileMergeCachePersistence (a crash mid-write
        skips a clean-shutdown path entirely, so saves must always be
        complete on their own). No-op under TEMPORARY_MEMORY."""
        if self.behavior is not MergeCacheBehavior.LOCAL_STORAGE:
            return
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            root_obj = {
                "entries": [
                    {
                        "content_hash": e.content_hash,
                        "obj_type": e.obj_type,
                        "version": e.version,
                        "display_name": e.display_name,
                        "is_top_level_pull": e.is_top_level_pull,
                        "origins": [{"source": o.source, "address": list(o.address)} for o in e.origins],
                        "referenced_by": sorted(e.referenced_by),
                        "ref_sites": [
                            {
                                "owner_hash": s.owner_hash,
                                "target_address": list(s.target_address),
                                "resolved_content_hash": s.resolved_content_hash,
                            }
                            for s in e.ref_sites
                        ],
                    }
                    for e in self._by_hash.values()
                ],
                "placed_addresses": {h: list(a) for h, a in self._placed_addresses.items()},
            }
            self._path().write_text(json.dumps(root_obj, indent=2), encoding="utf-8")
        except Exception as ex:
            print(f"[merge-cache] snapshot save failed: {ex}")

    def load(self) -> None:
        """Load merge_cache.json into this instance in place (missing file =
        empty cache, not an error). Body bytes are looked up lazily from the
        blob store by content_hash -- see module docstring. No-op under
        TEMPORARY_MEMORY (mirrors InMemoryMergeCachePersistence.Load() =>
        null, so a fresh MergeCache always starts empty)."""
        if self.behavior is not MergeCacheBehavior.LOCAL_STORAGE:
            return
        p = self._path()
        if not p.exists():
            return
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception as ex:
            print(f"[merge-cache] snapshot load failed: {ex}")
            return

        self._by_hash.clear()
        for ed in raw.get("entries", []):
            content_hash = ed.get("content_hash", "")
            body = self.blobs.get(content_hash)
            if body is None:
                print(f"[merge-cache] snapshot entry {content_hash[:8]} has no matching blob, skipping")
                continue
            entry = MergeEntry(
                content_hash=content_hash, obj_type=int(ed.get("obj_type", 0)), body=body,
                version=int(ed.get("version", 0)), display_name=ed.get("display_name", ""),
                is_top_level_pull=bool(ed.get("is_top_level_pull", False)),
            )
            entry.origins.extend(
                MergeOrigin(o["source"], tuple(o["address"])) for o in ed.get("origins", []))
            entry.referenced_by.update(ed.get("referenced_by", []))
            entry.ref_sites.extend(
                MergeRefSite(owner_hash=s["owner_hash"], target_address=tuple(s["target_address"]),
                             resolved_content_hash=s.get("resolved_content_hash"))
                for s in ed.get("ref_sites", []))
            self._by_hash[content_hash] = entry

        self._placed_addresses = {h: tuple(a) for h, a in raw.get("placed_addresses", {}).items()}
        self._rebuild_pending_gap_index()

    def _rebuild_pending_gap_index(self) -> None:
        self._pending_gap_sites.clear()
        for entry in self._by_hash.values():
            for site in entry.ref_sites:
                if site.resolved_content_hash is None:
                    self._register_gap(site)

    def set_behavior(self, new_behavior: MergeCacheBehavior) -> None:
        """Switches which persistence strategy future mutations go through --
        e.g. the user flips the Merge behavior setting while the Librarian
        is open. LOCAL_STORAGE -> TEMPORARY_MEMORY deletes the old snapshot
        file but keeps everything already staged in memory for the rest of
        this session -- only future persistence stops. TEMPORARY_MEMORY ->
        LOCAL_STORAGE persists whatever is CURRENTLY staged to the new
        location immediately, so switching mid-session doesn't silently lose
        it (mirrors MergeCache.SetPersistence)."""
        was_file_backed = self.behavior is MergeCacheBehavior.LOCAL_STORAGE
        if was_file_backed and new_behavior is not MergeCacheBehavior.LOCAL_STORAGE:
            try:
                self._path().unlink(missing_ok=True)
            except Exception as ex:
                print(f"[merge-cache] snapshot clear failed: {ex}")
        self.behavior = new_behavior
        self.save()

    # -- pulling ----------------------------------------------------------------

    def pull_recursive(self, entry_or_address, resolve_content: ResolveContent,
                        resolve_refs: ResolveRefs, source: str = "pull"
                        ) -> Tuple[List[MergeEntry], List[Tuple[Address, str]]]:
        """Pulls one object -- and, fully automatically and transitively,
        everything it references that resolves via resolve_content -- into
        the merge cache. Byte-identical content already staged (from this or
        any earlier pull) is recognized and NOT duplicated; only genuinely
        new content is returned in `added`. Gaps are references that don't
        resolve right now -- the caller decides how to surface them.

        entry_or_address: an (obj_type, bank, number) tuple, or an object
            with obj_type/bank/number-like attributes (see _normalize_address).
        resolve_content(obj_type, bank, number) -> Optional[bytes]: the
            source lookup (a loaded PCG, Local Library, or a test fake).
        resolve_refs(obj_type, body) -> list[(obj_type, bank, number)]: the
            injected reference walker (e.g. Combi timbre refs / Set-List
            slot refs) -- deliberately not hardcoded here, see module
            docstring.
        source: label recorded on MergeOrigin (a PCG filename, or
            MergeCache.LOCAL_SOURCE_LABEL).

        Returns (added, gaps) -- mirrors C# MergeCache.Pull's return shape.
        """
        added: List[MergeEntry] = []
        gaps: List[Tuple[Address, str]] = []
        self._pull_one(_normalize_address(entry_or_address), resolve_content, resolve_refs,
                        source, True, added, gaps)
        self.save()
        return added, gaps

    def _pull_one(self, address: Address, resolve_content: ResolveContent,
                   resolve_refs: ResolveRefs, source: str, is_top_level: bool,
                   added: List[MergeEntry], gaps: List[Tuple[Address, str]]) -> Optional[str]:
        """Returns the content hash of whatever now represents `address` (an
        existing deduped entry, a freshly-added one, or None if it's a real
        gap). No cycle guard needed: a Program never references anything, a
        Combi only ever references Programs, and a Set List only ever
        references Combis/Programs -- this reference graph is acyclic by
        construction (same assumption the C# source relies on)."""
        obj_type, bank, number = address
        body = resolve_content(obj_type, bank, number)
        if body is None:
            gaps.append((address, "pull" if is_top_level else "dependency"))
            return None

        content_hash = BlobStore.compute_hash(body)
        existing = self._by_hash.get(content_hash)
        if existing is not None:
            if is_top_level:
                existing.is_top_level_pull = True
            if not any(o.source == source and o.address == address for o in existing.origins):
                existing.origins.append(MergeOrigin(source, address))
            self.reconcile_gaps(address, content_hash)
            return content_hash   # dedup -- already walked its own deps when first added

        self.blobs.put(body)
        entry = MergeEntry(
            content_hash=content_hash, obj_type=obj_type, body=body,
            display_name=_extract_display_name(body), is_top_level_pull=is_top_level,
        )
        entry.origins.append(MergeOrigin(source, address))
        self._by_hash[content_hash] = entry
        added.append(entry)
        self.reconcile_gaps(address, content_hash)

        for ref_address in resolve_refs(obj_type, body):
            dep_hash = self._pull_one(ref_address, resolve_content, resolve_refs, source, False, added, gaps)
            ref_site = MergeRefSite(owner_hash=content_hash, target_address=ref_address,
                                     resolved_content_hash=dep_hash)
            entry.ref_sites.append(ref_site)
            if dep_hash is not None:
                self._by_hash[dep_hash].referenced_by.add(content_hash)
            else:
                self._register_gap(ref_site)
        return content_hash

    def _register_gap(self, site: MergeRefSite) -> None:
        self._pending_gap_sites.setdefault(site.target_address, []).append(site)

    def reconcile_gaps(self, address: Address, resolved_hash: str) -> None:
        """A dependency that was missing when some earlier entry was pulled
        can become available the moment the SAME address is later pulled
        successfully -- typically from a different PCG that happens to have
        it. There's no separate address space in a bag-based cache, only
        "was this exact reference ever satisfied since" -- which is exactly
        what "resolve later by loading a different PCG and pulling it in"
        means. Called automatically for EVERY address pulled (top-level or
        dependency, dedup-hit or new) from inside pull_recursive -- mirrors
        MergeCache.PullRecursive's own unconditional ReconcileGaps call, so
        a previously-missing dependency resolves retroactively just by being
        pulled from anywhere, with no extra step the caller has to remember.
        Also safe to call directly (e.g. after recording a placement made
        outside a pull)."""
        sites = self._pending_gap_sites.pop(address, None)
        if not sites:
            return
        for site in sites:
            site.resolved_content_hash = resolved_hash
            self._by_hash[resolved_hash].referenced_by.add(site.owner_hash)

    # -- lookup / lifecycle -----------------------------------------------------

    def try_get(self, content_hash: str) -> Optional[MergeEntry]:
        return self._by_hash.get(content_hash)

    def remove(self, content_hash: str) -> bool:
        """Removes one entry -- called after it's successfully placed into
        Local Library (move semantics: the Merge Window only ever shows
        what's still pending placement) or when the user abandons it without
        placing it. _placed_addresses is untouched: if this WAS placed,
        mark_placed already captured where, which is exactly what lets a
        sibling entry still staged resolve against it later."""
        if content_hash not in self._by_hash:
            return False
        del self._by_hash[content_hash]
        self.save()
        return True

    def clear(self) -> None:
        """Explicit "Clear Merge" -- abandons everything still staged,
        whether or not any of it was ever placed. Placement bookkeeping is
        cleared too: once the whole batch is gone, nothing remains that
        could ever look it up again."""
        self._by_hash.clear()
        self._pending_gap_sites.clear()
        self._placed_addresses.clear()
        self.save()

    # -- placement tracking -------------------------------------------------

    def mark_placed(self, content_hash: str, address: Address) -> None:
        """Records that `content_hash` now lives at `address` in Local
        Library -- the mechanism behind "many-to-one" dependency dedup:
        every OTHER still-staged entry whose ref_sites resolved to this same
        hash can be repointed at exactly this destination once a
        placement-patching step is built (mirrors MergeCache.RecordPlacement).
        """
        self._placed_addresses[content_hash] = address
        self.save()

    def is_placed(self, content_hash: str) -> bool:
        return content_hash in self._placed_addresses

    def placed_address(self, content_hash: str) -> Optional[Address]:
        return self._placed_addresses.get(content_hash)


# ── Self-test (python merge_cache.py) ───────────────────────────────────────


def _selftest() -> None:
    import shutil
    import sys
    import tempfile

    fails: List[str] = []

    def check(name: str, cond: bool) -> None:
        if not cond:
            fails.append(name)

    OBJ_PROGRAM, OBJ_COMBI = 0, 1

    root = pathlib.Path(tempfile.gettempdir()) / "kronos_selftest_merge_cache"
    if root.exists():
        shutil.rmtree(root)
    try:
        # ── Scenario 1: pull a Combi referencing 2 resolvable Programs ──
        combi_body = b"COMBI-BODY-ONE................."
        prog1_body = b"PROGRAM-BODY-ONE"
        prog2_body = b"PROGRAM-BODY-TWO"
        source1 = {
            (OBJ_COMBI, 0, 0): combi_body,
            (OBJ_PROGRAM, 0, 1): prog1_body,
            (OBJ_PROGRAM, 0, 2): prog2_body,
        }

        def resolve_content_1(obj_type, bank, number):
            return source1.get((obj_type, bank, number))

        def resolve_refs_combi(obj_type, body):
            if obj_type == OBJ_COMBI and body == combi_body:
                return [(OBJ_PROGRAM, 0, 1), (OBJ_PROGRAM, 0, 2)]
            return []

        mc = MergeCache(root)
        added1, gaps1 = mc.pull_recursive((OBJ_COMBI, 0, 0), resolve_content_1, resolve_refs_combi,
                                           source="pcg_A.PCG")
        check("s1-added-3", len(added1) == 3)
        check("s1-no-gaps", gaps1 == [])
        check("s1-hashes-distinct", len({e.content_hash for e in added1}) == 3)
        for e in added1:
            check(f"s1-blob-stored-{e.content_hash[:8]}", mc.blobs.exists(e.content_hash))

        combi_hash = BlobStore.compute_hash(combi_body)
        combi_entry = mc.try_get(combi_hash)
        check("s1-combi-2-refsites", combi_entry is not None and len(combi_entry.ref_sites) == 2)
        check("s1-combi-fully-resolved", combi_entry is not None and not combi_entry.has_unresolved_dependencies)

        # ── Scenario 2: a reference that ISN'T resolvable yet -> pending gap,
        #    then pull it separately -> retroactive resolution ──
        combi2_body = b"COMBI-BODY-TWO................."
        prog3_body = b"PROGRAM-BODY-THREE"
        source2 = {(OBJ_COMBI, 0, 1): combi2_body}   # prog3 deliberately absent

        def resolve_content_2(obj_type, bank, number):
            return source2.get((obj_type, bank, number))

        def resolve_refs_combi2(obj_type, body):
            if obj_type == OBJ_COMBI and body == combi2_body:
                return [(OBJ_PROGRAM, 0, 3)]
            return []

        added2, gaps2 = mc.pull_recursive((OBJ_COMBI, 0, 1), resolve_content_2, resolve_refs_combi2,
                                           source="pcg_B.PCG")
        check("s2-gap-tracked", gaps2 == [((OBJ_PROGRAM, 0, 3), "dependency")])
        combi2_hash = BlobStore.compute_hash(combi2_body)
        combi2_entry = mc.try_get(combi2_hash)
        check("s2-combi-has-gap", combi2_entry is not None and combi2_entry.has_unresolved_dependencies)
        check("s2-gap-registered", (OBJ_PROGRAM, 0, 3) in mc._pending_gap_sites)

        source3 = {(OBJ_PROGRAM, 0, 3): prog3_body}

        def resolve_content_3(obj_type, bank, number):
            return source3.get((obj_type, bank, number))

        added3, gaps3 = mc.pull_recursive((OBJ_PROGRAM, 0, 3), resolve_content_3, lambda t, b: [],
                                           source="pcg_C.PCG")
        check("s2-prog3-pulled", len(added3) == 1)
        check("s2-no-gaps-now", gaps3 == [])
        prog3_hash = BlobStore.compute_hash(prog3_body)
        combi2_entry_after = mc.try_get(combi2_hash)
        check("s2-gap-retroactively-resolved",
              combi2_entry_after is not None and not combi2_entry_after.has_unresolved_dependencies)
        check("s2-refsite-points-at-prog3",
              combi2_entry_after is not None and
              any(s.target_address == (OBJ_PROGRAM, 0, 3) and s.resolved_content_hash == prog3_hash
                  for s in combi2_entry_after.ref_sites))
        prog3_entry = mc.try_get(prog3_hash)
        check("s2-referenced-by-updated", prog3_entry is not None and combi2_hash in prog3_entry.referenced_by)
        check("s2-pending-gap-cleared", (OBJ_PROGRAM, 0, 3) not in mc._pending_gap_sites)

        # ── Scenario 3: dedup -- same Program content pulled via a different
        #    address/source must NOT create a second blob or a second entry ──
        blob_count_before = len(list((root / "blobs").rglob("*.bin")))
        source4 = {(OBJ_PROGRAM, 5, 9): prog1_body}   # identical bytes to prog1, different address

        def resolve_content_4(obj_type, bank, number):
            return source4.get((obj_type, bank, number))

        added4, gaps4 = mc.pull_recursive((OBJ_PROGRAM, 5, 9), resolve_content_4, lambda t, b: [],
                                           source="pcg_D.PCG")
        blob_count_after = len(list((root / "blobs").rglob("*.bin")))
        check("s3-no-new-blob", blob_count_after == blob_count_before)
        check("s3-no-new-entry", len(added4) == 0)
        prog1_hash = BlobStore.compute_hash(prog1_body)
        prog1_entry = mc.try_get(prog1_hash)
        check("s3-second-origin-recorded",
              prog1_entry is not None and
              any(o.source == "pcg_D.PCG" and o.address == (OBJ_PROGRAM, 5, 9) for o in prog1_entry.origins))
        check("s3-still-one-entry", sum(1 for e in mc.entries if e.content_hash == prog1_hash) == 1)

        # ── mark_placed / is_placed ──
        check("placed-false-initially", not mc.is_placed(prog1_hash))
        mc.mark_placed(prog1_hash, (OBJ_PROGRAM, 0, 1))
        check("placed-true-after", mc.is_placed(prog1_hash))
        check("placed-address-correct", mc.placed_address(prog1_hash) == (OBJ_PROGRAM, 0, 1))

        # ── Scenario 4: save/load round-trip reproduces the same staging state ──
        def _snapshot(cache: MergeCache):
            return {
                e.content_hash: (e.obj_type, e.body, e.display_name, e.is_top_level_pull,
                                  sorted((o.source, o.address) for o in e.origins),
                                  sorted(e.referenced_by),
                                  sorted((s.target_address, s.resolved_content_hash) for s in e.ref_sites))
                for e in cache.entries
            }

        before = _snapshot(mc)
        placed_before = dict(mc._placed_addresses)
        gaps_before = {k: len(v) for k, v in mc._pending_gap_sites.items()}

        mc2 = MergeCache(root)   # fresh instance, same root -> load() runs in __init__
        after = _snapshot(mc2)
        check("roundtrip-entries-match", before == after)
        check("roundtrip-placed-match", placed_before == mc2._placed_addresses)
        check("roundtrip-gaps-match", gaps_before == {k: len(v) for k, v in mc2._pending_gap_sites.items()})
        reloaded_combi2 = mc2.try_get(combi2_hash)
        check("roundtrip-combi2-still-resolved",
              reloaded_combi2 is not None and not reloaded_combi2.has_unresolved_dependencies)

        # ── clear() wipes everything ──
        mc2.clear()
        check("clear-empty-entries", mc2.entries == [])
        check("clear-empty-placed", mc2._placed_addresses == {})
        check("clear-empty-gaps", mc2._pending_gap_sites == {})
        mc3 = MergeCache(root)
        check("clear-persisted", mc3.entries == [])

    finally:
        if root.exists():
            shutil.rmtree(root)

    if fails:
        print("FAIL:", ", ".join(fails))
        sys.exit(1)
    print("merge_cache self-test: OK")


if __name__ == "__main__":
    _selftest()
