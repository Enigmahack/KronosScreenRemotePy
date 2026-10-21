"""SP6: File Manager volume protection. The instrument's top-level storage volumes (SSD1/SSD2/...) must never be
renamed, moved, created over or deleted, however the path is spelled. Drives Tools.file_manager._FtpWorker's guards
with plain, case-varied, relative, double-slash and dot-segment spellings, and with the names the rename dialog can
produce, against a recording fake connection. Legitimate nested paths must still work (no over-blocking)."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("KRONOS_DATA_DIR", tempfile.mkdtemp(prefix="kr_data_"))
from Tools.file_manager import _FtpWorker, FtpTopLevelPathError, _ftp_parent

fails = []


def check(name, cond):
    if not cond:
        print("FAIL " + name)
        fails.append(name)


class Rec:
    def __init__(self, listing=None):
        self.calls, self.listing = [], listing or {}

    def rename(self, a, b): self.calls.append(("rename", a, b))
    def delete(self, p): self.calls.append(("delete", p))
    def rmd(self, p): self.calls.append(("rmd", p))
    def mkd(self, p): self.calls.append(("mkd", p))
    def mlsd(self, p):
        return iter(self.listing.get(p, []))


def worker(listing=None):
    w = _FtpWorker("h", 21, "u", "p")
    w._ftp = Rec(listing)
    w.ensure_connected = lambda: None
    return w


def refused(call):
    w = worker()
    try:
        call(w)
    except FtpTopLevelPathError:
        return not w._ftp.calls
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
w = worker()
w.rename("/SSD1/Sub/a.KSF", "/SSD1/Sub/b.KSF")
w.delete_file("/SSD1/Sub/b.KSF")
check("nested rename+delete allowed", w._ftp.calls == [("rename", "/SSD1/Sub/a.KSF", "/SSD1/Sub/b.KSF"), ("delete", "/SSD1/Sub/b.KSF")])
w = worker({"/SSD1/Sub": [("In", {"type": "dir"})], "/SSD1/Sub/In": [("f.KSF", {"type": "file", "size": "1"})]})
w.delete_dir("/SSD1/Sub")
check("nested delete_dir recurses to the leaf and removes the folder",
      w._ftp.calls == [("delete", "/SSD1/Sub/In/f.KSF"), ("rmd", "/SSD1/Sub/In"), ("rmd", "/SSD1/Sub")])
w = worker()
w.rename("/SSD1/a.b/c", "/SSD1/a.b/d")
check("dotted folder names are not mistaken for dot segments", w._ftp.calls == [("rename", "/SSD1/a.b/c", "/SSD1/a.b/d")])

print(f"{len(TOP)} volume spellings x4 operations + dialog names; {len(fails)} violations")
sys.exit(1 if fails else 0)
