"""SP6: File Manager volume protection. The instrument's top-level storage volumes (SSD1/SSD2/...) must never be
renamed, moved, created over or deleted, however the path is spelled. Drives Tools.file_manager._FtpWorker's guards
with plain, case-varied, relative, double-slash and dot-segment spellings, and with the names the rename dialog can
produce, against a recording fake connection. Legitimate nested paths must still work (no over-blocking)."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("KRONOS_DATA_DIR", tempfile.mkdtemp(prefix="kr_data_"))
from Tools.file_manager import FtpTopLevelPathError, _ftp_parent
from ftp_fake import FakeFtpWorker

fails = []


def check(name, cond):
    if not cond:
        print("FAIL " + name)
        fails.append(name)


def worker(files=None, dirs=()):
    """The real _FtpWorker over the instrument-faithful fake (tests/ftp_fake.py)."""
    return FakeFtpWorker(files, dirs=dirs, volumes=["SSD1", "SSD2", "SSD3"])


def refused(call):
    w = worker({"/SSD1/Sub/x": b"x"})
    try:
        call(w)
    except FtpTopLevelPathError:
        return not w.server.log          # refused before anything reached the instrument
    except Exception:
        return False
    return False


TOP = ["/SSD1", "/SSD1/", "SSD1", "/ssd2", "/", "", "//SSD1", "/./SSD1", "/SSD1/.", "/SSD1/..", "/SSD1/Sub/..",
       "/Sub/../SSD1", "/SSD1/../SSD2", "/SSD1/Sub/../../SSD3"]
for p in TOP:
    check(f"delete_file refuses {p!r}", refused(lambda w: w.delete_file(p)))
    check(f"delete_dir refuses {p!r}", refused(lambda w: w.delete_dir(p)))
    check(f"rename-from refuses {p!r}", refused(lambda w: w.rename(p, "/SSD1/Sub/x")))
    check(f"rename-to refuses {p!r}", refused(lambda w: w.rename("/SSD1/Sub/x", p)))

# what the rename dialog builds: f"{parent}/{typed name}"
for typed in ("..", ".", "../X", "../../X"):
    new_path = f"{_ftp_parent('/SSD1/Folder').rstrip('/')}/{typed}"
    check(f"rename dialog name {typed!r} ({new_path}) refused", refused(lambda w: w.rename("/SSD1/Folder", new_path)))

# legitimate nested paths still work
w = worker({"/SSD1/Sub/a.KSF": b"a"})
w.rename("/SSD1/Sub/a.KSF", "/SSD1/Sub/b.KSF")
w.delete_file("/SSD1/Sub/b.KSF")
check("nested rename+delete allowed", w.server.mutations() == [("RNFR", "/SSD1/Sub/a.KSF"), ("RNTO", "/SSD1/Sub/b.KSF"),
                                                               ("DELE", "/SSD1/Sub/b.KSF")] and not w.files)
w = worker({"/SSD1/Sub/In/f.KSF": b"f"})
w.delete_dir("/SSD1/Sub")
check("nested delete_dir recurses to the leaf and removes the folder",
      w.server.mutations() == [("DELE", "/SSD1/Sub/In/f.KSF"), ("RMD", "/SSD1/Sub/In"), ("RMD", "/SSD1/Sub")]
      and "/SSD1/Sub" not in w.dirs and "/SSD1" in w.dirs)
w = worker({"/SSD1/a.b/c": b"c"})
w.rename("/SSD1/a.b/c", "/SSD1/a.b/d")
check("dotted folder names are not mistaken for dot segments",
      w.server.mutations() == [("RNFR", "/SSD1/a.b/c"), ("RNTO", "/SSD1/a.b/d")] and "/SSD1/a.b/d" in w.files)

print(f"{len(TOP)} volume spellings x4 operations + dialog names; {len(fails)} violations")
sys.exit(1 if fails else 0)
