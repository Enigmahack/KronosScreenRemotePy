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
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Tuple

from Data import dx7_voice

_RES = Path(__file__).resolve().parent.parent / "Resources" / "Conversion"
PROGRAM_SIZE = 4954
OSC_STRIDE = 45
EG_STRIDE = 23
VEL_SENS = (0, 18, 35, 47, 58, 67, 75, 82)          # DX7 key velocity sens 0-7 -> OSC Volume Velocity Sens (exact, 8 of 8)
# DX7 algorithm (0-based) -> MOD-7 "[OSC] Algorithm" value. Seen in the 64 loaded voices; the rest are None.
ALGORITHM: Dict[int, int] = {1: 55, 2: 56, 3: 57, 4: 58, 5: 59, 6: 60, 7: 60, 9: 61, 11: 62, 13: 63, 14: 63, 15: 64, 16: 64,
                             17: 65, 18: 66, 21: 69, 22: 70, 24: 72, 28: 75, 29: 76, 30: 77, 31: 78}
UNMAPPED = ("EG times", "OSC Volume", "OSC Feedback Level", "amp mod sens", "LFO", "pitch EG", "key scaling ramps",
            "algorithms not seen in the 64 voices (0, 8, 10, 12, 19, 20, 23, 25-27)")


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


def eg_level(x: int) -> int:
    """DX7 EG level 0-99 -> MOD-7 EG level 0-99: round(99 * 2^((x-99)/8)). Exact on all 65 distinct pairs seen."""
    return int(99 * 2.0 ** (-(99 - x) / 8) + 0.5)


def frequency(mode: int, coarse: int, fine: int, detune: int) -> Tuple[int, int, int, int]:
    """-> (ratio coarse, ratio fine, offset coarse, offset fine) as signed field values.
    Ratio mode: coarse (0 = 0.5) * (1 + fine/100) * 2^((detune-7)/1200), rounded to 0.001, split into integer part and
    thousandths. Fixed mode (thinly evidenced: 2 samples): Hz = 10^(coarse % 4 + fine/100), detune applied as in ratio mode (only approximately: 2 voices with detune != 7 are
    off by ~0.04 Hz), whole Hz + hundredths (rounded to the hundredth below 1000 Hz, to the Hz above)."""
    if mode == 0:
        r = (0.5 if coarse == 0 else coarse) * (1 + fine / 100.0) * 2.0 ** ((detune - 7) / 1200.0)
        n = int(r * 1000 + 0.5)
        return n // 1000, n % 1000, 0, 0
    hz = 10.0 ** ((coarse % 4) + fine / 100.0) * 2.0 ** ((detune - 7) / 1200.0)
    if hz >= 1000:                                                  # seen: 2089 and 6607 carry no hundredths
        return 0, 0, int(hz + 0.5), 0
    n = int(hz * 100 + 0.5)
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
    a = ALGORITHM.get(p["algorithm"])
    if a is not None:
        f.set(b, "[OSC] Algorithm", a)
    b[2852] = (p["transpose"] - 24) & 0xFF
    for op in range(6):
        g = lambda k: p[f"op{op}_{k}"]                                # noqa: E731
        e, o = EG_STRIDE * op, OSC_STRIDE * op
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
        f.set(b, "[OSC1] Volume Velocity Sens", VEL_SENS[g("key_vel_sens")], o)
        f.set(b, "[OSC1 Keyboard Track] Center Key", g("bp") + 21, o)
    return bytes(b)
