"""SP7 known defect: Core.sample_ftp._upload_one deletes the existing remote file before renaming the staged
upload into place, so a drop between the two leaves the instrument with neither the old nor the new file (and the
failure path then deletes the staged copy too). Invariant: a previously-valid remote file is never missing after
a failed push. Fails until _upload_one stops deleting the old file before the new one is safely in place."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("KRONOS_DATA_DIR", tempfile.mkdtemp(prefix="kr_data_"))
import logging; logging.disable(logging.CRITICAL)
from pathlib import Path
import Core.sample_ftp as ftp
from pipeline_fakes import FaultyFtp

REMOTE = "/SSD2/Show/MS001000.KSF"
with tempfile.TemporaryDirectory(prefix="kr_sp7w_") as tmp:
    local = Path(tmp) / "MS001000.KSF"
    local.write_bytes(b"NEW" * 100)
    probe = FaultyFtp({REMOTE: b"OLD" * 100})
    ftp._upload_one(probe, str(local), REMOTE, None, [])
    lost = []
    for dead in (True, False):
        for i in range(probe.ops):
            f = FaultyFtp({REMOTE: b"OLD" * 100}, fail_at=i, dead=dead)
            failures = []
            ftp._upload_one(f, str(local), REMOTE, None, failures)
            if failures and REMOTE not in f.files:
                lost.append(f"dead={dead} op={i}")
print(f"{len(lost)} fault points lose the previously-valid remote file: {lost}")
sys.exit(1 if lost else 0)
