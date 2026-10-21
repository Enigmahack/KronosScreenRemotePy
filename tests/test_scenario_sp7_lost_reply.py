"""SP7: the new file lands but the server's reply to the final rename is lost. Core.sample_ftp._promote must not
rename the old version back over the file that landed: both versions are kept and the failure names which path holds
which. If the final path is verifiably empty the old version is put back as before."""
import os, sys, tempfile, ftplib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("KRONOS_DATA_DIR", tempfile.mkdtemp(prefix="kr_data_"))
import logging; logging.disable(logging.CRITICAL)
from pathlib import Path
import Core.sample_ftp as ftp
from pipeline_fakes import SERVERS

REMOTE = "/SSD2/Show/MS001000.KSF"
OLD, NEW = b"OLD" * 100, b"NEW" * 100
bad = []


def lossy(base, lose_reply):
    class Lossy(base):
        """First rename onto an existing file is refused (forces the swap); the final rename either lands and
        loses its reply, or fails without landing."""
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.n_rename_to_remote = 0

        def rename(self, old, new):
            if new == REMOTE:
                self.n_rename_to_remote += 1
                if self.n_rename_to_remote == 1:
                    raise ftplib.error_perm("550 target exists")
                if self.n_rename_to_remote == 2:
                    if lose_reply:
                        super().rename(old, new)
                    raise ConnectionError("reply lost")
            return super().rename(old, new)
    return Lossy


with tempfile.TemporaryDirectory(prefix="kr_lost_") as tmp:
    local = Path(tmp) / "MS001000.KSF"
    local.write_bytes(NEW)
    for server, base in SERVERS.items():
        for lose_reply in (True, False):
            tag = f"{server} landed={lose_reply}"
            f = lossy(base, lose_reply)({REMOTE: OLD}, overwrite=True)
            failures = []
            ftp._upload_one(f, str(local), REMOTE, None, failures)
            asides = {p: b for p, b in f.files.items() if p.endswith(".old")}
            if not failures:
                bad.append(f"{tag}: lost reply reported as success")
            if lose_reply:
                if f.files.get(REMOTE) != NEW:
                    bad.append(f"{tag}: the new file that landed was overwritten")
                if list(asides.values()) != [OLD]:
                    bad.append(f"{tag}: previous version not kept as an .old file")
                if not any(Path(a).name in m and REMOTE in m for a in asides for m in failures):
                    bad.append(f"{tag}: failure does not say which path holds which version: {failures}")
            else:
                if f.files.get(REMOTE) != OLD or asides:
                    bad.append(f"{tag}: old version not put back when the final path was empty")
print(f"{len(bad)} violations" + ("; " + "; ".join(bad[:3]) if bad else ""))
sys.exit(1 if bad else 0)
