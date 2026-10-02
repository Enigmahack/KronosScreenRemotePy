"""Port of C# SamplePhase7SelfTests: stereo-pair resolution, Combine-mode mirroring, Split not mirroring,
header-only partner, MoveZoneBoundary, MoveLoopRegion. Hand-built L/R collection, no fixtures."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("KRONOS_DATA_DIR", tempfile.mkdtemp(prefix="kr_data_"))
import numpy as np

from Core.sample_editor_model.model import SampleEditorModel
from Core.sample_editor_model.tree import enumerate_nodes
from Data.ksc_collection import KscCollection
from Data.kmp_multisample import KmpMultisample, KmpZone
from Data.ksf_sample import KsfSample

fails = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def pcm(vals):
    return np.array(vals, dtype=np.int16)


root = tempfile.mkdtemp(prefix="kr_phase7_")
ksc_path = os.path.join(root, "Stereo.KSC")
os.makedirs(os.path.join(root, "Stereo"))
KscCollection(entries=["Stereo-L.KMP", "Stereo-R.KMP"]).save(ksc_path)

for suf, mno, fn in (("-L", 0, "MS000000.KSF"), ("-R", 1, "MS001000.KSF")):
    k = KmpMultisample(name="Stereo", suffix=suf, mno1=mno)
    k.zones.append(KmpZone(filename=fn, original_key=60, top_key=60))
    k.save(os.path.join(root, "Stereo", f"Stereo{suf}.KMP"))

# Large magnitudes: a +6 dB gain on tiny values truncates back to the same value and fakes a no-op.
L_VALS, R_VALS = [1000, 2000, 3000, 4000, 5000], [10000, 11000, 12000, 13000, 14000]


def write_ksf(suf, fn, vals):
    d = os.path.join(root, "Stereo", f"Stereo{suf}")
    os.makedirs(d, exist_ok=True)
    s = KsfSample(name="Snare", suffix=suf, sample_rate=44100)
    if vals is not None:
        s.set_samples(pcm(vals))
    s.save(os.path.join(d, fn))


write_ksf("-L", "MS000000.KSF", L_VALS)
write_ksf("-R", "MS001000.KSF", R_VALS)


def find_zone(m, filename):
    for n in enumerate_nodes(m.roots):
        if n.zone_ref is not None and n.zone_ref[0].filename == filename:
            return n
    return None


def fresh(filename="MS000000.KSF"):
    m = SampleEditorModel()
    m.open_collection(ksc_path)
    m.select_node(find_zone(m, filename))
    return m


def vals(w):
    return None if w is None else [int(x) for x in w]


m = fresh()
check("left-zone-found", m.has_stereo_pair)
check("primary-is-left", m.is_primary_left_channel)
check("left-waveform-is-primary", vals(m.left_sample_waveform) == L_VALS)
check("right-waveform-is-partner", vals(m.right_sample_waveform) == R_VALS)

m = fresh("MS001000.KSF")
check("stereo-pair-detected-from-right", m.has_stereo_pair)
check("primary-is-right", not m.is_primary_left_channel)
check("left-waveform-is-partner-when-primary-right", vals(m.left_sample_waveform) == L_VALS)
check("right-waveform-is-primary-when-primary-right", vals(m.right_sample_waveform) == R_VALS)

# Combine mirrors, Split doesn't
m = fresh()
check("combine-is-default", not m.split_lr)
m.selection_start_frame, m.selection_end_frame = 0, 3
m.apply_gain_adjust(6.0)
check("combine-mirrors-primary-changed", m.sample_waveform[0] != 1000)
check("combine-mirrors-partner-changed", m.right_sample_waveform[0] != 10000)
m.undo()
check("combine-undo-restores-primary", m.sample_waveform[0] == 1000)
check("combine-undo-restores-partner", m.right_sample_waveform[0] == 10000)
m.split_lr = True
m.selection_start_frame, m.selection_end_frame = 0, 3
m.apply_gain_adjust(6.0)
check("split-still-changes-primary", m.sample_waveform[0] != 1000)
check("split-does-not-touch-partner", m.right_sample_waveform[0] == 10000)
m.undo()

# loop-from-selection / sample edits mirror status
m = fresh()
m.selection_start_frame, m.selection_end_frame = 1, 4
m.set_loop_from_selection()
check("loop-from-selection-mirrors-status", "both L/R" in m.status_text)
m.apply_sample_edits(44100, True, 0, 1, 4)
check("sample-edits-mirrors-status", "both L/R" in m.status_text)

# Header-only partner (real hardware: 124-byte corrupted-save signature) is a pair but never mirrored into.
write_ksf("-R", "MS001000.KSF", None)
m = fresh()
check("header-only-partner-still-detected-as-pair", m.has_stereo_pair)
check("header-only-partner-waveform-is-null", m.right_sample_waveform is None)
m.selection_start_frame, m.selection_end_frame = 0, 3
m.apply_gain_adjust(6.0)
check("header-only-partner-not-mirrored-into", "both L/R" not in m.status_text)
check("header-only-partner-primary-still-edited", m.sample_waveform[0] != 1000)
m.undo()
write_ksf("-R", "MS001000.KSF", R_VALS)

# MoveZoneBoundary
zm = KmpMultisample(name="T", mno1=9)
zm.zones.append(KmpZone(filename="A.KSF", original_key=0, top_key=40))
zm.zones.append(KmpZone(filename="B.KSF", original_key=41, top_key=80))
SampleEditorModel().move_zone_boundary(zm.zones[0], 50)
check("move-boundary-updates-topkey", zm.zones[0].top_key == 50)

# MoveLoopRegion
m = fresh()
m.move_loop_region(2, 5)
check("move-loop-region-sets-start", m.loop_start == 2)
check("move-loop-region-sets-end", m.loop_end == 5)
check("move-loop-region-mirrors-status", "both L/R" in m.status_text)

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
