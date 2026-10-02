"""Port of C# SamplePhase5SelfTests + SamplePhase6SelfTests: Remove Sample / Delete Zone, import-into-zone repo
cleanup, recent files, multisample export, normalization report, loop provider wrap, note names, _UserBank
refusal, gain effect, clipboard cut/copy/paste/fade/loop-from-selection."""
import os, sys, tempfile, wave
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("KRONOS_DATA_DIR", tempfile.mkdtemp(prefix="kr_data_"))
import numpy as np

from Core.sample_editor_model.model import SampleEditorModel
from Core.sample_editor_model.core import is_user_bank
from Core.sample_editor_model.tree import enumerate_nodes
from Core.sample_dsp import GainAdjustEffect
from Core.sample_support import SampleClipboard
from Core.sample_playback import LoopingProvider
from Utils import midi_note_name as mnn
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


def build(root, name, mno1, zones, samples):
    """zones: [(filename, key)]; samples: {filename: KsfSample}. -> ksc, kmp paths."""
    ksc = os.path.join(root, f"{name}.KSC")
    d = os.path.join(root, name, name)
    os.makedirs(d, exist_ok=True)
    KscCollection(entries=[f"{name}.KMP"]).save(ksc)
    k = KmpMultisample(name=name, mno1=mno1)
    for fn, key in zones:
        k.zones.append(KmpZone(filename=fn, original_key=key, top_key=key))
    kp = os.path.join(root, name, f"{name}.KMP")
    k.save(kp)
    for fn, s in samples.items():
        s.save(os.path.join(d, fn))
    return ksc, kp


def ksf(name, vals, rate=44100):
    s = KsfSample(name=name, sample_rate=rate)
    s.set_samples(pcm(vals))
    return s


root = tempfile.mkdtemp(prefix="kr_phase5_")

# ---- Delete zone flow
ksc, kmp = build(root, "Test", 1, [("MS001000.KSF", 60), ("MS001001.KSF", 61)],
                 {"MS001000.KSF": ksf("S0", [1, 2, 3, 4, 5]), "MS001001.KSF": ksf("S1", [1, 2, 3, 4, 5])})
m = SampleEditorModel()
m.open_collection(ksc)
zn = find_zone(m, "MS001000.KSF")
check("delete-zone-found", zn is not None)
m.select_node(zn)
check("delete-zone-selected-before", m.has_zone_selected and not m.zone_is_skipped)
zone, kp = zn.zone_ref
ksf_path = os.path.join(os.path.dirname(kp), os.path.splitext(os.path.basename(kp))[0], zone.filename)
m.remove_selected_sample()
check("delete-zone-marks-skipped", m.zone_is_skipped)
check("delete-zone-clears-sample-panel", not m.has_sample_loaded)
check("delete-zone-ksf-file-actually-deleted", not os.path.exists(ksf_path))
m.save_selected_multisample()
re = KmpMultisample.open(read(kmp))
check("delete-zone-persisted-as-skipped", re is not None and any(z.is_skipped for z in re.zones))
before = len(re.zones)
check("delete-zone-completely-succeeds", m.delete_zone_completely() is not None)
check("delete-zone-completely-status-mentions-deleted", "Deleted zone" in m.status_text)
m.save_selected_multisample()
check("delete-zone-completely-persisted-removal", len(KmpMultisample.open(read(kmp)).zones) == before - 1)

# ---- Remove Sample after import into a skipped zone
root2 = tempfile.mkdtemp(prefix="kr_phase5r_")
ksc2, kmp2 = build(root2, "RemoveTest", 1, [("SKIPPEDSAMPLE", 60)], {})
wav_path = os.path.join(root2, "source.wav")
with wave.open(wav_path, "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100)
    w.writeframes(pcm([i * 10 for i in range(100)]).tobytes())
m2 = SampleEditorModel()
m2.open_collection(ksc2)
z2 = m2.roots[0].children[0].children[0]
m2.select_node(z2)
kp_after = m2.import_sample_into_zone(z2.zone_ref[0], [wav_path])
check("removesample-import-succeeded", kp_after is not None)
check("removesample-repo-has-entry-after-import", len(list(m2.bare_sample_entries())) == 1)
msn = next(c for c in m2.roots[0].children if c.multisample_ref[1] == kp_after)
m2.select_node(msn.children[m2.last_imported_zone_index])
check("removesample-zone-reselected-with-sample", m2.has_sample_loaded)
zsel, kpsel = m2.selected_zone, kp_after
own = os.path.join(os.path.dirname(kpsel), os.path.splitext(os.path.basename(kpsel))[0], zsel.filename)
check("removesample-own-file-exists-before", os.path.exists(own))
m2.remove_selected_sample()
check("removesample-zone-own-file-deleted", not os.path.exists(own))
check("removesample-repo-entry-deleted", not list(m2.bare_sample_entries()))
check("removesample-status-mentions-repository", "repository" in m2.status_text)
check("removesample-zone-marked-skipped", m2.zone_is_skipped)

# ---- Recent files
from types import SimpleNamespace
m3 = SampleEditorModel(SimpleNamespace(sample_recent_files=[]), lambda st: None)
m3.clear_recent_files()
m3.open_collection(ksc)
rec = m3.get_recent_files()
check("recent-files-has-entry", ksc in rec)
check("recent-files-newest-first", len(rec) > 0 and rec[0] == ksc)
m3.open_collection(ksc)
check("recent-files-no-duplicate", m3.get_recent_files().count(ksc) == 1)
m3.clear_recent_files()
check("recent-files-clears", len(m3.get_recent_files()) == 0)

# ---- Multisample export skips the deleted zone
m4 = SampleEditorModel()
m4.open_collection(ksc)
m4.select_node(find_zone(m4, "MS001001.KSF"))
out = os.path.join(root, "multisample_export_out")
m4.export_selected_multisample_to_folder(out)
check("export-multisample-status-ok", m4.status_text.startswith("Exported"))
check("export-multisample-only-remaining-zone", os.path.exists(os.path.join(out, "S1.wav")))
check("export-multisample-skips-deleted-zone", not os.path.exists(os.path.join(out, "S0.wav")))

# ---- Normalization report
rroot = tempfile.mkdtemp(prefix="kr_phase5n_")
rksc, _ = build(rroot, "R", 9, [("MS009000.KSF", 10), ("MS009001.KSF", 20), ("MS009002.KSF", 30)],
                {"MS009000.KSF": ksf("Normal1", [1, 2, 3]), "MS009001.KSF": ksf("Normal2", [1, 2, 3]),
                 "MS009002.KSF": ksf("Outlier", [1, 2, 3], 22050)})
m5 = SampleEditorModel()
m5.open_collection(rksc)
rep = m5.build_normalization_report()
check("report-entry-count", len(rep) == 3)
check("report-majority-unflagged", all(not e.flagged for e in rep if e.sample_name in ("Normal1", "Normal2")))
check("report-outlier-flagged", next(e for e in rep if e.sample_name == "Outlier").flagged)

# ---- Loop provider wrap + degenerate
p = LoopingProvider(pcm([10, 20, 30, 40, 50]), None, 44100, 1, 1, 4, False)
check("loop-provider-wraps-correctly", ints(p.read(10).reshape(-1)) == [20, 30, 40, 20, 30, 40, 20, 30, 40, 20])
p = LoopingProvider(pcm([10, 20, 30, 40, 50]), None, 44100, 3, 3, 1, False)
check("loop-provider-degenerate-falls-back-to-whole-buffer", ints(p.read(5).reshape(-1)) == [10, 20, 30, 40, 50])

# ---- Phase 6: note names
check("note-c4-is-60", mnn.try_parse("C4") == 60)
check("note-60-is-c4", mnn.to_name(60) == "C4")
check("note-e1", mnn.try_parse("E1") == 28)
check("note-g5", mnn.try_parse("G5") == 79)
check("note-sharp", mnn.try_parse("C#4") == 61)
check("note-flat-equals-sharp-below", mnn.try_parse("Db4") == mnn.try_parse("C#4"))
check("note-negative-octave", mnn.try_parse("C-1") == 0)
check("note-roundtrip-all-127", all(mnn.try_parse(mnn.to_name(n)) == n for n in range(128)))
check("note-garbage-null", mnn.try_parse("not a note") is None)
check("note-empty-null", mnn.try_parse("") is None)
check("note-out-of-range-null", mnn.try_parse("C11") is None)

# ---- _UserBank refusal
ub = os.path.join(root, "SomeBank_UserBank.KSC")
with open(ub, "wb") as f:
    f.write(b"#KORG Script Version 1.0\r\n#v2\r\n")
check("userbank-detected", is_user_bank(ub))
check("userbank-detection-case-insensitive", is_user_bank(os.path.join(root, "x_userbank.ksc")))
check("userbank-normal-ksc-not-flagged", not is_user_bank(os.path.join(root, "Normal.KSC")))
m6 = SampleEditorModel(SimpleNamespace(sample_recent_files=[]), lambda st: None)
m6.open_collection(ub)
check("userbank-open-refused-no-roots", len(m6.roots) == 0)
check("userbank-open-refused-status-message", "_UserBank.KSC" in m6.status_text)
check("userbank-open-refused-not-added-to-recent", ub not in m6.get_recent_files())

# ---- Gain effect
base = pcm([1000, -1000, 16000, -16000])
up = GainAdjustEffect(6.0).apply(base, 44100)
down = GainAdjustEffect(-6.0).apply(base, 44100)
check("gain-up-6db-roughly-doubles", abs(int(up[0]) - 2000) < 50)
check("gain-down-6db-roughly-halves", abs(int(down[0]) - 500) < 50)
check("gain-clamps-at-full-scale", int(GainAdjustEffect(24.0).apply(pcm([30000]), 44100)[0]) == 32767)

# ---- Clipboard + fade + loop-from-selection
croot = tempfile.mkdtemp(prefix="kr_phase6_")
cksc, _ = build(croot, "Clip", 1, [("MS001000.KSF", 60)], {"MS001000.KSF": ksf("S", list(range(10)))})
m7 = SampleEditorModel()
m7.open_collection(cksc)
m7.select_node(find_zone(m7, "MS001000.KSF"))
orig = ints(m7.sample_waveform)
m7.selection_start_frame, m7.selection_end_frame = 2, 5
m7.copy_selection()
check("copy-captures-exact-range", ints(SampleClipboard.pcm) == [2, 3, 4])
check("copy-is-non-destructive", ints(m7.sample_waveform) == orig)
m7.cut_selection()
check("cut-shrinks-sample", ints(m7.sample_waveform) == [0, 1, 5, 6, 7, 8, 9])
check("cut-clears-selection", m7.selection_end_frame <= m7.selection_start_frame)
m7.undo()
check("cut-undo-restores-original", ints(m7.sample_waveform) == orig)
m7.selection_start_frame, m7.selection_end_frame = 0, 2
m7.paste_at_selection()
check("paste-replaces-selection", ints(m7.sample_waveform) == [2, 3, 4, 2, 3, 4, 5, 6, 7, 8, 9])
m7.undo()
check("paste-undo-restores-original", ints(m7.sample_waveform) == orig)
m7.selection_start_frame = m7.selection_end_frame = 3
m7.paste_at_selection()
check("paste-inserts-at-cursor", ints(m7.sample_waveform) == [0, 1, 2, 2, 3, 4, 3, 4, 5, 6, 7, 8, 9])
m7.undo()
m7.selection_start_frame, m7.selection_end_frame = 4, 9
m7.apply_fade_in_selection()
f = ints(m7.sample_waveform)
check("fade-in-selection-leaves-outside-untouched", f[0] == 0 and f[1] == 1 and f[2] == 2 and f[9] == 9)
check("fade-in-selection-starts-near-zero", f[4] == 0)
check("fade-in-selection-ends-at-original", f[8] == 8)
m7.undo()
m7.selection_start_frame, m7.selection_end_frame = 2, 7
m7.set_loop_from_selection()
check("loop-from-selection-start", m7.loop_start == 2)
check("loop-from-selection-end", m7.loop_end == 7)

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
