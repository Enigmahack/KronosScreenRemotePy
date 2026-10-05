"""SampleEditorModel core + edit layer against a COPY of a real fixture: open, select, edit, mirror, undo/redo.
Run from the repo root."""
import os, shutil, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("KRONOS_DATA_DIR", tempfile.mkdtemp(prefix="kr_data_"))
import numpy as np

from Core.sample_editor_model.model import SampleEditorModel
from Core.sample_editor_model.tree import enumerate_nodes

from fixture_paths import SAMPLE_FIXTURES as FIX
fails = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


tmp = tempfile.mkdtemp(prefix="kr_samplemodel_")
work = os.path.join(tmp, "SMPTEST")
shutil.copytree(os.path.join(FIX, "SMPTEST"), work)

m = SampleEditorModel()
m.open_collection(os.path.join(work, "LOOP.KSC"))
nodes = list(enumerate_nodes(m.roots))
check("tree built", len(nodes) > 2)
zone_nodes = [n for n in nodes if n.zone_ref is not None]
check("has zone nodes", len(zone_nodes) > 0)

for zn in zone_nodes:
    m.select_node(zn)
    if m.selected_sample is not None and not m.selected_sample.is_header_only:
        break
s = m.selected_sample
check("sample selected with audio", s is not None and not s.is_header_only)
n0 = s.frame_count
orig = s.samples().copy()
check("not dirty initially", not m.has_unsaved_changes)

# crop
m.selection_start_frame, m.selection_end_frame = 10, min(n0, 510)
m.apply_crop()
check("crop length", s.frame_count == min(n0, 510) - 10)
check("crop: dirty + can_undo", m.has_unsaved_changes and m.can_undo and not m.can_redo)
check("crop: markers inside buffer", s.sample_start <= s.frame_count and s.loop_end <= s.frame_count)
m.undo()
check("undo restores pcm", s.frame_count == n0 and np.array_equal(s.samples(), orig))
check("undo -> can_redo", m.can_redo and not m.can_undo)
m.redo()
check("redo re-applies", s.frame_count == min(n0, 510) - 10)
m.undo()

# gain / reverse / normalize / dc / tempo
m.selection_start_frame = m.selection_end_frame = 0
m.apply_reverse()
check("reverse whole", np.array_equal(s.samples(), orig[::-1]))
m.undo()
m.apply_gain_adjust(-6.0)
check("gain -6 dB halves peak", abs(int(np.abs(s.samples()).max()) / max(1, int(np.abs(orig).max())) - 0.501) < 0.01)
m.undo()
m.apply_normalize()
check("normalize hits ~ -0.1 dBFS", int(np.abs(s.samples().astype(np.int32)).max()) >= 32000)
m.undo()
m.apply_tempo_pitch(2.0, 0.0)
check("tempo x2 halves length", abs(s.frame_count - n0 // 2) <= n0 * 0.02 + 2)
m.undo()
check("tempo undo restores", np.array_equal(s.samples(), orig))
m.apply_tempo_pitch(100.0, 100.0)
check("tempo/pitch clamped (no hang)", s.frame_count == int(s.frame_count))
m.undo()

# cut / paste / clipboard
m.selection_start_frame, m.selection_end_frame = 100, 200
m.cut_selection()
check("cut removes 100 frames", s.frame_count == n0 - 100)
m.paste_at_selection()
m.selection_start_frame = m.selection_end_frame = 0
check("paste restores length", s.frame_count == n0)
m.undo(); m.undo()
check("two undos back to original", np.array_equal(s.samples(), orig))

# sample fields funnel
m.apply_sample_edits(int(s.sample_rate), True, 50, 10, 5)
check("start<=loop_start<=loop_end enforced", s.sample_start <= s.loop_start <= s.loop_end and s.loop_start >= 50)
check("loop flag off bit", (s.flags & 0x80) == 0)
m.apply_sample_edits(int(s.sample_rate), False, 0, 0, 100)
check("one-shot sets 0x80", (s.flags & 0x80) == 0x80)
m.undo(); m.undo()

# header-only protection / insert silence
m.apply_insert_silence(64, True, True)
check("insert silence adds frames", s.frame_count == n0 + 64)
m.undo()

# zone key edit + undo
zone = m.selected_zone
ok, tk = zone.original_key, zone.top_key
m.apply_zone_edits(ok, tk)
check("no-op zone edit pushes no undo step", not m.can_undo)
new_ok = ok - 1 if ok > 0 else ok + 1
m.apply_zone_edits(new_ok, tk)
check("zone edit applied + undoable", zone.original_key == new_ok and m.can_undo and m._undo_domains[-1] == "Zone")
m.undo()
check("zone undo restores", (zone.original_key, zone.top_key) == (ok, tk))

# revert wipes undo
m.revert_all_changes()
check("revert clears undo/dirty", not m.can_undo and not m.has_unsaved_changes)

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
