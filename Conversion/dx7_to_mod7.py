"""DX7 voice -> Nautilus EXi Program with a MOD-7 patch.

This is a port of the instrument's own import, `CFileDX7Syx::ImportProgram` / `ImportOperator` / `ImportCommonParams` /
`FixAmpEG` / `ConvertKeyboardLevelScaling` in the EVA application (Z:/Decomp/EVA_Decomp, Eva 3.2.2), with the tables it uses
(`DX7ConvertionTables`, read from the Eva binary into Resources/Conversion/Dx7EgTables.json and Dx7CommonTables.json). Earlier
versions of this file were fitted from Programs the Nautilus built from ROM1A/ROM1B and from P/Q bank dumps; the decompiled code
replaced those fits and explains them (e.g. Release Time is computed from the mid level of the four EG levels, Slope Time takes
DX7 R3, Release Time R4, the EG levels are first scaled by the operator output level).
Checked against 228 distinct voice/Program pairs: 220 are byte-exact over everything this module writes; the other 8 are bank-P
voices whose names repeat in the corpus (probably paired with the wrong voice). No hardware has accepted a Program built by this
module yet.

The MOD-7 section starts at body byte 2902; its fields are bit-packed (table in `Resources/Conversion/Mod7Fields.json`,
generated from `Prog_EXi.txt`). DX7 operators are numbered as stored (index 0 = operator 6) and operator i drives
OSC(i+1) / EG(i+1) (OSC blocks are 45 bytes apart, EG blocks 23). The firmware's parameter ids are the field indexes of that
table (id 591 = [OSC1] Frequency Ratio (Coarse), 378 = [Amplifier EG] Start Level, ...); a Program body offset is the firmware's
CMOSSProg offset minus 6 for the LFO / transpose bytes.
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
# share one MOD-7 value. Identical to the firmware's `kDX7ToSTGAlgorithm` (all 32; the six that no reference voice used,
# DX 13, 20, 21, 24, 26, 27, are confirmed by that table).
_ALG_GROUPS = ((1, 2), (3,), (4,), (5,), (6,), (7, 8, 9), (10, 11), (12, 13), (14, 15), (16, 17), (18,), (19,), (20,), (21,),
               (22,), (23,), (24,), (25,), (26, 27), (28,), (29,), (30,), (31,), (32,))
ALGORITHM: Dict[int, int] = {dx - 1: 55 + i for i, grp in enumerate(_ALG_GROUPS) for dx in grp}      # key: 0-based as stored
ALGORITHM_SEEN = frozenset(a - 1 for a in range(1, 33) if a not in (13, 20, 21, 24, 26, 27))     # 0-based, as stored
# DX7 carriers per algorithm number (operator numbers 1-6). Identical to the firmware's `kDX7IsCarrierTable` bit masks.
CARRIERS = {1: (1, 3), 2: (1, 3), 3: (1, 4), 4: (1, 4), 5: (1, 3, 5), 6: (1, 3, 5), 7: (1, 3), 8: (1, 3), 9: (1, 3),
            10: (1, 4), 11: (1, 4), 12: (1, 3), 13: (1, 3), 14: (1, 3), 15: (1, 3), 16: (1,), 17: (1,), 18: (1,),
            19: (1, 4, 5), 20: (1, 2, 4), 21: (1, 2, 4, 5), 22: (1, 3, 4, 5), 23: (1, 2, 4, 5), 24: (1, 2, 3, 4, 5),
            25: (1, 2, 3, 4, 5), 26: (1, 2, 4), 27: (1, 2, 4), 28: (1, 3, 6), 29: (1, 2, 3, 5), 30: (1, 2, 3, 6),
            31: (1, 2, 3, 4, 5), 32: (1, 2, 3, 4, 5, 6)}
UNMAPPED = ()                                   # nothing known to be missing; the 8 inexact reference pairs are pairing doubts


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


# EG conversion: a port of the firmware's own `CFileDX7Syx::ImportOperator` (EVA 3.2.2, 0x0839d210) with its tables
# `DX7ConvertionTables::kEGKnobToDuration`, `kDX7EGRateScale{Rise,Decay}` and `kDX7EGRateToDuration{Rise,Decay}`
# (Resources/Conversion/Dx7EgTables.json, read from the Eva binary).
@lru_cache(maxsize=1)
def _eg_tables() -> dict:
    return json.loads((_RES / "Dx7EgTables.json").read_text())


def _snap(dur: int, floor: int) -> int:
    """A duration in the firmware's units -> the nearest EG time knob 0-99 (ties go to the lower knob), at least `floor`."""
    knob = _eg_tables()["knob"]
    if dur == 0:
        return floor
    if dur >= 0x1FFF8:
        return 99
    if dur == 1:
        k = 1
    else:
        k = 2
        while knob[k] < dur:
            k += 1
    lower = k - 1
    return max(floor, k) if knob[k] - dur < dur - knob[lower] else max(floor, lower)


def _stage(rate: int, start: int, end: int) -> Tuple[int, int]:
    """One EG stage from level `start` to `end` (the DX7 levels scaled by the operator output level) at `rate` (after rate
    scaling) -> (MOD-7 time, EG curve)."""
    t = _eg_tables()
    if end > start:                       # rising
        dur = _round((t["rs_rise"][end] - t["rs_rise"][start]) * t["rtd_rise"][rate] * 1500.0)
        return _snap(dur, 1), 0
    dur = _round((t["rs_decay"][start] - t["rs_decay"][end]) * t["rtd_decay"][rate] * 1500.0)
    return _snap(dur, 5), _round((start - end) / 99.0 * 10.0)


def _round(x: float) -> int:
    return int(round(x))                  # the firmware rounds to nearest even (fistp)


def eg_params(rates, levels, rate_scale: int, output_level: int) -> dict:
    """The EG fields of one operator from its DX7 rates R1-R4, levels L1-L4, rate scaling 0-7 and output level.
    -> {field name without the [EG1] prefix: value}. Mirrors the firmware, flat-envelope special case included."""
    shift = _round((rate_scale & 7) * 2.516)
    r = [min(99, (x % 100) + shift) for x in rates]
    lv = [0 if x > 99 else x for x in levels]                          # raw L1-L4, a corrupt value counts as 0
    f = float(output_level) / 99.0 if output_level < 99 else 1.0
    sc = [_round(float(x) * f) for x in lv]                              # levels scaled by the output level
    out = {"Start Level": eg_level(lv[3]), "Attack Level": eg_level(lv[0]), "Break Level": eg_level(lv[1]),
           "Sustain Level": eg_level(lv[2]), "Release Level": eg_level(lv[3]), "Time Mod.Source": 30}
    inten = _round((rate_scale & 7) * -4.5)
    stages = (("Attack", r[0], sc[3], sc[0]), ("Decay", r[1], sc[0], sc[1]), ("Slope", r[2], sc[1], sc[2]))
    for name, rate, a, b in stages:
        if a == b:
            t_, c_ = 0, 0
        else:
            t_, c_ = _stage(rate, a, b)
        out[f"{name} Time"], out[f"{name} Time EG Curve"] = t_, c_
        out[f"Time Mod.Intensity To {name} Time"] = inten if t_ > 6 else 0
    l1, l2, l3, l4 = lv
    flat = (l1 == l2 and l1 != l3 and l3 == l4 and r[2] == r[3]) if l1 == l2 else (l2 == l3 and l2 == l4 and r[1] == r[3])
    if flat:      # the last stage is redundant: the firmware copies the Decay stage into Release and sets the reset flags
        out["Trigger At Note On"], out["Reset Source"], out["Reset Threshold"] = 0, 31, 0
        out["Release Time"], out["Release Time EG Curve"] = out["Decay Time"], out["Decay Time EG Curve"]
        out["Time Mod.Intensity To Release Time"] = out["Time Mod.Intensity To Decay Time"]
    else:
        out["Trigger At Note On"], out["Reset Source"], out["Reset Threshold"] = 1, 0, 0
        mid = (max(sc) + min(sc)) // 2                                   # Release runs from the mid level down to L4
        if mid < sc[3]:
            out["Release Time"], _ = _stage(r[3], mid, sc[3])
            out["Release Time EG Curve"] = 0
        else:
            out["Release Time"], _ = _stage(r[3], mid, sc[3]) if mid > sc[3] else (5, 0)
            out["Release Time EG Curve"] = 10
        out["Time Mod.Intensity To Release Time"] = inten if out["Release Time"] > 6 else 0
    return out


def keyboard_level_scaling(depth: int, curve: int, span1: int, span2: int) -> Tuple[int, int]:
    """Port of the firmware's `CFileDX7Syx::ConvertKeyboardLevelScaling` -> (inner ramp, outer ramp), each -100..100."""
    if depth == 0:
        return 0, 0
    if curve == 1 or curve == 2:
        if curve == 1:
            depth = -depth
        f1 = math.exp((span1 - 72.0) / 18.0) * depth
        f3 = math.exp((span1 + span2 - 72.0) / 18.0) * depth
    else:
        if curve == 0:
            depth = -depth
        f1 = span1 * depth / 45.0
        f3 = (span2 + span1) * depth / 45.0
    d5, d6 = 2.0 ** (f1 * 0.125), 2.0 ** (0.125 * f3)

    def ramp(num: float, span: int) -> int:
        if span == 0:
            return -100                       # x / 0 overflows the float -> int conversion to INT_MIN, then clamps
        return max(-100, min(100, _round(num / span)))

    return ramp((1.0 - d5) * -1188.0, span1), ramp((d5 - d6) * -1188.0, span2)


def keyboard_track(bp: int, left_depth: int, right_depth: int, curves: int) -> dict:
    """DX7 breakpoint / scaling depths / curves byte -> the OSC Keyboard Track fields (firmware `ImportOperator`)."""
    centre = bp + 21 if bp < 99 else 120
    ld = left_depth if left_depth < 99 else 99
    span_l = 24.48 if left_depth >= 99 else (ld * -0.04 + 6.0) * 12.0
    rd = right_depth if right_depth < 99 else 99
    low = max(21, _round(centre - span_l))
    high = min(120, _round((rd * -0.04 + 6.0) * 12.0 + centre))
    low_mid, high_mid = (low + centre) >> 1, (high + centre) // 2
    out = {"Low Key": low_mid, "Center Key": centre, "High Key": high_mid}
    if ld:
        out["Center Low Ramp"], out["Low Ramp"] = keyboard_level_scaling(ld, curves & 3, centre - low_mid, low_mid - low)
    if rd:
        out["Center High Ramp"], out["High Ramp"] = keyboard_level_scaling(rd, (curves >> 2) & 3, high_mid - centre, high - high_mid)
    return out


def amp_release_time(carriers_stored, egs, voice) -> int:
    """Port of the firmware's `FixAmpEG`: the Amplifier EG Release Time. A carrier that does not end silent (its L4 is not 0
    and its output is not 0) makes the voice sustain (99); otherwise it is the longest carrier release in seconds (release
    time knob, lengthened by the rate-scaling intensity), mapped back to a knob and moved one up."""
    knob = _eg_tables()["knob"]
    secs, sustain, first = 0.0, False, True
    for i in carriers_stored:                                  # stored index, highest first (the firmware's bit order)
        ol, l4 = voice[f"op{i}_output_level"], voice[f"op{i}_eg_l4"]
        if not ((ol < 99 and ol == 0) or l4 > 99 or l4 == 0):
            sustain = True
            continue
        eg = egs[i]
        v = knob[eg["Release Time"]] / 1500.0
        frac = abs(eg["Time Mod.Intensity To Release Time"]) / 99.0
        if frac != 0.0:
            if v < 0.005333333:
                v = 0.005333333
            v *= 2.0 ** min(frac * 12.0, 15.0)
        v = max(v, 0.0)
        secs = v if first else max(secs, v)
        first = False
    if sustain:
        return 99
    dur = _round(secs * 1500.0)
    if dur == 0:
        return 1
    if dur >= 0x1FFF8:
        return 99
    k = 1 if dur == 1 else 2
    if dur != 1:
        while knob[k] < dur:
            k += 1
    if dur - knob[k - 1] <= knob[k] - dur:
        k -= 1
    return min(99, k + 1)


@lru_cache(maxsize=1)
def _common_tables() -> dict:
    return json.loads((_RES / "Dx7CommonTables.json").read_text())


def _pitch_scale(x: float) -> float:
    """Semitones -> the MOD-7 pitch intensity scale (the firmware's piecewise curve, used for the pitch EG and the LFO)."""
    if x < 0.0:
        if x > -0.55:
            return x * 100.0
        if x > -1.0:
            return (x + 0.5) * 20.0 - 50.0
        if x > -12.0:
            return (x + 1.0) * 5.0 - 60.0
        return (x + 12.0) - 115.0
    if x < 0.55:
        return x * 100.0
    if x < 1.0:
        return (x - 0.5) * 20.0 + 50.0
    if x >= 12.0:
        return (x - 12.0) + 115.0
    return (x - 1.0) * 5.0 + 60.0


def _snap_down(dur: int, floor: int = 0) -> int:
    """Duration -> nearest knob, as in the pitch-EG and Amplifier-EG code (0 stays 0, ties go to the lower knob)."""
    knob = _eg_tables()["knob"]
    if dur == 0:
        return 0
    if dur >= 0x1FFF8:
        return 99
    if dur == 1:
        return 1
    k = 2
    while knob[k] < dur:
        k += 1
    return k - 1 if dur - knob[k - 1] <= knob[k] - dur else k


EG_DEFAULT = {"Start Level": 0, "Attack Level": 99, "Break Level": 75, "Sustain Level": 75, "Release Level": 0,
              "Attack Time": 0, "Decay Time": 50, "Slope Time": 30, "Release Time": 20, "Attack Time EG Curve": 3,
              "Decay Time EG Curve": 6, "Slope Time EG Curve": 6, "Release Time EG Curve": 6, "Trigger At Note On": 1}


def pitch_eg(p: dict, ceil_max: bool = True):
    """The voice's pitch EG -> (EG9 fields, Common Pitch Mod.Intensity) or None when it is flat. Firmware `ImportCommonParams`."""
    t = _common_tables()
    pl = []
    for i in range(1, 5):
        x = p[f"peg_l{i}"]
        pl.append(t["pitch_eg_level"][x if x < 100 else 50])
    top = max(abs(x) for x in pl)
    top = math.ceil(top) if ceil_max else float(_round(top))
    if not top > 0:
        return None
    out = {"Start Level": _round(pl[3] * (99.0 / top)), "Attack Level": _round(pl[0] * (99.0 / top)),
           "Break Level": _round(pl[1] * (99.0 / top)), "Sustain Level": _round(pl[2] * (99.0 / top)),
           "Release Level": _round(pl[3] * (99.0 / top))}
    out["Release Level"] = out["Start Level"]
    rates = [p[f"peg_r{i}"] % 100 for i in range(1, 5)]
    names = ("Attack Time", "Decay Time", "Slope Time")
    for k, name in enumerate(names):                          # L4 -> L1, L1 -> L2, L2 -> L3
        frm, to = (pl[3], pl[0]) if k == 0 else (pl[k - 1], pl[k])
        out[name] = _snap_down(_round(abs(to - frm) * t["pitch_eg_time"][rates[k]] / 48.0 * 1500.0))
    big = max(abs(pl[3] - pl[i]) for i in range(3))
    out["Release Time"] = _snap_down(_round(big * t["pitch_eg_time"][rates[3]] * 0.5 / 48.0 * 1500.0))
    for n in ("Attack", "Decay", "Slope", "Release"):
        out[f"{n} Time EG Curve"] = 0
    return out, _round(_pitch_scale(top))


def lfo_and_common(p: dict) -> Tuple[Dict[str, int], Dict[int, int]]:
    """LFO, pitch EG, LFO delay and the other voice-wide fields (firmware `ImportCommonParams`).
    -> ({full field name: value}, {Program body offset: byte})."""
    t = _common_tables()
    f: Dict[str, int] = {"[PCM OSC] Volume EG Select": 7, "[Common Pitch] LFO Select": 4,
                         "[Common Pitch] LFO Int.Mod.Source": 34}
    for eg in ("EG7", "EG9"):
        for k, v in EG_DEFAULT.items():
            f[f"[{eg}] {k}"] = v
    pe = pitch_eg(p)
    if pe:
        for k, v in pe[0].items():
            f[f"[EG9] {k}"] = v
        f["[Common Pitch] Mod.Source"], f["[Common Pitch] Mod.Intensity"] = 19, pe[1]
    wave = min(p["lfo_wave"], 5)
    e = t["lfo_wave"][wave * 4: wave * 4 + 4]
    sign = e[3] - 256 if e[3] > 127 else e[3]
    pms = t["pms_semis"][p["pitch_mod_sens"]]
    depth = (p["lfo_pmd"] / 99.0 if p["lfo_pmd"] < 99 else 1.0) * sign * pms
    inten = _round(_pitch_scale(depth))
    delay = p["lfo_delay"]
    if delay == 0:
        f["[Common Pitch] LFO Intensity"] = inten
    else:
        f["[Common Pitch] LFO Intensity"], f["[Common Pitch] LFO Int.Mod.Source"] = 0, 17
        f["[Common Pitch] LFO Int.Mod.Intensity"] = inten
        d = delay if delay < 99 else 74
        d = ((d - 50) >> 1) + 50 if d > 49 else d
        tm = _round(d * 0.66)
        f.update({"[PCM OSC] Volume EG Select": 0, "[EG7] Trigger At Note On": 0, "[EG7] Reset Source": 31,
                  "[EG7] Reset Threshold": 0, "[EG7] Start Level": 0, "[EG7] Attack Level": 0, "[EG7] Break Level": 99,
                  "[EG7] Sustain Level": 99, "[EG7] Release Level": 99, "[EG7] Attack Time": d, "[EG7] Decay Time": tm,
                  "[EG7] Slope Time": 0, "[EG7] Release Time": 0, "[EG7] Attack Time EG Curve": 0,
                  "[EG7] Decay Time EG Curve": 0, "[EG7] Slope Time EG Curve": 0, "[EG7] Release Time EG Curve": 0})
    f["[Common Pitch] LFO JS(+Y) Intensity"] = _round(_pitch_scale(sign * pms * 0.7333333333333333))
    raw = {2713: t["lfo_freq"][min(p["lfo_speed"], 99)], 2720: 0x28 if p["lfo_sync"] else 0,
           2715: e[2], 2716: e[1], 2888: (sign * 50) & 0xFF, 2889: ((sign * 50) >> 8) & 0xFF, "wave": e[0] & 0x3F}
    return f, raw


def feedback(p: dict, dx_alg_index: int) -> Dict[Tuple[str, int], int]:
    """The feedback loop: {(field, OSC index): value}. Algorithms 4 and 6 (0-based 3, 5) feed an Input Level instead."""
    t = _common_tables()
    osc = 5 - t["fb_dest"][dx_alg_index]
    fb = t["fb_level"][p["feedback"]]
    if dx_alg_index in (3, 5):
        return {("[OSC1] Input Level1", osc): _round(math.sqrt(fb) * 100.0)}
    return {("[OSC1] Feedback Level", osc): _round((fb + fb) ** (1.0 / 3.0) * 100.0), ("[OSC1] Feedback Pre/Post", osc): 1}


def amp_mod(ams: int, amd: int, delay: int) -> Dict[str, int]:
    """Amplitude-modulation sensitivity of one operator -> its Volume Mod fields."""
    if ams == 0:
        return {}
    x = 1.0 - amd / 100.0 if amd < 99 else 0.01
    y = x ** 0.25 if ams == 1 else math.sqrt(x) if ams == 2 else x
    a = _round(math.sqrt(1.0 - y) * 100.0)
    b = _round(math.sqrt(1.0 - {1: 0.8408964, 2: 0.70710677, 3: 0.5}[ams]) * 100.0)
    out = {"Volume Mod.Source": 2, "Volume Mod.Mode": 1}
    if delay == 0 or a == 0:
        out.update({"Volume Mod.Intensity": a, "Volume Mod.Int.Mod.Source": 37, "Volume Mod.Int.Mod.Intensity": b})
    else:
        out.update({"Volume Mod.Intensity": 0, "Volume Mod.Int.Mod.Source": 17, "Volume Mod.Int.Mod.Intensity": a})
    return out


def _signed(v: int, bits: int) -> int:
    return v & ((1 << bits) - 1)


# Bits of the MOD-7 section that no named field covers: the unit's Init Program has them set where the template has them
# clear. Offset -> bits to flip; the same on all 228 reference Programs.
_INIT_XOR = {3086: 128, 3190: 32, 3224: 16, 3464: 3, 3472: 50, 3495: 8, 3509: 3, 3540: 16, 3554: 3, 3585: 24, 3599: 3,
             3607: 50, 3630: 32, 3644: 3, 3652: 50, 3675: 40, 3689: 3, 3720: 48}


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
    egs = {}
    common, raw = lfo_and_common(p)
    for k_, v_ in common.items():
        f.set(b, k_, _signed(v_, f.width(k_)))
    for off, v_ in raw.items():
        if off == "wave":
            b[2712] = v_ | (b[2712] & 0xC0)
        else:
            b[off] = v_
    b[2852] = (p["transpose"] - 24) & 0xFF
    for op in range(6):
        g = lambda k: p[f"op{op}_{k}"]                                # noqa: E731
        e, o = EG_STRIDE * op, OSC_STRIDE * op
        f.set(b, "[EG1] Time Mod.Source", 30, e)                       # key follower: carries the DX7 rate scaling
        eg = eg_params([g("eg_r1"), g("eg_r2"), g("eg_r3"), g("eg_r4")], [g("eg_l1"), g("eg_l2"), g("eg_l3"), g("eg_l4")],
                       g("rate_scale"), g("output_level"))
        egs[op] = eg
        for k_, v_ in eg.items():
            if True:
                f.set(b, f"[EG1] {k_}", _signed(v_, f.width(f"[EG1] {k_}")), e)
        for k_, v_ in amp_mod(g("amp_mod_sens"), p["lfo_amd"], p["lfo_delay"]).items():
            f.set(b, f"[OSC1] {k_}", _signed(v_, f.width(f"[OSC1] {k_}")), o)
        f.set(b, "[OSC1] Input Level1", 100, o)                           # default; feedback algorithms 4 and 6 override it
        f.set(b, "[OSC1] Phase Sync", 0 if p["osc_sync"] else 2, o)     # DX7 oscillator key sync on -> 0, off -> 2
        rc, rf, oc, of = frequency(g("osc_mode"), g("freq_coarse"), g("freq_fine"), g("detune"))
        f.set(b, "[OSC1] Frequency Ratio (Coarse)", rc, o)
        f.set(b, "[OSC1] Frequency Ratio (Fine)", _signed(rf, 11), o)
        f.set(b, "[OSC1] Frequency Offset (Coarse)", _signed(oc, 15), o)
        f.set(b, "[OSC1] Frequency Offset (Fine)", _signed(of, 8), o)
        is_carrier = (6 - op) in carriers                              # stored index 0 is DX operator 6
        f.set(b, f"[Macro] OSC{op + 1} Class", 0 if is_carrier else 1)
        f.set(b, "[OSC1] Volume", osc_volume(g("output_level"), len(carriers), is_carrier), o)
        f.set(b, "[OSC1] Volume Velocity Sens", VEL_SENS[g("key_vel_sens")], o)
        for k_, v_ in keyboard_track(g("bp"), g("left_depth"), g("right_depth"), g("left_curve") | (g("right_curve") << 2)).items():
            f.set(b, f"[OSC1 Keyboard Track] {k_}", _signed(v_, f.width(f"[OSC1 Keyboard Track] {k_}")), o)
    for (k_, osc), v_ in feedback(p, p["algorithm"]).items():
        f.set(b, k_, _signed(v_, f.width(k_)), OSC_STRIDE * osc)
    f.set(b, "[Amplifier EG] Release Time", amp_release_time(sorted((6 - c for c in carriers), reverse=True)[::-1], egs, p))
    for off_, x_ in _INIT_XOR.items():
        b[off_] ^= x_
    if 11 <= p["algorithm"] <= 17:                                 # an algorithm-dependent bit: set for DX 12-18 (0-based 11-17)
        b[3891] |= 32
    return bytes(b)
