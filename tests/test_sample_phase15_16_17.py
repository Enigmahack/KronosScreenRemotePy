"""Port of C# SamplePhase15/16/17SelfTests: Save Collection As (pending rename carries to the copy, original
untouched, same-path refused), reverse one-shot/loop providers, playback generation token, pan law."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("KRONOS_DATA_DIR", tempfile.mkdtemp(prefix="kr_data_"))
import numpy as np

from Core.sample_editor_model.model import SampleEditorModel
from Core.sample_playback import LoopingProvider, OneShotProvider, SamplePlayback
from Data.ksc_collection import content_dir_for
from Data.kmp_multisample import KmpMultisample

fails = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def pcm(v):
    return np.array(v, dtype=np.int16)


def flat(a):
    return [int(x) for x in a.reshape(-1)]


def read(p):
    with open(p, "rb") as f:
        return f.read()


# ================= Phase 15
root = tempfile.mkdtemp(prefix="kr_phase15_")
old_ksc, new_ksc = os.path.join(root, "Orig.KSC"), os.path.join(root, "Copy.KSC")
m = SampleEditorModel()
m.new_collection(old_ksc)
m.new_multisample_in_collection("Kick", 0)
initial_kmp = os.path.join(content_dir_for(old_ksc), KmpMultisample.auto_file_name("Kick", 0))
m.select_node(m.roots[0].children[0])
m.rename_selected_multisample("KickRenamed")
check("rename-is-pending-before-save-as", m.has_unsaved_changes)
old_kmp = os.path.join(content_dir_for(old_ksc), "KICKR000.KMP")
check("rename-moved-file-immediately", os.path.exists(old_kmp) and not os.path.exists(initial_kmp))
if os.path.exists(old_kmp):
    original = read(old_kmp)
    m.save_collection_as(new_ksc)
    check("original-kmp-untouched-immediately-after-save-as", read(old_kmp) == original)
    check("active-path-switched-to-new-collection", m.active_collection_path == new_ksc)
    check("old-root-removed-from-tree", all(r.collection_ref[1] != old_ksc for r in m.roots))
    check("new-root-added-to-tree", any(r.collection_ref[1] == new_ksc for r in m.roots))
    nr = next((r for r in m.roots if r.collection_ref[1] == new_ksc), None)
    nms = nr.children[0] if nr is not None and len(nr.children) == 1 else None
    check("new-tree-has-the-multisample", nms is not None)
    if nms is not None:
        m.select_node(nms)
        check("pending-rename-survived-at-new-path", m.current_multisample_name == "KickRenamed")
    new_kmp = os.path.join(content_dir_for(new_ksc), "KICKR000.KMP")
    check("new-kmp-not-renamed-on-disk-until-saved", KmpMultisample.open(read(new_kmp)).name == "Kick")
    m.save_all_changes()
    check("rename-written-to-new-kmp-after-save", KmpMultisample.open(read(new_kmp)).name == "KickRenamed")
    check("original-kmp-still-untouched-after-save", KmpMultisample.open(read(old_kmp)).name == "Kick")
else:
    check("rest-of-phase15-skipped (rename didn't move the file)", False)

m2 = SampleEditorModel()
m2.new_collection(os.path.join(root, "SamePath.KSC"))
m2.save_collection_as(m2.active_collection_path)
check("save-as-same-path-refused", "different file name" in m2.status_text)

# ================= Phase 16
p = OneShotProvider(pcm([10, 11, 12, 13, 14]), None, 44100, start_frame=1, reverse=True)
got = flat(p.read(10))
check("reverse-mono-reads-expected-frame-count", len(got) == 4)
check("reverse-mono-reads-backward-from-end-to-startframe", got == [14, 13, 12, 11])
check("reverse-mono-position-decrements-one-past-bound", p.position_frame == 0)

p = OneShotProvider(pcm([10, 11, 12, 13, 14]), None, 44100, start_frame=0, reverse=True)
got = flat(p.read(5))
check("reverse-mono-exact-buffer-does-not-overrun", len(got) == 5)
check("reverse-mono-exact-buffer-reads-backward", got == [14, 13, 12, 11, 10])

p = OneShotProvider(pcm([1, 2, 3]), pcm([10, 20, 30]), 44100, start_frame=0, reverse=True)
out = p.read(10)
check("reverse-stereo-reads-all-3-frames", len(out) == 3)
check("reverse-stereo-first-frame-is-last-source-frame", list(out[0]) == [3, 30])
check("reverse-stereo-last-frame-is-first-source-frame", list(out[2]) == [1, 10])

p = OneShotProvider(pcm([10, 11, 12, 13, 14]), None, 44100, start_frame=1, reverse=False)
check("forward-mono-unaffected-by-reverse-path", flat(p.read(10)) == [11, 12, 13, 14])

p = LoopingProvider(pcm(range(100, 110)), None, 44100, 0, 6, 9, True)
check("reverse-loop-intro-reads-backward-from-end-to-loopstart",
      flat(p.read(7)) == [109, 108, 107, 106, 108, 107, 106])

pb = SamplePlayback()
g0 = pb.generation
check("generation-stable-with-no-activity", pb.generation == g0)
pb.stop()
g1 = pb.generation
check("stop-changes-generation", g1 != g0)
token = pb.generation
check("token-matches-when-nothing-intervenes", token == pb.generation)
pb.stop()
check("token-goes-stale-after-intervening-activity", token != pb.generation)


# ================= Phase 17: pan law through the real render chain (volume/boost scale both sides equally)
def render_with(left, right, pan):
    pb = SamplePlayback()
    pb._provider = OneShotProvider(pcm(left), None if right is None else pcm(right), 44100)
    pb.pan = pan
    out, _ = pb.render(len(left))
    return out


def close(name, a, b, tol=0.02):
    check(name, abs(a - b) <= tol * max(1.0, abs(b)))


full = render_with([16384] * 4, None, 0)
ref = float(full[0, 0])
check("pan-output-format-is-always-stereo", full.shape[1] == 2)
check("mono-hard-left-fills-left-channel", ref > 0)
close("mono-hard-left-silences-right-channel", float(full[0, 1]), 0.0)
hr = render_with([16384] * 4, None, 127)
close("mono-hard-right-silences-left-channel", float(hr[0, 0]), 0.0)
close("mono-hard-right-fills-right-channel", float(hr[0, 1]), ref)
c = render_with([16384] * 4, None, 64)
close("mono-center-left-near-equal-power", float(c[0, 0]), ref * 0.707, 0.03)
close("mono-center-right-near-equal-power", float(c[0, 1]), ref * 0.707, 0.03)
s_l = render_with([16384] * 2, [-16384] * 2, 0)
close("stereo-hard-left-keeps-left-channel-value", float(s_l[0, 0]), ref)
close("stereo-hard-left-silences-right-channel", float(s_l[0, 1]), 0.0)
s_r = render_with([16384] * 2, [-16384] * 2, 127)
close("stereo-hard-right-silences-left-channel", float(s_r[0, 0]), 0.0)
close("stereo-hard-right-keeps-right-channel-value", float(s_r[0, 1]), -ref)

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
