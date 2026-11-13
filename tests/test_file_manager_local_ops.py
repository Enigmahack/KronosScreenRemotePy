"""File Manager local paste / move / delete: name collisions go through the same Rename / Overwrite / Skip /
Cancel prompt as cross-pane transfers (no silent overwrite or folder merge), overwrite never leaves a half-written
file, a folder can't be copied/moved into itself or onto itself, and the work runs on a worker thread so the window
keeps repainting. Headless: QT_QPA_PLATFORM=offscreen, FTP replaced by a duck-typed fake."""
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("KRONOS_DATA_DIR", tempfile.mkdtemp(prefix="kr_data_"))

from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

import Tools.file_manager as fm
from ftp_fake import FakeFtpWorker, FtpFakeServer

fails = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# the remote pane talks to the instrument-faithful fake (tests/ftp_fake.py) through the real _FtpWorker
fm._FtpWorker = FakeFtpWorker.bound_to(FtpFakeServer(volumes=["SSD1", "SSD2"]))
app = QApplication.instance() or QApplication([])
win = fm.FileManagerWindow("127.0.0.1", 21, "u", "p")


class Answer:
    """Scripted conflict answers; records each prompt."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked = []

    def __call__(self, name, suggested):
        self.asked.append((name, suggested))
        action, new_name, apply_all = self.answers.pop(0) if self.answers else ("cancel", "", False)
        req = fm._ConflictRequest(name, suggested)
        req.action = action
        req.new_name = new_name if new_name is not None else suggested
        req.apply_all = apply_all
        return req


def entry(p: Path) -> fm.FileEntry:
    return fm.FileEntry(p.name, str(p), p.is_dir(), 0 if p.is_dir() else p.stat().st_size, None)


def run(items, dest, move, *answers):
    ans = Answer(*answers)
    win._resolve_conflict = ans
    done, cancelled, failures = win._do_local_batch([entry(p) for p in items], str(dest), move)
    return ans, done, cancelled, failures


def leftovers(root: Path):
    return [p for p in root.rglob("*") if p.name.endswith(".part")]


with tempfile.TemporaryDirectory(prefix="kr_fm_local_") as tmp:
    root = Path(tmp)
    src, dst = root / "src", root / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "a.txt").write_text("NEW")
    (dst / "a.txt").write_text("OLD")

    # skip keeps the destination
    ans, done, cancelled, _ = run([src / "a.txt"], dst, False, ("skip", None, False))
    check("copy collision: prompted", ans.asked and ans.asked[0][0] == "a.txt")
    check("copy collision: suggested a free name", ans.asked and ans.asked[0][1] == "a (1).txt")
    check("copy skip: destination untouched", (dst / "a.txt").read_text() == "OLD" and done == 0)

    # rename makes a second file
    ans, done, *_ = run([src / "a.txt"], dst, False, ("rename", "a (1).txt", False))
    check("copy rename: new file", (dst / "a (1).txt").read_text() == "NEW" and (dst / "a.txt").read_text() == "OLD")

    # overwrite replaces atomically, no .part left
    ans, done, *_ = run([src / "a.txt"], dst, False, ("overwrite", None, False))
    check("copy overwrite: replaced", (dst / "a.txt").read_text() == "NEW" and done == 1)
    check("copy overwrite: source kept", (src / "a.txt").exists())
    check("copy overwrite: no temp file left", leftovers(root) == [])

    # cancel stops the batch
    (src / "b.txt").write_text("B")
    (dst / "b.txt").write_text("OLDB")
    ans, done, cancelled, _ = run([src / "a.txt", src / "b.txt"], dst, False, ("cancel", None, False))
    check("cancel: batch stopped", cancelled and len(ans.asked) == 1 and (dst / "b.txt").read_text() == "OLDB")

    # apply-to-all
    (dst / "a.txt").write_text("OLD")
    ans, done, cancelled, _ = run([src / "a.txt", src / "b.txt"], dst, False, ("skip", None, True))
    check("apply-all: asked once, both skipped", len(ans.asked) == 1 and done == 0
          and (dst / "a.txt").read_text() == "OLD" and (dst / "b.txt").read_text() == "OLDB")

    # move with overwrite
    ans, done, *_ = run([src / "b.txt"], dst, True, ("overwrite", None, False))
    check("move overwrite: replaced and source gone", (dst / "b.txt").read_text() == "B" and not (src / "b.txt").exists())

    # no collision: no prompt
    (src / "c.txt").write_text("C")
    ans, done, *_ = run([src / "c.txt"], dst, True)
    check("move no-collision: no prompt, moved", ans.asked == [] and (dst / "c.txt").read_text() == "C"
          and not (src / "c.txt").exists())

    # folder collision: overwrite = merge, nothing extra deleted
    (src / "F" / "sub").mkdir(parents=True)
    (src / "F" / "same.txt").write_text("NEW")
    (src / "F" / "sub" / "deep.txt").write_text("NEWDEEP")
    (dst / "F" / "sub").mkdir(parents=True)
    (dst / "F" / "same.txt").write_text("OLD")
    (dst / "F" / "keep.txt").write_text("KEEP")
    (dst / "F" / "sub" / "deep.txt").write_text("OLDDEEP")
    ans, done, *_ = run([src / "F"], dst, False, ("skip", None, False))
    check("folder copy collision: prompted, skip leaves it", ans.asked and (dst / "F" / "same.txt").read_text() == "OLD")
    ans, done, *_ = run([src / "F"], dst, False, ("overwrite", None, False))
    check("folder overwrite: same-named files replaced",
          (dst / "F" / "same.txt").read_text() == "NEW" and (dst / "F" / "sub" / "deep.txt").read_text() == "NEWDEEP")
    check("folder overwrite: other files kept", (dst / "F" / "keep.txt").read_text() == "KEEP")
    check("folder overwrite copy: source kept", (src / "F" / "same.txt").exists())
    ans, done, _, failures = run([src / "F"], dst, True, ("overwrite", None, False))
    check("folder move overwrite: source removed", not (src / "F").exists() and done == 1 and failures == [])
    check("folder move overwrite: no temp left", leftovers(root) == [])

    # folder copy with no collision lands complete
    (src / "G").mkdir()
    (src / "G" / "x.txt").write_text("X")
    ans, done, *_ = run([src / "G"], dst, False)
    check("folder copy no-collision", (dst / "G" / "x.txt").read_text() == "X" and ans.asked == [])

    # file vs folder clash on overwrite: skipped, nothing lost
    (src / "H").write_text("FILE")
    (dst / "H").mkdir()
    (dst / "H" / "inside.txt").write_text("IN")
    ans, done, _, failures = run([src / "H"], dst, False, ("overwrite", None, False))
    check("type clash: skipped", done == 0 and (dst / "H" / "inside.txt").exists() and failures)

    # a folder into itself / its own subfolder
    (src / "I" / "child").mkdir(parents=True)
    (src / "I" / "f.txt").write_text("I")
    ans, done, _, failures = run([src / "I"], src / "I" / "child", False)
    check("copy into own subfolder: refused", done == 0 and failures and not (src / "I" / "child" / "I").exists())
    ans, done, _, failures = run([src / "I"], src / "I", True)
    check("move into itself: refused", done == 0 and (src / "I" / "f.txt").exists())

    # a folder moved onto itself (cut + paste in the same folder): a no-op, never "merge then delete source"
    ans, done, _, failures = run([src / "I"], src, True, ("overwrite", None, False))
    check("move onto itself: no prompt, nothing lost", ans.asked == [] and (src / "I" / "f.txt").read_text() == "I")
    if os.name == "nt":
        e = fm.FileEntry("i", str(src / "i"), True, 0, None)
        win._resolve_conflict = Answer(("overwrite", None, False))
        win._do_local_batch([e], str(src), True)
        check("move onto itself, other case (Windows): nothing lost", (src / "I" / "f.txt").read_text() == "I")

    # copy onto itself: prompt; overwrite is refused, rename makes a copy
    ans, done, _, failures = run([src / "a.txt"], src, False, ("overwrite", None, False))
    check("copy onto itself: overwrite refused, file intact", (src / "a.txt").read_text() == "NEW" and done == 0)
    ans, done, *_ = run([src / "a.txt"], src, False, ("rename", None, False))
    check("copy onto itself: rename makes a copy", (src / "a (1).txt").read_text() == "NEW")

    # ---- async: paste returns at once, the work runs on a worker thread, busy guards the window
    (src / "slow.txt").write_text("SLOW")
    real_put = fm._put_local_file
    seen_threads = []

    def slow_put(s, d, move):
        import threading
        seen_threads.append(threading.current_thread() is threading.main_thread())
        time.sleep(0.6)
        real_put(s, d, move)

    fm._put_local_file = slow_put
    win._local_path = str(dst)
    win._clipboard = fm.ClipboardPayload(is_cut=False, from_remote=False, items=[entry(src / "slow.txt")])
    t0 = time.monotonic()
    win._do_paste(False)
    returned = time.monotonic() - t0
    check("paste returns immediately (work is off the GUI thread)", returned < 0.3)
    check("paste: window is busy while it runs", win._busy)
    win._do_paste(False)   # a second paste while busy is ignored
    deadline = time.monotonic() + 10
    while win._busy and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()
    fm._put_local_file = real_put
    check("paste: finished and busy cleared", not win._busy and (dst / "slow.txt").read_text() == "SLOW")
    check("paste: copy ran on a worker thread", seen_threads == [False])
    check("paste: status reports it", "Copied 1/1" in win._status_label.text())

    # ---- local delete runs in the background too
    (dst / "del_me").mkdir()
    (dst / "del_me" / "f").write_text("x")
    win._refresh_local()
    for i in range(win._local_tree.topLevelItemCount()):
        it = win._local_tree.topLevelItem(i)
        it.setSelected(win._local_entries[i].name == "del_me")
    orig_q = QMessageBox.question
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
    try:
        win._on_local_delete()
        check("delete: busy while running", win._busy)
        deadline = time.monotonic() + 10
        while win._busy and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.02)
        app.processEvents()
    finally:
        QMessageBox.question = orig_q
    check("delete: folder removed", not (dst / "del_me").exists())
    check("delete: status", "Deleted 1/1" in win._status_label.text())

    # ---- local rename never replaces an existing item
    win._refresh_local()
    for i in range(win._local_tree.topLevelItemCount()):
        win._local_tree.topLevelItem(i).setSelected(win._local_entries[i].name == "c.txt")
    a_before = (dst / "a.txt").read_text()
    orig_t = QInputDialog.getText
    QInputDialog.getText = staticmethod(lambda *a, **k: ("a.txt", True))
    try:
        win._on_local_rename()
    finally:
        QInputDialog.getText = orig_t
    check("rename onto existing: refused", (dst / "c.txt").exists() and (dst / "a.txt").read_text() == a_before)
    check("no temp files anywhere", leftovers(root) == [])

win.close()
print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
