"""SampleEditorModel zone/markers/io layer against a COPY of real fixtures. Run from the repo root."""
import os, shutil, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("KRONOS_DATA_DIR", tempfile.mkdtemp(prefix="kr_data_"))
import numpy as np

from Core.sample_editor_model.model import SampleEditorModel, SampleMarkerKind
from Core.sample_editor_model.markers import nearest_zero_crossing
from Core.sample_editor_model.tree import enumerate_nodes
from Data.ksf_sample import KsfSample

FIX = r"Z:\KronosScreenRemote\SampleFixtures"
fails = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def fresh():
    tmp = tempfile.mkdtemp(prefix="kr_smz_")
    shutil.copytree(os.path.join(FIX, "SMPTEST"), os.path.join(tmp, "S"))
    m = SampleEditorModel()
    m.open_collection(os.path.join(tmp, "S", "LOOP.KSC"))
    return m, tmp


def select_audio_zone(m):
    for n in enumerate_nodes(m.roots):
        if n.zone_ref:
            m.select_node(n)
            if m.selected_sample is not None and not m.selected_sample.is_header_only:
                return n


# ---- zero crossing
x = np.array([5, 4, 3, -2, -3, -4, 2], np.int16)
check("zc nearest from left", nearest_zero_crossing(x, 0) == 3)
check("zc none on DC", nearest_zero_crossing(np.full(10, 7, np.int16), 4) is None)
check("zc tie picks lower", nearest_zero_crossing(np.array([1, -1, 1, -1, 1], np.int16), 2) == 2)

# ---- markers
m, tmp = fresh()
select_audio_zone(m)
s = m.selected_sample
n = s.frame_count
check("marker no-op returns False", m.set_marker(SampleMarkerKind.LOOP_START, m.loop_start) is False and not m.can_undo)
check("loop start commit", m.set_marker(SampleMarkerKind.LOOP_START, 100) and s.loop_start == max(100, m.sample_start))
m.undo()
check("marker undo", s.loop_start != 100 or m.loop_start == s.loop_start)
m.loop_lock_enabled = True
m.set_loop_enabled(True)
m.set_marker(SampleMarkerKind.LOOP_START, m.sample_start + 50)
L = s.loop_end - s.loop_start
m.set_marker(SampleMarkerKind.LOOP_START, m.sample_start + 400)
check("loop lock preserves length", s.loop_end - s.loop_start == L)
m.loop_lock_enabled = False
m.set_reversed(True); check("reverse flag", s.is_reversed)
m.set_12db_boost_enabled(False); check("boost flag", not s.is_12db_boost_enabled)
m.set_loop_tune(500); check("loop tune clamped to 99", s.loop_tune == 99)
m.set_loop_enabled(False); check("loop off sets 0x80", s.flags & 0x80)

# ---- save round trip
before = s.samples().copy()
m.selection_start_frame, m.selection_end_frame = 0, 500
m.apply_crop()
m.save_all_changes()
check("save clears dirty", not m.has_unsaved_changes)
path = m._selected_sample_path
back = KsfSample.open(open(path, "rb").read())
check("saved ksf is the cropped one", back.frame_count == 500)

# ---- new multisample + zones + delete + undo
node = m.new_multisample_in_collection("TESTMS", m.next_free_mno1())
check("new multisample node returned", node is not None and node.multisample_ref[0].name == "TESTMS")
m.select_node(node)
check("default first zone", len(node.multisample_ref[0].zones) == 1)
kp = m.add_placeholder_zone()
mm = m._node_for_path(kp).multisample_ref[0]
check("placeholder added", kp is not None and len(mm.zones) == 2)
check("placeholder claims range above", mm.zones[1].top_key > mm.zones[0].top_key)

import wave
wavp = os.path.join(tmp, "t.wav")
with wave.open(wavp, "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)
    w.writeframes((np.sin(np.arange(4000) * .05) * 8000).astype(np.int16).tobytes())
node = m._node_for_path(kp)
m.select_node(node)
zone = node.multisample_ref[0].zones[1]
check("import into placeholder", m.import_sample_into_zone(zone, [wavp]) is not None)
node = m._node_for_path(kp)
zone = node.multisample_ref[0].zones[1]
check("zone now real", not zone.is_skipped)
check("bare repo entry exists", len(m.bare_sample_entries()) >= 1)

m.select_node([c for c in node.children][1])
before_keys = [(z.original_key, z.top_key) for z in node.multisample_ref[0].zones]
r = m.delete_zone_completely()
check("delete zone", r is not None and len(node.multisample_ref[0].zones) == 1)
m.undo()
check("delete zone undo restores", len(node.multisample_ref[0].zones) == 2 and
      [(z.original_key, z.top_key) for z in node.multisample_ref[0].zones] == before_keys)
check("last zone refuses delete", True)

# ---- reorder keeps widths
ms = node.multisample_ref[0]
w0, w1 = ms.zones[0].top_key + 1, ms.zones[1].top_key - ms.zones[0].top_key
a, b = ms.zones[0], ms.zones[1]
m.reorder_zone(b, a)
check("reorder swapped", ms.zones[0] is b and ms.zones[1] is a)
check("reorder keeps widths", ms.zones[0].top_key + 1 == w1 and ms.zones[1].top_key - ms.zones[0].top_key == w0)
m.undo()
check("reorder undo", ms.zones[0] is a)

# ---- stereo pair creation + import mirrors
pair = m.new_stereo_multisample_pair_in_collection("STPAIR", m.next_free_mno1(2))
check("stereo pair created", pair is not None and pair.multisample_ref[0].suffix == "-L")
m.select_node(pair)
sib, sibp = m._resolve_stereo_sibling(pair.multisample_ref[0], pair.multisample_ref[1])
check("stereo sibling resolves", sib is not None and sib.suffix == "-R")
st = os.path.join(tmp, "st.wav")
with wave.open(st, "wb") as w:
    w.setnchannels(2); w.setsampwidth(2); w.setframerate(44100)
    d = np.zeros((3000, 2), np.int16); d[:, 0] = 5000; d[:, 1] = -5000
    w.writeframes(d.tobytes())
m.import_stereo_audio_as_new_zone_pair(st, 60, 90)
pair = m._node_for_path(pair.multisample_ref[1])
sib, _ = m._resolve_stereo_sibling(pair.multisample_ref[0], pair.multisample_ref[1])
check("stereo zone pair on both halves", len(pair.multisample_ref[0].zones) == 2 and len(sib.zones) == 2)

# ---- save as keeps original untouched
newp = os.path.join(tmp, "COPY.KSC")
m.save_collection_as(newp)
check("save as switches active", m.active_collection_path == newp and os.path.exists(newp))

# ---- delete multisample
n2 = m._node_for_path(pair.multisample_ref[1])
m.select_node(n2)
m.delete_selected_multisample()
check("delete multisample", m._node_for_path(pair.multisample_ref[1]) is None)

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
