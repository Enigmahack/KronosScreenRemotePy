"""A pending session dependency is resolved from where the Merge Window placed it, even when the body written
there differs from the staged one (its own references were re-pointed), so no content-hash match exists.
Without this the 2-Way Sync gate kept asking about dependencies that were in fact placed. Run: python tests/test_pending_dep_placed_elsewhere.py"""
import os
import sys
import tempfile
import time

os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_pdp_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unittest.mock import MagicMock
from PySide6.QtWidgets import QApplication

app = QApplication([])

from Conversion.data import init_combi_template, init_program_template
from Data.librarian_model import ObjLoc
from Data.librarian_sysex import OBJ_COMBI, OBJ_PROGRAM, combi_timbre_ref, set_combi_timbre_ref
from Data.local_library_store import LocalIndexEntry
from Models.app_settings import AppSettings
from Commands.session_dependency_clipboard import SessionDependencyEntry
import Views.librarian_shell_window as L


class Fake:
    can_dump = True
    is_nautilus = False

    def __getattr__(self, name):
        return MagicMock()

    def cached_bank_names(self, *a, **k):
        return {}

    def cached_bank_types(self):
        return {}


w = L.LibrarianShellWindow("1.2.3.4", Fake(), parent=None, settings=AppSettings())
end = time.time() + 20
while w._probe_running and time.time() < end:
    app.processEvents()


def put(obj_type, bank, slot, body, name):
    h = w._blobs.put(body)
    w._index.set_entry(obj_type, bank, slot, LocalIndexEntry(
        version=5, baseline_hash=h, current_hash=h, display_name=name, created_utc="", modified_utc="",
        conflicted=False, has_resolved_dependencies=True, is_exi=True, pending_delete=False))


prog = bytearray(init_program_template(False, True)); prog[:24] = b"Dep".ljust(24, b"\0")
put(OBJ_PROGRAM, 0x41, 7, bytes(prog), "Dep")                       # where the Merge Window placed it
combi = bytearray(init_combi_template(False)); combi[:24] = b"Referrer".ljust(24, b"\0")
set_combi_timbre_ref(combi, 0, 18, 3)                               # still points at the PCG's address
put(OBJ_COMBI, 0x40, 0, bytes(combi), "Referrer")

staged_hash = "staged-hash-differs-from-written-body"
w._merge.mark_placed(staged_hash, (OBJ_PROGRAM, 0x41, 7))
w._clipboard.add(SessionDependencyEntry(missing_ref=ObjLoc(OBJ_PROGRAM, 0x40, 3), ref_kind="timbre 0", site=0,
                                        required_by=ObjLoc(OBJ_COMBI, 0x40, 0), expected_content_hash=staged_hash))
assert len(w._clipboard.pending) == 1
w._retry_resolve_pending_dependencies()
assert not w._clipboard.pending, "dependency placed by the Merge Window was not recognised"
body = w._blobs.get(w._index.get(OBJ_COMBI, 0x40, 0).current_hash)
assert combi_timbre_ref(body, 0) == (19, 7), combi_timbre_ref(body, 0)    # U-A is func33 18, so U-B is 19
print("pending dependency resolved from placement record ok")
