"""File Manager cross-pane FOLDER transfer: dragging / Send-to / paste of a folder uploads or downloads the whole
tree (merging into an existing same-name folder, prompting per colliding file), and a cut removes the emptied
source folders. Headless against the instrument-faithful FTP fake."""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("KRONOS_DATA_DIR", tempfile.mkdtemp(prefix="kr_data_"))

from PySide6.QtWidgets import QApplication

import Tools.file_manager as fm
from ftp_fake import FakeFtpWorker, FtpFakeServer

fails = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


server = FtpFakeServer(files={"/SSD1/Songs/a.txt": b"A", "/SSD1/Songs/sub/b.txt": b"BB",
                              "/SSD1/Songs/sub/.hid": b"H"}, volumes=["SSD1", "SSD2"])
fm._FtpWorker = FakeFtpWorker.bound_to(server)
app = QApplication.instance() or QApplication([])
win = fm.FileManagerWindow("127.0.0.1", 21, "u", "p")
win._status_signal.connect(lambda m: None)
win._resolve_conflict = lambda name, sug: _Req("overwrite")


class _Req:
    def __init__(self, action):
        self.action, self.new_name, self.apply_all = action, None, True


tmp = Path(tempfile.mkdtemp(prefix="fm_folder_"))

# download: instrument folder -> PC, nested + hidden file
remote = fm.FileEntry("Songs", "/SSD1/Songs", True, 0, None)
done, cancelled = win._do_download_batch([remote], str(tmp))
check("download counts files", done == 3 and not cancelled)
check("download nested file", (tmp / "Songs" / "sub" / "b.txt").read_bytes() == b"BB")
check("download hidden file", (tmp / "Songs" / "sub" / ".hid").read_bytes() == b"H")

# upload: PC folder -> instrument (new name), merging into existing folder with a collision
up = tmp / "Up"
(up / "deep").mkdir(parents=True)
(up / "x.bin").write_bytes(b"X")
(up / "deep" / "y.bin").write_bytes(b"Y")
done, cancelled = win._do_upload_batch([fm.FileEntry("Up", str(up), True, 0, None)], "/SSD2")
check("upload counts files", done == 2 and not cancelled)
check("upload created tree", server.files.get("/SSD2/Up/deep/y.bin") == b"Y" and "/SSD2/Up/deep" in server.dirs)
(up / "x.bin").write_bytes(b"X2")
done, _ = win._do_upload_batch([fm.FileEntry("Up", str(up), True, 0, None)], "/SSD2")
check("upload merges + overwrites on collision", server.files["/SSD2/Up/x.bin"] == b"X2" and done == 2)

# cut-upload removes emptied source folders
done, _ = win._do_upload_batch([fm.FileEntry("Up", str(up), True, 0, None)], "/SSD1", True)
check("cut upload removes source folder", not up.exists() and server.files["/SSD1/Up/deep/y.bin"] == b"Y")

# cut-download removes emptied remote folders
done, _ = win._do_download_batch([fm.FileEntry("Up", "/SSD1/Up", True, 0, None)], str(tmp), True)
check("cut download removes remote folder", "/SSD1/Up" not in server.dirs and (tmp / "Up" / "deep" / "y.bin").exists())

# a file in the way of a folder is skipped, not clobbered
(tmp / "Clash").write_bytes(b"f")
done, _ = win._do_download_batch([fm.FileEntry("Songs", "/SSD1/Songs", True, 0, None)], str(tmp / "Clash"))
check("file blocking folder is not replaced", (tmp / "Clash").is_file())

# the cross-pane drop handler no longer refuses folders
win._run_bg = lambda fn, *a: fn(*a)
win._bg_refresh_remote = lambda: None
win._handle_cross_pane_drop(True, False, [remote], None)
check("drop handler accepts a folder", "not yet supported" not in win._status_label.text())

# progress: x / total counted up front, one update per file, final == total
counts = []
win._count_signal.connect(lambda d, t, e: counts.append((d, t)))
src = tmp / "Many"
src.mkdir()
for i in range(12):
    (src / f"f{i}.bin").write_bytes(b"z")
win._do_upload_batch([fm.FileEntry("Many", str(src), True, 0, None)], "/SSD2")
app.processEvents()
check("count total known up front", counts and all(t == 12 for _, t in counts))
check("count advances to total", counts[-1] == (12, 12) and [d for d, _ in counts] == sorted(d for d, _ in counts))
win._on_count(5, 12, 30.0)
check("bar shows x / total + eta", win._progress_bar.format().startswith("5 / 12") and "left" in win._progress_bar.format()
      and win._progress_bar.maximum() == 12)

# properties: totals for a remote folder and a local folder
import threading
res = {}
remote_worker = win._ftp
t = fm.gather_properties([remote], lambda p: remote_worker.list_dir(p, include_hidden=True), threading.Event(), lambda t: None)
check("remote properties totals", t["files"] == 3 and t["folders"] == 1 and t["bytes"] == 4 and t["done"])
t = fm.gather_properties([fm.FileEntry("Many", str(src), True, 0, None)], fm._local_children, threading.Event(), lambda t: None)
check("local properties totals", t["files"] == 12 and t["bytes"] == 12 and t["newest"] is not None)
ev = threading.Event(); ev.set()
t = fm.gather_properties([remote], lambda p: remote_worker.list_dir(p), ev, lambda t: None)
check("cancelled walk is not marked done", not t["done"])
dlg = fm._PropertiesDialog([remote], True, lambda p: remote_worker.list_dir(p, include_hidden=True), win)
import time
end = time.monotonic() + 5
while time.monotonic() < end and "3 files" not in dlg._rows["Contains"].text():
    app.processEvents(); time.sleep(0.02)
check("dialog fills in contents", "3 files, 1 folders" in dlg._rows["Contains"].text() and "4 bytes" in dlg._rows["Size"].text())
dlg.reject()

# drag & drop gestures (real startDrag; only QDrag.exec is faked)
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
from PySide6.QtTest import QTest

drags = []


class FakeDrag:
    def __init__(self, src):
        self.src = src

    def setMimeData(self, m):
        self.m = m

    def exec(self, actions, default=None):
        drags.append((win._drag_payload, self.m.hasFormat(fm._DRAG_MIME)))


fm.QDrag = FakeDrag
dl = tmp / "DragLocal"
dl.mkdir()
for n in ("p.txt", "q.txt", "r.txt"):
    (dl / n).write_text(n)
win._local_path = str(dl)
win._refresh_local()
win.show()
tree = win._local_tree


def names(payload):
    return sorted(e.name for e in payload.items)


def gesture(row, mods=Qt.NoModifier):
    r = tree.visualItemRect(tree.topLevelItem(row)).center()
    QTest.mousePress(tree.viewport(), Qt.LeftButton, mods, r)
    QTest.mouseMove(tree.viewport(), r + QPoint(30, 30))
    QTest.mouseMove(tree.viewport(), r + QPoint(70, 70))
    QTest.mouseRelease(tree.viewport(), Qt.LeftButton, mods, r + QPoint(70, 70))


rows = {tree.topLevelItem(i).text(0): i for i in range(tree.topLevelItemCount())}
gesture(rows["q.txt"])
check("drag starts on a row that was not clicked first",
      len(drags) == 1 and drags[0][1] and names(drags[0][0]) == ["q.txt"])
check("payload cleared after the drag", win._drag_payload is None)
tree.clearSelection()
for n in ("p.txt", "r.txt"):
    tree.topLevelItem(rows[n]).setSelected(True)
drags.clear()
gesture(rows["p.txt"])
check("dragging a selected row keeps the multi-selection", drags and names(drags[0][0]) == ["p.txt", "r.txt"])
win._busy = True
drags.clear()
gesture(rows["q.txt"])
check("no drag while a transfer is running, and the user is told", not drags and "Wait" in win._status_label.text())
win._busy = False

# drop onto the other pane runs the cross-pane handler
got = []
win._handle_cross_pane_drop = lambda *a: got.append(a)
win._drag_payload = fm.DragPayload(True, [remote])
mime = QMimeData()
mime.setData(fm._DRAG_MIME, b"1")
vp = tree.viewport()
pt = QPointF(5, 200)   # empty area below the rows
for ev in (QDragEnterEvent(pt.toPoint(), Qt.CopyAction | Qt.MoveAction, mime, Qt.LeftButton, Qt.NoModifier),
           QDragMoveEvent(pt.toPoint(), Qt.CopyAction | Qt.MoveAction, mime, Qt.LeftButton, Qt.NoModifier),
           QDropEvent(pt, Qt.CopyAction | Qt.MoveAction, mime, Qt.LeftButton, Qt.NoModifier)):
    ev.setAccepted(False)
    app.sendEvent(vp, ev)
check("remote->local drop reaches the transfer handler", len(got) == 1 and got[0][0] is True and got[0][1] is False and got[0][3] is None)
win._drag_payload = None

print("FAILED: %s" % fails if fails else "ALL PASS")
sys.exit(1 if fails else 0)
