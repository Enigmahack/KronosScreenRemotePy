"""A partial-save PCG stores user Program bank ids one high; the loader re-aligns them so the Combis' references resolve.
Needs the real file (skips if absent)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from Data.pcg_file import open_pcg
from Data.librarian_sysex import OBJ_PROGRAM, OBJ_COMBI, iter_combi_timbre_refs
import Core.kronos_sysex as ksx

F = os.environ.get("KRONOS_OTE_PCG",
                   r"S:\ALL_KRONOS\Kronos 2 2026 (SSD2)\Old SSD1\MY SOUNDS\OVER THE EDGE\OVER THE EDGE.PCG")
if not os.path.exists(F):
    print("SKIP: fixture not present"); sys.exit(0)
pcg = open_pcg(open(F, "rb").read())
assert pcg.program_bank_shift == -1
banks = {e.bank.label: e.is_exi for e in pcg.objects if e.obj_type == OBJ_PROGRAM}
assert banks == {"U-DD": True, "U-EE": False}, banks
have = {(e.bank.obj_bank, e.index) for e in pcg.objects if e.obj_type == OBJ_PROGRAM}
miss = [1 for e in pcg.objects if e.obj_type == OBJ_COMBI
        for _t, fb, n in iter_combi_timbre_refs(e.body)
        if ksx._func33_to_obj_bank(1, fb) >= 0x40 and (ksx._func33_to_obj_bank(1, fb), n) not in have]
assert not miss, len(miss)
print("pcg realign ok")
