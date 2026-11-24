"""A Kronos Combi Timbre's Program bank byte counts user banks from 17 (U-A), like the Nautilus and the set-list slot byte.
Read as 18-based, every reference landed one bank low (User EE:082 shown as User DD:082) and a loader heuristic had to
shift the file's Program banks to hide it. The real partial-save file must now resolve with no shift (skips if absent)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from Data.pcg_file import open_pcg
from Data.librarian_sysex import OBJ_PROGRAM, OBJ_COMBI, iter_combi_timbre_refs, set_combi_timbre_ref, combi_timbre_ref
import Core.kronos_sysex as ksx

# the byte itself: 17 = U-A (func33 18), 30 = U-GG (func33 31), GM 6 = func33 7, INT unchanged
b = bytearray(7810)
for t, byte in enumerate((0, 5, 6, 16, 17, 30)):
    b[4802 + t * 188 + 1] = byte
got = [combi_timbre_ref(bytes(b), t)[0] for t in range(6)]
assert got == [0, 5, 7, 17, 18, 31], got
assert [ksx._func33_to_obj_bank(1, g) for g in got[4:]] == [0x40, 0x4D]
set_combi_timbre_ref(b, 0, 18, 1); assert b[4803] == 17

F = os.environ.get("KRONOS_OTE_PCG",
                   r"S:\ALL_KRONOS\Kronos 2 2026 (SSD2)\Old SSD1\MY SOUNDS\OVER THE EDGE\OVER THE EDGE.PCG")
if not os.path.exists(F):
    print("pcg realign ok (real file skipped: not present)"); sys.exit(0)
pcg = open_pcg(open(F, "rb").read())
banks = {e.bank.label: e.is_exi for e in pcg.objects if e.obj_type == OBJ_PROGRAM}
assert banks == {"U-EE": True, "U-FF": False}, banks
have = {(e.bank.obj_bank, e.index) for e in pcg.objects if e.obj_type == OBJ_PROGRAM}
refs = [(ksx._func33_to_obj_bank(1, fb), n) for e in pcg.objects if e.obj_type == OBJ_COMBI
        for _t, fb, n in iter_combi_timbre_refs(e.body) if ksx._func33_to_obj_bank(1, fb) >= 0x40]
assert len(refs) == 431 and all(r in have for r in refs), len([r for r in refs if r not in have])
print("pcg realign ok")
