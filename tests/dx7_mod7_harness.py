"""Compare Conversion/dx7_to_mod7 against the 64 Programs the Nautilus produced from ROM1A/ROM1B.
Prints exact-match counts and the remaining mismatching offsets grouped by MOD-7 field. Not a pass/fail test yet."""
import collections, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from Data import dx7_voice as D
from Conversion import dx7_to_mod7 as M
base = os.environ.get("KRONOS_DX7_EXAMPLES", "Z:/DX7 EXAMPLES/")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dx7_pairs
_pairs = dx7_pairs.all_pairs()
packed = [x[0] for x in _pairs]
real = [x[1] for x in _pairs]
F = M._fields()
vary = [o for o in range(4954) if len({p[o] for p in real}) > 1]
outs = [M.convert_voice(pk) for pk in packed]
exact = sum(all(o[i] == r[i] for i in vary) for o, r in zip(outs, real))
print(f"voices byte-exact over the {len(vary)} varying offsets: {exact}/{len(real)}")
tot = collections.Counter(); seen_off = set()
for n in F.by_name:
    ys = [F.get(r, n) for r in real]
    if len(set(ys)) < 2: continue
    m = re.match(r"^\[(OSC|EG)(\d)\](.*)", n)
    key = (f"[{m.group(1)}1-6]{m.group(3)}" if int(m.group(2)) <= 6 else n) if m else n
    for o, r in zip(outs, real):
        if F.get(o, n) != F.get(r, n): tot[key] += 1
    for oo, _, _ in F.by_name[n]: seen_off.add(F.base + oo)
print("fields still mismatching (count over voices x ops):")
for k, v in tot.most_common(70): print(f"{v:5d}  {k}")
print("varying offsets below the MOD-7 section:", sorted(o for o in vary if o < F.base and any(outs[i][o] != real[i][o] for i in range(64))))
