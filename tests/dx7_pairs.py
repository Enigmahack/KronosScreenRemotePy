"""Pair Nautilus Programs (read back after native DX7 loads) with their source DX7 voices from the corpus.

A Program is matched to the corpus voice(s) with the same name whose *exact* converter fields (algorithm, EG levels, frequency,
volume) reproduce it; an ambiguous match (identical voices) is fine, they convert identically. Used by the harness and tests.
`load_pairs()` -> list of (packed voice, Program body, label)."""
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from Conversion import dx7_to_mod7 as M
from Data import dx7_voice as D

BASE = os.environ.get("KRONOS_DX7_EXAMPLES", "Z:/DX7 EXAMPLES/")
EXACT = ("[OSC] Algorithm", "[EG1] Attack Level", "[EG1] Break Level", "[EG1] Sustain Level", "[EG1] Release Level",
         "[OSC1] Frequency Ratio (Coarse)", "[OSC1] Volume")


def corpus_voices():
    by_name = {}
    seen = set()
    for f in sorted(glob.glob(BASE + "SysexFormat/*.SYX") + glob.glob(BASE + "SysexFormat/*.syx")):
        try:
            vs = D.parse_bank(open(f, "rb").read())
        except (D.Dx7Error, OSError):
            continue
        for v in vs:
            if v not in seen:
                seen.add(v)
                by_name.setdefault(D.name_of(v).strip(), []).append(v)
    return by_name


def _exact_equal(out: bytes, body: bytes) -> bool:
    f = M._fields()
    for op in range(6):
        e, o = M.EG_STRIDE * op, M.OSC_STRIDE * op
        for nm in EXACT:
            sh = e if nm.startswith("[EG") else (o if nm.startswith("[OSC1]") else 0)
            if f.get(out, nm, sh) != f.get(body, nm, sh):
                return False
    return True


SCORED = EXACT + ("[OSC1] Frequency Ratio (Fine)", "[OSC1] Volume Velocity Sens", "[OSC1 Keyboard Track] Center Key",
                  "[EG1] Start Level")


def _score(out: bytes, body: bytes) -> int:
    """How many fitted fields agree (more = a better candidate when several voices share a name)."""
    f = M._fields()
    n = int(out[2852] == body[2852])
    for op in range(6):
        e, o = M.EG_STRIDE * op, M.OSC_STRIDE * op
        for nm in SCORED:
            sh = e if nm.startswith("[EG") else (o if nm.startswith("[OSC1") else 0)
            n += f.get(out, nm, sh) == f.get(body, nm, sh)
    return n


def load_pairs(dump_dirs=("Probes/Loaded",), skip_names=("INIT VOICE", "Init EXi P")):
    by_name = corpus_voices()
    pairs = []
    for d in dump_dirs:
        for path in sorted(glob.glob(BASE + d + "/bank_*.bin")):
            data = open(path, "rb").read()
            for slot in range(len(data) // M.PROGRAM_SIZE):
                body = data[slot * M.PROGRAM_SIZE:(slot + 1) * M.PROGRAM_SIZE]
                name = bytes(body[:24]).split(b"\0")[0].decode("latin-1").strip()
                if not name or name in skip_names:
                    continue
                best = None
                for v in by_name.get(name, []):
                    out = M.convert_voice(v)
                    if _exact_equal(out, body):
                        sc = _score(out, body)
                        if best is None or sc > best[0]:
                            best = (sc, v)
                if best is not None:
                    pairs.append((best[1], body, f"{os.path.basename(path)}[{slot}] {name}"))
    return pairs


def rom_pairs():
    """The first ground truth: ROM1A / ROM1B natively loaded into Nautilus banks Q / R."""
    out = []
    for f, loaded in (("ROM1A", "ROM1A_loaded_in_bank_Q"), ("ROM1B", "ROM1B_loaded_in_bank_R")):
        vs = D.parse_bank(open(BASE + f"SysexFormat/{f}.SYX", "rb").read())
        b = open(BASE + f"NautilusLoaded/{loaded}.bin", "rb").read()
        out += [(v, b[i * M.PROGRAM_SIZE:(i + 1) * M.PROGRAM_SIZE], f"{f}[{i}]") for i, v in enumerate(vs)]
    return out


def all_pairs():
    """ROM1A/B plus every paired Program from the later bank dumps, one entry per distinct (voice, Program)."""
    seen, out = set(), []
    for v, b, label in rom_pairs() + load_pairs():
        if (v, b) not in seen:
            seen.add((v, b)); out.append((v, b, label))
    return out


if __name__ == "__main__":
    ps = all_pairs()
    print(len(ps), "distinct (voice, Program) pairs")
