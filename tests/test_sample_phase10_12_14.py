"""Port of C# SamplePhase10/12/14SelfTests: zone undo/redo + chronology, stereo trim, zone reorder widths,
stereo Add Zone, TopKey floor, placeholder KMP warnings, unload/revert, stereo assign-existing, MNO1 allocation."""
import os, sys, tempfile, wave
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("KRONOS_DATA_DIR", tempfile.mkdtemp(prefix="kr_data_"))
import numpy as np

from Core.sample_editor_model.model import SampleEditorModel
from Core.sample_editor_model.markers import SampleMarkerKind
from Core.sample_editor_model.tree import enumerate_nodes
from Core.sample_import_builder import create_stereo_multisample_pair
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


def ints(w):
    return [int(x) for x in w]


def read(p):
    with open(p, "rb") as f:
        return f.read()


def find_zone(m, fn):
    return next((n for n in enumerate_nodes(m.roots) if n.zone_ref is not None and n.zone_ref[0].filename == fn), None)


def ms_node(m, kmp_path):
    return next(c for c in m.roots[0].children if c.multisample_ref[1] == kmp_path)


def ksf(name, suffix, vals):
    s = KsfSample(name=name, suffix=suffix, sample_rate=44100)
    s.set_samples(pcm(vals))
    return s


# ================= Phase 10
root = tempfile.mkdtemp(prefix="kr_phase10_")
ksc = os.path.join(root, "P10.KSC")
os.makedirs(os.path.join(root, "P10", "P10-L"))
os.makedirs(os.path.join(root, "P10", "P10-R"))
KscCollection(entries=["P10-L.KMP", "P10-R.KMP"]).save(ksc)
for suf, mno, pre in (("-L", 0, "MS000"), ("-R", 1, "MS001")):
    k = KmpMultisample(name="P10", suffix=suf, mno1=mno)
    k.zones += [KmpZone(filename=f"{pre}000.KSF", original_key=48, top_key=59),
                KmpZone(filename=f"{pre}001.KSF", original_key=60, top_key=71),
                KmpZone(filename="SKIPPEDSAMPLE", original_key=72, top_key=90)]
    k.save(os.path.join(root, "P10", f"P10{suf}.KMP"))


def new_vm(left, right):
    for suf, d, files in (("-L", "P10-L", ("MS000000.KSF", "MS000001.KSF")), ("-R", "P10-R", ("MS001000.KSF", "MS001001.KSF"))):
        vals = left if suf == "-L" else right
        ksf("P10", suf, vals).save(os.path.join(root, "P10", d, files[0]))
        ksf("P10b", suf, vals).save(os.path.join(root, "P10", d, files[1]))
    m = SampleEditorModel()
    m.open_collection(ksc)
    m.select_node(find_zone(m, "MS000000.KSF"))
    return m


m = new_vm([1000, 2000, 3000, 4000, 5000], [1000, 2000, 3000, 4000, 5000])
m.use_zero_crossing = False
m.set_marker(SampleMarkerKind.SAMPLE_START, 0)
check("usezero-off-lands-exactly-on-zero", m.sample_start == 0)
m.set_marker(SampleMarkerKind.SAMPLE_START, 3)
check("usezero-off-lands-exactly-on-target", m.sample_start == 3)

m = new_vm([1, 2, 3], [1, 2, 3])
fz = m.current_multisample_zones[0]
check("zone-undo-starts-disabled", not m.can_undo)
m.move_zone_boundary(fz, 55)
check("zone-undo-applies", fz.top_key == 55)
check("zone-undo-marks-available", m.can_undo)
m.undo()
check("zone-undo-reverts", fz.top_key == 59)
check("zone-undo-enables-redo", m.can_redo)
m.redo()
check("zone-redo-reapplies", fz.top_key == 55)

m = new_vm([1, 2, 3], [1, 2, 3])
fz = m.current_multisample_zones[0]
m.move_zone_boundary(fz, 50)
m.select_node(find_zone(m, "MS000001.KSF"))
check("zone-undo-survives-same-multisample-navigation", m.can_undo)
m.undo()
check("zone-undo-still-reverts-after-navigation", fz.top_key == 59)

m = new_vm([1, 2, 3, 4, 5], [1, 2, 3, 4, 5])
fz = m.current_multisample_zones[0]
m.use_zero_crossing = False
m.set_marker(SampleMarkerKind.SAMPLE_START, 2)
m.move_zone_boundary(fz, 50)
m.undo()
check("chrono-first-undo-reverts-zone-only", fz.top_key == 59)
check("chrono-first-undo-keeps-sample-edit", m.sample_start == 2)
m.undo()
check("chrono-second-undo-reverts-sample-edit", m.sample_start == 0)

m = new_vm([0, 0, 0, 0, 0, 1000, 2000, 3000], [0, 0, 500, 600, 700, 800, 900, 1000])
m.apply_silence_trim()
la, ra = ints(m.sample_waveform), ints(m.right_sample_waveform)
check("trim-stereo-shared-start-not-independent", len(la) == 6 and len(ra) == 6)
check("trim-stereo-left-keeps-its-own-silent-tail", la == [0, 0, 0, 1000, 2000, 3000])
check("trim-stereo-right-unchanged-past-shared-start", ra == [500, 600, 700, 800, 900, 1000])

m = new_vm([1, 2, 3], [1, 2, 3])
zones = m.current_multisample_zones
a, b, c = zones[0], zones[1], zones[2]
check("reorder-setup-widths", a.top_key == 59 and b.top_key == 71 and c.top_key == 90)
m.reorder_zone(b, a)
check("reorder-preserves-order-list", zones[0] is b and zones[1] is a and zones[2] is c)
check("reorder-b-keeps-its-own-width-in-new-slot", b.top_key == 11)
check("reorder-a-keeps-its-own-width-in-new-slot", a.top_key == 71)
check("reorder-c-completely-unaffected", c.top_key == 90)
check("reorder-marks-undo-available", m.can_undo)
m.undo()
check("reorder-undo-restores-order", zones[0] is a and zones[1] is b and zones[2] is c)
check("reorder-undo-restores-widths", a.top_key == 59 and b.top_key == 71 and c.top_key == 90)

# ================= Phase 12
root = tempfile.mkdtemp(prefix="kr_phase12_")
cp = os.path.join(root, "Stereo.KSC")
os.makedirs(os.path.join(root, "Stereo"))
coll = KscCollection(path=cp)
coll.save(cp)
left, lp, right, rp = create_stereo_multisample_pair(coll, cp, "StereoKit", 20)
left.zones.append(KmpZone(filename="MS020000.KSF", original_key=0, top_key=127))
right.zones.append(KmpZone(filename="MS021000.KSF", original_key=0, top_key=127))
left.save(lp)
right.save(rp)
for kp, fn in ((lp, "MS020000.KSF"), (rp, "MS021000.KSF")):
    d = os.path.join(os.path.dirname(kp), os.path.splitext(os.path.basename(kp))[0])
    os.makedirs(d, exist_ok=True)
    ksf(os.path.splitext(fn)[0], "", [1, 2, 3, 4, 5]).save(os.path.join(d, fn))
m = SampleEditorModel()
m.open_collection(cp)
m.select_node(ms_node(m, lp).children[0])
check("stereo-add-resolves-partner-before-add", m.has_stereo_pair)
added = m.add_placeholder_zone()
check("stereo-add-reports-success", added == lp)
check("stereo-add-status-mentions-both-channels", "both stereo channels" in m.status_text)
rl, rr = KmpMultisample.open(read(lp)), KmpMultisample.open(read(rp))
check("stereo-add-left-gained-a-zone", len(rl.zones) == 2)
check("stereo-add-right-gained-a-zone", len(rr.zones) == 2)
check("stereo-add-key-ranges-match-exactly",
      rl.zones[0].top_key == rr.zones[0].top_key and rl.zones[1].original_key == rr.zones[1].original_key
      and rl.zones[1].top_key == rr.zones[1].top_key)
check("stereo-add-right-placeholder-is-skipped", rr.zones[1].is_skipped)
m.select_node(ms_node(m, lp).children[0])
check("stereo-partner-still-resolves-after-add", m.has_stereo_pair)

# TopKey floor
fksc = os.path.join(root, "Floor.KSC")
os.makedirs(os.path.join(root, "Floor"))
KscCollection(entries=["Floor.KMP"]).save(fksc)
k = KmpMultisample(name="Floor")
k.zones += [KmpZone(filename="MS000000.KSF", original_key=0, top_key=60), KmpZone(filename="MS000001.KSF", original_key=61, top_key=90)]
k.save(os.path.join(root, "Floor", "Floor.KMP"))
m = SampleEditorModel()
m.open_collection(fksc)
m.select_node(m.roots[0].children[0].children[1])
m.apply_zone_edits(61, 55)
check("topkey-floor-clamped-to-previous-plus-one", m.zone_top_key == 61)
m.apply_zone_edits(61, 95)
check("topkey-above-floor-unclamped", m.zone_top_key == 95)

# placeholder KMP warnings
pksc = os.path.join(root, "Placeholder.KSC")
os.makedirs(os.path.join(root, "Placeholder"))
KscCollection(entries=["NEWMS000.KMP", "NEWMS001.KMP", "RealMissing.KMP"]).save(pksc)
m = SampleEditorModel()
m.open_collection(pksc)
check("newms-placeholder-kmps-not-warned", "NEWMS000" not in m.status_text and "NEWMS001" not in m.status_text)
check("other-missing-kmp-still-warned", "RealMissing" in m.status_text)


def simple(name):
    p = os.path.join(root, f"{name}.KSC")
    os.makedirs(os.path.join(root, name), exist_ok=True)
    KscCollection().save(p)
    return p


kx, ky = simple("UnloadX"), simple("UnloadY")
m = SampleEditorModel()
m.open_collection(kx)
m.open_collection(ky)
check("unload-setup-two-roots", len(m.roots) == 2)
check("unload-active-path-is-most-recent", m.active_collection_path == ky)
m.unload_collection(kx)
check("unload-removes-only-that-root", len(m.roots) == 1 and m.roots[0].collection_ref[1] == ky)
check("unload-of-inactive-collection-keeps-active-path", m.active_collection_path == ky)
m.unload_collection(ky)
check("unload-of-active-collection-clears-it", len(m.roots) == 0 and not m.has_active_collection)


def one_zone(name):
    p = os.path.join(root, f"{name}.KSC")
    os.makedirs(os.path.join(root, name), exist_ok=True)
    KscCollection(entries=[f"{name}.KMP"]).save(p)
    k = KmpMultisample(name=name)
    k.zones.append(KmpZone(filename="MS000000.KSF", original_key=60, top_key=60))
    k.save(os.path.join(root, name, f"{name}.KMP"))
    return p


kr, ko = one_zone("RevertMe"), one_zone("LeaveMeAlone")
m = SampleEditorModel()
m.open_collection(kr)
m.open_collection(ko)
other = next(r for r in m.roots if r.collection_ref[1] == ko)
exp_before = other.is_expanded
m.select_node(next(r for r in m.roots if r.collection_ref[1] == kr).children[0].children[0])
check("revert-active-path-tracks-selection", m.active_collection_path == kr)
m.apply_zone_edits(60, 70)
m.revert_active_collection_changes()
rz = next(r for r in m.roots if r.collection_ref[1] == kr).children[0].children[0]
check("revert-discards-unsaved-topkey-edit", rz.zone_ref[0].top_key == 60)
other2 = next((r for r in m.roots if r.collection_ref[1] == ko), None)
check("revert-leaves-other-collection-in-place", other2 is not None and other2.is_expanded == exp_before)

ka, kb = os.path.join(root, "AllA.KSC"), os.path.join(root, "AllB.KSC")
KscCollection(path=ka).save(ka)
KscCollection(path=kb).save(kb)
m = SampleEditorModel()
m.open_collection(ka)
m.open_collection(kb)
check("revert-all-setup-two-roots", len(m.roots) == 2)
m.revert_all_changes()
check("revert-all-clears-every-root", len(m.roots) == 0)
check("revert-all-clears-active-collection", not m.has_active_collection)
check("revert-all-clears-unsaved-flag", not m.has_unsaved_changes)
check("revert-all-clears-undo", not m.can_undo and not m.can_redo)

# ================= Phase 14
root = tempfile.mkdtemp(prefix="kr_phase14_")
cp = os.path.join(root, "Stereo.KSC")
os.makedirs(os.path.join(root, "Stereo"))
coll = KscCollection(path=cp)
coll.save(cp)
left, lp, right, rp = create_stereo_multisample_pair(coll, cp, "StereoKit", 0)
left.zones.append(KmpZone(filename="SKIPPEDSAMPLE", original_key=0, top_key=127))
right.zones.append(KmpZone(filename="SKIPPEDSAMPLE", original_key=0, top_key=127))
left.save(lp)
right.save(rp)
wav = os.path.join(root, "stereo_source.wav")
with wave.open(wav, "wb") as w:
    w.setnchannels(2); w.setsampwidth(2); w.setframerate(44100)
    w.writeframes(np.stack([np.full(100, 1000, np.int16), np.full(100, -1000, np.int16)], 1).tobytes())
m = SampleEditorModel()
m.open_collection(cp)
written = m.import_samples_to_collection([wav])
check("bug1-import-wrote-two-bare-entries", len(written) == 2)
rpath = next((p for p in written if "-r" in p.lower()), None)
check("bug1-found-R-entry", rpath is not None)
if rpath is not None:
    lz = ms_node(m, lp).multisample_ref[0].zones[0]
    res = m.assign_existing_ksf_to_zone(lz, rpath)
    check("bug1-assign-reported-success", res is not None)
    check("bug1-status-mentions-both-channels", "both channels" in m.status_text)
    rl, rr = KmpMultisample.open(read(lp)), KmpMultisample.open(read(rp))
    lk = KsfSample.open(read(rl.zones[0].ksf_path(lp)))
    rk = KsfSample.open(read(rr.zones[0].ksf_path(rp)))
    check("bug1-left-zone-has-left-audio-not-swapped", bool(np.all(lk.samples() == 1000)))
    check("bug1-right-zone-has-right-audio-not-swapped", bool(np.all(rk.samples() == -1000)))
    check("bug1-left-zone-suffix-is-L", lk.suffix == "-L")
    check("bug1-right-zone-suffix-is-R", rk.suffix == "-R")

mc = os.path.join(root, "Mno1.KSC")
os.makedirs(os.path.join(root, "Mno1"))
KscCollection(path=mc).save(mc)
m = SampleEditorModel()
m.open_collection(mc)
m.new_multisample_in_collection("A", 0)
m.new_multisample_in_collection("B", 1)
an = next(c for c in m.roots[0].children if c.multisample_ref[0].name == "A")
m.select_node(an)
m.delete_selected_multisample()
check("bug2-next-free-mno1-single-still-0", m.next_free_mno1() == 0)
check("bug2-next-free-mno1-pair-skips-past-B", m.next_free_mno1(2) == 2)

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
