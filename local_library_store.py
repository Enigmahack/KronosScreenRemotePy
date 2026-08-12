r"""
Local Library storage layer — content-addressed blob store, index, and
write-ahead op-log for the offline-first Program/Combi/Set-List mirror.

Port of Core/LocalLibrary/LocalObjectStore.cs, LocalLibraryIndex.cs, and
OpLog.cs. Core/HostKeyedCache.cs was checked (grep for callers): it is used
only by Storage.cs, not by any of the three files above, so it is generic
caching infra unrelated to this subsystem and is NOT ported here.

Design differences from the C# source (deliberate, not oversights — see the
porting-gap analysis this module was built from):
  * No SMB-share performance workarounds. The C# team's DataDir was an SMB
    share, so LocalLibraryCache.cs batches OpLog appends, memoizes catalog
    reads, and runs a background warm-up thread to hide network latency.
    This Python port's DataDir is local disk (storage._data_dir()) — none of
    that is needed. OpLog.append() here is a plain synchronous single-line
    append, and there is no in-memory "display mirror" to keep in step.
  * No garbage collection of orphaned blobs, exactly like the C# source — an
    unreferenced blob left behind by a discard is accepted debris, not a
    correctness problem.

Three cooperating pieces, following LibraryCatalog/RefIndex's class-based
style in librarian_model.py (mutable, stateful, incremental read/mutate/
append) rather than storage.py's free-function style (which fits pure
load/save-a-whole-dataclass, not these three's per-entry mutation methods):

  * BlobStore          — content-addressed blob store, {root}/blobs/<hh>/<sha1>.bin
  * LocalLibraryIndex  — {root}/index.json, keyed "type:bank:number"
  * OpLog              — {root}/oplog.jsonl, append-only, one JSON object/line

Storage root: {DataDir}/local_library/, where DataDir is storage._data_dir()
— the SAME base directory the rest of the app already persists settings.json/
palette_override.json/etc. to. Reused here, not reinvented.

The core invariant this whole store exists to protect: a locally-dirty
object must never be silently overwritten. LocalIndexEntry.is_dirty
(current_hash != baseline_hash) plus the Conflicted flag give the (future)
pull pipeline everything it needs to detect dirty-vs-clean and conflict
instead of clobbering; LocalLibraryIndex.set_entry() itself does not enforce
this (same division of responsibility as the C# LibraryPullPipeline, which
checks cache.IsDirty BEFORE ever constructing a new baseline record).
"""
from __future__ import annotations

import contextlib
import logging
import hashlib
import json
import pathlib
import threading
from collections import OrderedDict
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

import storage as _storage

log = logging.getLogger(__name__)


class LocalLibraryWriteError(OSError):
    """A local-library write could not be completed (read-only/disconnected
    share, disk full, permissions).

    Raised rather than swallowed: a BlobStore.put() that silently did nothing
    would leave the index pointing at a body that does not exist, and
    changeset_sync would later push that missing blob's slot to the synth.
    Callers in the UI catch this per user action, report it, and stop — which is
    what makes an unwritable data directory a message instead of a crash."""


def local_library_dir() -> pathlib.Path:
    """{DataDir}/local_library/ — same base-dir convention as storage.py
    (script dir if writable, else ~/.config/KronosScreenRemote/)."""
    d = _storage._data_dir() / "local_library"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Blob store (content-addressed) ──────────────────────────────────────────

class BlobStore:
    """Content-addressed blob store at {root}/blobs/<hh>/<sha1>.bin — a
    two-level fan-out so no single directory holds thousands of files. Every
    unique body is written once; dedup is free because identical content
    hashes to the identical path. No compaction/GC subsystem, same as
    LocalObjectStore.cs — an unreferenced blob left behind by a discard is a
    harmless orphan file, not a correctness problem.

    get() is served from a bounded in-memory LRU. Blobs are immutable by
    construction (the path IS the hash of the contents), so a cached body can
    never be stale — the only thing the cache can be wrong about is a blob
    deleted out from under the process, which nothing in this app does. The
    cache is what makes the hot read paths viable: a free-slot scan reads every
    occupied slot's body, a tree refresh reads every dirty Combi/Set List's, and
    the catalog build reads all of them — on a network-mounted data directory
    those were thousands of individual round trips per user action."""

    #: Cache budget in bytes. Bodies run 3.7 KB (HD-1 Program) to 69 KB (Set
    #: List), so this holds a few thousand Programs or a few hundred Set Lists —
    #: comfortably the whole working set of a full library.
    CACHE_BUDGET_BYTES = 64 * 1024 * 1024

    def __init__(self, root: Optional[pathlib.Path] = None):
        self.root = pathlib.Path(root) if root is not None else local_library_dir()
        self._cache: "OrderedDict[str, bytes]" = OrderedDict()
        self._cache_bytes = 0
        self._cache_lock = threading.Lock()

    @staticmethod
    def compute_hash(data: bytes) -> str:
        return hashlib.sha1(data).hexdigest()

    def _path_for(self, sha1: str) -> pathlib.Path:
        return self.root / "blobs" / sha1[:2] / f"{sha1}.bin"

    # -- cache ---------------------------------------------------------------

    def _cache_get(self, sha1: str) -> Optional[bytes]:
        with self._cache_lock:
            data = self._cache.get(sha1)
            if data is not None:
                self._cache.move_to_end(sha1)
            return data

    def _cache_put(self, sha1: str, data: bytes) -> None:
        if len(data) > self.CACHE_BUDGET_BYTES:
            return
        with self._cache_lock:
            if sha1 in self._cache:
                self._cache_bytes -= len(self._cache[sha1])
                del self._cache[sha1]
            self._cache[sha1] = data
            self._cache_bytes += len(data)
            while self._cache_bytes > self.CACHE_BUDGET_BYTES and self._cache:
                _, evicted = self._cache.popitem(last=False)
                self._cache_bytes -= len(evicted)

    def clear_cache(self) -> None:
        with self._cache_lock:
            self._cache.clear()
            self._cache_bytes = 0

    # -- read/write ----------------------------------------------------------

    def put(self, data: bytes) -> str:
        """Write `data` if not already present; return its sha1 hex digest.
        Idempotent — writing the same content twice is a no-op the second
        time (mirrors LocalObjectStore.Put).

        The write is atomic. A torn write here is worse than a torn write
        anywhere else in this project: a truncated file would sit at the
        correct content-addressed path, so put() would report success forever
        after (the `exists()` guard short-circuits) and get() would hand back
        corrupt bytes that changeset_sync pushes straight to the synth.

        Raises LocalLibraryWriteError if the blob could not be written —
        never returns a hash for content that isn't on disk."""
        sha1 = self.compute_hash(data)
        path = self._path_for(sha1)
        if not path.exists():
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                _storage.atomic_write_bytes(path, data)
            except OSError as e:
                raise LocalLibraryWriteError(
                    f"could not write object body to {path}: {e}") from e
        self._cache_put(sha1, data)
        return sha1

    def get(self, sha1: str) -> Optional[bytes]:
        """Return the blob's bytes, or None if absent. `sha1` may be
        LocalLibraryIndex.NO_BASELINE_SENTINEL (empty string) — that is
        never a real SHA-1 hash, so it always returns None rather than
        raising (mirrors LocalObjectStore.TryGet's own guard).

        A read error (share went away mid-session) is reported as absent, the
        same as a missing file: every caller already handles None, and raising
        here would take out an unrelated tree refresh."""
        if not sha1:
            return None
        cached = self._cache_get(sha1)
        if cached is not None:
            return cached
        path = self._path_for(sha1)
        try:
            if not path.exists():
                return None
            data = path.read_bytes()
        except OSError as e:
            log.warning("blob read failed for %s: %s", sha1, e)
            return None
        self._cache_put(sha1, data)
        return data

    def get_verified(self, sha1: str) -> Optional[bytes]:
        """Like get(), but re-hashes the blob and returns None if it does not
        match the key it was stored under.

        Use this on any path whose bytes reach hardware. put() is atomic, so a
        mismatch means the blob predates that guarantee or the file was damaged
        after the fact; either way, writing it to a synth would corrupt the
        slot, and refusing is always the better failure."""
        data = self.get(sha1)
        if data is None:
            return None
        if self.compute_hash(data) != sha1:
            return None
        return data

    def exists(self, sha1: str) -> bool:
        if not sha1:
            return False
        if self._cache_get(sha1) is not None:
            return True
        return self._path_for(sha1).exists()


# ── Index ────────────────────────────────────────────────────────────────

@dataclass
class LocalIndexEntry:
    """One tracked object's pointers into the CAS blob store, keyed
    "type:bank:number" in LocalLibraryIndex.entries. Mirrors the C#
    `LocalIndexEntry` record, mutable here (unlike the C# `record`) so
    mark_conflicted/mark_pending_delete can update in place.

    is_dirty (current_hash != baseline_hash) is the exact comparison a pull/
    push pipeline must use to decide refresh-vs-conflict instead of ever
    silently clobbering an edit — this is the core invariant the whole store
    exists to protect.
    """
    version: int = 0
    baseline_hash: str = ""            # "" = LocalLibraryIndex.NO_BASELINE_SENTINEL
    current_hash: str = ""
    display_name: str = ""
    created_utc: str = ""              # ISO-8601, UTC
    modified_utc: str = ""             # ISO-8601, UTC
    conflicted: bool = False
    has_resolved_dependencies: bool = True
    is_exi: bool = True
    pending_delete: bool = False

    @property
    def is_dirty(self) -> bool:
        return self.current_hash != self.baseline_hash

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "LocalIndexEntry":
        return LocalIndexEntry(
            version=int(d.get("version", 0)),
            baseline_hash=d.get("baseline_hash", ""),
            current_hash=d.get("current_hash", ""),
            display_name=d.get("display_name", ""),
            created_utc=d.get("created_utc", ""),
            modified_utc=d.get("modified_utc", ""),
            conflicted=bool(d.get("conflicted", False)),
            has_resolved_dependencies=bool(d.get("has_resolved_dependencies", True)),
            is_exi=bool(d.get("is_exi", True)),
            pending_delete=bool(d.get("pending_delete", False)),
        )


class LocalLibraryIndex:
    """{root}/index.json — a CACHE, not a second source of truth: current_hash
    is exactly "fold the op-log forward from baseline" (rebuild_current_from_oplog
    reproduces it), so a lost/corrupted index.json is recoverable from
    oplog.jsonl alone — the disaster-recovery path.

    Deliberately NOT host-keyed (unlike storage.py's name/dumped-bank/setlist
    caches) — the local library is a single global store; the Kronos's IP
    can change but the objects don't (mirrors LocalLibraryIndex.cs's note).
    """

    NO_BASELINE_SENTINEL = ""          # no real hardware baseline yet (brand-new local-only object)
    DELETED_TOMBSTONE = "<deleted>"    # a committed deletion, for the oplog fold

    def __init__(self, root: Optional[pathlib.Path] = None):
        self.root = pathlib.Path(root) if root is not None else local_library_dir()
        self.entries: Dict[str, LocalIndexEntry] = {}
        self.bank_digest_baseline: Dict[str, str] = {}
        # Whole-bank HD-1/EXi type-change intent (port of LocalLibraryIndex.cs's
        # PendingProgramBankTypeChanges) — a Program bank -> target is_exi flag, set by some
        # separate, deliberate UI flow (e.g. "convert this bank to EXi"), consumed by
        # changeset_sync.py's build_changeset (its `pending_bank_type_change` callable param,
        # which get_pending_bank_type_change below is the exact shape for), and cleared by the
        # caller after a successful func-0x7C reformat (confirmed from source: SyncPipeline.cs's
        # PushAsync calls cache.ClearPendingBankTypeChange(bank) for every plan.BankTypeChanges
        # entry right after recording push successes — so a completed reformat never re-triggers
        # on the next push). Keyed by plain bank int, not "type:bank" — HD-1/EXi has no obj_type
        # axis, matching bank_key's own Program-only bank-type convention elsewhere in this
        # subsystem (see changeset_sync.py's module docstring).
        self.bank_type_pending: Dict[int, bool] = {}
        self._lock = threading.Lock()
        # defer_saves() batching — see that method.
        self._defer_lock = threading.RLock()
        self._defer_depth = 0
        self._save_pending = False
        #: Last save failure, or None. The UI reads this to tell the user their
        #: edits are in memory only (unwritable share) instead of leaving them
        #: to find out at Sync time.
        self.last_save_error: Optional[str] = None

    @staticmethod
    def key(obj_type: int, bank: int, number: int) -> str:
        return f"{obj_type}:{bank}:{number}"

    @staticmethod
    def bank_key(obj_type: int, bank: int) -> str:
        return f"{obj_type}:{bank}"

    def _path(self) -> pathlib.Path:
        return self.root / "index.json"

    # -- persistence ----------------------------------------------------------

    def load(self) -> "LocalLibraryIndex":
        """Load index.json into this instance in place (missing file = empty
        index, not an error — mirrors LocalLibraryIndex.Load). Returns self
        for chaining, e.g. `idx = LocalLibraryIndex(root).load()`."""
        p = self._path()
        if not p.exists():
            return self
        try:
            with self._lock:
                root = json.loads(p.read_text(encoding="utf-8"))
            self.entries = {
                k: LocalIndexEntry.from_dict(v) for k, v in root.get("entries", {}).items()
            }
            self.bank_digest_baseline = dict(root.get("bank_digest_baseline", {}))
            self.bank_type_pending = {
                int(k): bool(v) for k, v in root.get("bank_type_pending", {}).items()
            }
        except Exception as e:
            log.warning("index load failed: %s", e)
        return self

    @contextlib.contextmanager
    def defer_saves(self):
        """Coalesce every save() inside the block into ONE save on exit.

        index.json is a full rewrite of every tracked slot (a real library is
        ~2 MB of JSON) followed by an fsync, so a bulk operation that saves
        per item costs O(n) full rewrites — which is what made Auto-Fill and
        multi-item placement lock the UI up for minutes on a network-mounted
        data directory. The op-log is the write-ahead authority and is still
        appended per item (see OpLog's class docstring), so a crash inside the
        block loses nothing that rebuild_current_from_oplog can't fold back.

        Re-entrant and safe to nest; only the outermost exit writes."""
        with self._defer_lock:
            self._defer_depth += 1
        try:
            yield self
        finally:
            with self._defer_lock:
                self._defer_depth -= 1
                flush = self._defer_depth == 0 and self._save_pending
                if flush:
                    self._save_pending = False
            if flush:
                self._save_now()

    def save(self) -> None:
        with self._defer_lock:
            if self._defer_depth > 0:
                self._save_pending = True
                return
        self._save_now()

    def _save_now(self) -> None:
        try:
            with self._lock:
                self.root.mkdir(parents=True, exist_ok=True)
                root = {
                    "entries": {k: v.to_dict() for k, v in self.entries.items()},
                    "bank_digest_baseline": self.bank_digest_baseline,
                    # JSON object keys are always strings; int-ified back on load() above.
                    "bank_type_pending": {str(k): v for k, v in self.bank_type_pending.items()},
                }
                _storage.atomic_write_text(self._path(), json.dumps(root, indent=2))
            self.last_save_error = None
        except Exception as e:
            self.last_save_error = str(e)
            log.error("index save failed: %s", e)

    # -- entry access -----------------------------------------------------------

    def get(self, obj_type: int, bank: int, number: int) -> Optional[LocalIndexEntry]:
        return self.entries.get(self.key(obj_type, bank, number))

    # Optional observer hook, the Python mirror of LocalLibraryCache's
    # SlotMutating event: called with (obj_type, bank, number, prior_entry)
    # immediately BEFORE every index write/removal below. Set by
    # LibrarianUndoRecorder so a scope's captures land automatically no matter
    # how deep inside LocalEditOps/BatchLibrarian an edit happens. An undo's
    # own restores suppress capture via the recorder's _restoring flag.
    slot_mutating_cb = None

    def set_entry(self, obj_type: int, bank: int, number: int, entry: LocalIndexEntry) -> None:
        """Install/replace the entry for this slot. Does NOT itself refuse an
        overwrite of a dirty entry — exactly like a dict assignment. Callers
        (the pull pipeline, built in a follow-up task) must check
        `get(...).is_dirty` first and route to mark_conflicted() instead of
        calling this when the object is locally dirty AND its hardware bank
        changed — never call this unconditionally from a pull."""
        key = self.key(obj_type, bank, number)
        prior = self.entries.get(key)
        if self.slot_mutating_cb is not None:
            self.slot_mutating_cb(obj_type, bank, number, prior)
        self.entries[key] = entry

    def delete(self, obj_type: int, bank: int, number: int) -> None:
        key = self.key(obj_type, bank, number)
        prior = self.entries.get(key)
        if self.slot_mutating_cb is not None:
            self.slot_mutating_cb(obj_type, bank, number, prior)
        self.entries.pop(key, None)

    def find_by_content_hash(self, obj_type: int, content_hash: str
                             ) -> Optional[Tuple[int, int]]:
        """First (bank, number) of obj_type whose current content IS
        content_hash — the Python mirror of LocalLibraryCache.
        FindByContentHash. Pending-delete entries never match (a slot marked
        for deletion is not a reuse target). Returns None when nothing
        matches, so a caller falls through to placing a fresh copy."""
        prefix = f"{obj_type}:"
        for key, e in self.entries.items():
            if e.pending_delete or e.current_hash != content_hash:
                continue
            if not key.startswith(prefix):
                continue
            _, bank_s, num_s = key.split(":")
            return (int(bank_s), int(num_s))
        return None

    def mark_conflicted(self, obj_type: int, bank: int, number: int, conflicted: bool = True) -> None:
        e = self.get(obj_type, bank, number)
        if e is not None:
            e.conflicted = conflicted
            e.modified_utc = _now_iso()

    def mark_pending_delete(self, obj_type: int, bank: int, number: int, pending: bool = True) -> None:
        e = self.get(obj_type, bank, number)
        if e is not None:
            e.pending_delete = pending
            e.modified_utc = _now_iso()

    def set_bank_digest_baseline(self, obj_type: int, bank: int, digest_hex: str) -> None:
        self.bank_digest_baseline[self.bank_key(obj_type, bank)] = digest_hex

    # -- pending whole-bank HD-1/EXi type change -----------------------------
    # Port of LocalLibraryCache.cs's Set/Get/ClearPendingBankTypeChange. Stages an intentional
    # conversion for a Program bank that changeset_sync.py's build_changeset will turn into a
    # func-0x7C reformat; the caller must clear it once that reformat actually succeeds on
    # hardware (confirmed from source, see bank_type_pending's field docstring above) — this
    # class does not clear it automatically, matching set_entry/mark_conflicted's own "caller's
    # job" division of responsibility elsewhere in this file.

    def set_pending_bank_type_change(self, bank: int, to_exi: bool) -> None:
        self.bank_type_pending[bank] = to_exi

    def clear_pending_bank_type_change(self, bank: int) -> None:
        self.bank_type_pending.pop(bank, None)

    def get_pending_bank_type_change(self, bank: int) -> Optional[bool]:
        """bank -> staged target is_exi, or None if untouched. Exact shape of
        changeset_sync.py's `PendingBankTypeChange` callable type, so `index.
        get_pending_bank_type_change` can be passed directly as build_changeset's/
        sync_library's/commit_changes' `pending_bank_type_change` argument."""
        return self.bank_type_pending.get(bank)

    # -- disaster recovery --------------------------------------------------

    @staticmethod
    def rebuild_current_from_oplog(entries: Iterable[dict]) -> Dict[str, str]:
        """Replay the op-log, last-writer-wins per "type:bank:number" by
        timestamp order (the log is append-only, so file order IS
        chronological order). Only reconstructs current_hash; baselines only
        ever move via a pull/push recording call, never via a fold — mirrors
        LocalLibraryIndex.RebuildCurrentFromOpLog exactly. `entries` is
        typically OpLog.replay()'s output."""
        current: Dict[str, str] = {}
        for entry in entries:
            for t in entry.get("targets", []):
                k = LocalLibraryIndex.key(t["obj_type"], t["bank"], t["number"])
                if t.get("result_hash") == LocalLibraryIndex.DELETED_TOMBSTONE:
                    current.pop(k, None)   # a committed deletion removes the slot
                else:
                    current[k] = t.get("result_hash", "")
        return current


# ── Op-log (write-ahead, append-only) ───────────────────────────────────────

class OpLog:
    """Append-only log at {root}/oplog.jsonl — one JSON object per line. This
    is the AUTHORITATIVE history; LocalLibraryIndex.current_hash is a cached
    fold of it, always rebuildable if lost
    (LocalLibraryIndex.rebuild_current_from_oplog).

    Every mutation to the local library must be appended here BEFORE the
    index is updated (write-ahead), so a crash mid-write is always
    recoverable by replaying the log. A "Discard" entry (target hash = the
    object's baseline hash) is itself logged here — a revert is an auditable
    event, not an erasure (same discipline as OpLog.cs).

    No in-memory "display mirror" here (unlike OpLog.cs's _displayMirror) —
    that existed purely to avoid re-reading a growing file over an SMB share
    on every UI refresh. This port's DataDir is local disk, so replay() just
    reads the file each time; a caller wanting a UI history view can call
    replay() directly with no separate cache to keep in step.
    """

    def __init__(self, root: Optional[pathlib.Path] = None):
        self.root = pathlib.Path(root) if root is not None else local_library_dir()
        self._lock = threading.Lock()
        self._buffer: List[dict] = []
        self._defer_lock = threading.RLock()
        self._defer_depth = 0
        #: Last append failure, or None — see LocalLibraryIndex.last_save_error.
        self.last_append_error: Optional[str] = None

    def _path(self) -> pathlib.Path:
        return self.root / "oplog.jsonl"

    @contextlib.contextmanager
    def defer_appends(self):
        """Buffer appends inside the block and write them as ONE open/append/
        close on exit, preserving order.

        Same motivation as LocalLibraryIndex.defer_saves: on a network-mounted
        data directory the per-append open/close round trip dominates a bulk
        operation. Deliberately narrower than deferring the index save — the
        log is the recovery source of truth, so blocks should stay short (one
        Auto-Fill chunk, one batch placement), never a whole session."""
        with self._defer_lock:
            self._defer_depth += 1
        try:
            yield self
        finally:
            with self._defer_lock:
                self._defer_depth -= 1
                flush = self._defer_depth == 0
            if flush:
                self.flush()

    def append(self, op: dict) -> None:
        """Write-ahead append of one operation as a single JSON line. A
        plain buffered write (open/append/close) — no fsync, matching the
        durability level storage.py itself uses for settings.json et al."""
        with self._defer_lock:
            if self._defer_depth > 0:
                self._buffer.append(op)
                return
        self.append_many([op])

    def flush(self) -> None:
        """Write out anything buffered by defer_appends()."""
        with self._defer_lock:
            pending, self._buffer = self._buffer, []
        if pending:
            self.append_many(pending)

    def append_many(self, ops: Iterable[dict]) -> None:
        """Append several operations in one open/append/close, in order."""
        lines = "".join(json.dumps(op) + "\n" for op in ops)
        if not lines:
            return
        with self._lock:
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                with open(self._path(), "a", encoding="utf-8") as f:
                    f.write(lines)
                self.last_append_error = None
            except Exception as e:
                self.last_append_error = str(e)
                log.error("oplog append failed: %s", e)

    def replay(self) -> Iterator[dict]:
        """Disk-authoritative full read — the fold/recovery source of truth.
        ALWAYS reads the file (mirrors OpLog.ReadAll, not OpLog.ReadForDisplay
        — this port has no display-mirror shortcut to bypass, see class
        docstring)."""
        self.flush()   # anything buffered by defer_appends belongs in the read
        with self._lock:
            path = self._path()
            if not path.exists():
                return
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except Exception as e:
                log.warning("oplog read failed: %s", e)
                return
        for line in lines:
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except Exception as e:
                log.warning("oplog line parse failed: %s", e)

    def clear_all(self) -> None:
        """Wipe the audit trail (e.g. user 'Clear History'). Doesn't touch
        index.json/the CAS blob store — only the log; after this,
        rebuild_current_from_oplog's disaster-recovery fallback has nothing
        left to replay (mirrors OpLog.ClearAll)."""
        with self._defer_lock:
            self._buffer.clear()
        with self._lock:
            try:
                self._path().unlink(missing_ok=True)
            except Exception as e:
                log.warning("oplog clear failed: %s", e)


# ── Self-test (python local_library_store.py) ───────────────────────────────

def _selftest() -> None:
    import shutil
    import sys
    import tempfile

    fails: list = []

    def check(name, cond):
        if not cond:
            fails.append(name)

    root = pathlib.Path(tempfile.gettempdir()) / "kronos_selftest_local_library"
    if root.exists():
        shutil.rmtree(root)
    try:
        # ── BlobStore: put/get round-trip, content-addressed dedup ──
        blobs = BlobStore(root)
        body_a = bytes([1, 2, 3, 4])
        body_b = bytes([1, 2, 3, 4])   # same content as A
        body_c = bytes([9, 9, 9])
        hash_a = blobs.put(body_a)
        hash_b = blobs.put(body_b)
        hash_c = blobs.put(body_c)
        check("cas-dedup", hash_a == hash_b)
        check("cas-distinct", hash_a != hash_c)
        check("cas-roundtrip", blobs.get(hash_a) == body_a)
        check("cas-missing-none", blobs.get("0" * 40) is None)
        check("cas-empty-sentinel-none", blobs.get("") is None)
        check("cas-exists", blobs.exists(hash_a) and not blobs.exists("f" * 40))
        blob_files = list((root / "blobs").rglob("*.bin"))
        check("cas-dedup-one-file", len(blob_files) == 2)   # a/b share one file, c is a second

        # ── OpLog: append + replay round-trip ──
        oplog = OpLog(root)
        entry1 = {
            "id": "11111111-1111-1111-1111-111111111111",
            "timestamp_utc": _now_iso(),
            "op_kind": "Rename",
            "targets": [{"obj_type": 0, "bank": 0x00, "number": 3, "result_hash": hash_a}],
            "description": "Renamed Program I-A:003",
            "sync_batch_id": None,
            "synced_at_utc": None,
        }
        oplog.append(entry1)
        replayed = list(oplog.replay())
        check("oplog-roundtrip-count", len(replayed) == 1)
        check("oplog-roundtrip-fields", replayed and replayed[0]["op_kind"] == "Rename"
              and replayed[0]["targets"][0]["result_hash"] == hash_a)

        # ── LocalLibraryIndex: save/load round-trip ──
        idx = LocalLibraryIndex(root)
        idx.set_entry(0, 0x00, 3, LocalIndexEntry(
            version=5, baseline_hash=hash_a, current_hash=hash_a,
            display_name="Test Name", created_utc=_now_iso(), modified_utc=_now_iso(),
            conflicted=False))
        idx.set_bank_digest_baseline(0, 0x00, "deadbeef")
        idx.save()

        reloaded = LocalLibraryIndex(root).load()
        re_entry = reloaded.get(0, 0x00, 3)
        check("index-roundtrip-entry", re_entry is not None and re_entry.current_hash == hash_a)
        check("index-roundtrip-name", re_entry is not None and re_entry.display_name == "Test Name")
        check("index-roundtrip-digest",
              reloaded.bank_digest_baseline.get(LocalLibraryIndex.bank_key(0, 0x00)) == "deadbeef")
        check("index-roundtrip-clean", re_entry is not None and not re_entry.is_dirty)

        # ── disaster recovery: oplog replay reconstructs an equivalent index ──
        folded = LocalLibraryIndex.rebuild_current_from_oplog(oplog.replay())
        check("index-fold-matches", folded.get(LocalLibraryIndex.key(0, 0x00, 3)) == hash_a)
        check("index-fold-equivalent-to-saved",
              folded == {k: v.current_hash for k, v in reloaded.entries.items()})

        # ── mark_conflicted / mark_pending_delete ──
        idx.mark_conflicted(0, 0x00, 3, True)
        check("mark-conflicted", idx.get(0, 0x00, 3).conflicted is True)
        idx.mark_pending_delete(0, 0x00, 3, True)
        check("mark-pending-delete", idx.get(0, 0x00, 3).pending_delete is True)

        # ── dirty detection: the core invariant this store exists to protect ──
        dirty_entry = LocalIndexEntry(baseline_hash=hash_a, current_hash=hash_c)
        clean_entry = LocalIndexEntry(baseline_hash=hash_a, current_hash=hash_a)
        check("is-dirty-true", dirty_entry.is_dirty)
        check("is-dirty-false", not clean_entry.is_dirty)

        # ── deletion tombstone removes the slot on fold ──
        oplog.append({
            "id": "22222222-2222-2222-2222-222222222222",
            "timestamp_utc": _now_iso(), "op_kind": "Delete",
            "targets": [{"obj_type": 0, "bank": 0x00, "number": 3,
                         "result_hash": LocalLibraryIndex.DELETED_TOMBSTONE}],
            "description": "Deleted Program I-A:003", "sync_batch_id": None, "synced_at_utc": None,
        })
        folded2 = LocalLibraryIndex.rebuild_current_from_oplog(oplog.replay())
        check("index-fold-tombstone-removes", LocalLibraryIndex.key(0, 0x00, 3) not in folded2)

        # ── clear_all wipes the log ──
        oplog.clear_all()
        check("oplog-clear", list(oplog.replay()) == [])

        # ── pending bank-type change: set/get, survives save/load round trip, then clear ──
        idx.set_pending_bank_type_change(0x02, True)
        check("banktype-pending-get", idx.get_pending_bank_type_change(0x02) is True)
        check("banktype-pending-unset-is-none", idx.get_pending_bank_type_change(0x03) is None)
        idx.save()

        reloaded_bt = LocalLibraryIndex(root).load()
        check("banktype-pending-roundtrip", reloaded_bt.get_pending_bank_type_change(0x02) is True)
        reloaded_bt.clear_pending_bank_type_change(0x02)
        check("banktype-pending-cleared", reloaded_bt.get_pending_bank_type_change(0x02) is None)
        reloaded_bt.save()
        reloaded_bt2 = LocalLibraryIndex(root).load()
        check("banktype-pending-clear-persists", reloaded_bt2.get_pending_bank_type_change(0x02) is None)

        # ── changeset_sync integration: index.get_pending_bank_type_change passed directly as
        # build_changeset's `pending_bank_type_change` callable (the exact consumer this field
        # exists to back) ──
        import changeset_sync as _csync
        from librarian_sysex import OBJ_PROGRAM as _OBJ_PROGRAM
        from session_dependency_clipboard import SessionDependencyClipboard as _Clipboard
        from pcg_file import WIRE_SIZE_EXI as _WIRE_SIZE_EXI

        bt_hash = blobs.put(bytes(_WIRE_SIZE_EXI))   # EXi-shaped body
        idx.set_entry(_OBJ_PROGRAM, 0x09, 0, LocalIndexEntry(
            version=1, baseline_hash=hash_a, current_hash=bt_hash,
            display_name="BT-Test", created_utc=_now_iso(), modified_utc=_now_iso()))
        idx.set_bank_digest_baseline(_OBJ_PROGRAM, 0x09, "bt-bankdigest-v1")

        plan_no_stage = _csync.build_changeset(
            idx, blobs, _Clipboard(),
            get_live_digest=lambda bk: "bt-bankdigest-v1",
            resolver=lambda t, b, n: True,
            get_live_bank_type=lambda bank: False if bank == 0x09 else None,   # live HD-1, body is EXi
            pending_bank_type_change=idx.get_pending_bank_type_change)
        check("csync-index-method-mismatch-refuses", plan_no_stage.is_refusable)

        idx.set_pending_bank_type_change(0x09, True)
        plan_staged = _csync.build_changeset(
            idx, blobs, _Clipboard(),
            get_live_digest=lambda bk: "bt-bankdigest-v1",
            resolver=lambda t, b, n: True,
            get_live_bank_type=lambda bank: False if bank == 0x09 else None,
            pending_bank_type_change=idx.get_pending_bank_type_change)
        check("csync-index-method-staged-not-refusable", not plan_staged.is_refusable)
        check("csync-index-method-reformat-queued", plan_staged.bank_type_changes == [(0x09, True)])

        idx.clear_pending_bank_type_change(0x09)
        check("csync-index-method-cleared-after-stage", idx.get_pending_bank_type_change(0x09) is None)

    finally:
        if root.exists():
            shutil.rmtree(root)

    if fails:
        print("FAIL:", ", ".join(fails))
        sys.exit(1)
    print("local_library_store self-test: OK")


if __name__ == "__main__":
    _selftest()
