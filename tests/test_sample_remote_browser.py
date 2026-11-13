"""RemoteBrowserDialog (Sample Editor FTP browser) against a slow duck-typed FTP fake: listings and folder changes run
off the GUI thread, stale listings are dropped, the dialog locks while a change runs. Run from the repo root."""
import os, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_data_")
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

import Views.sample_remote_source as srs
from Views.sample_remote_source import RemoteBrowserDialog
from ftp_fake import FakeFtpWorker

fails = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


DELAY = 0.3   # every LIST takes this long, so listings are in flight while the test acts


def make_ftp():
    """The real _FtpWorker over the instrument-faithful fake (tests/ftp_fake.py), LIST slowed down."""
    return FakeFtpWorker({"/SSD1/x.KSC": b"k", "/SSD1/y.txt": b"t", "/SSD1/A/z.KSC": b"z"}, delays={"LIST": DELAY})


def pump(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


def labels(parent):
    return [parent.child(i).text(0) for i in range(parent.childCount())]


ftp = make_ftp()
t0 = time.monotonic()
dlg = RemoteBrowserDialog(ftp, "file", ".KSC")
check("constructor does not wait for the listing", time.monotonic() - t0 < DELAY)
root = dlg._tree.invisibleRootItem()
check("'Loading...' row while listing", labels(root) == ["Loading..."])
check("root listed", pump(lambda: dlg._tree.topLevelItemCount() == 1 and "Loading" not in labels(root)[0]))
ssd = dlg._tree.topLevelItem(0)
dlg._tree.expandItem(ssd)
check("expand lists children", pump(lambda: ssd.childCount() == 2 and "Loading..." not in labels(ssd)))
check("only dirs and .KSC files shown", any("A" in l for l in labels(ssd)) and "x.KSC" in labels(ssd))
check("every FTP call ran off the GUI thread", ftp.log and all(e.worker_thread for e in ftp.log))

# stale: expand A, then refresh before its listing arrives -> the old listing must not land anywhere
a_item = next(ssd.child(i) for i in range(ssd.childCount()) if "A" in ssd.child(i).text(0))
dlg._tree.expandItem(a_item)
dlg._on_refresh()
pump(lambda: dlg._tree.topLevelItemCount() == 1 and "Loading" not in dlg._tree.topLevelItem(0).text(0))
time.sleep(DELAY * 2.5)
app.processEvents()
check("stale listing dropped after a refresh", dlg._tree.topLevelItemCount() == 1
      and dlg._tree.topLevelItem(0).childCount() == 0)

# top-level volume guard kept
ssd = dlg._tree.topLevelItem(0)
dlg._tree.setCurrentItem(ssd)
check("top-level volume cannot be renamed/deleted", not dlg._rename_btn.isEnabled() and not dlg._delete_btn.isEnabled())

# a change locks the dialog until it completes, then refreshes
srs.prompt_text = lambda *a, **k: "NEW"
dlg._on_new_folder()
check("tree/buttons disabled while mkdir runs", not dlg._tree.isEnabled() and not dlg._new_btn.isEnabled()
      and not dlg._ok.isEnabled())
check("mkdir finished and refreshed", pump(lambda: dlg._tree.isEnabled() and ftp.log[-1].cmd == "LIST"
                                           and ftp.log[-1].args == ("/",) and ftp.log[-1].t1 is not None))
check("mkdir hit the selected folder", ("MKD", "/SSD1/NEW") in ftp.server.mutations() and "/SSD1/NEW" in ftp.dirs)
check("override cursor released", QApplication.overrideCursor() is None)

# a failed change is reported, not raised, and unlocks the dialog
warned = []
srs.QMessageBox.warning = staticmethod(lambda *a, **k: warned.append(a[2]))
ssd = dlg._tree.topLevelItem(0) if pump(lambda: dlg._tree.topLevelItemCount() == 1 and "Loading" not in dlg._tree.topLevelItem(0).text(0)) else None
dlg._tree.expandItem(ssd)
pump(lambda: ssd.childCount() >= 2 and "Loading..." not in labels(ssd))
x = next(ssd.child(i) for i in range(ssd.childCount()) if ssd.child(i).text(0) == "x.KSC")
dlg._tree.setCurrentItem(x)
# the instrument refuses: x.KSC was deleted by someone else after it was listed -> 550 RNFR command failed.
del ftp.files["/SSD1/x.KSC"]
srs.prompt_text = lambda *a, **k: "renamed.KSC"
dlg._on_rename()
check("rename failure reported and dialog unlocked", pump(lambda: bool(warned) and dlg._tree.isEnabled())
      and "Rename failed" in warned[0] and "550 RNFR command failed." in warned[0])
check("nothing appeared under the new name", "/SSD1/renamed.KSC" not in ftp.files)

# renaming onto an existing name is refused before RNTO (the instrument would silently replace the file)
ftp.files["/SSD1/x.KSC"] = b"k"
ftp.files["/SSD1/w.KSC"] = b"KEEP"
warned.clear()
dlg._on_refresh()
ssd = dlg._tree.topLevelItem(0) if pump(lambda: dlg._tree.topLevelItemCount() == 1 and "Loading" not in dlg._tree.topLevelItem(0).text(0)) else None
dlg._tree.expandItem(ssd)
pump(lambda: ssd.childCount() >= 3 and "Loading..." not in labels(ssd))
x = next(ssd.child(i) for i in range(ssd.childCount()) if ssd.child(i).text(0) == "x.KSC")
dlg._tree.setCurrentItem(x)
srs.prompt_text = lambda *a, **k: "w.KSC"
dlg._on_rename()
check("rename onto an existing file refused", pump(lambda: bool(warned) and dlg._tree.isEnabled())
      and "already exists" in warned[0] and ftp.files.get("/SSD1/w.KSC") == b"KEEP" and "/SSD1/x.KSC" in ftp.files)

# closing with work in flight: the late result is ignored and wait_idle joins the thread
dlg._on_refresh()
dlg.reject()
check("wait_idle joins outstanding work", dlg.wait_idle(5.0))
app.processEvents()
check("late result after close ignored (no crash)", True)
dlg.deleteLater()

print(f"{len(fails)} failure(s)")
sys.exit(1 if fails else 0)
