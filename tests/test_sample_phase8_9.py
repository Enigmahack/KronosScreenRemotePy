"""Port of C# SamplePhase8SelfTests + SamplePhase9SelfTests: loop/one-shot provider sequencing, 128-zone cap,
SetMarker choke point (ordering, Loop Lock, Use Zero incl. dual-channel + tie), stereo-shared Normalize,
field-edit undo and chronological undo order."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("KRONOS_DATA_DIR", tempfile.mkdtemp(prefix="kr_data_"))
import numpy as np

from Core.sample_editor_model.model import SampleEditorModel
from Core.sample_editor_model.markers import SampleMarkerKind
from Core.sample_editor_model.tree import enumerate_nodes
from Core.sample_playback import LoopingProvider, OneShotProvider
from Core.sample_import_builder import add_sample_zone, MAX_ZONES_PER_MULTISAMPLE
from Data.ksc_collection import KscCollection
from Data.kmp_multisample import KmpMultisample, KmpZone
from Data.ksf_sample import KsfSample

fails = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def pcm(v):
    return np.array(v, dtype=np.int16)


def frames_of(p, n):
    return [int(x) for x in p.read(n).reshape(-1)]


def ints(w):
    return [int(x) for x in w]


# ---- providers
p = LoopingProvider(pcm([0, 10, 20, 30, 40, 50]), None, 44100, 0, 2, 5, False)
check("loop-intro-then-forward-loop", frames_of(p, 11) == [0, 10, 20, 30, 40, 20, 30, 40, 20, 30, 40])
p = LoopingProvider(pcm([0, 10, 20, 30, 40, 50]), None, 44100, 0, 2, 5, True)
check("loop-intro-then-reverse-loop", frames_of(p, 11) == [50, 40, 30, 20, 40, 30, 20, 40, 30, 20, 40])
p = LoopingProvider(pcm([1, 100, 200, 300]), pcm([-1, -100, -200, -300]), 44100, 0, 1, 4, True)
check("stereo-reverse-loop-keeps-channels-paired", frames_of(p, 7) == [
    300, -300, 200, -200, 100, -100, 300, -300, 200, -200, 100, -100, 300, -300])
p = OneShotProvider(pcm([1, 3]), pcm([2, 4]), 44100)
out = p.read(4)
check("oneshot-stereo-reads-only-available", len(out) == 2)
check("oneshot-stereo-preserves-interleaving", ints(out.reshape(-1)) == [1, 2, 3, 4])
check("oneshot-position-after-full-read", p.position_frame == 2)
check("oneshot-second-read-returns-zero", len(p.read(4)) == 0)

# ---- 128-zone cap
root = tempfile.mkdtemp(prefix="kr_phase8_")
m = KmpMultisample(name="Cap", mno1=77)
kmp_path = os.path.join(root, "Cap.KMP")
for i in range(MAX_ZONES_PER_MULTISAMPLE):
    add_sample_zone(m, kmp_path, f"S{i}", pcm([1, 2, 3]), 44100, i, i)
check("zone-cap-reached-exactly", len(m.zones) == MAX_ZONES_PER_MULTISAMPLE)
threw = False
try:
    add_sample_zone(m, kmp_path, "Overflow", pcm([1, 2, 3]), 44100, 127, 127)
except Exception:
    threw = True
check("zone-cap-129th-throws", threw)
check("zone-cap-not-silently-added", len(m.zones) == MAX_ZONES_PER_MULTISAMPLE)


def find_zone(model, filename):
    return next((n for n in enumerate_nodes(model.roots)
                 if n.zone_ref is not None and n.zone_ref[0].filename == filename), None)


# ---- marker choke point on a mono collection
mroot = tempfile.mkdtemp(prefix="kr_phase8m_")
ksc = os.path.join(mroot, "Marker.KSC")
os.makedirs(os.path.join(mroot, "Marker", "Marker"))
KscCollection(entries=["Marker.KMP"]).save(ksc)
kmp = KmpMultisample(name="Marker", mno1=0)
kmp.zones.append(KmpZone(filename="MS000000.KSF", original_key=60, top_key=60))
kmp_file = os.path.join(mroot, "Marker", "Marker.KMP")
kmp.save(kmp_file)
ksf_dir = os.path.join(mroot, "Marker", "Marker")
s = KsfSample(name="Marker", sample_rate=44100)
s.set_samples(pcm([5, 10, 15, -5, -10, 5, 10]))
s.save(os.path.join(ksf_dir, "MS000000.KSF"))


def new_mono(fn="MS000000.KSF"):
    mm = SampleEditorModel()
    mm.open_collection(ksc)
    mm.select_node(find_zone(mm, fn))
    return mm


v = new_mono()
v.set_marker(SampleMarkerKind.SAMPLE_START, 3)
check("marker-sample-start-set", v.sample_start == 3)
check("marker-loop-floored-to-new-sample-start", v.loop_start >= 3 and v.loop_end >= 3)
v.set_marker(SampleMarkerKind.LOOP_START, 1)
check("marker-loop-start-cannot-precede-sample-start", v.loop_start == 3)

v = new_mono()
v.set_marker(SampleMarkerKind.SAMPLE_START, 0)
v.set_marker(SampleMarkerKind.LOOP_START, 1)
v.set_marker(SampleMarkerKind.LOOP_END, 5)
v.loop_lock_enabled = True
v.set_marker(SampleMarkerKind.LOOP_START, 2)
check("loop-lock-preserves-length", v.loop_start == 2 and v.loop_end == 6)

v = new_mono()
v.set_marker(SampleMarkerKind.SAMPLE_START, 3)
v.move_loop_region(4, 6)
check("move-loop-region-normal-case", v.loop_start == 4 and v.loop_end == 6)
v.move_loop_region(1, 3)
check("move-loop-region-stops-at-sample-start-wall", v.loop_start == 3)
check("move-loop-region-preserves-length-at-wall", v.loop_end - v.loop_start == 2)

v = new_mono()
v.use_zero_crossing = True
v.set_marker(SampleMarkerKind.SAMPLE_START, 0)
check("use-zero-snaps-to-crossing", v.sample_start == 3)

nc = KsfSample(name="NoCross", sample_rate=44100)
nc.set_samples(pcm([5, 10, 15, 20, 25]))
nc.save(os.path.join(ksf_dir, "MS000001.KSF"))
kmp.zones.append(KmpZone(filename="MS000001.KSF", original_key=61, top_key=61))
kmp.save(kmp_file)
v = new_mono("MS000001.KSF")
v.use_zero_crossing = True
v.set_marker(SampleMarkerKind.SAMPLE_START, 2)
check("use-zero-no-crossing-falls-back-unchanged", v.sample_start == 2)

# ---- Phase 9: stereo fixture
proot = tempfile.mkdtemp(prefix="kr_phase9_")
pksc = os.path.join(proot, "P9.KSC")
os.makedirs(os.path.join(proot, "P9", "P9-L"))
os.makedirs(os.path.join(proot, "P9", "P9-R"))
KscCollection(entries=["P9-L.KMP", "P9-R.KMP"]).save(pksc)
for suf, mno, fn in (("-L", 0, "MS000000.KSF"), ("-R", 1, "MS001000.KSF")):
    k = KmpMultisample(name="P9", suffix=suf, mno1=mno)
    k.zones.append(KmpZone(filename=fn, original_key=60, top_key=60))
    k.save(os.path.join(proot, "P9", f"P9{suf}.KMP"))


def new_stereo(left, right):
    for suf, fn, vals in (("-L", "MS000000.KSF", left), ("-R", "MS001000.KSF", right)):
        ks = KsfSample(name="P9", suffix=suf, sample_rate=44100)
        ks.set_samples(pcm(vals))
        ks.save(os.path.join(proot, "P9", f"P9{suf}", fn))
    mm = SampleEditorModel()
    mm.open_collection(pksc)
    mm.select_node(find_zone(mm, "MS000000.KSF"))
    return mm


v = new_stereo([5, 10, 15, 20, 25, 30, 35], [5, 10, -10, -20, -25, -30, -35])
v.use_zero_crossing = True
v.set_marker(SampleMarkerKind.SAMPLE_START, 0)
check("use-zero-finds-crossing-on-partner-channel", v.sample_start == 2)

v = new_stereo([5, -5, -10, -15, -20, -25, -30], [20, 25, 30, 35, 20, -5, -10])
v.use_zero_crossing = True
v.set_marker(SampleMarkerKind.SAMPLE_START, 3)
check("use-zero-tie-picks-lower-frame", v.sample_start == 1)

v = new_stereo([1000, -2000, 10000, -5000], [2000, -4000, 20000, -10000])
v.apply_normalize()
lp = int(np.abs(v.sample_waveform.astype(np.int32)).max())
rp = int(np.abs(v.right_sample_waveform.astype(np.int32)).max())
check("normalize-partner-reaches-target-peak", rp >= 32000)
check("normalize-primary-stays-proportionally-quieter", lp < rp - 5000)

v = new_stereo([1, 2, 3, 4, 5], [10, 20, 30, 40, 50])
check("field-edit-undo-starts-disabled", not v.can_undo)
v.set_marker(SampleMarkerKind.LOOP_START, 2)
check("field-edit-marks-undo-available", v.can_undo)
check("field-edit-applied", v.loop_start == 2)
v.undo()
check("field-edit-undo-reverts-value", v.loop_start == 0)
check("field-edit-undo-does-not-touch-pcm", ints(v.sample_waveform) == [1, 2, 3, 4, 5])

v = new_stereo([1000, 2000, 3000, 4000], [1000, 2000, 3000, 4000])
v.apply_gain_adjust(6.0)
after = ints(v.sample_waveform)
v.set_marker(SampleMarkerKind.SAMPLE_START, 1)
check("chrono-field-edit-on-top-of-pcm-edit", v.sample_start == 1)
check("chrono-pcm-unchanged-by-field-edit", ints(v.sample_waveform) == after)
v.undo()
check("chrono-first-undo-reverts-field-edit-only", v.sample_start == 0)
check("chrono-first-undo-keeps-pcm-edit", ints(v.sample_waveform) == after)
v.undo()
check("chrono-second-undo-reverts-pcm-edit", ints(v.sample_waveform) == [1000, 2000, 3000, 4000])

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
