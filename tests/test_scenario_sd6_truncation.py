"""SD6: a truncated .KSF/.KMP is refused, never half-read. KsfSample.open / KmpMultisample.open used to clamp the
declared sizes to the bytes present, so a half-downloaded KSF opened as a shorter sample (and an odd-byte cut then
raised from samples()), and a KMP cut at a chunk boundary silently lost zones. Rule: every strict prefix of a real
hardware-captured file opens as None; the whole file still opens and round-trips byte-identical; header-only and
SMF1-stub files (frame count 0, no PCM) are NOT truncated and still open; a pull that meets a cut-off KMP reports it."""
import os, sys, glob, random, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("KRONOS_DATA_DIR", tempfile.mkdtemp(prefix="kr_data_"))
import logging; logging.disable(logging.CRITICAL)
from pathlib import Path
from fixture_paths import SAMPLE_FIXTURES
import Core.sample_ftp as ftp
from Data import korg_riff_chunk as riff
from Data.ksc_collection import KscCollection
from Data.ksf_sample import KsfSample
from Data.kmp_multisample import KmpMultisample, KmpZone
from pipeline_fakes import FaultyFtp

fails = []


def check(name, cond):
    if not cond:
        print("FAIL " + name)
        fails.append(name)


files = {k: sorted(glob.glob(f"{SAMPLE_FIXTURES}/**/*.{k}", recursive=True)) for k in ("KSF", "KMP")}
if not files["KSF"]:
    print("SKIPPED: no hardware fixtures at", SAMPLE_FIXTURES)
    sys.exit(0)

rng = random.Random(9)
header_only = n_cuts = 0
for kind, parse in (("KSF", KsfSample.open), ("KMP", KmpMultisample.open)):
    for f in files[kind]:
        name = Path(f).name
        d = Path(f).read_bytes()
        whole = parse(d)
        check(f"{kind} {name}: whole file opens", whole is not None)
        if whole is None:
            continue
        check(f"{kind} {name}: round-trips byte-identical", whole.to_bytes() == d)
        if kind == "KSF" and whole.is_header_only:
            header_only += 1
            check(f"KSF {name}: header-only file is not mistaken for truncated", whole.frame_count == 0)
        ends, pos = [], 0
        for _, payload in riff.read_chunks(d):
            pos += 8 + len(payload)
            ends.append(pos)
        cuts = {c for e in ends for c in (e - 2, e - 1, e, e + 1) if 0 < c < len(d)}
        cuts |= {1, 2, 7, 8, 9, 40, 100, len(d) - 1, len(d) - 2, len(d) - 3, len(d) // 2}
        cuts |= {rng.randrange(1, len(d)) for _ in range(25)}
        for c in sorted(x for x in cuts if 0 < x < len(d)):
            n_cuts += 1
            r = parse(d[:c])
            if r is not None:
                check(f"{kind} {name}: prefix of {c}/{len(d)} bytes accepted", False)
check("fixtures include header-only/stub KSF files", header_only > 0)

# a pull that meets a cut-off KMP says so instead of silently skipping its samples
kmp = KmpMultisample(name="Show", mno1=1)
kmp.zones += [KmpZone(filename="MS001000.KSF", original_key=60, top_key=60),
              KmpZone(filename="MS001001.KSF", original_key=61, top_key=61)]
full = kmp.to_bytes()
check("synthetic KMP opens", KmpMultisample.open(full) is not None)
with tempfile.TemporaryDirectory(prefix="kr_sd6t_") as d:
    ksc = Path(d) / "Show.KSC"
    KscCollection(entries=["Show.KMP"]).save(str(ksc))
    remote = FaultyFtp({"/SSD2/Show/Show.KSC": ksc.read_bytes(), "/SSD2/Show/Show/Show.KMP": full[:-5]})
    _, mapping, failures = ftp.pull(remote, "/SSD2/Show/Show.KSC", str(Path(d) / "out"))
    check("pull reports a truncated KMP", any("Show.KMP" in m for m in failures))

print(f"{n_cuts} truncations of {len(files['KSF'])} KSF + {len(files['KMP'])} KMP fixtures; {len(fails)} violations")
sys.exit(1 if fails else 0)
