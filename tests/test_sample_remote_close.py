"""Closing the Sample Editor's FTP browser while a long folder Delete runs must not block the GUI thread: the caller
returns at once, the delete finishes on its own thread, and the FTP disconnect (which waits for the same lock, like
_FtpWorker's) runs off the GUI thread afterwards; the dialog is deleted later on the GUI thread. Slow duck-typed FTP
fake. Run from the repo root."""
import os, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_data_")
import shiboken6
from PySide6.QtCore import QEvent, QTimer
from PySide6.QtWidgets import QApplication, QWidget

app = QApplication.instance() or QApplication([])

import Views.sample_remote_source as srs
from Views.sample_remote_source import KronosRemoteSampleSource, RemoteBrowserDialog
from ftp_fake import FakeFtpWorker

fails = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


DELETE_S = 1.5


class SlowFtp(FakeFtpWorker):
    """The real _FtpWorker (one RLock around every call) over the instrument-faithful fake; the final RMD of a folder
    delete takes DELETE_S seconds. Records each disconnect (on a worker thread?, time)."""

    def __init__(self):
        super().__init__(dirs=["/SSD1/BIG"], delays={"LIST": 0.05, "RMD": DELETE_S})
        self.disconnects = []

    def disconnect(self):
        with self._lock:
            self.disconnects.append((threading.current_thread() is not threading.main_thread(), time.monotonic()))
            super().disconnect()


def rmd_events(ftp):
    return ftp.server.calls("RMD")


def pump(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


ftp = SlowFtp()
owner = QWidget()
srs.confirm = lambda *a, **k: True
src = KronosRemoteSampleSource(owner, "h", 21, "u", "p")
src._connect = lambda: ftp
seen = {}


def drive():
    dlg = next((d for d in QApplication.topLevelWidgets() if isinstance(d, RemoteBrowserDialog) and d.isVisible()), None)
    if dlg is None:
        QTimer.singleShot(20, drive)
        return
    seen["dlg"] = dlg
    pump(lambda: dlg._tree.topLevelItemCount() == 1 and "Loading" not in dlg._tree.topLevelItem(0).text(0))
    ssd = dlg._tree.topLevelItem(0)
    dlg._tree.expandItem(ssd)
    pump(lambda: ssd.childCount() == 1 and "Loading" not in ssd.child(0).text(0))
    dlg._tree.setCurrentItem(ssd.child(0))
    dlg._on_delete()                                # long recursive delete starts on the worker
    pump(lambda: bool(rmd_events(ftp)), 2.0)
    seen["closed_at"] = time.monotonic()
    dlg.reject()                                    # the user gives up and closes the browser


ticks = []
ticker = QTimer()
ticker.setInterval(20)
ticker.timeout.connect(lambda: ticks.append(time.monotonic()))
ticker.start()
QTimer.singleShot(0, drive)
result = src.pick_and_pull(".KSC", tempfile.mkdtemp())
returned_at = time.monotonic()
dlg = seen.get("dlg")
check("the delete was running when the browser closed", bool(rmd_events(ftp))
      and rmd_events(ftp)[0].t1 is None)
check("pick_and_pull returned without waiting for the delete", returned_at - seen.get("closed_at", 0) < 0.5)
check("cancel status mentions the background change", "cancelled" in result.status_message
      and "background" in result.status_message)

# The GUI keeps running while the delete finishes: the ticker keeps firing.
mark = len(ticks)
done = pump(lambda: dlg.release_done.is_set(), DELETE_S * 4)
check("release completes in the background", done)
gaps = [b - a for a, b in zip(ticks[mark:], ticks[mark + 1:])]
check("GUI thread never blocked while the delete finished", len(ticks) - mark > 10 and (max(gaps) if gaps else 1) < 0.4)
check("the folder is gone from the instrument", "/SSD1/BIG" not in ftp.dirs)
check("disconnect ran after the delete finished", ftp.disconnects and rmd_events(ftp)[0].t1 is not None
      and ftp.disconnects[0][1] >= rmd_events(ftp)[0].t1 and ftp.server.calls("QUIT"))
check("disconnect ran off the GUI thread", ftp.disconnects and all(t for t, _ in ftp.disconnects))
pump(lambda: not shiboken6.isValid(dlg) or dlg not in srs._PENDING_RELEASE, 2.0)
app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
app.processEvents()
check("dialog deleted on the GUI thread afterwards", not shiboken6.isValid(dlg))
check("nothing left pending", not srs._PENDING_RELEASE)

# Plain cancel with nothing running: still no blocking, still disconnects.
ftp2 = SlowFtp()
src._connect = lambda: ftp2


def cancel_now():
    d = next((d for d in QApplication.topLevelWidgets() if isinstance(d, RemoteBrowserDialog) and d.isVisible()), None)
    if d is None:
        QTimer.singleShot(20, cancel_now)
        return
    seen["dlg2"] = d
    d.reject()


QTimer.singleShot(0, cancel_now)
r2 = src.pick_and_pull(".KSC", tempfile.mkdtemp())
check("plain cancel status", r2.status_message == "Load from Instrument cancelled.")
check("plain cancel disconnects", pump(lambda: bool(ftp2.disconnects) and ftp2._ftp is None, 3.0))
ticker.stop()

print(f"{len(fails)} failure(s)")
sys.exit(1 if fails else 0)
