"""Drum-mode Programs report their Drum Kit (Oscillator Mode enum: Drums 2, Double Drums 5) and Convert to <instrument> offers
the referenced user Programs / Drum Kits / Wave Sequences. Real file: MAIN-REPERTOIRE.PCG (skips if absent)."""
import os, sys, tempfile
os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_cdep_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unittest.mock import MagicMock
from PySide6.QtWidgets import QApplication, QMessageBox
app = QApplication([])
import Data.pcg_file as pf
from Data.librarian_sysex import OBJ_PROGRAM, OBJ_COMBI, OBJ_DRUM_KIT, is_drum_kit_zone
from Data.librarian_model import ObjLoc
from Tools.dependency_scanner import walk_object_references

fails = []
def check(n, c):
    if not c: fails.append(n)

check("zones", is_drum_kit_zone(2, 0, 0) and not is_drum_kit_zone(2, 1, 0) and not is_drum_kit_zone(2, 0, 1)
      and is_drum_kit_zone(5, 1, 0) and not is_drum_kit_zone(4, 0, 0) and not is_drum_kit_zone(1, 0, 0))

F = os.environ.get("KRONOS_MAIN_REP_PCG", "E:/MAIN-REPERTOIRE.PCG")
if os.path.exists(F):
    pcg = pf.open_pcg(open(F, "rb").read())
    progs = {e.name.strip(): e for e in pcg.objects if e.obj_type == OBJ_PROGRAM}
    for nm, slot in (("OTE-PERC HOLD", 9), ("OTE-PERC RELEASE", 10)):
        refs = [(r.ref.obj_type, r.ref.bank, r.ref.number) for r in walk_object_references(
            OBJ_PROGRAM, pf.wire_body_from_pcg_entry(OBJ_PROGRAM, progs[nm]))]
        check("kit-ref-" + nm, refs == [(OBJ_DRUM_KIT, 0x42, slot)])

    import Views.librarian_shell_window as L
    from Core.device_family import DeviceFamily
    svc = MagicMock(); svc.is_nautilus = True; svc.can_dump = True
    w = L.LibrarianShellWindow("10.0.0.9", svc, 21, "nautilus", "nautilus")
    w._pcg = pcg
    w._pcg_by_addr = {(e.obj_type, e.bank.obj_bank, e.index): e for e in pcg.objects if e.bank is not None}
    combi = next(e for e in pcg.objects if e.obj_type == OBJ_COMBI and e.name.strip() == "PERC Gm Am Dm &")
    asked = []
    QMessageBox.question = staticmethod(lambda *a, **k: (asked.append(a[2]), QMessageBox.StandardButton.Yes)[1])
    kind, item = w._convert_item_for("pcg", ObjLoc(OBJ_COMBI, combi.bank.obj_bank, combi.index), "nautilus")
    check("combi-item", kind == "item")
    deps = w._convert_dependency_items("pcg", "nautilus",
                                       {(OBJ_COMBI, combi.bank.obj_bank, combi.index): item.body}, "Convert")
    names = sorted(d.name.strip() for d in deps)
    check("deps-include-kits", "OTE-PERC HOLD" in names and "OTE-PERC RELEASE" in names
          and sum(d.obj_type == OBJ_DRUM_KIT for d in deps) >= 2)
    check("deps-include-programs", sum(d.obj_type == OBJ_PROGRAM for d in deps) >= 2 and len(asked) == 1)
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)
    check("no-means-none", w._convert_dependency_items("pcg", "nautilus", {(OBJ_COMBI, combi.bank.obj_bank, combi.index): item.body}, "Convert") == [])
    w._closed = True
if fails:
    print("FAIL:", fails); sys.exit(1)
print("convert dependencies tests passed")
