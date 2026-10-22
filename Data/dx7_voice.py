"""Yamaha DX7 voice data: the 32-voice bulk dump (F0 43 0n 09 20 00 + 4096 bytes + checksum + F7, 128 packed bytes per voice) and
the 155 unpacked voice parameters. Operators are numbered as stored: index 0 is operator 6."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

BULK_HEADER = bytes([0xF0, 0x43, 0x00, 0x09, 0x20, 0x00])
PACKED_VOICE = 128
OP_NAMES = ("eg_r1", "eg_r2", "eg_r3", "eg_r4", "eg_l1", "eg_l2", "eg_l3", "eg_l4", "bp", "left_depth", "right_depth",
            "left_curve", "right_curve", "rate_scale", "detune", "amp_mod_sens", "key_vel_sens", "output_level",
            "osc_mode", "freq_coarse", "freq_fine")


class Dx7Error(ValueError):
    pass


def parse_bank(data: bytes) -> List[bytes]:
    """The 32 packed voices of a bulk dump, checksum verified."""
    if len(data) != 4104 or data[:6] != BULK_HEADER or data[-1] != 0xF7:
        raise Dx7Error("Not a DX7 32-voice bulk dump.")
    body = data[6:6 + 4096]
    if (-sum(body)) & 0x7F != data[6 + 4096]:
        raise Dx7Error("The bank's checksum is wrong.")
    return [body[i * PACKED_VOICE:(i + 1) * PACKED_VOICE] for i in range(32)]


def unpack(v: bytes) -> Dict[str, int]:
    """Packed 128-byte voice -> named parameters."""
    if len(v) != PACKED_VOICE:
        raise Dx7Error("A packed DX7 voice is 128 bytes.")
    p: Dict[str, int] = {}
    for op in range(6):
        b = v[op * 17:(op + 1) * 17]
        vals = list(b[0:11]) + [b[11] & 3, (b[11] >> 2) & 3, b[12] & 7, (b[12] >> 3) & 15, b[13] & 3,
                                (b[13] >> 2) & 7, b[14], b[15] & 1, (b[15] >> 1) & 31, b[16]]
        for n, x in zip(OP_NAMES, vals):
            p[f"op{op}_{n}"] = x
    for i in range(4):
        p[f"peg_r{i + 1}"] = v[102 + i]
        p[f"peg_l{i + 1}"] = v[106 + i]
    p["algorithm"] = v[110] & 31
    p["feedback"] = v[111] & 7
    p["osc_sync"] = (v[111] >> 3) & 1
    p["lfo_speed"], p["lfo_delay"], p["lfo_pmd"], p["lfo_amd"] = v[112], v[113], v[114], v[115]
    p["lfo_sync"] = v[116] & 1
    p["lfo_wave"] = (v[116] >> 1) & 7
    p["pitch_mod_sens"] = (v[116] >> 4) & 7
    p["transpose"] = v[117]
    return p


def name_of(v: bytes) -> str:
    return v[118:128].decode("latin-1").rstrip()
