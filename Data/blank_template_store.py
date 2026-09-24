r"""
Blank/initialized object-body template store — port of
Core/LocalLibrary/BlankTemplateStore.cs, plus the erase-time template preference from
Core/LocalLibrary/EraseBody.cs's header comment and ChangesetBuilder.cs's actual usage
(`BlankTemplates.EnsureAsync(...) ?? EraseBody.Build(...)`).

The Kronos SysEx protocol has no delete opcode and no "empty slot" encoding — func 0x73
always writes a real body, and every Program/Combi/Set-List slot always contains
something. The only faithful way to make a slot read as "erased" is to overwrite it
with the exact bytes the instrument itself uses for a blank object of that kind. Those
bytes never change for a given Kronos OS, so they are captured ONCE (from a
currently-blank slot) and reused forever — this module is that cache, built on top of
local_library_store.BlobStore rather than a separate bespoke file store (dedup + the
same {root}/blobs/<hh>/<sha1>.bin layout the rest of the local library uses). A small
{root}/blank_templates.json side-map records which blob hash belongs to which object
kind, since BlobStore itself is purely content-addressed and has no notion of "kind".

One template per kind, keyed the same as BlankTemplateStore.cs's `Key()`: Programs
split by wire format (an EXi body and an HD-1 body are different bytes for the same
logical "blank Program"), Combi/Set List do not split. There is no separate "EXi"
object kind — EXi is the is_exi wire-format flag on Program, exactly as in the C#
source (LibObj has Program/Combi/SetList only).

Source-slot configurability (confirmed, not guessed): BlankTemplateStore.cs's
`BlankTemplates.SourceFor` hardcodes "The user's current-blank slots (2026-07): U-EE000
EXi program, U-GG000 HD-1 program, U-A000 combi, Set List 127" directly as a source
comment. Per the porting-gap analysis this was flagged as inherently
per-installation/per-factory-state (which slots are blank on THIS Kronos, at THIS
point in time) rather than a fixed protocol constant, so this port does NOT copy those
numbers as a literal here — `source_slot()` below reads them from
AppSettings.get_blank_template_source() (app_settings.py's
DEFAULT_BLANK_TEMPLATE_SOURCE_SLOTS supplies the same 2026-07 values only as the
out-of-the-box default, overridable via settings.json).

Erase mechanism — confirmed from the C# source, and it is NOT the single atomic step
the name suggests. It's genuinely two-phase:
  1. Core/LocalLibrary/LocalEditOps.cs `SetPendingDelete` -> LocalLibraryCache.
     SetPendingDelete: flips the PendingDelete flag ONLY. CurrentHash/BaselineHash are
     untouched — "mark for delete" is purely a flag at this point.
  2. Core/LocalLibrary/ChangesetBuilder.cs (step 4b, ~line 155-183): at commit/push
     time, prefers a captured real blank body (BlankTemplateStore, via
     BlankTemplates.EnsureAsync) over EraseBody.Build's derived name-blanked fallback,
     and queues it as the slot's new write. Only after SyncPipeline.cs confirms a
     SUCCESSFUL hardware write does LocalLibraryCache.RecordPushSuccesses rebuild the
     entry with the blank body as its new baseline/current AND clear PendingDelete back
     to false (SyncPipeline.cs comment: "the LOCAL slot advances to that same blank
     body... clean and with its pending-delete flag cleared").
This module has no hardware-push step of its own (that is a future SyncPipeline port's
job), so `erase()` here deliberately stages the blank content locally (current_hash ->
the template's hash) while LEAVING pending_delete=True — mirroring phase 1's "flagged,
not yet resolved" state plus an offline-friendly local content update, but stopping
short of phase 2's "confirmed synced, flag cleared" claim, which would be false without
an actual successful hardware write.
"""
from __future__ import annotations

import logging
import json
import pathlib
from typing import Optional, Tuple

import Data.librarian_sysex as lsx
from Models.app_settings import AppSettings, DEFAULT_BLANK_TEMPLATE_SOURCE_SLOTS
import Models.storage as _storage
from Data.local_library_store import BlobStore, LocalLibraryIndex

log = logging.getLogger(__name__)


def template_key(obj_type: int, is_exi: bool = True) -> str:
    """Mirrors BlankTemplateStore.cs's `Key()` switch."""
    if obj_type == lsx.OBJ_PROGRAM:
        return "program_exi" if is_exi else "program_hd1"
    if obj_type == lsx.OBJ_COMBI:
        return "combi"
    if obj_type == lsx.OBJ_SET_LIST:
        return "setlist"
    return f"obj{obj_type:02x}"


class BlankTemplateStore:
    """Durable store of one real blank body per object kind, built on
    local_library_store.BlobStore (reused, not reinvented) plus a small key->sha1
    side-map persisted at {root}/blank_templates.json."""

    def __init__(self, blobs: Optional[BlobStore] = None, root: Optional[pathlib.Path] = None):
        self.blobs = blobs if blobs is not None else BlobStore(root)
        self.root = self.blobs.root
        self._map: dict = {}
        self._load_map()

    def _map_path(self) -> pathlib.Path:
        return self.root / "blank_templates.json"

    def _load_map(self) -> None:
        p = self._map_path()
        if not p.exists():
            return
        try:
            self._map = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("map load failed: %s", e)
            self._map = {}

    def _save_map(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            _storage.atomic_write_text(self._map_path(), json.dumps(self._map, indent=2))
        except Exception as e:
            log.error("map save failed: %s", e)

    def _template_hash(self, obj_type: int, is_exi: bool = True) -> Optional[str]:
        return self._map.get(template_key(obj_type, is_exi))

    def blank_body_for(self, obj_type: int, is_exi: bool = True) -> Optional[bytes]:
        """Return the stored blank body for (obj_type, is_exi), or None if never
        captured (mirrors BlankTemplateStore.cs's `Get`). Caller falls back to
        EraseBody's derived blank when this returns None (offline AND never
        captured) — replicated here as the same "None means try the fallback"
        contract, though the fallback itself is out of this module's scope."""
        sha1 = self._template_hash(obj_type, is_exi)
        if not sha1:
            return None
        return self.blobs.get(sha1)

    def set_blank_body(self, obj_type: int, body: bytes, is_exi: bool = True) -> str:
        """Capture/persist `body` as obj_type's blank template. Idempotent — the same
        bytes captured twice hash to the same blob (mirrors BlankTemplateStore.cs's
        `Set`, minus its own file-per-kind scheme; BlobStore's content-addressing
        gives the same dedup for free). Returns the sha1 hex digest."""
        sha1 = self.blobs.put(body)
        self._map[template_key(obj_type, is_exi)] = sha1
        self._save_map()
        return sha1

    @staticmethod
    def source_slot(obj_type: int, is_exi: bool = True,
                     settings: Optional[AppSettings] = None) -> Tuple[int, int]:
        """(bank, number) to source obj_type's blank template from, for a future
        hardware-capture routine (mirrors BlankTemplates.SourceFor — "a capture hint
        only", per the C# class comment: it does not assume the slot STAYS blank).
        Reads AppSettings.get_blank_template_source() when `settings` is given
        (the configurable path this was ported to use instead of a hardcoded
        literal); falls back to DEFAULT_BLANK_TEMPLATE_SOURCE_SLOTS otherwise."""
        key = template_key(obj_type, is_exi)
        if settings is not None:
            return settings.get_blank_template_source(key)
        pair = DEFAULT_BLANK_TEMPLATE_SOURCE_SLOTS.get(key, [0, 0])
        return (int(pair[0]), int(pair[1]))

    def erase(self, index: LocalLibraryIndex, obj_type: int, bank: int, number: int,
              is_exi: bool = True) -> bool:
        """Stage a local erase of (obj_type, bank, number): overwrite the index
        entry's current_hash with the blank template's hash and set
        pending_delete=True (see module docstring for exactly how this maps onto,
        and deliberately stops short of, the C# source's real two-phase mechanism).

        Returns False (no-op, nothing mutated) if there is no index entry for the
        slot, or no blank template has been captured yet for this obj_type/is_exi.
        """
        entry = index.get(obj_type, bank, number)
        if entry is None:
            return False
        sha1 = self._template_hash(obj_type, is_exi)
        if sha1 is None:
            return False
        entry.current_hash = sha1
        index.mark_pending_delete(obj_type, bank, number, True)
        return True


# ── Self-test (python blank_template_store.py) ──────────────────────────────

def _selftest() -> None:
    import shutil
    import sys
    import tempfile
    from datetime import datetime, timezone

    from Data.local_library_store import LocalIndexEntry

    fails: list = []

    def check(name, cond):
        if not cond:
            fails.append(name)

    root = pathlib.Path(tempfile.gettempdir()) / "kronos_selftest_blank_templates"
    if root.exists():
        shutil.rmtree(root)
    try:
        blobs = BlobStore(root)
        store = BlankTemplateStore(blobs)

        # ── capture + lookup round-trip ──
        check("missing-before-capture", store.blank_body_for(lsx.OBJ_PROGRAM, True) is None)

        fake_blank_exi = bytes([0xAA]) * 16
        fake_blank_hd1 = bytes([0xBB]) * 12
        fake_blank_combi = bytes([0xCC]) * 20
        fake_blank_setlist = bytes([0xDD]) * 8

        store.set_blank_body(lsx.OBJ_PROGRAM, fake_blank_exi, is_exi=True)
        store.set_blank_body(lsx.OBJ_PROGRAM, fake_blank_hd1, is_exi=False)
        store.set_blank_body(lsx.OBJ_COMBI, fake_blank_combi)
        store.set_blank_body(lsx.OBJ_SET_LIST, fake_blank_setlist)

        check("program-exi-roundtrip", store.blank_body_for(lsx.OBJ_PROGRAM, True) == fake_blank_exi)
        check("program-hd1-roundtrip", store.blank_body_for(lsx.OBJ_PROGRAM, False) == fake_blank_hd1)
        check("program-exi-hd1-distinct",
              store.blank_body_for(lsx.OBJ_PROGRAM, True) != store.blank_body_for(lsx.OBJ_PROGRAM, False))
        check("combi-roundtrip", store.blank_body_for(lsx.OBJ_COMBI) == fake_blank_combi)
        check("setlist-roundtrip", store.blank_body_for(lsx.OBJ_SET_LIST) == fake_blank_setlist)

        # ── persistence: a fresh store instance over the same root sees the same map ──
        reopened = BlankTemplateStore(BlobStore(root))
        check("reopen-roundtrip", reopened.blank_body_for(lsx.OBJ_PROGRAM, True) == fake_blank_exi)

        # ── source_slot: default (no settings) matches the documented default ──
        default_program_exi = store.source_slot(lsx.OBJ_PROGRAM, True)
        check("source-default-program-exi", default_program_exi == (0x4B, 0))
        check("source-default-setlist", store.source_slot(lsx.OBJ_SET_LIST) == (0, 127))

        # ── source_slot: an AppSettings override is honored (not hardcoded) ──
        settings = AppSettings()
        settings.blank_template_source_slots["program_exi"] = [0x10, 3]
        check("source-override-honored", store.source_slot(lsx.OBJ_PROGRAM, True, settings) == (0x10, 3))
        check("source-override-other-keys-unaffected",
              store.source_slot(lsx.OBJ_COMBI, settings=settings) == (0x40, 0))

        # ── erase: writes blank content through LocalLibraryIndex + sets pending_delete ──
        idx = LocalLibraryIndex(root)
        now = datetime.now(timezone.utc).isoformat()
        existing_hash = blobs.put(bytes([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]))
        idx.set_entry(lsx.OBJ_PROGRAM, 0x00, 3, LocalIndexEntry(
            version=1, baseline_hash=existing_hash, current_hash=existing_hash,
            display_name="Fake Existing Program", created_utc=now, modified_utc=now,
            is_exi=True))

        check("erase-missing-entry-fails", not store.erase(idx, lsx.OBJ_PROGRAM, 0x00, 99, is_exi=True))
        check("erase-no-template-fails",
              not store.erase(idx, lsx.OBJ_COMBI, 0x99, 0, is_exi=True))  # entry doesn't exist either -> False

        ok = store.erase(idx, lsx.OBJ_PROGRAM, 0x00, 3, is_exi=True)
        check("erase-returns-true", ok)

        erased_entry = idx.get(lsx.OBJ_PROGRAM, 0x00, 3)
        check("erase-content-is-blank-template",
              erased_entry is not None and blobs.get(erased_entry.current_hash) == fake_blank_exi)
        check("erase-pending-delete-set", erased_entry is not None and erased_entry.pending_delete is True)
        check("erase-baseline-unchanged-still-dirty",
              erased_entry is not None and erased_entry.is_dirty)  # baseline untouched, current changed

    finally:
        if root.exists():
            shutil.rmtree(root)

    if fails:
        print("FAIL:", ", ".join(fails))
        sys.exit(1)
    print("blank_template_store self-test: OK")


if __name__ == "__main__":
    _selftest()
