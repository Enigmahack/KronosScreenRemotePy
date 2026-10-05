"""Run from the repository root: python -B tests\\test_sync_safety.py."""
import hashlib
import os
from pathlib import Path
import shutil
import sys
import threading
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ROOT = Path(__file__).resolve().parents[1] / f".sync-safety-{uuid.uuid4().hex}"
os.environ["KRONOS_DATA_DIR"] = str(ROOT)
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from Commands.session_dependency_clipboard import SessionDependencyClipboard
from Data.changeset_sync import build_changeset
from Data.librarian_sysex import (
    OBJ_COMBI, OBJ_PROGRAM, OBJ_SET_LIST, set_combi_timbre_ref, set_setlist_slot_ref,
)
from Data.local_library_store import BlobStore, LocalIndexEntry, LocalLibraryIndex
from Data.pcg_file import WIRE_SIZE_EXI, WIRE_SIZE_HD1
from Views.librarian_shell_window import LibrarianShellWindow, SYNC_PUSH_ONLY


class Instrument:
    def __init__(self):
        self.can_dump = True
        self.events = []
        self.objects = {(OBJ_PROGRAM, 0, n): SimpleNamespace(
            version=0, body=bytes([n]) + bytes(WIRE_SIZE_HD1 - 1)) for n in range(128)}
        self.fail_write = self.fail_store = self.fail_backup = self.fail_reformat = False
        self.cancel_on_reformat = None
        self.before_backup = None
        self.missing = None

    def bank_digest(self, obj, bank):
        bodies = [d.body for (t, b, n), d in sorted(self.objects.items())
                  if (t, b) == (obj, bank)]
        return hashlib.sha1(b"".join(bodies)).digest()

    def dump_object_parsed(self, obj, bank, number, **kwargs):
        self.events.append(("dump", obj, bank, number))
        if self.missing == (obj, bank, number):
            return None
        return self.objects.get((obj, bank, number))

    def backup_objects(self, ops, path):
        self.events.append(("backup", [(o.obj, o.bank, o.index, o.body) for o in ops], path))
        if self.fail_backup:
            raise OSError("backup disk unavailable")
        assert Path(path).is_relative_to(ROOT), "backup escaped isolated test root"
        Path(path).write_bytes(b"".join(o.body for o in ops))
        if self.before_backup:
            self.before_backup()

    def change_program_bank_type(self, bank, is_exi):
        self.events.append(("reformat", bank))
        if self.fail_reformat:
            return 3
        size = WIRE_SIZE_EXI if is_exi else WIRE_SIZE_HD1
        for n in range(128):
            self.objects[(OBJ_PROGRAM, bank, n)] = SimpleNamespace(version=0, body=bytes(size))
        if self.cancel_on_reformat:
            self.cancel_on_reformat()
        return 0

    def write_object(self, op):
        self.events.append(("write", op.obj, op.bank, op.index))
        if self.fail_write and op.index == 1:
            return 3
        self.objects[(op.obj, op.bank, op.index)] = SimpleNamespace(version=op.version, body=op.body)
        return 0

    def store_bank(self, obj, bank):
        self.events.append(("store", obj, bank))
        return 3 if self.fail_store else 0


class Harness:
    """Bind the real worker/adapters without constructing a window or running its probes."""
    def __init__(self, root, *, force=False, conversion=False):
        self._service = Instrument()
        self._index = LocalLibraryIndex(root)
        self._blobs = BlobStore(root)
        self._clipboard = SessionDependencyClipboard()
        self._sync_cancel = threading.Event()
        self._transaction_lock = threading.RLock()
        self._hardware_transaction_active = False
        self._sync_thread_running = True
        self._closed = False
        self._armed_digests = {}
        self._run_mode, self._run_full, self._run_force = SYNC_PUSH_ONLY, False, force
        self.logs, self.done = [], None
        self._log = self.logs.append
        self._emit_progress = self.logs.append
        self._emit_sync_done = lambda *args: setattr(self, "done", args)
        for n in (0, 1):
            old = self._service.objects[(OBJ_PROGRAM, 0, n)].body
            new = bytes([n + 10]) + bytes((WIRE_SIZE_EXI if conversion else WIRE_SIZE_HD1) - 1)
            self._index.set_entry(OBJ_PROGRAM, 0, n, LocalIndexEntry(
                version=0, baseline_hash=self._blobs.put(old), current_hash=self._blobs.put(new)))
        self._index.set_bank_digest_baseline(OBJ_PROGRAM, 0, self._get_live_digest("0:0"))
        if conversion:
            self._index.set_pending_bank_type_change(0, True)

    def __getattr__(self, name):
        method = getattr(LibrarianShellWindow, name)
        if isinstance(method, property):
            return method.__get__(self, type(self))
        return MethodType(method, self)

    def run(self):
        self._sync_worker()
        assert self.done is not None, "worker did not report completion"
        return self.done[1], self.done[2]

    def events(self, kind):
        return [e for e in self._service.events if e[0] == kind]


class SyncSafetyTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / self.id().rsplit(".", 1)[-1]
        self.root.mkdir(parents=True)
        backups = self.root / "backups"
        backups.mkdir()
        self.backup_patch = patch("Models.storage.backup_dir", return_value=backups)
        self.backup_patch.start()
        self.addCleanup(self.backup_patch.stop)

    def test_force_multiple_writes_store_once(self):
        h = Harness(self.root, force=True)
        h._index.set_bank_digest_baseline(OBJ_PROGRAM, 0, "stale pull baseline")
        _, result = h.run()
        self.assertEqual((result.written, result.failed), (2, 0))
        self.assertEqual(len(h.events("store")), 1)
        self.assertTrue(all(not e.is_dirty for e in h._index.entries.values()))

    def test_normal_multiple_writes_store_once(self):
        h = Harness(self.root)
        _, result = h.run()
        self.assertEqual((result.written, result.failed), (2, 0))
        self.assertEqual(len(h.events("store")), 1)
        self.assertEqual(h._index.bank_digest_baseline["0:0"], h._get_live_digest("0:0"))

    def test_conversion_backup_before_erasure_and_cancel_finishes(self):
        h = Harness(self.root, conversion=True)
        original = h._service.objects[(OBJ_PROGRAM, 0, 127)].body
        h._service.cancel_on_reformat = h._sync_cancel.set
        _, result = h.run()
        self.assertEqual((result.written, result.failed, result.reformatted), (2, 0, 1))
        self.assertFalse(result.cancelled)
        backup = h.events("backup")[0]
        self.assertEqual(len(backup[1]), 128)
        self.assertEqual(backup[1][-1][-1], original)
        self.assertTrue(Path(backup[2]).is_file())
        kinds = [e[0] for e in h._service.events]
        self.assertLess(kinds.index("backup"), kinds.index("reformat"))
        self.assertEqual(len(h.events("store")), 1)
        self.assertIsNone(h._index.get_pending_bank_type_change(0))
        loaded = LocalLibraryIndex(self.root)
        loaded.load()
        self.assertIsNone(loaded.get_pending_bank_type_change(0))
        self.assertNotIn("0:0", loaded.bank_digest_baseline)

    def test_conversion_invalidates_cached_unwritten_slots(self):
        h = Harness(self.root, conversion=True)
        body_hash = h._blobs.put(h._service.objects[(OBJ_PROGRAM, 0, 127)].body)
        h._index.set_entry(OBJ_PROGRAM, 0, 127, LocalIndexEntry(
            version=0, baseline_hash=body_hash, current_hash=body_hash))
        h.run()
        self.assertIsNone(h._index.get(OBJ_PROGRAM, 0, 127))

    def test_conversion_target_format_mismatch_prevents_erasure(self):
        h = Harness(self.root)
        h._index.set_pending_bank_type_change(0, True)
        plan, _ = h.run()
        self.assertTrue(plan.is_refusable)
        self.assertFalse(h.events("reformat") or h.events("write") or h.events("store"))

    def test_short_hd1_body_cannot_erase_bank(self):
        h = Harness(self.root, conversion=True)
        for entry in h._index.entries.values():
            entry.current_hash = h._blobs.put(b"short")
        h._index.set_pending_bank_type_change(0, False)
        plan, _ = h.run()
        self.assertTrue(plan.is_refusable)
        self.assertFalse(h.events("reformat") or h.events("write") or h.events("store"))

    def test_conversion_backup_failure_prevents_mutation(self):
        h = Harness(self.root, conversion=True)
        h._service.fail_backup = True
        h.run()
        self.assertFalse(h.events("reformat") or h.events("write") or h.events("store"))
        self.assertTrue(h._index.get_pending_bank_type_change(0))

    def test_conversion_missing_backup_slot_prevents_mutation(self):
        h = Harness(self.root, conversion=True)
        h._service.missing = (OBJ_PROGRAM, 0, 127)
        h.run()
        self.assertFalse(h.events("reformat") or h.events("write") or h.events("store"))

    def test_stale_digest_during_backup_prevents_mutation(self):
        h = Harness(self.root, conversion=True)
        h._service.before_backup = lambda: setattr(
            h._service.objects[(OBJ_PROGRAM, 0, 127)], "body", b"panel edit")
        h.run()
        self.assertFalse(h.events("reformat") or h.events("write") or h.events("store"))

    def test_cancel_during_backup_prevents_erasure(self):
        h = Harness(self.root, conversion=True)
        h._service.before_backup = h._sync_cancel.set
        h.run()
        self.assertFalse(h.events("reformat") or h.events("write") or h.events("store"))

    def test_failed_refill_never_stores_or_clears_conversion(self):
        h = Harness(self.root, conversion=True)
        h._service.fail_write = True
        _, result = h.run()
        self.assertGreater(result.failed, 0)
        self.assertFalse(h.events("store"))
        self.assertTrue(h._index.get_pending_bank_type_change(0))
        self.assertTrue(all(e.is_dirty for e in h._index.entries.values()))

    def test_nonconversion_failed_write_does_not_store_partial_bank(self):
        h = Harness(self.root)
        h._service.fail_write = True
        _, result = h.run()
        self.assertEqual(result.written, 0)
        self.assertFalse(h.events("store"))
        self.assertTrue(all(e.is_dirty for e in h._index.entries.values()))

    def test_cancel_during_write_finishes_bank(self):
        h = Harness(self.root)
        real_write = h._service.write_object

        def cancel_then_write(op):
            h._sync_cancel.set()
            return real_write(op)

        h._service.write_object = cancel_then_write
        _, result = h.run()
        self.assertEqual((result.written, result.failed), (2, 0))
        self.assertFalse(result.cancelled)
        self.assertEqual(len(h.events("store")), 1)

    def test_preserves_plan_version(self):
        h = Harness(self.root)
        h._index.get(OBJ_PROGRAM, 0, 1).version = 17
        h.run()
        self.assertEqual(h._service.objects[(OBJ_PROGRAM, 0, 1)].version, 17)

    def test_shutdown_blocked_from_inside_reformat(self):
        h = Harness(self.root, conversion=True)
        permitted = []
        h._service.cancel_on_reformat = lambda: permitted.append(h.request_shutdown())
        h.run()
        self.assertEqual(permitted, [False])
        self.assertFalse(h._hardware_transaction_active)
        self.assertTrue(h.request_shutdown())

    def test_failed_store_leaves_edits_and_conversion_pending(self):
        h = Harness(self.root, conversion=True)
        h._service.fail_store = True
        _, result = h.run()
        self.assertEqual(result.written, 0)
        self.assertGreater(result.failed, 0)
        self.assertTrue(h._index.get_pending_bank_type_change(0))
        self.assertTrue(all(e.is_dirty for e in h._index.entries.values()))

    def test_reformat_rejection_never_writes_or_stores(self):
        h = Harness(self.root, conversion=True)
        h._service.fail_reformat = True
        _, result = h.run()
        self.assertGreater(result.failed, 0)
        self.assertFalse(h.events("write") or h.events("store"))
        self.assertTrue(h._index.get_pending_bank_type_change(0))

    def test_same_plan_erased_dependency_is_refused(self):
        h = Harness(self.root)
        body = bytearray(7810)
        for timbre in range(16):
            set_combi_timbre_ref(body, timbre, 0, 1)
        h._index.set_entry(OBJ_COMBI, 0, 0, LocalIndexEntry(
            version=0, baseline_hash="old", current_hash=h._blobs.put(bytes(body))))
        h._index.get(OBJ_PROGRAM, 0, 1).pending_delete = True
        plan = build_changeset(
            h._index, h._blobs, h._clipboard, lambda key: "same",
            h._local_resolver, force_destructive_write=True)
        self.assertTrue(plan.is_refusable)
        self.assertFalse(plan.entries)

    def test_conflict_excluded_deletion_does_not_break_reference(self):
        h = Harness(self.root)
        body = bytearray(7810)
        for timbre in range(16):
            set_combi_timbre_ref(body, timbre, 0, 1)
        h._index.set_entry(OBJ_COMBI, 0, 0, LocalIndexEntry(
            version=0, baseline_hash="old", current_hash=h._blobs.put(bytes(body))))
        h._index.set_bank_digest_baseline(OBJ_COMBI, 0, "same")
        h._index.get(OBJ_PROGRAM, 0, 1).pending_delete = True
        plan = build_changeset(
            h._index, h._blobs, h._clipboard,
            lambda key: "changed" if key == "0:0" else "same", h._local_resolver)
        self.assertFalse(plan.is_refusable)
        self.assertEqual(len(plan.entries), 1)
        self.assertFalse(plan.entries[0].is_erase)

    def test_setlist_reference_to_erased_combi_is_refused(self):
        h = Harness(self.root)
        slot_body = bytearray(69416)
        slot_body[24:48] = b"Audition".ljust(24, b" ")
        set_setlist_slot_ref(slot_body, 0, func33_bank=0, index=0, type_=0)
        h._index.set_entry(OBJ_SET_LIST, 0, 0, LocalIndexEntry(
            version=0, baseline_hash="old", current_hash=h._blobs.put(bytes(slot_body))))
        h._index.set_entry(OBJ_COMBI, 0, 0, LocalIndexEntry(
            version=0, baseline_hash="old", current_hash=h._blobs.put(bytes(7810)),
            pending_delete=True))
        plan = build_changeset(
            h._index, h._blobs, h._clipboard, lambda key: "same",
            h._local_resolver, force_destructive_write=True)
        self.assertTrue(plan.is_refusable)
        self.assertFalse(plan.entries)

    def test_two_banks_store_once_each(self):
        h = Harness(self.root, force=True)
        for n in range(128):
            h._service.objects[(OBJ_PROGRAM, 1, n)] = SimpleNamespace(
                version=0, body=bytes(WIRE_SIZE_HD1))
        for n in (0, 1):
            old = h._blobs.put(bytes(WIRE_SIZE_HD1))
            new = h._blobs.put(bytes([n + 42]) + bytes(WIRE_SIZE_HD1 - 1))
            h._index.set_entry(OBJ_PROGRAM, 1, n, LocalIndexEntry(
                version=0, baseline_hash=old, current_hash=new))
        _, result = h.run()
        self.assertEqual(result.written, 4)
        self.assertEqual(h.events("store"), [("store", OBJ_PROGRAM, 0), ("store", OBJ_PROGRAM, 1)])

    def test_rejected_second_bank_store_advances_only_committed_bank(self):
        h = Harness(self.root, force=True)
        old = h._blobs.put(bytes(WIRE_SIZE_HD1))
        new = h._blobs.put(b"X" + bytes(WIRE_SIZE_HD1 - 1))
        h._service.objects[(OBJ_PROGRAM, 1, 0)] = SimpleNamespace(version=0, body=bytes(WIRE_SIZE_HD1))
        h._index.set_entry(OBJ_PROGRAM, 1, 0, LocalIndexEntry(
            version=0, baseline_hash=old, current_hash=new))
        real_store = h._service.store_bank
        h._service.store_bank = lambda obj, bank: 3 if bank == 1 else real_store(obj, bank)
        _, result = h.run()
        self.assertEqual((result.written, result.failed), (2, 1))
        self.assertTrue(h._index.get(OBJ_PROGRAM, 1, 0).is_dirty)
        self.assertFalse(h._index.get(OBJ_PROGRAM, 0, 0).is_dirty)

    def test_live_dump_failure_refuses_unprotected_write(self):
        h = Harness(self.root)
        h._service.missing = (OBJ_PROGRAM, 0, 1)
        plan, result = h.run()
        self.assertEqual(result.written, 0)
        self.assertTrue(plan.is_refusable)
        self.assertFalse(h.events("write") or h.events("store"))

    def test_force_overwrite_cannot_back_up_stale_cached_content(self):
        h = Harness(self.root, force=True)
        h._index.set_bank_digest_baseline(OBJ_PROGRAM, 0, "stale pull baseline")
        h._service.objects[(OBJ_PROGRAM, 0, 1)].body = b"unpulled panel edit"
        h._service.missing = (OBJ_PROGRAM, 0, 1)
        plan, result = h.run()
        self.assertEqual(result.written, 0)
        self.assertTrue(plan.is_refusable)
        self.assertFalse(h.events("write") or h.events("store"))

    def test_new_local_object_still_requires_live_destination_backup(self):
        h = Harness(self.root)
        h._index.get(OBJ_PROGRAM, 0, 1).baseline_hash = LocalLibraryIndex.NO_BASELINE_SENTINEL
        h._service.missing = (OBJ_PROGRAM, 0, 1)
        plan, result = h.run()
        self.assertEqual(result.written, 0)
        self.assertTrue(plan.is_refusable)
        self.assertFalse(h.events("write") or h.events("store"))

    def test_write_exception_releases_shutdown_guard_without_store(self):
        h = Harness(self.root, conversion=True)
        h._service.write_object = lambda op: (_ for _ in ()).throw(OSError("bridge dropped"))
        with patch("Views.librarian_shell_window.log.exception"):
            h.run()
        self.assertFalse(h.events("store"))
        self.assertFalse(h._hardware_transaction_active)
        self.assertTrue(h._index.get_pending_bank_type_change(0))
        self.assertTrue(all(e.is_dirty for e in h._index.entries.values()))

    def test_silent_pull_never_starts_push_even_when_forced(self):
        from Views.librarian_shell_window import SYNC_TWO_WAY
        h = Harness(self.root, force=True)
        h._run_mode = SYNC_TWO_WAY
        h._service.bank_digest = lambda *args: None
        h.run()
        self.assertTrue(h.done[0].aborted)
        self.assertFalse(h.events("write") or h.events("store"))

    def test_write_capable_workers_are_not_daemon_threads(self):
        h = Harness(self.root)
        h._show_sync_gate_dialog_if_blocked = lambda: True
        h._force_destructive = lambda: False
        h._begin_run = lambda *args: None
        with patch("Views.librarian_shell_window.threading.Thread") as thread:
            h._start_push_only()
            self.assertFalse(thread.call_args.kwargs["daemon"])
            h._start_two_way()
            self.assertFalse(thread.call_args.kwargs["daemon"])

    def test_close_and_escape_veto_active_transaction(self):
        from PySide6.QtGui import QCloseEvent
        from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
        app = QApplication.instance() or QApplication([])
        w = LibrarianShellWindow.__new__(LibrarianShellWindow)
        QDialog.__init__(w)
        w._transaction_lock = threading.RLock()
        w._hardware_transaction_active = True
        w._sync_cancel = threading.Event()
        w._closed = False
        w._auto_fill_timer = SimpleNamespace(stop=lambda: None)
        event = QCloseEvent()
        with patch.object(QMessageBox, "warning"):
            w.closeEvent(event)
        self.assertFalse(event.isAccepted())
        w.reject()
        self.assertFalse(w._closed)
        self.assertFalse(w._sync_cancel.is_set())
        w._hardware_transaction_active = False
        w.reject()
        self.assertTrue(w._closed)
        w.deleteLater()
        app.processEvents()

    def test_shutdown_cannot_enter_after_cancel_or_close_active_transaction(self):
        h = Harness(self.root, conversion=True)
        self.assertTrue(h.request_shutdown())
        h.run()
        self.assertFalse(h.events("reformat") or h.events("write") or h.events("store"))
        h._hardware_transaction_active = True
        self.assertFalse(h.request_shutdown())


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        shutil.rmtree(ROOT, ignore_errors=True)
