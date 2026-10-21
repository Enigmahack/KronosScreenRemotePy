"""SD6: damaged KSF/KMP/KSC. Mutates real hardware-captured fixtures (truncations, byte flips, zeroed, empty) and
checks the parsers never raise or hang, and that a push of a collection with damaged local files reports it and
never uploads a half-read file. Truncation DETECTION is the separate, known-failing test_scenario_sd6_truncation."""
import os, sys, tempfile, random, time, glob, shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("KRONOS_DATA_DIR", tempfile.mkdtemp(prefix="kr_data_"))
import logging; logging.disable(logging.CRITICAL)
from pathlib import Path
from fixture_paths import SAMPLE_FIXTURES
import Core.sample_ftp as ftp
from Data.ksf_sample import KsfSample
from Data.kmp_multisample import KmpMultisample
from Data.ksc_collection import KscCollection
from pipeline_fakes import FaultyFtp

fails = []


def check(name, cond):
    if not cond:
        print("FAIL " + name)
        fails.append(name)


PARSE = {"KSF": KsfSample.open, "KMP": KmpMultisample.open, "KSC": KscCollection.open}
rng = random.Random(6)
found = {k: sorted(glob.glob(f"{SAMPLE_FIXTURES}/**/*.{k}", recursive=True))[:8] for k in PARSE}
if not any(found.values()):
    print("SKIPPED: no hardware fixtures at", SAMPLE_FIXTURES)
    sys.exit(0)

push_case_ran = False
n = 0
for kind, files in found.items():
    for f in files:
        d = Path(f).read_bytes()
        muts = [b"", d[:1], d[:12], d[:len(d) // 2], d[:-1], b"\0" * len(d), b"\xff" * 64, d * 2]
        for _ in range(40):
            b = bytearray(d)
            for _ in range(rng.randint(1, 8)):
                b[rng.randrange(len(b))] = rng.randrange(256)
            muts += [bytes(b), d[:rng.randrange(len(d))]]
        for m in muts:
            n += 1
            t = time.time()
            try:
                PARSE[kind](m)
            except Exception as e:
                check(f"{kind} {Path(f).name}: parser raised {type(e).__name__} on {len(m)}-byte input", False)
            check(f"{kind}: parse took >2s", time.time() - t < 2)

# push of a collection whose local files are damaged
src = next((Path(p) for p in glob.glob(f"{SAMPLE_FIXTURES}/**/*.KSC", recursive=True)
            if KscCollection.open(Path(p).read_bytes()).entries and any(e.upper().endswith(".KMP") for e in KscCollection.open(Path(p).read_bytes()).entries)), None)
if src:
    with tempfile.TemporaryDirectory(prefix="kr_sd6_") as tmp:
        dst = Path(tmp) / src.name
        shutil.copy(src, dst)
        if (src.parent / src.stem).is_dir():
            shutil.copytree(src.parent / src.stem, Path(tmp) / src.stem)
        col = KscCollection.open(dst.read_bytes())
        col.path = str(dst)
        kmps = sorted((Path(tmp) / src.stem).glob("*.KMP"))
        if kmps:
            kmps[0].write_bytes(b"\x00garbage, not a KMP\xff" * 5)
            remote = FaultyFtp({"/SSD2/x/seed": b""})
            failures = ftp.push_closure(remote, str(dst), col, "/SSD2/x")
            check("damaged KMP: push reports a failure", any(kmps[0].name in f for f in failures))
            check("damaged KMP: zones of the damaged KMP not uploaded",
                  not any(p.endswith(".KSF") and f"/{kmps[0].stem}/" in p for p in remote.files))
            check("damaged KMP: no .part left", not [p for p in remote.files if p.endswith(".part")])
            push_case_ran = True

# an entry that tries to leave the collection folder must not be followed
evil = KscCollection(entries=["../../outside.KSF"])
with tempfile.TemporaryDirectory(prefix="kr_sd6_") as tmp:
    p = Path(tmp) / "Evil.KSC"
    evil.save(str(p))
    evil.path = str(p)
    remote = FaultyFtp({"/SSD2/x/seed": b""})
    try:
        r = ftp.push_closure(remote, str(p), evil, "/SSD2/x")
        check("path traversal: not uploaded", not any("outside" in k for k in remote.files))
    except Exception:
        pass   # a guard exception is an acceptable refusal

check("damaged-KMP push case actually ran (fixture with a KMP found)", push_case_ran)
print(f"{n} mutated inputs; {len(fails)} violations")
sys.exit(1 if fails else 0)
