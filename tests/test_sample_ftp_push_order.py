"""Core.sample_ftp push order + crafted-name guards.

Push is child-before-parent (.KSF, then .KMP, then .KSC / _UserBank.KSC) and a parent whose sample or multisample
failed is skipped and reported, never uploaded pointing at a missing/old child. A .KSC entry or .KMP zone filename
that names another folder ("..", "/", "\\", "C:", rooted) refuses the whole push before anything is sent, and is
not followed on pull either (locally or on the instrument)."""
import os
import re
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


def MemoryFtp(files=None, fail_final=()):
    """The shared instrument-faithful fake (tests/ftp_fake.py) under the real _FtpWorker; `fail_final` drops the
    connection on the STOR of any staged file whose final name is in the set."""
    fail_final = set(fail_final)

    def fault(cmd, path):
        final = re.sub(r"\.[0-9a-f]{8}\.part$", "", path)
        if cmd == "STOR" and final in fail_final:
            return ConnectionError("simulated connection drop")
        return None
    return FakeFtpWorker(files, volumes=["SSD2"], fault=fault)


def promoted(f):
    """Final paths in the order they went live (successful RNTOs)."""
    return [e.args[0] for e in f.server.calls("RNTO") if e.error is None]


def requested(f):
    return [e.args[0] for e in f.server.calls("RETR")]


def build(root: Path, zone_names=("MS001000.KSF", "MS001001.KSF"), bare=("Loose.KSF",), entries_extra=()):
    """Show.KSC -> Show/Show.KMP (zones) + Show/<bare>, all real samples."""
    content = root / "Show"
    (content / "Show").mkdir(parents=True, exist_ok=True)
    ksc = root / "Show.KSC"
    KscCollection(entries=["Show.KMP", *bare, *entries_extra]).save(str(ksc))
    k = KmpMultisample(name="Show", mno1=1)
    k.zones += [KmpZone(filename=fn, original_key=60 + i, top_key=60 + i) for i, fn in enumerate(zone_names)]
    k.save(str(content / "Show.KMP"))
    for fn in zone_names:
        if "/" in fn or "\\" in fn or ".." in fn or ":" in fn:
            continue
        s = KsfSample(name=fn[:8], sample_rate=44100)
        s.set_samples(np.array([1, 2, 3, 4], dtype=np.int16))
        s.save(str(content / "Show" / fn))
    for fn in bare:
        s = KsfSample(name=fn[:8], sample_rate=44100)
        s.set_samples(np.array([5, 6, 7], dtype=np.int16))
        s.save(str(content / fn))
    col = KscCollection.open(ksc.read_bytes())
    col.path = str(ksc)
    return ksc, col


DEST = "/SSD2/Show"
KSC_R = f"{DEST}/Show.KSC"
KMP_R = f"{DEST}/Show/Show.KMP"
Z0_R = f"{DEST}/Show/Show/MS001000.KSF"
Z1_R = f"{DEST}/Show/Show/MS001001.KSF"
BARE_R = f"{DEST}/Show/Loose.KSF"


def rank(path):
    return 0 if path.endswith(".KSF") else 1 if path.endswith(".KMP") else 2


with tempfile.TemporaryDirectory(prefix="kr_push_order_") as tmp:
    ksc, col = build(Path(tmp))
    (Path(tmp) / "Show_UserBank.KSC").write_bytes(ksc.read_bytes())
    UB_R = f"{DEST}/Show_UserBank.KSC"

    # 1. clean push: everything lands, leaves strictly before parents
    f = MemoryFtp()
    failures = ftp.push_closure(f, str(ksc), col, DEST)
    check("clean push: no failures", failures == [])
    check("clean push: every file landed", all(p in f.files for p in (KSC_R, UB_R, KMP_R, Z0_R, Z1_R, BARE_R)))
    ranks = [rank(p) for p in promoted(f)]
    check("clean push: .KSF before .KMP before .KSC", ranks == sorted(ranks))
    check("clean push: .KSC is last", promoted(f)[-1] in (KSC_R, UB_R) and promoted(f)[-2] in (KSC_R, UB_R))
    check("clean push: nothing staged left", not [p for p in f.files if p.endswith((".part", ".old"))])

    # 2. a zone sample fails: its KMP and the KSC (+UserBank) are skipped, other leaves still go
    old = {KSC_R: b"OLD KSC", KMP_R: b"OLD KMP", Z0_R: b"OLD Z0", Z1_R: b"OLD Z1"}
    f = MemoryFtp(old, fail_final={Z1_R})
    failures = ftp.push_closure(f, str(ksc), col, DEST)
    check("zone fails: failure names the sample", any("MS001001.KSF" in m and "drop" in m for m in failures))
    check("zone fails: KMP skipped and says why",
          any(m.startswith("Show.KMP: skipped") and "MS001001.KSF" in m for m in failures))
    check("zone fails: KSC skipped and says why", any(m.startswith("Show.KSC: skipped") for m in failures))
    check("zone fails: UserBank skipped too", any(m.startswith("Show_UserBank.KSC: skipped") for m in failures))
    check("zone fails: old KMP untouched", f.files[KMP_R] == b"OLD KMP")
    check("zone fails: old KSC untouched", f.files[KSC_R] == b"OLD KSC")
    check("zone fails: old failed sample untouched", f.files[Z1_R] == b"OLD Z1")
    check("zone fails: no UserBank created", UB_R not in f.files)
    check("zone fails: the good sample still went", f.files[Z0_R] != b"OLD Z0")
    check("zone fails: no parent promoted", not [p for p in promoted(f) if not p.endswith(".KSF")])

    # 3. a bare sample fails: the KMP (not its parent) still goes, the KSC doesn't
    f = MemoryFtp(old, fail_final={BARE_R})
    failures = ftp.push_closure(f, str(ksc), col, DEST)
    check("bare fails: KMP uploaded", f.files[KMP_R] != b"OLD KMP")
    check("bare fails: KSC skipped", f.files[KSC_R] == b"OLD KSC" and any("Show.KSC: skipped" in m for m in failures))

    # 4. the KMP itself fails: KSC skipped
    f = MemoryFtp(old, fail_final={KMP_R})
    failures = ftp.push_closure(f, str(ksc), col, DEST)
    check("kmp fails: KSC skipped naming the KMP",
          f.files[KSC_R] == b"OLD KSC" and any(m.startswith("Show.KSC: skipped") and "Show.KMP" in m for m in failures))

    # 5. a zone sample missing locally blocks its parents too
    z1_local = Path(tmp) / "Show" / "Show" / "MS001001.KSF"
    saved = z1_local.read_bytes()
    z1_local.unlink()
    f = MemoryFtp(old)
    failures = ftp.push_closure(f, str(ksc), col, DEST)
    check("missing locally: reported", any("MS001001.KSF: not found locally" in m for m in failures))
    check("missing locally: KMP + KSC skipped", f.files[KMP_R] == b"OLD KMP" and f.files[KSC_R] == b"OLD KSC")
    z1_local.write_bytes(saved)

    # 6. only_ksf_paths: unedited samples are assumed present and don't block; KSC still last
    f = MemoryFtp(old)
    failures = ftp.push_closure(f, str(ksc), col, DEST, only_ksf_paths={str(Path(tmp) / "Show" / "Show" / "MS001000.KSF")})
    check("filtered: no failures", failures == [])
    check("filtered: only the edited sample + parents uploaded",
          sorted(promoted(f)) == sorted([Z0_R, KMP_R, KSC_R, UB_R]))
    check("filtered: KSC after KMP after KSF", [rank(p) for p in promoted(f)] == sorted(rank(p) for p in promoted(f)))

# 7. crafted names refuse the whole push, nothing sent, nothing written outside
for label, entries, zones in [
    ("ksc entry ../", ["../../outside.KSF"], ("MS001000.KSF",)),
    ("ksc entry rooted", ["/etc/x.KSF"], ("MS001000.KSF",)),
    ("ksc entry subfolder", ["sub/x.KMP"], ("MS001000.KSF",)),
    ("ksc entry backslash", ["..\\x.KSF"], ("MS001000.KSF",)),
    ("ksc entry drive", ["C:x.KSF"], ("MS001000.KSF",)),
    ("zone ../", [], ("../../x.KSF",)),
    ("zone rooted", [], ("/x.KSF",)),
    ("zone subfolder", [], ("a/b.KSF",)),
    ("zone backslash", [], ("..\\x.KSF",)),
    ("zone dotdot", [], ("..",)),
]:
    with tempfile.TemporaryDirectory(prefix="kr_push_evil_") as tmp:
        ksc, col = build(Path(tmp), zone_names=zones, bare=(), entries_extra=entries)
        if zones and not entries:
            reread = KmpMultisample.open((Path(tmp) / "Show" / "Show.KMP").read_bytes())
            check(f"{label}: fixture kept the crafted zone name", reread.zones[0].filename == zones[0])
        if entries:
            check(f"{label}: fixture kept the crafted entry", entries[0] in col.entries)
        f = MemoryFtp({KSC_R: b"OLD"})
        try:
            failures = ftp.push_closure(f, str(ksc), col, DEST)
        except Exception as ex:   # must refuse with a message, not raise
            failures = None
            check(f"{label}: push raised {type(ex).__name__}: {ex}", False)
        if failures is not None:
            check(f"{label}: refused with a clear message",
                  any("refused" in m for m in failures) and failures[-1].startswith("Nothing was uploaded"))
            check(f"{label}: nothing sent", promoted(f) == [] and f.files == {KSC_R: b"OLD"})

# 8. pull: crafted entries / zone names are not followed (no '..' sent to the server, nothing written outside)
with tempfile.TemporaryDirectory(prefix="kr_pull_evil_") as tmp:
    seed = {}
    evil_ksc = KscCollection(entries=["Show.KMP", "../../../../../outside.KSF", "/abs.KSF", "a\\b.KSF"])
    p = Path(tmp) / "src.KSC"
    evil_ksc.save(str(p))
    seed[KSC_R] = p.read_bytes()
    k = KmpMultisample(name="Show", mno1=1)
    k.zones += [KmpZone(filename="../../x.KSF", original_key=60, top_key=60),
                KmpZone(filename="OK.KSF", original_key=61, top_key=61)]
    kp = Path(tmp) / "src.KMP"
    k.save(str(kp))
    seed[KMP_R] = kp.read_bytes()
    seed[f"{DEST}/Show/Show/OK.KSF"] = b"ok"
    remote = MemoryFtp(seed)
    dest = Path(tmp) / "pulled"
    dest.mkdir()
    _, mapping, failures = ftp.pull(remote, KSC_R, str(dest))
    check("pull: no '..' or rooted path requested from the server",
          not any(".." in r or "\\" in r or r.count("//") for r in requested(remote)))
    check("pull: crafted entries reported", sum("not pulled" in m for m in failures) >= 4)
    check("pull: the good zone still pulled", f"{DEST}/Show/Show/OK.KSF" in requested(remote))
    outside = [x for x in Path(tmp).rglob("*") if x.is_file() and dest not in x.parents and x.name not in ("src.KSC", "src.KMP")]
    check("pull: nothing written outside the destination", outside == [])

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
