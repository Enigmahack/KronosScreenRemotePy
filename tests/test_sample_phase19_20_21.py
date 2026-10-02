"""Port of C# SamplePhase19/20/21SelfTests: Split-mode channel move (pad/trim, clamp, undo/redo, partner pending),
live loop-bounds retarget, Link-to-existing-sample stub (SNO1/SMF1, refusals, field edits keep it a stub)."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("KRONOS_DATA_DIR", tempfile.mkdtemp(prefix="kr_data_"))
import numpy as np

from Core.sample_editor_model.model import SampleEditorModel
from Core.sample_editor_model.tree import enumerate_nodes
from Core.sample_import_builder import create_stereo_multisample_pair
from Core.sample_link_resolver import resolve
from Core.sample_playback import LoopingProvider
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


def eq(a, b):
    return np.array_equal(np.asarray(a), np.asarray(b))


def read(p):
    with open(p, "rb") as f:
        return f.read()


# ================= Phase 19
root = tempfile.mkdtemp(prefix="kr_phase19_")
ksc = os.path.join(root, "MoveTest.KSC")
os.makedirs(os.path.join(root, "MoveTest"))
coll = KscCollection(path=ksc)
coll.save(ksc)
left, lp, right, rp = create_stereo_multisample_pair(coll, ksc, "MoveTest", 0)


def audio(seed):
    return (np.arange(100) + seed).astype(np.int16)


def write_real_zone(k, kmp_path, a):
    fn = k.next_ksf_filename()
    k.zones.append(KmpZone(filename=fn, original_key=36, top_key=36))
    d = os.path.join(os.path.dirname(kmp_path), os.path.splitext(os.path.basename(kmp_path))[0])
    os.makedirs(d, exist_ok=True)
    s = KsfSample(name="MoveTest", sample_rate=44100)
    s.set_samples(a)
    s.sample_start, s.loop_start, s.loop_end, s.flags = 5, 10, 90, 0
    s.save(os.path.join(d, fn))


la, ra = audio(1000), audio(2000)
write_real_zone(left, lp, la)
write_real_zone(right, rp, ra)
left.save(lp)
right.save(rp)
coll.save(ksc)

m = SampleEditorModel()
m.open_collection(ksc)
lnode = next((c for c in m.roots[0].children if c.multisample_ref[1] == lp), None)
zn = lnode.children[0] if lnode is not None and lnode.children else None
check("fixture-left-zone-node-found", zn is not None)
m.select_node(zn)
check("fixture-stereo-pair-resolved", m.has_stereo_pair)


def self_markers(label):
    check(f"{label}-self-markers-unchanged", (m.sample_start, m.loop_start, m.loop_end) == (5, 10, 90))


def partner_markers(label):
    check(f"{label}-partner-markers-unchanged",
          (m.partner_sample_start, m.partner_loop_start, m.partner_loop_end) == (5, 10, 90))


m.split_lr = False
before = m.sample_frame_count
m.apply_channel_move(False, 10)
check("combine-mode-move-is-a-no-op", m.sample_frame_count == before)

m.split_lr = True
of, op = m.sample_frame_count, len(m.partner_sample_waveform)
self_markers("baseline")
m.apply_channel_move(False, 7)
check("self-pad-grows-by-delta", m.sample_frame_count == of + 7)
check("self-pad-leaves-partner-untouched", len(m.partner_sample_waveform) == op)
self_markers("self-pad")
w = m.sample_waveform
check("self-pad-content-leading-silence", not np.any(w[:7]))
check("self-pad-content-original-audio-preserved", eq(w[7:], la))
m.apply_channel_move(False, -4)
check("self-partial-trim-shrinks-by-delta", m.sample_frame_count == of + 3)
self_markers("self-partial-trim")
m.apply_channel_move(False, -100)
check("self-remaining-trim-clamps-to-available-padding", m.sample_frame_count == of)
self_markers("self-remaining-trim")
check("self-fully-trimmed-content-byte-identical-to-original", eq(m.sample_waveform, la))
n = m.sample_frame_count
m.apply_channel_move(False, -1)
check("self-trim-past-zero-padding-is-a-hard-stop", m.sample_frame_count == n)
check("self-hard-stop-content-still-byte-identical", eq(m.sample_waveform, la))
check("self-hard-stop-leaves-partner-untouched", len(m.partner_sample_waveform) == op)
m.undo(); m.undo(); m.undo()
check("self-section-fully-unwound", m.sample_frame_count == of)
self_markers("post-undo")
m.redo()
check("redo-self-pad-reapplies", m.sample_frame_count == of + 7)
m.undo()
check("partner-baseline-untouched", len(m.partner_sample_waveform) == op)
partner_markers("partner-baseline")

m.apply_channel_move(True, 11)
check("partner-pad-leaves-self-untouched", m.sample_frame_count == of)
check("partner-pad-grows-by-delta", len(m.partner_sample_waveform) == op + 11)
partner_markers("partner-pad")
pw = m.partner_sample_waveform
check("partner-pad-content-leading-silence", not np.any(pw[:11]))
check("partner-pad-content-original-audio-preserved", eq(pw[11:], ra))
m.apply_channel_move(True, -100)
check("partner-trim-clamps-to-available-padding-not-buffer-length", len(m.partner_sample_waveform) == op)
check("partner-trim-content-byte-identical-to-original", eq(m.partner_sample_waveform, ra))
check("partner-trim-leaves-self-untouched", m.sample_frame_count == of)
m.undo(); m.undo()
check("partner-section-fully-unwound", len(m.partner_sample_waveform) == op)
check("undo-partner-section-self-still-untouched", m.sample_frame_count == of)
m.redo()
check("redo-partner-pad-reapplies", len(m.partner_sample_waveform) == op + 11)
m.redo()
check("redo-partner-trim-reapplies", len(m.partner_sample_waveform) == op)
check("partner-edit-marks-partner-path-pending-save",
      m.try_get_pending_sample_info(right.zones[0].ksf_path(rp)) is not None)

# ================= Phase 20
p = LoopingProvider(np.arange(100, dtype=np.int16), None, 44100, 0, 0, 20, False)


def read_frame():
    return int(p.read(1).reshape(-1)[0])


for _ in range(25):
    read_frame()
p.update_loop_bounds(50, 70)
prev, wrap = -1, -1
for _ in range(200):
    if wrap >= 0:
        break
    f = read_frame()
    if prev >= 0 and f < prev:
        wrap = f
    prev = f
check("loop-bounds-update-lands-on-new-start", wrap == 50)
check("loop-bounds-update-stays-in-new-range", all(50 <= read_frame() < 70 for _ in range(40)))

# ================= Phase 21
root = tempfile.mkdtemp(prefix="kr_phase21_")
ksc = os.path.join(root, "LinkTest.KSC")
d = os.path.join(root, "LinkTest", "LinkTest")
os.makedirs(d)
KscCollection(entries=["LinkTest.KMP"]).save(ksc)
kmp_path = os.path.join(root, "LinkTest", "LinkTest.KMP")
k = KmpMultisample(name="LinkTest", mno1=1)
k.zones += [KmpZone(filename="MS001000.KSF", original_key=60, top_key=60),
            KmpZone(filename="MS001001.KSF", original_key=61, top_key=61)]
k.save(kmp_path)
real_path, ph_path = os.path.join(d, "MS001000.KSF"), os.path.join(d, "MS001001.KSF")
real = KsfSample(name="RealSrc", sample_rate=44100, sno1=42, flags=0x01, sample_start=5, loop_start=10, loop_end=40)
real.set_samples((np.arange(50) * 100).astype(np.int16))
real.save(real_path)
KsfSample(name="Placeholder", sample_rate=44100, sno1=99).save(ph_path)


def zone_of(model, fn):
    n = next(n for n in enumerate_nodes(model.roots) if n.zone_ref is not None and n.zone_ref[0].filename.upper() == fn)
    return n


m = SampleEditorModel()
m.open_collection(ksc)
zone1 = zone_of(m, "MS001001.KSF").zone_ref[0]
check("link-reports-success", m.link_existing_ksf_to_zone(zone1, real_path) is not None)
linked = KsfSample.open(read(ph_path))
check("link-file-still-readable", linked is not None)
check("link-is-header-only", linked.is_header_only)
check("link-sno1-matches-source", linked.sno1 == real.sno1)
check("link-smf1-present-and-correct", linked.stub_target_filename == "MS001000.KSF")
check("link-own-loop-fields-seeded-from-source",
      (linked.sample_start, linked.loop_start, linked.loop_end) == (real.sample_start, real.loop_start, real.loop_end))
check("link-name-matches-source", linked.name == real.name)
check("link-never-omits-smf1", linked.stub_target_filename is not None)
res = resolve(linked, kmp_path)
check("link-resolves-via-resolver", res is not None)
check("link-resolver-finds-real-pcm", res is not None and eq(res.sample.samples(), real.samples()))
check("link-resolver-sno1-verified", res is not None and res.sno1_verified)

m.open_collection(ksc)
z1 = zone_of(m, "MS001001.KSF").zone_ref[0]
check("link-refuses-linking-to-a-link", m.link_existing_ksf_to_zone(z1, ph_path) is None)
z0 = zone_of(m, "MS001000.KSF").zone_ref[0]
check("link-refuses-self-link", m.link_existing_ksf_to_zone(z0, real_path) is None)
check("link-refused-self-link-left-source-untouched", not KsfSample.open(read(real_path)).is_header_only)

long_path = os.path.join(d, "DirtyBit-L.KSF")
ls = KsfSample(name="DirtyBit", suffix="-L", sample_rate=44100, sno1=7)
ls.set_samples(pcm([1, 2, 3, 4, 5]))
ls.save(long_path)
z1 = zone_of(m, "MS001001.KSF").zone_ref[0]
check("link-refuses-filename-too-long-for-smf1", m.link_existing_ksf_to_zone(z1, long_path) is None)
check("link-refusal-message-names-the-file", "DirtyBit-L.KSF" in m.status_text)
sl = KsfSample.open(read(ph_path))
check("link-refused-long-name-left-zone-file-untouched", sl.is_header_only and sl.sno1 == real.sno1)

m.open_collection(ksc)
m.select_node(zone_of(m, "MS001001.KSF"))
check("edit-linked-stub-is-recognized-as-linked", m.sample_is_linked_stub)
check("edit-linked-stub-shows-resolved-frame-count", m.sample_frame_count == real.frame_count)
m.set_loop_tune(5)
check("edit-linked-stub-loop-tune-committed", m.sample_loop_tune == 5)
m.set_reversed(True)
check("edit-linked-stub-reverse-committed", m.sample_reverse_enabled)
check("edit-linked-stub-marked-dirty", m.has_unsaved_changes)
m.save_all_changes()
af = KsfSample.open(read(ph_path))
check("edit-linked-stub-still-header-only-after-field-edits", af.is_header_only)
check("edit-linked-stub-sno1-unchanged-after-field-edits", af.sno1 == real.sno1)
check("edit-linked-stub-smf1-unchanged-after-field-edits", af.stub_target_filename == "MS001000.KSF")

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
