"""SD6 known defect: a truncated .KSF/.KMP opens as a valid, shorter object. Data.ksf_sample.KsfSample.open clamps
the declared frame_count to the bytes present (min(wanted, available)), though its own docstring says a truncated
download "must fail loudly"; the same happens for KMP zone lists, and a KSF cut at an odd byte opens fine and then
raises ValueError from samples(). Invariant (sound-designer.md SD6): damaged input is refused, never half-read.
Fails until open() returns None (or a flagged object) when the declared size exceeds the bytes present."""
import os, sys, glob, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("KRONOS_DATA_DIR", tempfile.mkdtemp(prefix="kr_data_"))
from pathlib import Path
from fixture_paths import SAMPLE_FIXTURES
from Data.ksf_sample import KsfSample
from Data.kmp_multisample import KmpMultisample

bad = []
for kind, parse in (("KSF", KsfSample.open), ("KMP", KmpMultisample.open)):
    files = [f for f in sorted(glob.glob(f"{SAMPLE_FIXTURES}/**/*.{kind}", recursive=True)) if Path(f).stat().st_size > 400][:4]
    for f in files:
        d = Path(f).read_bytes()
        for cut in (1, 2, 100, len(d) // 2):
            if parse(d[:-cut] if cut < len(d) // 2 else d[:cut]) is not None:
                bad.append(f"{kind} {Path(f).name} cut={cut}")
if not bad and not glob.glob(f"{SAMPLE_FIXTURES}/**/*.KSF", recursive=True):
    print("SKIPPED: no hardware fixtures"); sys.exit(0)
print(f"{len(bad)} truncated inputs accepted as valid; e.g. {bad[:3]}")
sys.exit(1 if bad else 0)
