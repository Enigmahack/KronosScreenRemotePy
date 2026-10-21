"""SD6/SP7: a truncated or unreadable local file must never replace the instrument's good copy. push_closure
validates every file it would upload BEFORE sending anything; a bad one aborts the whole push (a closure must not
land half-pushed), names the file, and leaves every remote byte as it was."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("KRONOS_DATA_DIR", tempfile.mkdtemp(prefix="kr_data_"))
import logging; logging.disable(logging.CRITICAL)
import numpy as np
from pathlib import Path
import Core.sample_ftp as ftp
from Data.ksc_collection import KscCollection
from Data.kmp_multisample import KmpMultisample, KmpZone
from Data.ksf_sample import KsfSample
from pipeline_fakes import FaultyFtp

fails = []


def check(name, cond):
    if not cond:
        print("FAIL " + name)
        fails.append(name)


def build(root, tag):
    root = Path(root)
    (root / "Show" / "Show").mkdir(parents=True)
    ksc = root / "Show.KSC"
    col = KscCollection(entries=["Show.KMP", "BARE.KSF"])
    col.save(str(ksc))
    k = KmpMultisample(name="Show", mno1=1)
    k.zones.append(KmpZone(filename="MS001000.KSF", original_key=60, top_key=60))
    k.save(str(root / "Show" / "Show.KMP"))
    for rel in ("Show/Show/MS001000.KSF", "Show/BARE.KSF"):
        s = KsfSample(name=tag, sample_rate=44100)
        s.set_samples(np.arange(3000, dtype=np.int16) + ord(tag))
        s.save(str(root / rel))
    col = KscCollection.open(ksc.read_bytes())
    col.path = str(ksc)
    return ksc, col, root


def snapshot(root):
    return {str(p.relative_to(root)).replace(os.sep, "/"): p.read_bytes() for p in Path(root).rglob("*") if p.is_file()}


DEST = "/SSD2/Show"
CASES = {"truncated KMP": ("Show/Show.KMP", "Show.KMP"),
         "truncated zone KSF": ("Show/Show/MS001000.KSF", "MS001000.KSF"),
         "truncated bare KSF": ("Show/BARE.KSF", "BARE.KSF")}
with tempfile.TemporaryDirectory(prefix="kr_pre_") as tmp:
    _, _, old_root = build(Path(tmp) / "old", "a")
    old = {f"{DEST}/{k}": v for k, v in snapshot(old_root).items()}
    ksc, col, root = build(Path(tmp) / "new", "b")
    good = snapshot(root)
    check("fixture: a clean push goes through", ftp.push_closure(FaultyFtp(old), str(ksc), col, DEST) == [])
    for tag, (rel, name) in CASES.items():
        data = good[rel]
        for cut in (1, 2, len(data) // 2):
            (root / rel).write_bytes(data[:-cut])
            remote = FaultyFtp(old)
            failures = ftp.push_closure(remote, str(ksc), col, DEST)
            label = f"{tag} (cut {cut})"
            check(f"{label}: failure names {name}", any(name in f for f in failures))
            check(f"{label}: nothing uploaded, old bytes untouched", remote.files == old)
            check(f"{label}: no transport call made", remote.ops == 0)
        (root / rel).write_bytes(data)
    (root / "Show/Show.KMP").unlink()
    failures = ftp.push_closure(FaultyFtp(old), str(ksc), col, DEST)
    check("missing KMP keeps the old 'not found locally' behaviour", any("Show.KMP: not found locally" in f for f in failures))

print(f"{len(fails)} violations")
sys.exit(1 if fails else 0)
