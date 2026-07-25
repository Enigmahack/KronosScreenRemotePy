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

import hashlib
import json
import pathlib
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Dict, Iterable, Iterator, Optional

import storage as _storage


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
    harmless orphan file, not a correctness problem."""

    def __init__(self, root: Optional[pathlib.Path] = None):
        self.root = pathlib.Path(root) if root is not None else local_library_dir()

    @staticmethod
    def compute_hash(data: bytes) -> str:
        return hashlib.sha1(data).hexdigest()

    def _path_for(self, sha1: str) -> pathlib.Path:
        return self.root / "blobs" / sha1[:2] / f"{sha1}.bin"

    def put(self, data: bytes) -> str:
        """Write `data` if not already present; return its sha1 hex digest.
        Idempotent — writing the same content twice is a no-op the second
        time (mirrors LocalObjectStore.Put)."""
        sha1 = self.compute_hash(data)
        path = self._path_for(sha1)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return sha1

    def get(self, sha1: str) -> Optional[bytes]:
        """Return the blob's bytes, or None if absent. `sha1` may be
        LocalLibraryIndex.NO_BASELINE_SENTINEL (empty string) — that is
        never a real SHA-1 hash, so it always returns None rather than
        raising (mirrors LocalObjectStore.TryGet's own guard)."""
        if not sha1:
            return None
        path = self._path_for(sha1)
        return path.read_bytes() if path.exists() else None

    def exists(self, sha1: str) -> bool:
        if not sha1:
            return False
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
        self._lock = threading.Lock()

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
        except Exception as e:
            print(f"[local-library] index load failed: {e}")
        return self

    def save(self) -> None:
        try:
            with self._lock:
                self.root.mkdir(parents=True, exist_ok=True)
                root = {
                    "entries": {k: v.to_dict() for k, v in self.entries.items()},
                    "bank_digest_baseline": self.bank_digest_baseline,
                }
                self._path().write_text(json.dumps(root, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"[local-library] index save failed: {e}")

    # -- entry access -----------------------------------------------------------

    def get(self, obj_type: int, bank: int, number: int) -> Optional[LocalIndexEntry]:
        return self.entries.get(self.key(obj_type, bank, number))

    def set_entry(self, obj_type: int, bank: int, number: int, entry: LocalIndexEntry) -> None:
        """Install/replace the entry for this slot. Does NOT itself refuse an
        overwrite of a dirty entry — exactly like a dict assignment. Callers
        (the pull pipeline, built in a follow-up task) must check
        `get(...).is_dirty` first and route to mark_conflicted() instead of
        calling this when the object is locally dirty AND its hardware bank
        changed — never call this unconditionally from a pull."""
        self.entries[self.key(obj_type, bank, number)] = entry

    def delete(self, obj_type: int, bank: int, number: int) -> None:
        self.entries.pop(self.key(obj_type, bank, number), None)

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

    def _path(self) -> pathlib.Path:
        return self.root / "oplog.jsonl"

    def append(self, op: dict) -> None:
        """Write-ahead append of one operation as a single JSON line. A
        plain buffered write (open/append/close) — no fsync, matching the
        durability level storage.py itself uses for settings.json et al."""
        with self._lock:
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                with open(self._path(), "a", encoding="utf-8") as f:
                    f.write(json.dumps(op) + "\n")
            except Exception as e:
                print(f"[local-library] oplog append failed: {e}")

    def replay(self) -> Iterator[dict]:
        """Disk-authoritative full read — the fold/recovery source of truth.
        ALWAYS reads the file (mirrors OpLog.ReadAll, not OpLog.ReadForDisplay
        — this port has no display-mirror shortcut to bypass, see class
        docstring)."""
        with self._lock:
            path = self._path()
            if not path.exists():
                return
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except Exception as e:
                print(f"[local-library] oplog read failed: {e}")
                return
        for line in lines:
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except Exception as e:
                print(f"[local-library] oplog line parse failed: {e}")

    def clear_all(self) -> None:
        """Wipe the audit trail (e.g. user 'Clear History'). Doesn't touch
        index.json/the CAS blob store — only the log; after this,
        rebuild_current_from_oplog's disaster-recovery fallback has nothing
        left to replay (mirrors OpLog.ClearAll)."""
        with self._lock:
            try:
                self._path().unlink(missing_ok=True)
            except Exception as e:
                print(f"[local-library] oplog clear failed: {e}")


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

    finally:
        if root.exists():
            shutil.rmtree(root)

    if fails:
        print("FAIL:", ", ".join(fails))
        sys.exit(1)
    print("local_library_store self-test: OK")


if __name__ == "__main__":
    _selftest()
