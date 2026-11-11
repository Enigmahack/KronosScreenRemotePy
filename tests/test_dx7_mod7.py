"""The fields Conversion/dx7_to_mod7 claims to know must reproduce the 64 Programs the Nautilus built from ROM1A/ROM1B.
Skips (exit 0) when the DX7 fixtures are not available. The remaining, unfitted fields are listed by
tests/dx7_mod7_harness.py."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from Conversion import dx7_to_mod7 as M
from Data import dx7_voice as D

base = os.environ.get("KRONOS_DX7_EXAMPLES", "Z:/DX7 EXAMPLES/")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dx7_pairs

try:
    _pairs = dx7_pairs.all_pairs()
except OSError:
    print("SKIP: DX7 fixtures not found (set KRONOS_DX7_EXAMPLES)")
    sys.exit(0)
packed = [x[0] for x in _pairs]
real = [x[1] for x in _pairs]

F = M._fields()
fails = []
KNOWN_ALG = set(M.ALGORITHM)   # every algorithm; the ten unseen ones are inferred, see ALGORITHM_SEEN
for i, (pk, rp) in enumerate(zip(packed, real)):
    out = M.convert_voice(pk)
    p = D.unpack(pk)
    if out[0:24] != rp[0:24]:
        fails.append((i, "name"))
    if out[2852] != rp[2852]:
        fails.append((i, "transpose"))
    if p["algorithm"] in KNOWN_ALG and F.get(out, "[OSC] Algorithm") != F.get(rp, "[OSC] Algorithm"):
        fails.append((i, "algorithm"))
    for op in range(6):
        e, o = M.EG_STRIDE * op, M.OSC_STRIDE * op
        if F.get(out, "[OSC1] Volume", o) != F.get(rp, "[OSC1] Volume", o):
            fails.append((i, f"op{op} volume"))
        if F.get(out, f"[Macro] OSC{op + 1} Class") != F.get(rp, f"[Macro] OSC{op + 1} Class"):
            fails.append((i, f"op{op} class"))
        for nm in ("[EG1] Start Level", "[EG1] Attack Level", "[EG1] Break Level", "[EG1] Sustain Level",
                   "[EG1] Release Level"):
            if F.get(out, nm, e) != F.get(rp, nm, e):
                fails.append((i, f"op{op} {nm}"))
        for nm in ("[OSC1] Frequency Ratio (Coarse)", "[OSC1] Frequency Ratio (Fine)", "[OSC1] Frequency Offset (Coarse)",
                   "[OSC1] Frequency Offset (Fine)", "[OSC1] Volume Velocity Sens", "[OSC1 Keyboard Track] Center Key"):
            if p[f"op{op}_osc_mode"] == 1 and "Offset" in nm:
                continue                                    # fixed mode: compared as one number below (+-0.01 Hz, see frequency())
            if F.get(out, nm, o) != F.get(rp, nm, o):
                fails.append((i, f"op{op} {nm}"))
for i, (pk, rp) in enumerate(zip(packed, real)):
    out, p = M.convert_voice(pk), D.unpack(pk)
    for op in range(6):
        if p[f"op{op}_osc_mode"] == 1:
            hz = lambda b: F.get(b, "[OSC1] Frequency Offset (Coarse)", M.OSC_STRIDE * op) * 100 + F.get(b, "[OSC1] Frequency Offset (Fine)", M.OSC_STRIDE * op)  # noqa: E731
            tol = 1                                        # 0.01 Hz rounding
            if abs(hz(out) - hz(rp)) > tol:
                fails.append((i, f"op{op} fixed frequency {hz(out)} vs {hz(rp)}"))
assert M.eg_level(99) == 99 and M.eg_level(98) == 91 and M.eg_level(0) == 0
assert M.frequency(0, 1, 0, 7) == (1, 0, 0, 0) and M.frequency(0, 0, 0, 7) == (0, 500, 0, 0)
assert abs(M.frequency(1, 2, 60, 7)[2] * 100 + M.frequency(1, 2, 60, 7)[3] - 39810) <= 1
print(f"{len(packed)} voices: {len(fails)} mismatches in the fitted fields")
for f in fails[:20]:
    print("  ", f)
sys.exit(1 if fails else 0)
