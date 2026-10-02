"""Stereo-pair behaviour of SampleEditorModel: Combine-mode mirroring, partner undo, save of BOTH halves, shared
normalize, Split-mode channel move + Partner-domain undo, per-channel insert silence. Real files, copied fixture."""
import os, shutil, sys, tempfile, wave
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("KRONOS_DATA_DIR", tempfile.mkdtemp(prefix="kr_data_"))
import numpy as np

from Core.sample_editor_model.model import SampleEditorModel
from Data.ksf_sample import KsfSample

FIX = r"Z:\KronosScreenRemote\SampleFixtures"
fails = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


tmp = tempfile.mkdtemp(prefix="kr_stereo_")
shutil.copytree(os.path.join(FIX, "SMPTEST"), os.path.join(tmp, "S"))
m = SampleEditorModel()
m.open_collection(os.path.join(tmp, "S", "LOOP.KSC"))

N = 4000
t = np.arange(N)
left = (np.sin(t * 0.05) * 20000).astype(np.int16)
right = (np.sin(t * 0.11) * 5000).astype(np.int16)             # different shape AND a quieter peak
wav = os.path.join(tmp, "pair.wav")
with wave.open(wav, "wb") as w:
    w.setnchannels(2); w.setsampwidth(2); w.setframerate(44100)
    w.writeframes(np.stack([left, right], 1).tobytes())

node = m.new_stereo_multisample_pair_in_collection("PAIR", m.next_free_mno1(2))
m.select_node(node)
m.import_stereo_audio_as_new_zone_pair(wav, 60, 90)


def select_left():
    n = m._node_for_path(node.multisample_ref[1])
    m.select_node(n.children[1])


def partner_path():
    return m._partner_sample_path


select_left()
check("pair detected", m.has_stereo_pair and m.partner_sample is not None)
check("combine mode mirrors", m.should_mirror_to_partner)
check("left is primary", m.is_primary_left_channel)
L, R = m.selected_sample, m.partner_sample
l0, r0 = L.samples().copy(), R.samples().copy()
check("channels differ", not np.array_equal(l0, r0))

# ---- gain in Combine: both change, undo/redo restore/reapply both
m.selection_start_frame = m.selection_end_frame = 0
m.apply_gain_adjust(-6.0)
l1, r1 = L.samples().copy(), R.samples().copy()
check("gain changed L", np.abs(l1).max() < np.abs(l0).max() * 0.55)
check("gain changed R", np.abs(r1).max() < np.abs(r0).max() * 0.55)
m.undo()
check("undo restored BOTH", np.array_equal(L.samples(), l0) and np.array_equal(R.samples(), r0))
m.redo()
check("redo reapplied BOTH", np.array_equal(L.samples(), l1) and np.array_equal(R.samples(), r1))
m.undo()

# ---- save in Combine: the partner edit must reach disk
m.apply_reverse()
ppath = partner_path()
m.save_all_changes()
check("save cleared dirty", not m.has_unsaved_changes)
on_disk_r = KsfSample.open(open(ppath, "rb").read()).samples()
check("partner (-R) edit reached disk", np.array_equal(on_disk_r, r0[::-1]))
m.undo()
m.save_all_changes()

# ---- normalize: one shared factor
select_left()
L, R = m.selected_sample, m.partner_sample
l0, r0 = L.samples().copy(), R.samples().copy()
m.apply_normalize(-0.1)
lp, rp = int(np.abs(L.samples().astype(np.int32)).max()), int(np.abs(R.samples().astype(np.int32)).max())
check("louder channel hits target", lp >= 32000)
check("quieter channel scaled by the SAME factor", abs(rp / max(1, int(np.abs(r0).max())) - lp / int(np.abs(l0).max())) < 0.01)
m.undo()
check("normalize undone on both", np.array_equal(L.samples(), l0) and np.array_equal(R.samples(), r0))

# ---- insert silence on one channel only (Combine), R must stay untouched through undo
m.selection_start_frame = m.selection_end_frame = 0
m._cursor_frame = 100
m.apply_insert_silence(64, True, False)
check("L got silence", L.frame_count == N + 64)
check("R untouched", R.frame_count == N and np.array_equal(R.samples(), r0))
m.undo()
check("undo: L restored", L.frame_count == N and np.array_equal(L.samples(), l0))
check("undo: R STILL untouched", R.frame_count == N and np.array_equal(R.samples(), r0))
m._cursor_frame = -1

# ---- Split L/R channel move on the partner
m.split_lr = True
check("split: no mirroring", not m.should_mirror_to_partner)
m.apply_channel_move(True, 100)
check("partner padded +100", R.frame_count == N + 100 and not np.any(R.samples()[:100]))
check("primary untouched by partner move", L.frame_count == N)
m.apply_channel_move(True, -50)
check("partner trimmed -50", R.frame_count == N + 50)
m.apply_channel_move(True, -1000)
check("trim clamps to remaining padding", R.frame_count == N)
check("real audio never eaten", np.array_equal(R.samples(), r0))
m.apply_channel_move(True, -10)
check("nothing left to trim -> no-op", R.frame_count == N)

m.apply_channel_move(True, 30)
check("padded again", R.frame_count == N + 30)
m.undo()
check("partner-domain undo", R.frame_count == N and np.array_equal(R.samples(), r0))
m.redo()
check("partner-domain redo", R.frame_count == N + 30)
m.undo()

# ---- primary channel move in Split
m.apply_channel_move(False, 25)
check("primary padded", L.frame_count == N + 25 and R.frame_count == N)
m.undo()
check("primary move undone", L.frame_count == N)

# ---- Split edits touch ONLY the selected channel
m.apply_gain_adjust(-6.0)
check("split gain: L changed", np.abs(L.samples()).max() < np.abs(l0).max() * 0.55)
check("split gain: R untouched", np.array_equal(R.samples(), r0))
m.undo()

# ---- zone key edit mirrors onto the sibling
m.split_lr = False
select_left()
sib, _ = m._resolve_stereo_sibling(*m._resolve_context_multisample())
zl = m.selected_zone
idx = [i for i, z in enumerate(m._resolve_context_multisample()[0].zones) if z is zl][0]
m.apply_zone_edits(zl.original_key, zl.top_key - 5 if zl.top_key - 5 >= zl.original_key else zl.top_key)
check("zone edit mirrored to sibling", sib.zones[idx].top_key == zl.top_key and sib.zones[idx].original_key == zl.original_key)

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
