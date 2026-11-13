"""The instruments' FTP server hides dotfiles from a plain LIST (measured 2026-11-13 on a Kronos and a Nautilus;
tests/ftp_contract.py), but they are there: RNTO over one replaces it, and RMD refuses a folder that holds one.
_FtpWorker must not report a present dotfile as absent, and delete_dir must remove a folder that holds one.
Runs the real _FtpWorker against tests/ftp_fake.py."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("KRONOS_DATA_DIR", tempfile.mkdtemp(prefix="kr_data_"))
from ftp_fake import FakeFtpWorker

fails = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


w = FakeFtpWorker({"/SSD1/D/.hidden": b"h"})
check("file_exists: a dotfile that is there (LIST hides it) is not reported absent", w.file_exists("/SSD1/D/.hidden"))

w = FakeFtpWorker({"/SSD1/F/a.KSF": b"a", "/SSD1/F/.DS_Store": b"d"})
try:
    w.delete_dir("/SSD1/F")
    err = None
except Exception as ex:
    err = ex
check("delete_dir removes a folder that holds a dotfile", err is None and "/SSD1/F" not in w.dirs)
check("... and does not stop half way (visible files deleted, folder kept)",
      not ("/SSD1/F/a.KSF" not in w.files and "/SSD1/F" in w.dirs))

print(f"\n{len(fails)} failure(s)")
sys.exit(1 if fails else 0)
