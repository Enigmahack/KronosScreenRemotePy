"""_FtpWorker.file_exists against a Kronos-like server (MLSD answers 500, LIST works): existing file/folder -> True,
missing file or missing parent folder -> False, a dropped connection propagates instead of reading as 'absent'."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("KRONOS_DATA_DIR", tempfile.mkdtemp(prefix="kr_data_"))
from pipeline_fakes import KronosLikeFtp

fails = []


def check(name, cond):
    if not cond:
        print("FAIL " + name)
        fails.append(name)


files = {"/SSD2/Show/a b.KSF": b"x", "/SSD2/Show/Sub/c.KSF": b"y"}
w = KronosLikeFtp(files)
check("existing file", w.file_exists("/SSD2/Show/a b.KSF"))
check("existing file with a space in its name", w.file_exists("/SSD2/Show/a b.KSF"))
check("existing nested file", w.file_exists("/SSD2/Show/Sub/c.KSF"))
check("existing folder", w.file_exists("/SSD2/Show/Sub"))
check("top-level volume", w.file_exists("/SSD2"))
check("missing file", not w.file_exists("/SSD2/Show/nope.KSF"))
check("missing parent folder is False, not an error", not w.file_exists("/SSD2/Nope/x.KSF"))
check("a name that only prefixes an existing one is absent", not w.file_exists("/SSD2/Show/a"))

dead = KronosLikeFtp(files, fail_at=0)
try:
    dead.file_exists("/SSD2/Show/a b.KSF")
    check("connection drop propagates", False)
except ConnectionError:
    pass
print(f"{len(fails)} violations")
sys.exit(1 if fails else 0)
