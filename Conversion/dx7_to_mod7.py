"""DX7 voice -> Nautilus EXi Program with a MOD-7 patch (work in progress).

Everything here is fitted from ground truth: the owner loaded ROM1A / ROM1B natively into Nautilus banks Q / R
(`DX7 EXAMPLES/NautilusLoaded/`) and the 64 resulting Programs were compared with the DX7 voices. Only fields that
reproduce those 64 Programs exactly are written; `UNMAPPED` lists what is still left at the template value.
No hardware has accepted a Program built by this module yet.

The MOD-7 section starts at body byte 2902; its fields are bit-packed (table in `Resources/Conversion/Mod7Fields.json`,
generated from `Prog_EXi.txt`). DX7 operators are numbered as stored (index 0 = operator 6) and operator i drives
OSC(i+1) / EG(i+1) (OSC blocks are 45 bytes apart, EG blocks 23).
"""
from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Tuple

from Data import dx7_voice

_RES = Path(__file__).resolve().parent.parent / "Resources" / "Conversion"
PROGRAM_SIZE = 4954
OSC_STRIDE = 45
EG_STRIDE = 23
VEL_SENS = (0, 18, 35, 47, 58, 67, 75, 82)          # DX7 key velocity sens 0-7 -> OSC Volume Velocity Sens (exact, 8 of 8)
# DX7 algorithm number 1-32 -> MOD-7 "[OSC] Algorithm" value. DX algorithms that differ only in where the feedback loop sits
# share one MOD-7 value. 26 of the 32 were seen in the 228 loaded voices and all agree with this table; the other six
# (DX 13, 20, 21, 24, 26, 27) are inferred from it: the seen values fill 55..78 with no gap once equivalent algorithms are
# merged. HW-VERIFY: load a DX7 bank that uses those six and compare.
_ALG_GROUPS = ((1, 2), (3,), (4,), (5,), (6,), (7, 8, 9), (10, 11), (12, 13), (14, 15), (16, 17), (18,), (19,), (20,), (21,),
               (22,), (23,), (24,), (25,), (26, 27), (28,), (29,), (30,), (31,), (32,))
ALGORITHM: Dict[int, int] = {dx - 1: 55 + i for i, grp in enumerate(_ALG_GROUPS) for dx in grp}      # key: 0-based as stored
ALGORITHM_SEEN = frozenset(a - 1 for a in range(1, 33) if a not in (13, 20, 21, 24, 26, 27))     # 0-based, as stored
# DX7 carriers per algorithm number (operator numbers 1-6). Checked against the Program's own Macro OSCn Class bits for the
# 22 algorithms seen (all agree).
CARRIERS = {1: (1, 3), 2: (1, 3), 3: (1, 4), 4: (1, 4), 5: (1, 3, 5), 6: (1, 3, 5), 7: (1, 3), 8: (1, 3), 9: (1, 3),
            10: (1, 4), 11: (1, 4), 12: (1, 3), 13: (1, 3), 14: (1, 3), 15: (1, 3), 16: (1,), 17: (1,), 18: (1,),
            19: (1, 4, 5), 20: (1, 2, 4), 21: (1, 2, 4, 5), 22: (1, 3, 4, 5), 23: (1, 2, 4, 5), 24: (1, 2, 3, 4, 5),
            25: (1, 2, 3, 4, 5), 26: (1, 2, 4), 27: (1, 2, 4), 28: (1, 3, 6), 29: (1, 2, 3, 5), 30: (1, 2, 3, 6),
            31: (1, 2, 3, 4, 5), 32: (1, 2, 3, 4, 5, 6)}
UNMAPPED = ("EG times and curves (and the rate-scaling Time Mod.Intensity, written only when a stage time is >= 7)", "OSC Feedback Level", "amp mod sens", "LFO", "pitch EG", "key scaling ramps")


class Mod7Fields:
    """Bit-level access to the MOD-7 section of an EXi Program body."""

    def __init__(self) -> None:
        d = json.loads((_RES / "Mod7Fields.json").read_text(encoding="utf-8"))
        self.base = d["base"]
        self.by_name = {f["name"]: [tuple(s) for s in f["segs"]] for f in d["fields"]}

    def segs(self, name: str, shift: int = 0):
        return [(o + shift, h, l) for o, h, l in self.by_name[name]]

    def get(self, body, name: str, shift: int = 0) -> int:
        v = sh = 0
        for o, h, l in self.segs(name, shift):
            v |= ((body[self.base + o] >> l) & ((1 << (h - l + 1)) - 1)) << sh
            sh += h - l + 1
        return v

    def set(self, body: bytearray, name: str, value: int, shift: int = 0) -> None:
        for o, h, l in self.segs(name, shift):                        # read-modify-write: bytes are shared between fields
            w = h - l + 1
            mask = ((1 << w) - 1) << l
            body[self.base + o] = (body[self.base + o] & ~mask & 0xFF) | ((value & ((1 << w) - 1)) << l)
            value >>= w

    def width(self, name: str) -> int:
        return sum(h - l + 1 for _, h, l in self.by_name[name])


@lru_cache(maxsize=1)
def _fields() -> Mod7Fields:
    return Mod7Fields()


@lru_cache(maxsize=1)
def _template() -> bytes:
    return (_RES / "Mod7DxTemplate.bin").read_bytes()


_LOW_LEVEL = (0, 5, 9, 13, 17, 20, 23, 25, 27, 29, 31, 33, 35, 37, 39, 41, 42, 43, 45, 46)   # DX7 internal level for 0-19


def _internal_level(x: int) -> int:
    """DX7 level 0-99 -> its internal 0-127 scale (x + 28 from 20 up, a small table below). 127 is full scale."""
    return _LOW_LEVEL[x] if x < 20 else x + 28


def eg_level(x: int) -> int:
    """DX7 EG level 0-99 -> MOD-7 EG level 0-99: round(99 * 2^((L-127)/8)) on the internal level L. Exact on all 65 distinct
    pairs of the first 64 voices and the ~170 loaded later; a value above 99 (corrupt DX7 data) becomes 0 on the unit."""
    if x > 99:
        return 0
    return int(99 * 2.0 ** (-(127 - _internal_level(x)) / 8) + 0.5)


def osc_volume(output_level: int, carriers: int, is_carrier: bool) -> int:
    """DX7 operator output level -> MOD-7 OSC Volume: 100 * 2^((x-99)/16), a carrier divided by sqrt(carrier count) so that
    the mix keeps its level; levels below 20 go through the DX7 table. Exact on all operators of the loaded voices."""
    if output_level == 0 or output_level > 99:
        return 0
    m = 100 * 2.0 ** (-(127 - _internal_level(output_level)) / 16)
    return int((m / math.sqrt(carriers) if is_carrier else m) + 0.5)


def frequency(mode: int, coarse: int, fine: int, detune: int) -> Tuple[int, int, int, int]:
    """-> (ratio coarse, ratio fine, offset coarse, offset fine) as signed field values.
    Detune is 0-14 (a stored 15 acts as 14), one step = 1 cent. Ratio mode: coarse (0 = 0.5) * (1 + fine/100) * detune,
    rounded to 0.001 and split into integer part and thousandths. Fixed mode: Hz = 10^(coarse % 4 + fine/100), first
    quantised (whole Hz from 1000, 0.1 Hz from 100, else unchanged), then detuned, then rounded to 0.01 Hz and split into whole
    Hz and hundredths. Checked on ~60 distinct fixed-frequency settings; may be 0.01 Hz out."""
    cents = (min(detune, 14) - 7) / 1200.0
    if mode == 0:
        r = (0.5 if coarse == 0 else coarse) * (1 + fine / 100.0) * 2.0 ** cents
        n = int(r * 1000 + 0.5)
        return n // 1000, n % 1000, 0, 0
    hz = 10.0 ** ((coarse % 4) + fine / 100.0)
    hz = float(int(hz + 0.5)) if hz >= 1000 else (int(hz * 10 + 0.5) / 10.0 if hz >= 100 else hz)
    n = int(hz * 2.0 ** cents * 100 + 0.5)
    return 0, 0, n // 100, n % 100


def _signed(v: int, bits: int) -> int:
    return v & ((1 << bits) - 1)


def convert_voice(packed: bytes) -> bytes:
    """One packed 128-byte DX7 voice -> a 4954-byte EXi Program body (template + the fitted fields)."""
    p = dx7_voice.unpack(packed)
    f = _fields()
    b = bytearray(_template())
    name = dx7_voice.name_of(packed).encode("latin-1")     # trailing spaces dropped, NUL padded (24-byte name field)
    b[0:24] = name[:24].ljust(24, bytes(1))
    dx_alg = p["algorithm"] + 1
    f.set(b, "[OSC] Algorithm", ALGORITHM[p["algorithm"]])
    carriers = CARRIERS[dx_alg]
    b[2852] = (p["transpose"] - 24) & 0xFF
    for op in range(6):
        g = lambda k: p[f"op{op}_{k}"]                                # noqa: E731
        e, o = EG_STRIDE * op, OSC_STRIDE * op
        f.set(b, "[EG1] Time Mod.Source", 30, e)                       # key follower: carries the DX7 rate scaling
        f.set(b, "[OSC1] Phase Sync", 0 if p["osc_sync"] else 2, o)     # DX7 oscillator key sync on -> 0, off -> 2
        f.set(b, "[EG1] Start Level", eg_level(g("eg_l4")), e)
        f.set(b, "[EG1] Attack Level", eg_level(g("eg_l1")), e)
        f.set(b, "[EG1] Break Level", eg_level(g("eg_l2")), e)
        f.set(b, "[EG1] Sustain Level", eg_level(g("eg_l3")), e)
        f.set(b, "[EG1] Release Level", eg_level(g("eg_l4")), e)
        rc, rf, oc, of = frequency(g("osc_mode"), g("freq_coarse"), g("freq_fine"), g("detune"))
        f.set(b, "[OSC1] Frequency Ratio (Coarse)", rc, o)
        f.set(b, "[OSC1] Frequency Ratio (Fine)", _signed(rf, 11), o)
        f.set(b, "[OSC1] Frequency Offset (Coarse)", _signed(oc, 15), o)
        f.set(b, "[OSC1] Frequency Offset (Fine)", _signed(of, 8), o)
        is_carrier = (6 - op) in carriers                              # stored index 0 is DX operator 6
        f.set(b, f"[Macro] OSC{op + 1} Class", 0 if is_carrier else 1)
        f.set(b, "[OSC1] Volume", osc_volume(g("output_level"), len(carriers), is_carrier), o)
        f.set(b, "[OSC1] Volume Velocity Sens", VEL_SENS[g("key_vel_sens")], o)
        f.set(b, "[OSC1 Keyboard Track] Center Key", min(g("bp") + 21, 120), o)   # 127 (invalid) gives 120
    return bytes(b)
