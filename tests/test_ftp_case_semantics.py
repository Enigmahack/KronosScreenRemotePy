"""The instruments' FTP volumes are CASE-SENSITIVE (measured 2026-11-13 on a Kronos and a
Nautilus; tests/ftp_contract.py). Production code must not treat a case variant as the same file, and a case-only
rename must not silently replace the other case variant (RNTO over an existing file succeeds there).
Runs the real code against tests/ftp_fake.py. Headless: QT_QPA_PLATFORM=offscreen."""
import os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("KRONOS_DATA_DIR", tempfile.mkdtemp(prefix="kr_data_"))
import logging; logging.disable(logging.CRITICAL)
from pathlib import Path

from PySide6.QtWidgets import QApplication

from ftp_fake import FakeFtpWorker, FtpFakeServer

app = QApplication.instance() or QApplication([])
import Core.sample_ftp as sample_ftp
import Tools.file_manager as fm
import Views.sample_remote_source as srs
from Views.sample_remote_source import RemoteBrowserDialog

fails = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def pump(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


# 1. file_exists answers for the exact name, as the server does (Tools/file_manager.py:_FtpWorker.file_exists)
w = FakeFtpWorker({"/SSD1/D/a.txt": b"lower"})
check("file_exists: the exact name exists", w.file_exists("/SSD1/D/a.txt"))
check("file_exists: a case variant the server cannot open is absent",
      w.file_exists("/SSD1/D/A.TXT") == w.server.is_file("/SSD1/D/A.TXT"))

# 2. sample push into a folder whose case variant exists: a new folder is made, the push succeeds
with tempfile.TemporaryDirectory(prefix="kr_case_") as tmp:
    local = Path(tmp) / "MS001000.KSF"
    local.write_bytes(b"NEW")
    w = FakeFtpWorker(dirs=["/SSD2/show"])
    failures = []
    sample_ftp._upload_one(w, str(local), "/SSD2/Show/MS001000.KSF", None, failures)
    check("sample push next to a case-variant folder succeeds", failures == [] and
          w.files.get("/SSD2/Show/MS001000.KSF") == b"NEW")

# 3. Sample Editor browser: case-only rename with both variants present must not destroy the other one
srs.QMessageBox.warning = staticmethod(lambda *a, **k: None)
w = FakeFtpWorker({"/SSD1/x.KSC": b"lower", "/SSD1/X.KSC": b"UPPER"})
dlg = RemoteBrowserDialog(w, "file", ".KSC")
pump(lambda: dlg._tree.topLevelItemCount() == 1 and "Loading" not in dlg._tree.topLevelItem(0).text(0))
ssd = dlg._tree.topLevelItem(0)
dlg._tree.expandItem(ssd)
pump(lambda: ssd.childCount() == 2 and "Loading..." not in [ssd.child(i).text(0) for i in range(ssd.childCount())])
item = next(ssd.child(i) for i in range(ssd.childCount()) if ssd.child(i).text(0) == "x.KSC")
dlg._tree.setCurrentItem(item)
srs.prompt_text = lambda *a, **k: "X.KSC"
dlg._on_rename()
pump(lambda: not dlg._mutating, 3.0)
check("Sample Editor browser: case-only rename keeps the other case variant",
      w.files.get("/SSD1/X.KSC") == b"UPPER" and w.files.get("/SSD1/x.KSC") == b"lower")
dlg.reject()
dlg.wait_idle(5.0)
dlg.deleteLater()

# 4. File Manager remote rename: same rule
server = FtpFakeServer({"/SSD1/x.KSC": b"lower", "/SSD1/X.KSC": b"UPPER"})
fm._FtpWorker = FakeFtpWorker.bound_to(server)
win = fm.FileManagerWindow("127.0.0.1", 21, "u", "p")
pump(lambda: win._ftp is not None, 2.0)
win._bg_remote_rename("/SSD1/x.KSC", "/SSD1/X.KSC", "X.KSC")
check("File Manager: case-only rename keeps the other case variant",
      server.files.get("/SSD1/X.KSC") == b"UPPER" and server.files.get("/SSD1/x.KSC") == b"lower")
# a case-only rename when no other variant exists still works
server.files.clear()
server.files["/SSD1/x.KSC"] = b"lower"
win._bg_remote_rename("/SSD1/x.KSC", "/SSD1/X.KSC", "X.KSC")
check("File Manager: plain case-only rename still works", server.files.get("/SSD1/X.KSC") == b"lower"
      and "/SSD1/x.KSC" not in server.files)

win.close()

print(f"\n{len(fails)} failure(s)")
sys.exit(1 if fails else 0)
