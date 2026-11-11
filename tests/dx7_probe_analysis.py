"""Analysis helper for the controlled DX7 probe banks (Conversion/dx7_probe_bank.py) after they were loaded into Nautilus bank R
(read back with tests/live_dump_programs.py to Z:/DX7 EXAMPLES/Probes/ProbeLoaded/bank_4B.bin; slots 0-31 ATK, 32-63 DEC,
64-95 SLP, 96-127 REL). Not a pass/fail test.

Findings (2026-11-11):
  * DX7 rate -> MOD-7 time field: R1 -> Attack Time, R2 -> Decay Time, R3 -> RELEASE Time, R4 -> SLOPE Time.
  * Attack, Decay and Slope(R4) give identical tables per (from, to) level pair; Release(R3) differs (different drop).
  * Time Curve byte = round((from - to) / 10) for a falling level, 0 for a rising or equal level (probe pairs, output 99).
  * Time is monotone in u = log2(drop) - log2(speed(q)), drop = internal DX7 level difference, q = (rate * 41) >> 6,
    speed(q) = (4 + (q & 3)) << (q >> 2): 0 violations on the attack sweep, but y(u) still jitters by 1-2, so a closed form is
    not found yet. Saturates at 99 (slow) and 5 (fast); rising levels (0 -> 99) go down to 1.
usage: python tests/dx7_probe_analysis.py ATK|DEC|SLP|REL
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from Conversion import dx7_probe_bank as P
from Conversion import dx7_to_mod7 as M

DATA = open(os.environ.get("KRONOS_DX7_EXAMPLES", "Z:/DX7 EXAMPLES/") + "Probes/ProbeLoaded/bank_4B.bin", "rb").read()
FIELD = {"ATK": "Attack", "DEC": "Decay", "SLP": "Release", "REL": "Slope"}      # the MOD-7 time field each probe stage drives
F = M._fields()


def rows(stage):
    base, plan, out = list(P.STAGES).index(stage) * 32, P.probe_plan(stage), []
    for v in range(32):
        body = DATA[(base + v) * 4954:(base + v + 1) * 4954]
        for i in range(6):
            t = plan[v * 6 + i]
            out.append((t, F.get(body, f"[EG1] {FIELD[stage]} Time", 23 * i), F.get(body, f"[EG1] {FIELD[stage]} Time EG Curve", 23 * i)))
    return out


def u_of(rate, a, b):
    q = (rate * 41) >> 6
    return math.log2(M._internal_level(a) - M._internal_level(b)) - (math.log2(4 + (q & 3)) + (q >> 2))


if __name__ == "__main__":
    stage = sys.argv[1]
    seen = {}
    for t, y, c in rows(stage)[:96]:
        if M._internal_level(t["b"]) < M._internal_level(t["a"]):
            seen[round(u_of(t["rate"], t["a"], t["b"]), 3)] = (y, c, t["rate"], (t["a"], t["b"]))
    for u, (y, c, r, pr) in sorted(seen.items()):
        print(f"u={u:7.3f}  time={y:3d} curve={c:2d}  rate={r:2d} levels={pr}")
