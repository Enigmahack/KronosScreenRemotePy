"""Sample Editor Save runs its disk writes on a worker (under the busy dialog) and changes the model's observable state
only on the GUI thread afterwards; write order (.KSF, then .KMP, then the orphan sweep) and failure semantics are kept:
a failed write leaves that file and the rest dirty, reports it, and skips the sweep. Against a COPY of a real fixture.
Run from the repo root."""
import os, shutil, sys, tempfile, threading, wave
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_data_")
import numpy as np
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

import Data.kmp_multisample as kmp_mod
import Data.ksf_sample as ksf_mod
import Data.ksc_collection as ksc_mod
import Views.sample_editor_window as sew
from Views.sample_editor_window import SampleEditorWindow

sew.confirm = lambda *a, **k: True

from fixture_paths import SAMPLE_FIXTURES as FIX
fails = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


writes = []                      # (kind, basename, on a worker thread?)
real_ksf_write, real_kmp_write = ksf_mod.atomic_write_bytes, kmp_mod.atomic_write_bytes
fail_names = set()


def spy(kind, real):
    def f(path, data, *a, **k):
        name = os.path.basename(str(path))
        writes.append((kind, name, threading.current_thread() is not threading.main_thread()))
        if name in fail_names:
            raise OSError("synthetic disk full")
        return real(path, data, *a, **k)
    return f


ksf_mod.atomic_write_bytes = spy("ksf", real_ksf_write)
kmp_mod.atomic_write_bytes = spy("kmp", real_kmp_write)


def setup(tag):
    tmp = tempfile.mkdtemp(prefix=f"kr_save_{tag}_")
    shutil.copytree(os.path.join(FIX, "SMPTEST"), os.path.join(tmp, "S"))
    ksc = os.path.join(tmp, "S", "LOOP.KSC")
    w = SampleEditorWindow("", 21, "", "", None)
    w.show()
    w.open_collection_path(ksc)
    m = w.model
    # a stereo pair edited in Combine mode: both halves pending (two .KSF), plus a zone edit (.KMP)
    n = 4000
    t = np.arange(n)
    wav = os.path.join(tmp, "pair.wav")
    with wave.open(wav, "wb") as f:
        f.setnchannels(2); f.setsampwidth(2); f.setframerate(44100)
        f.writeframes(np.stack([(np.sin(t * 0.05) * 20000).astype(np.int16),
                                (np.sin(t * 0.11) * 5000).astype(np.int16)], 1).tobytes())
    node = m.new_stereo_multisample_pair_in_collection("PAIR", m.next_free_mno1(2))
    m.select_node(node)
    m.import_stereo_audio_as_new_zone_pair(wav, 60, 90)
    m.save_all_changes()
    m.select_node(m._node_for_path(node.multisample_ref[1]).children[1])
    m.apply_gain_adjust(-1.0)
    z = m.selected_zone
    m.apply_zone_edits(z.original_key, z.top_key - 1)
    orphan = os.path.join(ksc_mod.content_dir_for(ksc), "ORPHAN_TEST.KSF")
    with open(orphan, "wb") as f:
        f.write(b"x")
    return w, m, orphan


# ── 1. a successful Save writes on the worker; every observable change happens on the GUI thread ────────────────
w, m, orphan = setup("ok")
pending_samples = [os.path.basename(p) for p in m._dirty_samples.keys()]
check("setup: two samples and a multisample pending", len(pending_samples) == 2 and len(m._dirty_multisamples) >= 1)
emits = []
listener = lambda name: emits.append(threading.current_thread() is threading.main_thread())
m.add_listener(listener)
writes.clear()
w.on_save_changes()
check("every file write ran on a worker thread", writes and all(t for _, _, t in writes))
kinds = [k for k, _, _ in writes]
check("write order: every .KSF before any .KMP", kinds == sorted(kinds, key=lambda k: k != "ksf"))
check("both pending samples written", sorted(n for k, n, _ in writes if k == "ksf") == sorted(pending_samples))
check("model notifications only on the GUI thread", emits and all(emits))
check("saved: nothing left dirty", not m.has_unsaved_changes)
check("orphan swept after a full save", not os.path.exists(orphan))
check("status reports the save", m.status_text.startswith("Saved"))
m.remove_listener(listener)
w.close()

# ── 2. the second sample's write fails: first clean, second still dirty, error named, orphan kept ───────────────
w, m, orphan = setup("fail")
order = [os.path.basename(p) for p in m._dirty_samples.keys()]
fail_names.add(order[1])
writes.clear()
w.on_save_changes()
fail_names.clear()
dirty_now = [os.path.basename(p) for p in m._dirty_samples.keys()]
check("first sample saved and clean", order[0] not in dirty_now)
check("failed sample still dirty", order[1] in dirty_now and m.has_unsaved_changes)
check("status names the failed file", f"Save failed for '{order[1]}'" in m.status_text)
check("orphan cleanup skipped after a failure", os.path.exists(orphan) and "Orphan cleanup skipped" in m.status_text)
check("failed run still wrote off the GUI thread", writes and all(t for _, _, t in writes))
# retry succeeds
w.on_save_changes()
check("retry saves the rest", not m.has_unsaved_changes)
w.close()

# ── 3. the worker itself blowing up commits nothing ─────────────────────────────────────────────────────────────
w, m, orphan = setup("boom")
before = sorted(m._dirty_samples.keys())


def boom(label, fn):
    raise RuntimeError("worker died")


m.run_heavy = boom
m.save_all_changes()
check("worker failure: everything still dirty", sorted(m._dirty_samples.keys()) == before and m.has_unsaved_changes)
check("worker failure reported", "Save failed" in m.status_text)
check("worker failure: orphan kept", os.path.exists(orphan))
m.run_heavy = w._run_heavy
w.close()

# ── 4. an orphan-sweep error does not discard the writes that landed ───────────────────────────────────────────
import Core.sample_editor_model.io as io_mod
w, m, orphan = setup("sweep")
real_listdir = io_mod.os.listdir


def bad_listdir(path):
    # only the save worker's sweep (os is shared: the window lists the same folder on the GUI thread)
    if (threading.current_thread() is not threading.main_thread()
            and os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.dirname(orphan))):
        raise PermissionError("synthetic: no listing")
    return real_listdir(path)


io_mod.os.listdir = bad_listdir
try:
    w.on_save_changes()
finally:
    io_mod.os.listdir = real_listdir
check("sweep error: the saved files are committed (clean)", not m.has_unsaved_changes)
check("sweep error reported", "Orphan cleanup failed" in m.status_text and m.status_text.startswith("Saved"))
w.close()

# ── 5. Save As copies on the worker; a failed copy leaves the original collection active ───────────────────────
import shutil as shutil_mod
w, m, orphan = setup("saveas")
m.save_all_changes()
copies = []
real_copy = shutil_mod.copyfile


def spy_copy(src, dst, *a, **k):
    copies.append(threading.current_thread() is not threading.main_thread())
    return real_copy(src, dst, *a, **k)


shutil_mod.copyfile = spy_copy
old_path = m.active_collection_path
new_path = os.path.join(os.path.dirname(old_path), "COPY.KSC")
m.run_heavy = w._run_heavy
m.save_collection_as(new_path)
check("Save As copied on a worker thread", copies and all(copies))
check("Save As switched to the copy", os.path.normcase(m.active_collection_path) == os.path.normcase(new_path))


def failing_copy(src, dst, *a, **k):
    raise OSError("synthetic copy failure")


shutil_mod.copyfile = failing_copy
before = m.active_collection_path
m.save_collection_as(os.path.join(os.path.dirname(old_path), "COPY2.KSC"))
shutil_mod.copyfile = real_copy
check("failed Save As keeps the active collection", m.active_collection_path == before and "Save As failed" in m.status_text)
w.close()

ksf_mod.atomic_write_bytes, kmp_mod.atomic_write_bytes = real_ksf_write, real_kmp_write
print(f"{len(fails)} failure(s)")
sys.exit(1 if fails else 0)
