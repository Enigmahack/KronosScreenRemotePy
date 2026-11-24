"""Bank names follow the connected instrument (Core/bank_names.py): no I-x / U-x names on a Nautilus."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import Core.bank_names as bn
import Core.kronos_sysex as ksx
import Data.library_pull_pipeline as pp
import Data.object_types as ot
from Data.librarian_model import ObjLoc
from Data.librarian_sysex import OBJ_COMBI, OBJ_DRUM_KIT, OBJ_PROGRAM, OBJ_WAVE_SEQ

fails = []


def check(name, cond):
    if not cond:
        fails.append(name)
        print("FAIL", name)


K, N = bn.KRONOS, bn.NAUTILUS
check("kronos-program", [K.program(b) for b in (0, 0x10, 0x40, 0x47, 0x4D)] == ["I-A", "GM", "U-A", "U-AA", "U-GG"])
check("nautilus-program", [N.program(b) for b in (0, 5, 0x40, 0x4D)] == ["A", "F", "G", "T"])
check("nautilus-no-gm", N.program(0x10).startswith("?"))
check("nautilus-combi", [N.combi(b) for b in (0, 6, 0x40, 0x46)] == ["A", "G", "H", "N"])
check("kronos-combi", [K.combi(b) for b in (0, 6, 0x40, 0x46)] == ["I-A", "I-G", "U-A", "U-G"])
check("drum-wave-same-both", N.drum_kit(0x4D) == K.drum_kit(0x4D) == "U-GG" and N.wave_seq(0) == "INT")
check("past-last-bank", N.program(0x4E).startswith("File bank") and N.combi(0x47).startswith("File bank"))
check("label_for-explicit", ot.label_for(OBJ_PROGRAM, 0x4D, True) == "T" and ot.label_for(OBJ_PROGRAM, 0x4D, False) == "U-GG")

# the connected instrument is the default everywhere a family is not passed
bn.set_active(True)
try:
    check("active-label_for", ot.label_for(OBJ_PROGRAM, 0x4D) == "T" and ot.label_for(OBJ_COMBI, 0x40) == "H")
    check("active-objloc", ObjLoc(OBJ_PROGRAM, 0x4D, 3).label() == "T:003")
    check("active-func33", ksx.resolve_bank_label(1, 31) == "T" and ksx.resolve_bank_label(0, 7) == "H")
    check("active-pull-progress", pp._bank_display_label(OBJ_PROGRAM, 0x4D) == "T")
    check("pull-family-str", pp.bank_key_label(f"{OBJ_PROGRAM}:77", True) == "Program T" and pp.bank_key_label(f"{OBJ_PROGRAM}:77", False) == "Program U-GG")
    check("family-none-uses-active", pp._nautilus_of(None) is None and pp._nautilus_of("nautilus") is True)
    check("drum-kit-active", ot.label_for(OBJ_DRUM_KIT, 0x40) == "U-A" and ot.label_for(OBJ_WAVE_SEQ, 0x40) == "U-A")
finally:
    bn.set_active(False)
check("active-reset", ot.label_for(OBJ_PROGRAM, 0x4D) == "U-GG")

if fails:
    sys.exit(1)
print("test_bank_names OK")
