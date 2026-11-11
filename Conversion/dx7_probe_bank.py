"""Controlled DX7 banks for reverse-engineering the Nautilus's DX7 -> MOD-7 import (envelope times and curves).

The 64 ROM1A/ROM1B voices fit the rate/level/output-level/rate-scale interplay only approximately, because those inputs
move together in real voices. These banks vary them on purpose: each of the 32 voices holds six independent operators
(algorithm 32, six carriers), so one bank is 192 isolated measurements of one envelope stage.

    python -m Conversion.dx7_probe_bank <out dir>        writes PROBE-ATK.SYX, PROBE-DEC.SYX, PROBE-SLP.SYX, PROBE-REL.SYX

Load each file into a Nautilus user EXi bank (MOD-7 import, as was done with ROM1A/ROM1B), read the 32 Programs back over
SysEx, and compare with `probe_plan(stage)` (same order: voice 0 operator stored-index 0 ... voice 31 index 5).
"""
from __future__ import annotations

import os
import sys
from typing import Dict, List, Tuple

from Data import dx7_voice as D

# stage -> (rate key, level before, level after); a DX7 EG runs L4 -> L1 (attack), L1 -> L2 (decay), L2 -> L3 (slope), L3 -> L4 (release)
STAGES: Dict[str, Tuple[str, str, str]] = {
    "ATK": ("eg_r1", "eg_l4", "eg_l1"), "DEC": ("eg_r2", "eg_l1", "eg_l2"),
    "SLP": ("eg_r3", "eg_l2", "eg_l3"), "REL": ("eg_r4", "eg_l3", "eg_l4"),
}
RATES = (0, 5, 10, 20, 25, 30, 40, 45, 50, 60, 65, 70, 80, 85, 90, 99)
LEVEL_PAIRS = ((99, 0), (99, 50), (99, 80), (50, 0), (0, 99), (50, 99))          # (from, to)
OUT_LEVELS = (99, 80, 60, 40, 20, 10)


def probe_plan(stage: str) -> List[Dict[str, int]]:
    """192 operator settings for one stage: rate, from, to, output level, rate scale (all other stages inert)."""
    plan: List[Dict[str, int]] = []
    for r in RATES:                                           # 96: rate x level pair, output 99, no rate scaling
        for a, b in LEVEL_PAIRS:
            plan.append(dict(rate=r, a=a, b=b, out=99, rs=0))
    for out in OUT_LEVELS:                                    # 24: output level x rate at the full swing
        for r in (30, 50, 70, 90):
            plan.append(dict(rate=r, a=99 if stage != "ATK" else 0, b=0 if stage != "ATK" else 99, out=out, rs=0))
    for rs in range(8):                                       # 32: rate scaling x rate at the full swing
        for r in (10, 30, 60, 80):
            plan.append(dict(rate=r, a=99 if stage != "ATK" else 0, b=0 if stage != "ATK" else 99, out=99, rs=rs))
    while len(plan) < 192:                                    # 40: half swings at the common rates and output levels
        i = len(plan) - 152
        plan.append(dict(rate=RATES[i % 16], a=75, b=25, out=(99, 70)[i % 2], rs=0))
    return plan[:192]


def build_stage_bank(stage: str) -> bytes:
    rk, ka, kb = STAGES[stage]
    plan = probe_plan(stage)
    voices = []
    for v in range(32):
        p = D.default_params()
        for i in range(6):
            t = plan[v * 6 + i]
            for k in ("eg_r1", "eg_r2", "eg_r3", "eg_r4"):
                p[f"op{i}_{k}"] = 99                                  # the other stages are instant
            for k in ("eg_l1", "eg_l2", "eg_l3", "eg_l4"):
                p[f"op{i}_{k}"] = 99 if k != "eg_l4" else 0
            p[f"op{i}_{rk}"] = t["rate"]
            p[f"op{i}_{ka}"], p[f"op{i}_{kb}"] = t["a"], t["b"]
            p[f"op{i}_output_level"], p[f"op{i}_rate_scale"] = t["out"], t["rs"]
        voices.append(D.pack(p, f"{stage}-{v:02d}"))
    return D.build_bank(voices)


def main(out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    for stage in STAGES:
        data = build_stage_bank(stage)
        path = os.path.join(out_dir, f"PROBE-{stage}.SYX")
        with open(path, "wb") as f:
            f.write(data)
        D.parse_bank(data)                                            # checksum / header sanity
        print("wrote", path, len(data), "bytes")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
