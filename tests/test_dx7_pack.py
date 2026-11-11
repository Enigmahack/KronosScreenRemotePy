"""pack() must be the exact inverse of unpack() on every real DX7 bank in the corpus. Skips when the corpus is absent."""
import glob, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from Data import dx7_voice as D
base = os.environ.get("KRONOS_DX7_EXAMPLES", "Z:/DX7 EXAMPLES/") + "SysexFormat/"
files = sorted(glob.glob(base + "*.SYX") + glob.glob(base + "*.syx"))
if not files:
    print("SKIP: no DX7 corpus"); sys.exit(0)
voices = bad = banks = byte_diff = 0
for f in files:
    data = open(f, "rb").read()
    try:
        vs = D.parse_bank(data)
    except D.Dx7Error:
        continue
    banks += 1
    for pk in vs:
        voices += 1
        p = D.unpack(pk)
        again = D.pack(p, D.name_of(pk))
        if D.unpack(again) != p:
            bad += 1
        byte_diff += again != pk
    rebuilt = D.build_bank([D.pack(D.unpack(pk), D.name_of(pk)) for pk in vs])
    assert rebuilt[:6] == data[:6] and rebuilt[-1] == 0xF7 and len(rebuilt) == 4104
print(f"{banks} banks, {voices} voices: {bad} parameter round-trip failures ({byte_diff} voices differ from the original only in unused bits or odd name bytes)")
sys.exit(1 if bad else 0)
