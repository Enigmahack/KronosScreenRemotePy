"""Port of C# SamplePhase13SelfTests: the bug-sweep regressions (navigate keeps edits, stereo save writes both
halves, cut/paste mirror, zone edits mirror + undo both halves, marker clamp on shorten, exact dirty tracking,
no-op commits, pending-sibling add zone, prefix-scoped unload, tempo clamp, positional stereo fallback,
Delete Zone / Remove Sample, last-zone refusal, stale selection after undo)."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("KRONOS_DATA_DIR", tempfile.mkdtemp(prefix="kr_data_"))
import numpy as np

from Core.sample_editor_model.model import SampleEditorModel
from Core.sample_editor_model.markers import SampleMarkerKind
from Core.sample_editor_model.edit import MIN_TEMPO_RATIO
from Core.sample_import_builder import create_stereo_multisample_pair
from Data.ksc_collection import KscCollection
from Data.kmp_multisample import KmpMultisample, KmpZone
from Data.ksf_sample import KsfSample

fails = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


scratch = tempfile.mkdtemp(prefix="kr_phase13_")


def write_ksf(kmp_path, filename, seed=None, frames=200):
    d = os.path.join(os.path.dirname(kmp_path), os.path.splitext(os.path.basename(kmp_path))[0])
    os.makedirs(d, exist_ok=True)
    ks = KsfSample(name=os.path.splitext(filename)[0], sample_rate=44100)
    ks.set_samples((np.arange(frames) + seed).astype(np.int16) if seed is not None else np.zeros(frames, np.int16))
    ks.save(os.path.join(d, filename))


def ksf_path(kmp_path, filename):
    return os.path.join(os.path.dirname(kmp_path), os.path.splitext(os.path.basename(kmp_path))[0], filename)


def open_ksf(path):
    with open(path, "rb") as f:
        return KsfSample.open(f.read())


def open_kmp(path):
    with open(path, "rb") as f:
        return KmpMultisample.open(f.read())


def make_fixture(name):
    d = os.path.join(scratch, name)
    os.makedirs(os.path.join(d, name))
    ksc_path = os.path.join(d, f"{name}.KSC")
    coll = KscCollection(path=ksc_path)
    coll.save(ksc_path)
    left, lp, right, rp = create_stereo_multisample_pair(coll, ksc_path, "Pair", 20)
    left.zones += [KmpZone(filename="MS020000.KSF", original_key=0, top_key=60),
                   KmpZone(filename="MS020001.KSF", original_key=61, top_key=127)]
    right.zones += [KmpZone(filename="MS021000.KSF", original_key=0, top_key=60),
                    KmpZone(filename="MS021001.KSF", original_key=61, top_key=127)]
    left.save(lp)
    right.save(rp)
    write_ksf(lp, "MS020000.KSF", 1000)
    write_ksf(lp, "MS020001.KSF", 2000)
    write_ksf(rp, "MS021000.KSF", 5000)
    write_ksf(rp, "MS021001.KSF", 6000)
    m = SampleEditorModel()
    m.open_collection(ksc_path)
    return m, lp, rp, ksc_path


def ms_node(m, kmp_path, root=0):
    return next(c for c in m.roots[root].children if c.multisample_ref is not None and c.multisample_ref[1] == kmp_path)


def zone_node(m, kmp_path, index):
    return ms_node(m, kmp_path).children[index]


def ms_obj(m, kmp_path):
    return ms_node(m, kmp_path).multisample_ref[0]


def ints(w):
    return [int(x) for x in w]


# 1. Unsaved sample edits survive navigating away and back
m, lp, rp, _ = make_fixture("Navigate")
m.select_node(zone_node(m, lp, 0))
m.split_lr = True
orig = m.sample_frame_count
m.selection_start_frame, m.selection_end_frame = 0, 50
m.apply_crop()
cropped = m.sample_frame_count
check("navigate-crop-actually-shortened", cropped == 50 and cropped != orig)
check("navigate-crop-marks-unsaved", m.has_unsaved_changes)
m.select_node(zone_node(m, lp, 1))
m.select_node(zone_node(m, lp, 0))
check("navigate-edit-survives-round-trip", m.sample_frame_count == cropped)
check("navigate-still-unsaved-after-round-trip", m.has_unsaved_changes)
m.save_selected_sample()
check("navigate-save-clears-unsaved", not m.has_unsaved_changes)
check("navigate-edit-persisted-to-disk", open_ksf(ksf_path(lp, "MS020000.KSF")).frame_count == cropped)

# 2. Stereo Combine save writes BOTH channels
m, lp, rp, _ = make_fixture("StereoSave")
m.select_node(zone_node(m, lp, 0))
check("stereosave-partner-resolved", m.has_stereo_pair and not m.split_lr)
m.selection_start_frame, m.selection_end_frame = 0, 40
m.apply_crop()
m.save_selected_sample()
sl, sr = open_ksf(ksf_path(lp, "MS020000.KSF")), open_ksf(ksf_path(rp, "MS021000.KSF"))
check("stereosave-left-written", sl.frame_count == 40)
check("stereosave-right-written-too", sr.frame_count == 40)
check("stereosave-channels-same-length", sl.frame_count == sr.frame_count)
check("stereosave-clears-unsaved", not m.has_unsaved_changes)

# 3. Cut and Paste mirror onto the partner
m, lp, rp, _ = make_fixture("StereoCut")
m.select_node(zone_node(m, lp, 0))
check("stereocut-partner-resolved", m.has_stereo_pair)
m.selection_start_frame, m.selection_end_frame = 10, 30
m.cut_selection()
check("stereocut-primary-shortened", m.sample_frame_count == 180)
check("stereocut-partner-shortened-too", len(m.partner_sample_waveform) == 180)
m.selection_start_frame = m.selection_end_frame = 0
m.paste_at_selection()
check("stereocut-paste-lengthens-primary", m.sample_frame_count == 200)
check("stereocut-paste-lengthens-partner-too", len(m.partner_sample_waveform) == 200)

# 4. Zone key-range edits mirror onto the sibling
m, lp, rp, _ = make_fixture("ZoneMirror")
m.select_node(zone_node(m, lp, 0))
check("zonemirror-partner-resolved-before", m.has_stereo_pair)
m.apply_zone_edits(0, 55)
lms, rms = ms_obj(m, lp), ms_obj(m, rp)
check("zonemirror-applyzoneedits-primary", lms.zones[0].top_key == 55)
check("zonemirror-applyzoneedits-sibling", rms.zones[0].top_key == 55)
check("zonemirror-applyzoneedits-status", "both L/R channels" in m.status_text)
m.select_node(zone_node(m, lp, 0))
check("zonemirror-partner-resolves-after-applyzoneedits", m.has_stereo_pair)
m.move_zone_boundary(lms.zones[0], 40)
check("zonemirror-boundary-primary", lms.zones[0].top_key == 40)
check("zonemirror-boundary-sibling", rms.zones[0].top_key == 40)
m.select_node(zone_node(m, lp, 0))
check("zonemirror-partner-resolves-after-boundary", m.has_stereo_pair)
m.reorder_zone(lms.zones[0], lms.zones[1])
check("zonemirror-reorder-same-order-both-sides",
      [z.top_key for z in lms.zones] == [z.top_key for z in rms.zones])
check("zonemirror-reorder-sibling-kept-its-own-filenames", all(z.filename.startswith("MS021") for z in rms.zones))

# 5. Zone undo restores both halves
m, lp, rp, _ = make_fixture("ZoneUndo")
m.select_node(zone_node(m, lp, 0))
lms, rms = ms_obj(m, lp), ms_obj(m, rp)
m.move_zone_boundary(lms.zones[0], 30)
check("zoneundo-edit-applied-both", lms.zones[0].top_key == 30 and rms.zones[0].top_key == 30)
check("zoneundo-can-undo", m.can_undo)
m.undo()
check("zoneundo-primary-restored", lms.zones[0].top_key == 60)
check("zoneundo-sibling-restored-too", rms.zones[0].top_key == 60)
m.redo()
check("zoneundo-redo-reapplies-both", lms.zones[0].top_key == 30 and rms.zones[0].top_key == 30)

# 6. Markers clamp when an edit shortens the buffer
m, lp, rp, _ = make_fixture("Markers")
m.select_node(zone_node(m, lp, 0))
m.set_loop_enabled(True)
m.selection_start_frame, m.selection_end_frame = 100, 190
m.set_loop_from_selection()
check("markers-loop-set", m.loop_end == 190)
m.selection_start_frame, m.selection_end_frame = 0, 50
m.apply_crop()
check("markers-cropped", m.sample_frame_count == 50)
check("markers-loopend-clamped", m.loop_end <= 50)
check("markers-loopstart-clamped", m.loop_start <= 50)
check("markers-samplestart-clamped", m.sample_start <= 50)
m.save_selected_sample()
check("markers-loopend-in-range-on-disk", open_ksf(ksf_path(lp, "MS020000.KSF")).loop_end <= 50)

# 7. HasUnsavedChanges tracks every pending file
m, lp, rp, _ = make_fixture("DirtyExact")
m.select_node(zone_node(m, lp, 0))
m.split_lr = True
m.apply_zone_edits(0, 50)
check("dirtyexact-zone-edit-marks-unsaved", m.has_unsaved_changes)
m.select_node(zone_node(m, lp, 1))
m.split_lr = True
m.selection_start_frame, m.selection_end_frame = 0, 20
m.apply_crop()
m.save_selected_sample()
check("dirtyexact-zone-edit-still-pending-after-sample-save", m.has_unsaved_changes)
m.save_selected_multisample()
check("dirtyexact-clean-after-both-saves", not m.has_unsaved_changes)

# 8. No-op field commits create no undo step and no dirt
m, lp, rp, _ = make_fixture("NoOp")
m.select_node(zone_node(m, lp, 0))
check("noop-clean-to-start", not m.has_unsaved_changes and not m.can_undo)
m.set_marker(SampleMarkerKind.SAMPLE_START, m.sample_start)
m.set_marker(SampleMarkerKind.LOOP_START, m.loop_start)
m.set_marker(SampleMarkerKind.LOOP_END, m.loop_end)
m.set_loop_enabled(m.sample_loop_enabled)
m.apply_zone_edits(m.zone_original_key, m.zone_top_key)
check("noop-no-undo-steps", not m.can_undo)
check("noop-not-marked-dirty", not m.has_unsaved_changes)
m.set_marker(SampleMarkerKind.SAMPLE_START, 5)
check("noop-real-edit-still-registers", m.can_undo and m.has_unsaved_changes)

# 9. Add Zone mirrors even when the sibling has a PENDING edit
m, lp, rp, _ = make_fixture("PendingSibling")
m.select_node(zone_node(m, lp, 0))
check("pendingsibling-partner-resolved", m.has_stereo_pair)
m.apply_zone_edits(0, 50)
check("pendingsibling-edit-marks-unsaved", m.has_unsaved_changes)
added = m.add_placeholder_zone()
check("pendingsibling-add-succeeded", added == lp)
lms, rms = ms_obj(m, lp), ms_obj(m, rp)
check("pendingsibling-both-halves-same-zone-count", len(lms.zones) == len(rms.zones))
check("pendingsibling-key-ranges-still-in-parity",
      [(z.original_key, z.top_key) for z in lms.zones] == [(z.original_key, z.top_key) for z in rms.zones])
check("pendingsibling-earlier-edit-survived", lms.zones[0].top_key == 50 and rms.zones[0].top_key == 50)
m.select_node(zone_node(m, lp, 0))
check("pendingsibling-partner-still-resolves", m.has_stereo_pair)

# 10. Pending-edit discard is scoped by whole directory, not bare prefix
mf, foo_left, _, foo_ksc = make_fixture("Foo")
_, foobar_left, _, foobar_ksc = make_fixture("FooBar")
mf.open_collection(foobar_ksc)
fb = next(i for i, r in enumerate(mf.roots) if r.collection_ref[1] == foobar_ksc)
mf.select_node(ms_node(mf, foobar_left, fb).children[0])
mf.apply_zone_edits(0, 45)
check("prefixscope-foobar-edit-pending", mf.has_unsaved_changes)
mf.unload_collection(foo_ksc)
check("prefixscope-foobar-edit-survives-foo-unload", mf.has_unsaved_changes)

# 11. Tempo/pitch bounded
m, lp, rp, _ = make_fixture("Tempo")
m.select_node(zone_node(m, lp, 0))
m.apply_tempo_pitch(0.001, 0)
check("tempo-clamped-not-exploded", m.sample_frame_count <= int(200 / MIN_TEMPO_RATIO) + 64)

# 12. Stereo still resolves when keymaps differ (positional fallback)
d = os.path.join(scratch, "Mismatch")
os.makedirs(os.path.join(d, "Mismatch"))
kp = os.path.join(d, "Mismatch.KSC")
coll = KscCollection(path=kp)
coll.save(kp)
left, lp, right, rp = create_stereo_multisample_pair(coll, kp, "Mismatch", 20)
left.zones.append(KmpZone(filename="MS020000.KSF", original_key=0, top_key=60))
right.zones.append(KmpZone(filename="MS021000.KSF", original_key=0, top_key=50))
left.save(lp)
right.save(rp)
write_ksf(lp, "MS020000.KSF", 0, 100)
write_ksf(rp, "MS021000.KSF", 0, 100)
m = SampleEditorModel()
m.open_collection(kp)
m.select_node(zone_node(m, lp, 0))
check("mismatch-no-exact-key-match-exists",
      not any(z.original_key == left.zones[0].original_key and z.top_key == left.zones[0].top_key for z in right.zones))
check("mismatch-stereo-still-resolves-by-position", m.has_stereo_pair)

# 13. Remove Sample soft-skips; Delete Zone removes outright, mirrored, undoable
m, lp, rp, _ = make_fixture("DeleteSkipped")
m.select_node(zone_node(m, lp, 0))
lms, rms = ms_obj(m, lp), ms_obj(m, rp)
before = len(lms.zones)
m.remove_selected_sample()
check("deleteskipped-remove-sample-skips-not-removes", len(lms.zones) == before and m.zone_is_skipped)
m.remove_selected_sample()
check("deleteskipped-remove-sample-refuses-when-already-skipped", "nothing to remove" in m.status_text)
m.save_selected_multisample()
check("deleteskipped-skip-saved-before-removal", not m.has_unsaved_changes)
deleted = m.delete_zone_completely()
check("deleteskipped-delete-zone-removes-from-primary", len(lms.zones) == before - 1)
check("deleteskipped-delete-zone-removes-from-sibling-too", len(rms.zones) == before - 1)
check("deleteskipped-marks-unsaved", m.has_unsaved_changes)
check("deleteskipped-delete-zone-tree-resynced-primary", len(ms_node(m, lp).children) == before - 1)
check("deleteskipped-delete-zone-tree-resynced-sibling", len(ms_node(m, rp).children) == before - 1)
check("deleteskipped-delete-zone-reselect-index-is-n-minus-1", m.last_deleted_zone_index == 0)
check("deleteskipped-delete-zone-kmp-path-returned", deleted is not None)
m.select_node(ms_node(m, lp).children[m.last_deleted_zone_index])
check("deleteskipped-delete-zone-undo-available", m.can_undo)
m.undo()
check("deleteskipped-undo-restores-primary-count", len(lms.zones) == before)
check("deleteskipped-undo-restores-sibling-count", len(rms.zones) == before)
check("deleteskipped-undo-resyncs-tree-primary", len(ms_node(m, lp).children) == before)
check("deleteskipped-undo-resyncs-tree-sibling", len(ms_node(m, rp).children) == before)
m.redo()
check("deleteskipped-redo-removes-again", len(lms.zones) == before - 1)
m.save_selected_multisample()
check("deleteskipped-removal-persisted-primary", len(open_kmp(lp).zones) == before - 1)
check("deleteskipped-removal-persisted-sibling", len(open_kmp(rp).zones) == before - 1)

# 13b. Last zone can't be deleted
m, lp, rp, _ = make_fixture("DeleteLastZone")
lms = ms_obj(m, lp)
m.select_node(zone_node(m, lp, 1))
m.delete_zone_completely()
check("deletelast-trimmed-to-one-zone", len(lms.zones) == 1)
m.select_node(zone_node(m, lp, 0))
refused = m.delete_zone_completely()
check("deletelast-refuses-last-zone", refused is None and len(lms.zones) == 1)
check("deletelast-refusal-message", "Can't delete the last zone" in m.status_text)

# 13c. Undo of a zone deletion leaves no stale selection
m, lp, rp, ksc_p = make_fixture("DeleteUndoStale")
m.select_node(zone_node(m, lp, 0))
m.delete_zone_completely()
m.select_node(ms_node(m, lp).children[m.last_deleted_zone_index])
m.undo()
check("deleteundostale-selection-active-before-unload", m.has_zone_selected)
m.unload_collection(ksc_p)
check("deleteundostale-unload-clears-selection", not m.has_zone_selected)

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
