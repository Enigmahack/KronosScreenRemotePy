"""Sample Editor: heavy work off the GUI thread, busy lock, undo-history caps, per-use dialog/menu cleanup.
Against a COPY of a real fixture. Run from the repo root."""
import os, shutil, sys, tempfile, threading, wave
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_data_")
import numpy as np
from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QMenu, QProgressDialog

app = QApplication.instance() or QApplication([])

import Core.sample_editor_model.edit as edit_mod
import Views.sample_editor_window as sew
import Views.sample_remote_source as srs
from Core.sample_editor_model.model import SampleEditorModel
from Core.sample_editor_model.tree import enumerate_nodes
from Views.sample_editor_window import SampleEditorWindow, find_node_for_zone

sew.confirm = lambda *a, **k: True

from fixture_paths import SAMPLE_FIXTURES as FIX
fails = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


tmp = tempfile.mkdtemp(prefix="kr_busy_")
shutil.copytree(os.path.join(FIX, "SMPTEST"), os.path.join(tmp, "S"))


def select_audio(m):
    for n in enumerate_nodes(m.roots):
        if n.zone_ref is not None:
            m.select_node(n)
            if m.selected_sample is not None and not m.selected_sample.is_header_only:
                return m.selected_sample
    return None


# ── 1. undo domains follow the stacks' own caps ───────────────────────────────────────────────
m = SampleEditorModel()
m.open_collection(os.path.join(tmp, "S", "LOOP.KSC"))
s = select_audio(m)
check("model: audio sample selected", s is not None)
cap_steps = 3
m._sample_undo.byte_cap = len(s.pcm) * cap_steps          # room for exactly 3 snapshots
for _ in range(20):
    m.apply_gain_adjust(-0.5)
check("domain list trimmed to the byte-capped stack", m._undo_domains.count("Sample") == m._sample_undo.undo_count <= cap_steps)
undone = 0
while m.can_undo and undone < 50:
    m.undo()
    undone += 1
check("can_undo goes False once the capped stack is drained", not m.can_undo and undone == m._sample_undo.redo_count)
check("redo domains match the redo stack", m._redo_domains.count("Sample") == m._sample_undo.redo_count)

# ── 2. tempo/pitch: computed before any undo step; long samples go through run_heavy ──────────
m2 = SampleEditorModel()
m2.open_collection(os.path.join(tmp, "S", "LOOP.KSC"))
s2 = select_audio(m2)
calls = []
m2.run_heavy = lambda label, fn: (calls.append(label), fn())[1]
old_threshold = edit_mod.HEAVY_TEMPO_PITCH_FRAMES
edit_mod.HEAVY_TEMPO_PITCH_FRAMES = 1
m2.apply_tempo_pitch(1.5, 0.0)
check("tempo/pitch goes through run_heavy above the threshold", calls == ["Changing tempo/pitch..."] and m2.can_undo)
m2.undo()
n_before, dom_before = s2.frame_count, list(m2._undo_domains)


def boom(label, fn):
    raise MemoryError("synthetic")


m2.run_heavy = boom
m2.apply_tempo_pitch(1.5, 0.0)
check("a failed tempo/pitch leaves no undo step and no change",
      s2.frame_count == n_before and m2._undo_domains == dom_before and "failed" in m2.status_text)
edit_mod.HEAVY_TEMPO_PITCH_FRAMES = old_threshold

# ── 3. the window runs heavy work on a worker thread, under a locked busy dialog ──────────────
w = SampleEditorWindow("", 21, "", "", None)
w.show()
w.open_collection_path(os.path.join(tmp, "S", "LOOP.KSC"))
wm = w.model
node = next((n for n in enumerate_nodes(wm.roots) if n.zone_ref is not None), None)
w._select_tree_node(node)
seen = {}


def work():
    seen["thread"] = threading.current_thread() is not threading.main_thread()
    seen["busy"] = w._busy
    dlg = next((d for d in w.findChildren(QProgressDialog) if d.isVisible()), None)
    seen["dialog"] = dlg is not None
    return 42


check("run_heavy returns the worker's result", w._run_heavy("x", work) == 42)
check("work ran off the GUI thread", seen.get("thread") is True)
check("busy flag held while it ran", seen.get("busy") is True and not w._busy)
check("busy dialog shown while it ran", seen.get("dialog") is True)
app.processEvents()
app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
check("busy dialog deleted afterwards", not w.findChildren(QProgressDialog))
try:
    w._run_heavy("x", lambda: (_ for _ in ()).throw(ValueError("bad")))
    check("worker exception re-raised", False)
except ValueError:
    check("worker exception re-raised", True)

# Escape / close during the work does not dismiss the dialog; keys to the window are swallowed.
probe = {}


def try_dismiss():
    dlg = next((d for d in w.findChildren(QProgressDialog) if d.isVisible()), None)
    if dlg is not None:
        dlg.reject()
        dlg.close()
        probe["still_visible"] = dlg.isVisible()
    w.activateWindow()
    ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    probe["swallowed"] = w.eventFilter(w, ev) if w.isActiveWindow() else True


def slow():
    import time
    time.sleep(0.4)
    return 1


QTimer.singleShot(100, try_dismiss)
w._run_heavy("slow", slow)
check("busy dialog cannot be dismissed mid-work", probe.get("still_visible") is True)
check("window key shortcuts swallowed while busy", probe.get("swallowed") is True)

# Real path: a WAV import decodes on the worker.
wav = os.path.join(tmp, "tone.wav")
with wave.open(wav, "wb") as f:
    f.setnchannels(1); f.setsampwidth(2); f.setframerate(22050)
    f.writeframes((np.sin(np.arange(22050) * 0.05) * 8000).astype("<i2").tobytes())
threads = []
real_decode = srs.run_blocking


def spy(parent, label, work_fn):
    def wrapped(p):
        threads.append(threading.current_thread() is not threading.main_thread())
        return work_fn(p)
    return real_decode(parent, label, wrapped)


sew.run_blocking = spy
zones_before = len(wm.current_multisample_zones or [])
w._after_keep_multisample(lambda: wm.import_audio_as_new_zone(wav, 60, 60))
sew.run_blocking = real_decode
check("import decoded on a worker thread", threads == [True])
check("import added a zone", len(wm.current_multisample_zones or []) == zones_before + 1)

# ── 4. per-use menus/dialogs do not pile up on the window ─────────────────────────────────────
QTimer.singleShot(50, lambda: [mn.close() for mn in w.findChildren(QMenu) if mn.isVisible()])
w._gain_menu(w._btn_save, (1, 3))
app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
menus_after_one = len(w.findChildren(QMenu))
for _ in range(3):
    QTimer.singleShot(50, lambda: [mn.close() for mn in w.findChildren(QMenu) if mn.isVisible()])
    w._gain_menu(w._btn_save, (1, 3))
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
check("gain popup menus are deleted after use", len(w.findChildren(QMenu)) == menus_after_one)

w.on_report()
first = w._report
w.on_report()
app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
from Views.sample_editor_dialogs import SampleReportWindow
check("only one report window kept", len(w.findChildren(SampleReportWindow)) == 1 and w._report is not first)

w.close()
check("close detaches the window's model listeners", w._on_model_changed not in wm._listeners
      and w._on_cursor_moved not in wm.cursor_moved_listeners)

print(f"{len(fails)} failure(s)")
sys.exit(1 if fails else 0)
