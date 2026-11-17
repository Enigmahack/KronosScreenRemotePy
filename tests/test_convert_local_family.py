"""Convert to Nautilus from the Keyboard Library pane: a body's instrument decides convert / copy / skip.

Drum Kits and Wave Sequences are the same size on both instruments, so only the recorded provenance
(LocalLibraryIndex.content_family) says whether one is a Kronos body to convert or a Nautilus body to copy; an unknown one
is skipped, never guessed. Also: push._convert copies a native body only when it fits the target slot, and the
Conversion Report records what Convert to Nautilus did. Real window code, fake instrument. Run from the repo root."""
import os
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_convlocal_test_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtWidgets import QApplication, QMessageBox
app = QApplication([])

from Conversion import karma
from Conversion.push import ConvertItem, ConvertTables, ItemReport, _convert
from Data.librarian_model import ObjLoc
from Data.librarian_sysex import OBJ_DRUM_KIT, OBJ_PROGRAM, OBJ_WAVE_SEQ
from Data.pcg_file import FAMILY_KRONOS, FAMILY_NAUTILUS
from Models.app_settings import AppSettings
import Views.librarian_shell_window as L

fails = []


def check(name, cond):
    if not cond:
        fails.append(name)


class FakeNautilus:
    can_dump = True
    is_nautilus = True

    def cached_bank_names(self, *a, **k): return {}
    def sync_names(self, **k): return None
    def __getattr__(self, name): return MagicMock()


KIT = 38424
bodies = {(OBJ_DRUM_KIT, 0, 1): b"K" * KIT, (OBJ_DRUM_KIT, 0, 2): b"N" * KIT, (OBJ_DRUM_KIT, 0, 3): b"U" * KIT,
          (OBJ_PROGRAM, 0x40, 0): b"P" * 3700}   # a Nautilus-sized HD-1 Program: its size says Nautilus
family = {"hK": FAMILY_KRONOS, "hN": FAMILY_NAUTILUS}
hashes = {(OBJ_DRUM_KIT, 0, 1): "hK", (OBJ_DRUM_KIT, 0, 2): "hN", (OBJ_DRUM_KIT, 0, 3): "hU", (OBJ_PROGRAM, 0x40, 0): "hP"}


class FakeIndex:
    incomplete_banks = {}

    def __getattr__(self, name):
        return MagicMock()

    def get(self, obj_type, bank, number):
        h = hashes.get((obj_type, bank, number))
        return SimpleNamespace(display_name=f"obj{number}", current_hash=h) if h else None

    def family_of(self, h):
        return family.get(h)


w = L.LibrarianShellWindow("1.2.3.4", FakeNautilus(), parent=None, settings=AppSettings())
w._index = FakeIndex()
w._local_resolve_content = lambda t, b, n: bodies.get((t, b, n))
w._selected_local_object_locs = lambda: [ObjLoc(*a) for a in bodies]
logged, ran = [], []
w._log = logged.append
w._run_convert = ran.append
w._convert_to_instrument("local")

items = {it.body[:1]: it for it in (ran[0] if ran else [])}
check("kronos-kit-converted", b"K" in items and not items[b"K"].native)
check("nautilus-kit-left-out", b"N" not in items and any("already in the Nautilus format" in m and "obj2" in m
                                                          for m in logged))
check("unknown-kit-skipped", b"U" not in items and any("obj3" in m and "not known" in m for m in logged))
check("unknown-kit-reported", any(e.status == "skipped" and e.name == "obj3" for e in w._conv_report.entries))
check("nautilus-program-left-out", b"P" not in items)

# connected to a Kronos: the Nautilus bodies are the ones converted, numbered as the Nautilus numbers them
w._service.is_nautilus = False
ran.clear()
w._convert_to_instrument("local")
items = {it.body[:1]: it for it in (ran[0] if ran else [])}
check("kronos-target-converts-nautilus", b"N" in items and b"P" in items and b"K" not in items
      and not any(i.native for i in items.values()))
check("kronos-target-src-ref-nautilus-bank", items.get(b"P") is not None and items[b"P"].src_ref == (17, 0))
w._service.is_nautilus = True

# push._convert: a native body is copied when it fits the slot, refused when it does not, for every type
tables = ConvertTables.load()
opts = karma.ArpOptions()
kit = ConvertItem(OBJ_DRUM_KIT, "kit", b"N" * KIT, native=True)
check("native-kit-copied", _convert(kit, b"\0" * KIT, tables, opts).body == b"N" * KIT)
seq = ConvertItem(OBJ_WAVE_SEQ, "seq", b"S" * 2216, native=True)
check("native-seq-copied", _convert(seq, b"\0" * 2216, tables, opts).body == b"S" * 2216)
check("native-wrong-size-refused", _convert(kit, b"\0" * 2216, tables, opts).body is None)

# the Conversion Report keeps what Convert to Nautilus did
QMessageBox.exec = lambda self: 0
w._converting = True
w._on_convert_done([ItemReport("Kit A", "U-A:015", "written", ["1 zone(s) switched off"])], "backup.syx")
check("report-recorded", any(e.source == "Convert to Nautilus" and "1 zone(s) switched off" in e.messages
                             for e in w._conv_report.entries))
check("report-button-count", w._btn_conv_report.text() == f"Conversion Report ({len(w._conv_report)})")
w._closed = True

# PCG pane dependencies: a Kronos Combi whose Timbre I-A000 the conversion re-points to the Nautilus factory A011 must
# not show the unrelated Program the Kronos PCG holds at A011
from Conversion.data import init_combi_template
from Data.librarian_dependencies import LocalSource, PcgSource, collect_pcg_deps
from Data.librarian_sysex import OBJ_COMBI
raw = bytearray(init_combi_template(False)); raw[4802], raw[4803], raw[4804] = 0, 0, 0x20
raw[4990], raw[4991], raw[4992] = 5, 0, 0x20          # timbre 2 -> I-A005, a user's Program kept in place
conv = bytearray(init_combi_template(True)); conv[2632], conv[2633], conv[2634] = 11, 0, 0x20
conv[2820], conv[2821], conv[2822] = 5, 0, 0x20
raw[:24] = conv[:24] = b"My Combi".ljust(24)        # not an Init Combi
objs = {ObjLoc(OBJ_COMBI, 0, 0): ("My Combi", bytes(raw)), ObjLoc(OBJ_PROGRAM, 0, 0): ("Grand", b"x"),
        ObjLoc(OBJ_PROGRAM, 0, 11): ("Unrelated", b"y"),
        ObjLoc(OBJ_PROGRAM, 0, 5): ("Kept", b"z")}
src = PcgSource(get=objs.get, converted=lambda loc: bytes(conv) if loc.obj_type == OBJ_COMBI else None)
rows = []
collect_pcg_deps(ObjLoc(OBJ_COMBI, 0, 0), src, LocalSource(walk=lambda l: None, available=lambda l: False, display_name=lambda l: ""),
                 set(), set(), rows)
text = " | ".join(r.description for r in rows)
check("dep-repointed-shown", "re-pointed by the conversion" in text)
check("dep-no-unrelated-program", "Unrelated" not in text and "Kept" in text)

if fails:
    print("FAILED:", fails)
    sys.exit(1)
print("ALL PASS")
