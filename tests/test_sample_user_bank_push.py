"""_UserBank.KSC handling in Core.sample_ftp.

On the instruments a _UserBank.KSC is a read-only, streaming reference repository (links the audio engine loads into
RAM). So: pull never fetches it; push uploads the local sibling, and regenerates it from the collection first when it
is older than the .KSC being pushed (the .KSC changed without it being rewritten). A collection with no local sibling
(an unedited pulled one) is pushed without one, never failing."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np

import Core.sample_ftp as ftp
from Data.ksc_collection import KscCollection
from Data.kmp_multisample import KmpMultisample, KmpZone
from Data.ksf_sample import KsfSample
from ftp_fake import FakeFtpWorker

fails = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def build(root: Path):
    content = root / "Show"
    (content / "Show").mkdir(parents=True, exist_ok=True)
    ksc = root / "Show.KSC"
    KscCollection(entries=["Show.KMP"]).save(str(ksc))
    k = KmpMultisample(name="Show", mno1=1)
    k.zones.append(KmpZone(filename="MS001000.KSF", original_key=60, top_key=60))
    k.save(str(content / "Show.KMP"))
    s = KsfSample(name="MS001000", sample_rate=44100)
    s.set_samples(np.array([1, 2, 3, 4], dtype=np.int16))
    s.save(str(content / "Show" / "MS001000.KSF"))
    col = KscCollection.open(ksc.read_bytes())
    col.path = str(ksc)
    return ksc, col


DEST = "/SSD2/Show"
UB_R = f"{DEST}/Show_UserBank.KSC"

with tempfile.TemporaryDirectory(prefix="kr_userbank_") as tmp:
    ksc, col = build(Path(tmp))
    ub = Path(tmp) / "Show_UserBank.KSC"
    expected = col.to_user_bank_bytes()

    # 1. stale sibling (older than the .KSC): regenerated, and the regenerated one is what goes out
    ub.write_bytes(b"STALE")
    os.utime(ub, (1_000_000_000, 1_000_000_000))
    f = FakeFtpWorker(volumes=["SSD2"])
    failures = ftp.push_closure(f, str(ksc), col, DEST)
    check("stale: push succeeds", failures == [])
    check("stale: the regenerated _UserBank.KSC was uploaded", f.files.get(UB_R) == expected)
    check("stale: the local sibling was rewritten", ub.read_bytes() == expected)

    # 2. fresh sibling (not older than the .KSC): uploaded as it is
    ub.write_bytes(b"FRESH LOCAL")
    now = os.path.getmtime(ksc) + 10
    os.utime(ub, (now, now))
    f = FakeFtpWorker(volumes=["SSD2"])
    failures = ftp.push_closure(f, str(ksc), col, DEST)
    check("fresh: uploaded unchanged", failures == [] and f.files.get(UB_R) == b"FRESH LOCAL")

    # 3. no local sibling: pushed without one, not a failure, the instrument's own copy is left alone
    ub.unlink()
    f = FakeFtpWorker({UB_R: b"INSTRUMENT COPY"}, volumes=["SSD2"])
    failures = ftp.push_closure(f, str(ksc), col, DEST)
    check("missing: no failure", failures == [])
    check("missing: the instrument's _UserBank.KSC is untouched", f.files[UB_R] == b"INSTRUMENT COPY")
    check("missing: the .KSC itself still went", f.files.get(f"{DEST}/Show.KSC") is not None)

    # 4. pull never fetches the _UserBank.KSC
    src = FakeFtpWorker({f"{DEST}/Show.KSC": ksc.read_bytes(),
                         f"{DEST}/Show_UserBank.KSC": b"INSTRUMENT COPY",
                         f"{DEST}/Show/Show.KMP": (Path(tmp) / "Show" / "Show.KMP").read_bytes(),
                         f"{DEST}/Show/Show/MS001000.KSF": (Path(tmp) / "Show" / "Show" / "MS001000.KSF").read_bytes()},
                        volumes=["SSD2"])
    out = Path(tmp) / "pulled"
    out.mkdir()
    local_path, remote_map, pull_failures = ftp.pull(src, f"{DEST}/Show.KSC", str(out))
    check("pull: succeeds", pull_failures == [] and os.path.isfile(local_path))
    check("pull: no _UserBank.KSC downloaded", not list(out.rglob("*_UserBank.KSC")))
    check("pull: never requested from the instrument",
          not [e for e in src.server.calls("RETR") if e.args[0].endswith("_UserBank.KSC")])

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
