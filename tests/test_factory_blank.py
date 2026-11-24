"""Deleting a Program / Combi / Set List / Drum Kit / Wave Sequence stages the instrument's own initialised object, in
the format the slot held (Kronos or Nautilus), not the old body with its name cleared. Run: python tests/test_factory_blank.py"""
import os
import sys
import tempfile
import time

os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_fb_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unittest.mock import MagicMock
from PySide6.QtWidgets import QApplication

app = QApplication([])

import Objects.factory_blank as fb
import Objects.object_body as ob
from Conversion.data import init_combi_template, init_program_template
from Data.librarian_sysex import OBJ_COMBI, OBJ_DRUM_KIT, OBJ_PROGRAM, OBJ_SET_LIST, OBJ_WAVE_SEQ
from Data.local_library_store import LocalIndexEntry
from Data.pcg_file import open_pcg
from Models.app_settings import AppSettings
import Views.librarian_shell_window as L

PRELOAD = os.environ.get("KRONOS_NAUTILUS_PRELOAD",
                         "Z:/Nautilus Research/PCG Info/FACTORY_extracted/FACTORY/PRELOAD.PCG")

# ---- pure: every kind, both families, reads as Init and has the wire size
for nautilus in (False, True):
    for exi in (True, False):
        p = fb.blank_body(OBJ_PROGRAM, b"", nautilus, exi)
        assert ob.is_init(OBJ_PROGRAM, p) and len(p) == ((4954 if exi else 3700) if nautilus else (4960 if exi else 3706)), (nautilus, exi, len(p))
    c = fb.blank_body(OBJ_COMBI, b"", nautilus)
    assert ob.is_init(OBJ_COMBI, c) and len(c) == (5640 if nautilus else 7810)
for t, size in ((OBJ_DRUM_KIT, 38424), (OBJ_WAVE_SEQ, 2216)):
    assert len(fb.blank_body(t, b"", False)) == size and ob.is_init(t, fb.blank_body(t, b"", True))
assert fb.blank_body(OBJ_SET_LIST, b"", True) is None          # nothing to size a Set List from

# ---- a Set List: colours and slot references go, and it matches a real factory list byte for byte
colored = bytearray(69416)
for s in range(ob.SLOT_COUNT):
    colored = bytearray(ob.write_setlist_slot_color(bytes(colored), s, 5))
    colored = bytearray(ob.write_setlist_slot_name(bytes(colored), s, f"Song {s}"))
blank = fb.blank_body(OBJ_SET_LIST, bytes(colored), False, number=7)
slots = [ob.parse_setlist_slot(blank, s) for s in range(ob.SLOT_COUNT)]
assert all(x.color == 0 and not x.name and (x.type, x.bank, x.index, x.hold_time, x.volume) == (1, 0, 0, 6, 127)
           for x in slots) and ob.parse_setlist_slot(blank, 0).comments == ""
if os.path.exists(PRELOAD):
    lists = [o for o in open_pcg(open(PRELOAD, "rb").read()).objects if o.obj_type == OBJ_SET_LIST]
    assert fb.blank_body(OBJ_SET_LIST, lists[2].body, True, number=2) == lists[2].body

# ---- through the window: Delete stages it
class Fake:
    is_nautilus = False
    can_dump = True

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


def seed(obj_type, bank, slot, body):
    h = w._blobs.put(body)
    w._index.set_entry(obj_type, bank, slot, LocalIndexEntry(
        version=5, baseline_hash=h, current_hash=h, display_name="x", created_utc="", modified_utc="",
        conflicted=False, has_resolved_dependencies=True, is_exi=True, pending_delete=False))
    return w._index.get(obj_type, bank, slot)


def find_leaf(payload):
    stack = [w._tree_local.topLevelItem(k) for k in range(w._tree_local.topLevelItemCount())]
    while stack:
        it = stack.pop()
        if it.data(0, 0x100) == payload:
            return it
        stack.extend(it.child(k) for k in range(it.childCount()))


def delete(obj_type, bank, slot):
    w._refresh_local_tree()
    w._tree_local.expandAll()
    w._tree_local.setCurrentItem(find_leaf(("local", obj_type, bank, slot)))
    w._toggle_delete_local_selected()
    e = w._index.get(obj_type, bank, slot)
    assert e.pending_delete
    return w._blobs.get(e.current_hash)


body = bytearray(init_program_template(False, True)); body[:24] = b"Real Patch".ljust(24, bytes(1))
got = delete_p = (seed(OBJ_PROGRAM, 0x40, 3, bytes(body)), delete(OBJ_PROGRAM, 0x40, 3))[1]
assert got == init_program_template(False, True), "Program was not reset to the Init Program"
hd1 = bytearray(init_program_template(False, False)); hd1[:24] = b"HD Patch".ljust(24, bytes(1))
seed(OBJ_PROGRAM, 0x41, 0, bytes(hd1))
assert len(delete(OBJ_PROGRAM, 0x41, 0)) == 3706
cb = bytearray(init_combi_template(False)); cb[:24] = b"Real Combi".ljust(24, bytes(1))
seed(OBJ_COMBI, 0x40, 1, bytes(cb))
assert delete(OBJ_COMBI, 0x40, 1) == init_combi_template(False)
seed(OBJ_SET_LIST, 0, 9, bytes(colored))
assert delete(OBJ_SET_LIST, 0, 9) == fb.blank_body(OBJ_SET_LIST, bytes(colored), False, number=9)
print("factory blank on delete ok")
