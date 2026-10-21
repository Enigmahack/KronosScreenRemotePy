"""SP7: connection drops mid-push / mid-pull. Sweeps a fault across every transport op of a collection push and
pull and checks the persona invariants (pipeline/personas/show-preparer.md): a previously-valid remote file is
never left truncated or missing, a failed pull never leaves a partial local file, and any failure is reported."""
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
    """A small real-format collection whose bytes differ per `tag`."""
    root = Path(root)
    (root / "Show").mkdir(parents=True)
    ksc = root / "Show.KSC"
    KscCollection(entries=["Show.KMP"]).save(str(ksc))
    k = KmpMultisample(name="Show", mno1=1)
    k.zones += [KmpZone(filename="MS001000.KSF", original_key=60, top_key=60),
                KmpZone(filename="MS001001.KSF", original_key=61, top_key=61)]
    k.save(str(root / "Show" / "Show.KMP"))
    (root / "Show" / "Show").mkdir()
    for i, fn in enumerate(("MS001000.KSF", "MS001001.KSF")):
        s = KsfSample(name=f"S{i}{tag}", sample_rate=44100)
        s.set_samples(np.arange(2000, dtype=np.int16) + (i + ord(tag)))
        s.save(str(root / "Show" / "Show" / fn))
    return ksc, KscCollection.open(ksc.read_bytes()), root


def snapshot(root):
    return {str(p.relative_to(root)).replace(os.sep, "/"): p.read_bytes() for p in Path(root).rglob("*") if p.is_file()}


DEST = "/SSD2/Show"
with tempfile.TemporaryDirectory(prefix="kr_sp7_") as tmp:
    old_ksc, old_col, old_root = build(Path(tmp) / "old", "a")
    new_ksc, new_col, new_root = build(Path(tmp) / "new", "b")
    old = {f"{DEST}/{k}": v for k, v in snapshot(old_root).items()}
    new = {f"{DEST}/{k}": v for k, v in snapshot(new_root).items()}
    check("fixture-old-new-differ", all(old[k] != new[k] for k in old if k.endswith(".KSF")))

    for overwrite in (True, False):
        clean = FaultyFtp(old, overwrite=overwrite)
        check(f"overwrite={overwrite} clean-push-no-failures", ftp.push_closure(clean, str(new_ksc), new_col, DEST) == [])
        check(f"overwrite={overwrite} clean-push-new-content", all(clean.files.get(k) == v for k, v in new.items()))
        check(f"overwrite={overwrite} clean-push-nothing-left-behind", sorted(clean.files) == sorted(new))
        n_ops = clean.ops

        for dead in (True, False):
            for i in range(n_ops):
                tag = f"push overwrite={overwrite} dead={dead} op={i}"
                f = FaultyFtp(old, fail_at=i, dead=dead, overwrite=overwrite)
                failures = ftp.push_closure(f, str(new_ksc), new_col, DEST)
                # silent success is only legitimate when a retry absorbed a one-off error (deleting the replaced
                # .old file) and the result is exactly the new collection with nothing left behind
                absorbed = not dead and sorted(f.files) == sorted(new) and all(f.files[k] == v for k, v in new.items())
                check(f"{tag}: failure reported", bool(failures) or absorbed)
                for path, was in old.items():
                    got = f.files.get(path)
                    name = path.rsplit('/', 1)[-1]
                    check(f"{tag}: {name} not truncated", got is None or got in (was, new[path]))
                    # the previous version survives at the final path or, mid-swap, at a named .old file
                    check(f"{tag}: {name} previous or new version survives", got in (was, new[path]) or was in f.files.values())
                if not dead:
                    check(f"{tag}: no leftover .part/.old", not [p for p in f.files if p.endswith((".part", ".old"))])

    # pull
    remote = FaultyFtp(new)
    with tempfile.TemporaryDirectory(prefix="kr_sp7_pull_") as d:
        _, mapping, failures = ftp.pull(remote, f"{DEST}/Show.KSC", d)
        check("clean-pull-no-failures", failures == [])
        pull_ops = remote.ops
    for dead in (True, False):
        for i in range(pull_ops):
            tag = f"pull dead={dead} op={i}"
            with tempfile.TemporaryDirectory(prefix="kr_sp7_pull_") as d:
                f = FaultyFtp(new, fail_at=i, dead=dead)
                try:
                    _, mapping, failures = ftp.pull(f, f"{DEST}/Show.KSC", d)
                except ConnectionError:
                    mapping, failures = {}, ["raised"]
                    check(f"{tag}: entry-file failure raised cleanly", True)
                check(f"{tag}: failure reported", bool(failures))
                local = snapshot(d)
                check(f"{tag}: no .part left", not [k for k in local if k.endswith(".part")])
                for lp, rp in mapping.items():
                    check(f"{tag}: mapped file complete", Path(lp).read_bytes() == new[rp])
                for k, v in local.items():
                    check(f"{tag}: local {k} complete", v == new[DEST + "/" + k.split("SSD2/Show/", 1)[-1]] if "SSD2/Show/" in k else True)

print(f"{n_ops} push ops x2 servers x2 modes, {pull_ops} pull ops x2 modes; {len(fails)} violations")
for n in fails[:12]:
    print("  FAIL " + n)
sys.exit(1 if fails else 0)
