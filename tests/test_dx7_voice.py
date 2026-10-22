"""Data.dx7_voice against the DX7 factory banks (skipped when the fixtures are absent), plus the loaded-Program ground truth."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from Data.dx7_voice import Dx7Error, name_of, parse_bank, unpack
from fixture_paths import DX7_EXAMPLES

fails = []


def check(name, cond):
    if not cond:
        fails.append(name)


def raises(data):
    try:
        parse_bank(data)
    except Dx7Error:
        return True
    return False


check("short-refused", raises(b"\xf0\x43"))
root = os.path.join(DX7_EXAMPLES, "SysexFormat")
f = os.path.join(root, "ROM1A.SYX")
if not os.path.exists(f):
    print("SKIP real files: fixtures not found in", root)
else:
    data = open(f, "rb").read()
    voices = parse_bank(data)
    check("32-voices", len(voices) == 32 and name_of(voices[0]) == "BRASS   1" and name_of(voices[3]) == "STRINGS 1")
    p = unpack(voices[0])
    check("param-count", len(p) == 145 and all(isinstance(v, int) for v in p.values()))   # 155 voice bytes less the 10-char name
    check("ranges", all(0 <= v <= 99 for k, v in p.items() if "eg_" in k) and 0 <= p["algorithm"] <= 31)
    bad = bytearray(data)
    bad[100] ^= 1
    check("checksum", raises(bytes(bad)))
    loaded = os.path.join(DX7_EXAMPLES, "NautilusLoaded", "ROM1A_loaded_in_bank_Q.bin")
    if os.path.exists(loaded):
        progs = open(loaded, "rb").read()
        check("loaded-names-match", all(progs[s * 4954:s * 4954 + 10].split(b"\x00")[0].decode().rstrip() == name_of(voices[s]).rstrip()
                                        for s in range(32)))
if fails:
    print("FAIL:", fails)
    sys.exit(1)
print("ALL PASS")
