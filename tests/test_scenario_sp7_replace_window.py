"""SP7: replacing an existing remote file must never destroy the previous version before the new one is in place.
Core.sample_ftp._upload_one renames the staged upload over the old file; a server that refuses to overwrite gets a
swap through a `.old` sibling. Rule, swept over a fault at every transport op, for servers that allow and refuse
overwrite, with a dead link and a one-off error: after a push that reports a failure, the old bytes are still on
the unit, either at the final path or at an aside file, and when they are only at the aside the failure message
names that file; the final path never holds anything but old or new bytes; a push that reports no failure leaves
the new bytes at the final path. After a one-off error nothing is left behind except what the rule allows (no .part,
no .old)."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("KRONOS_DATA_DIR", tempfile.mkdtemp(prefix="kr_data_"))
import logging; logging.disable(logging.CRITICAL)
from pathlib import Path
import Core.sample_ftp as ftp
from pipeline_fakes import FaultyFtp

REMOTE = "/SSD2/Show/MS001000.KSF"
OLD, NEW = b"OLD" * 100, b"NEW" * 100
bad = []
n = 0
with tempfile.TemporaryDirectory(prefix="kr_sp7w_") as tmp:
    local = Path(tmp) / "MS001000.KSF"
    local.write_bytes(NEW)
    for overwrite in (True, False):
        probe = FaultyFtp({REMOTE: OLD}, overwrite=overwrite)
        ftp._upload_one(probe, str(local), REMOTE, None, [])
        if probe.files.get(REMOTE) != NEW or len(probe.files) != 1:
            bad.append(f"overwrite={overwrite}: clean replace left {sorted(probe.files)}")
        for dead in (True, False):
            for i in range(probe.ops):
                n += 1
                tag = f"overwrite={overwrite} dead={dead} op={i}"
                f = FaultyFtp({REMOTE: OLD}, fail_at=i, dead=dead, overwrite=overwrite)
                failures = []
                ftp._upload_one(f, str(local), REMOTE, None, failures)
                final = f.files.get(REMOTE)
                asides = [p for p, b in f.files.items() if p != REMOTE and b == OLD]
                if final not in (None, OLD, NEW):
                    bad.append(f"{tag}: final path holds neither old nor new bytes")
                if not failures:
                    if final != NEW:
                        bad.append(f"{tag}: no failure reported but new bytes are not in place")
                    continue
                if final != OLD and not asides:
                    bad.append(f"{tag}: previous version lost")
                if final != OLD and asides and not any(Path(a).name in m for a in asides for m in failures):
                    bad.append(f"{tag}: previous version only at {asides} but the failure does not name it")
                if not dead and [p for p in f.files if p.endswith((".part", ".old"))]:
                    bad.append(f"{tag}: left {[p for p in f.files if p.endswith(('.part', '.old'))]}")
print(f"{n} fault points; {len(bad)} violations" + ("; e.g. " + "; ".join(bad[:3]) if bad else ""))
sys.exit(1 if bad else 0)
