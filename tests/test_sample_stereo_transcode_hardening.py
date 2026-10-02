"""Port of C# SampleStereoSelfTests + SampleTranscodeSelfTests + SampleTreeSelectionSelfTests +
ReviewHardeningSelfTests: stereo import/pair builder/sibling lookup, mono/stereo transcode + resample, import
builder + export, selection cleared by a tree rebuild, RIFF chunk hardening, SNO1 allocation, path guard."""
import os, sys, tempfile, wave
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("KRONOS_DATA_DIR", tempfile.mkdtemp(prefix="kr_data_"))
import numpy as np

from Core import audio_import as ai
from Core.sample_editor_model.model import SampleEditorModel
from Core.sample_editor_model.tree import enumerate_nodes
from Core.sample_export import export_sample_to_wav, export_collection
from Core.sample_import_builder import (add_sample_zone, add_stereo_sample_zone_pair,
                                        create_stereo_multisample_pair, find_stereo_sibling_on_disk)
from Data import korg_riff_chunk as riff
from Data.ksc_collection import KscCollection, Sno1Allocator, next_free_sno1
from Data.kmp_multisample import KmpMultisample, KmpZone
from Data.ksf_sample import KsfSample
from Data.sample_path_guard import ensure_under

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


root = tempfile.mkdtemp(prefix="kr_stereo_tc_")


def write_wav(name, interleaved, rate, channels):
    p = os.path.join(root, name)
    with wave.open(p, "wb") as w:
        w.setnchannels(channels); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(pcm(interleaved).tobytes())
    return p


# ---- stereo import
l, r = ai.import_stereo_to_lr_44100(write_wav("s.wav", [10000, -10000, 10000, -10000], 44100, 2))
check("stereo-import-frame-count", len(l) == 2 and len(r) == 2)
check("stereo-import-left-channel", bool(np.all(l == 10000)))
check("stereo-import-right-channel", bool(np.all(r == -10000)))
mono = [1234, -5678, 999]
l, r = ai.import_stereo_to_lr_44100(write_wav("m.wav", mono, 44100, 1))
check("mono-to-stereo-left-matches-source", eq(l, mono))
check("mono-to-stereo-right-duplicates-left", eq(r, l))

# ---- transcode
orig = [100, -200, 300, -400, 32767, -32768]
res = ai.import_to_mono_44100(write_wav("p.wav", orig, 44100, 1))
check("import-mono-passthrough-length", len(res) == len(orig))
check("import-mono-passthrough-values", eq(res, orig))
res = ai.import_to_mono_44100(write_wav("d.wav", [10000, -10000, 20000, 20000, -30000, -30000], 44100, 2))
check("import-stereo-downmix-frame-count", len(res) == 3)
check("import-stereo-downmix-first-frame-near-zero", len(res) > 0 and abs(int(res[0])) < 50)
check("import-stereo-downmix-second-frame", len(res) > 1 and int(res[1]) == 20000)
check("import-stereo-downmix-third-frame", len(res) > 2 and int(res[2]) == -30000)
sine = (1000 * np.sin(2 * np.pi * 440 * np.arange(2205) / 22050.0)).astype(np.int16)
res = ai.import_to_mono_44100(write_wav("r.wav", sine, 22050, 1))
check("import-resample-roughly-doubles-length", 3500 < len(res) < 5500)

# ---- stereo pair builder
croot = os.path.join(root, "stereo")
os.makedirs(os.path.join(croot, "StereoTest"))
cp = os.path.join(croot, "StereoTest.KSC")
coll = KscCollection(path=cp)
coll.save(cp)
left, lp, right, rp = create_stereo_multisample_pair(coll, cp, "MyStereoKit", 10)
check("stereo-pair-same-name", left.name == "MyStereoKit" and right.name == "MyStereoKit")
check("stereo-pair-suffixes", left.suffix == "-L" and right.suffix == "-R")
check("stereo-pair-mno1-adjacent", left.mno1 == 10 and right.mno1 == 11)
check("stereo-pair-kmp-files-written", os.path.exists(lp) and os.path.exists(rp))
check("stereo-pair-filename-no-suffix", "-" not in os.path.basename(lp) and "-" not in os.path.basename(rp))
check("stereo-pair-filename-convention", os.path.basename(lp) == "MYSTE010.KMP" and os.path.basename(rp) == "MYSTE011.KMP")
check("stereo-pair-added-to-collection-entries",
      os.path.basename(lp) in coll.entries and os.path.basename(rp) in coll.entries)
re_ = KscCollection.open(read(cp))
check("stereo-pair-collection-persisted", os.path.basename(lp) in re_.entries and os.path.basename(rp) in re_.entries)

fl, fl_path = find_stereo_sibling_on_disk(coll, left, lp)
check("sibling-from-left-finds-right", fl is not None and fl.name == right.name and fl.suffix == right.suffix and fl_path == rp)
fr, fr_path = find_stereo_sibling_on_disk(coll, right, rp)
check("sibling-from-right-finds-left", fr is not None and fr.name == left.name and fr.suffix == left.suffix and fr_path == lp)
ns, nsp = find_stereo_sibling_on_disk(coll, KmpMultisample(name="Solo", suffix=""), lp)
check("sibling-none-for-non-lr-suffix", ns is None and nsp is None)
lone, _ = find_stereo_sibling_on_disk(coll, KmpMultisample(name="NobodyElse", suffix="-L"), lp)
check("sibling-none-for-unmatched-name", lone is None)

lz, rz = add_stereo_sample_zone_pair(left, lp, right, rp, "Snare Hit", pcm([111, 222, 333]), pcm([444, 555, 666]), 44100, 50, 55)
check("stereo-zone-same-key-range", (lz.original_key, lz.top_key, rz.original_key, rz.top_key) == (50, 55, 50, 55))
lk, rk = KsfSample.open(read(lz.ksf_path(lp))), KsfSample.open(read(rz.ksf_path(rp)))
check("stereo-zone-left-ksf-readable", lk is not None)
check("stereo-zone-right-ksf-readable", rk is not None)
check("stereo-zone-left-name-suffix", lk.name == "Snare Hit" and lk.suffix == "-L")
check("stereo-zone-right-name-suffix", rk.name == "Snare Hit" and rk.suffix == "-R")
check("stereo-zone-left-pcm", eq(lk.samples(), [111, 222, 333]))
check("stereo-zone-right-pcm", eq(rk.samples(), [444, 555, 666]))
check("stereo-zone-sno1-distinct", lk.sno1 != rk.sno1)
lb, rb = left.to_bytes(), right.to_bytes()
check("stereo-msp1-tail-is-zone-count-left", lb[24] == 1 and lb[25] == 0)
check("stereo-msp1-tail-is-zone-count-right", rb[24] == 1 and rb[25] == 0)

# ---- import builder + export
tdir = os.path.join(root, "tc")
os.makedirs(tdir)
kp = os.path.join(tdir, "Test.KMP")
m = KmpMultisample(name="Test", mno1=3)
m.zones += [KmpZone(filename="MS003000.KSF", original_key=40, top_key=40), KmpZone(filename="MS003001.KSF", original_key=80, top_key=80)]
z = add_sample_zone(m, kp, "New Sound", pcm([1, 2, 3, 4]), 44100, 60, 60)
check("import-builder-filename-convention", z.filename == "MS003002.KSF")
check("import-builder-sorted-insert", next(i for i, q in enumerate(m.zones) if q is z) == 1)
check("import-builder-ksf-written", os.path.exists(z.ksf_path(kp)))
w = KsfSample.open(read(z.ksf_path(kp)))
check("import-builder-ksf-readable", w is not None)
check("import-builder-ksf-samples", w is not None and eq(w.samples(), [1, 2, 3, 4]))

wav = os.path.join(tdir, "export_test.wav")
s = KsfSample(name="Export Test", sample_rate=44100)
s.set_samples(pcm([1000, -1000, 2000, -2000, 32767, -32768]))
check("export-reports-success", export_sample_to_wav(s, wav))
check("export-file-written", os.path.exists(wav))
check("export-round-trip-bit-exact", eq(ai.import_to_mono_44100(wav), s.samples()))
hp = os.path.join(tdir, "header_only.wav")
check("export-refuses-header-only", not export_sample_to_wav(KsfSample(name="Empty"), hp))
check("export-refuses-header-only-no-file", not os.path.exists(hp))

cr = os.path.join(tdir, "coll")
os.makedirs(os.path.join(cr, "Coll", "Coll"))
kscp = os.path.join(cr, "Coll.KSC")
KscCollection(entries=["Coll.KMP"]).save(kscp)
k = KmpMultisample(name="Coll", mno1=5)
k.zones += [KmpZone(filename="MS005000.KSF", original_key=40, top_key=40),
            KmpZone(filename="MS005001.KSF", original_key=60, top_key=60),
            KmpZone(filename="SKIPPEDSAMPLE", original_key=80, top_key=80)]
k.save(os.path.join(cr, "Coll", "Coll.KMP"))
rs = KsfSample(name="RealVoice")
rs.set_samples(pcm([1, 2, 3]))
rs.save(os.path.join(cr, "Coll", "Coll", "MS005000.KSF"))
KsfSample(name="EmptyVoice").save(os.path.join(cr, "Coll", "Coll", "MS005001.KSF"))
out = os.path.join(cr, "export_out")
exported, skipped = export_collection(KscCollection.open(read(kscp)), kscp, out)
check("export-collection-exports-one", exported == 1)
check("export-collection-skips-header-only", skipped == 1)
check("export-collection-named-after-sample", os.path.exists(os.path.join(out, "RealVoice.wav")))

# ---- tree selection cleared by rebuild
tr = os.path.join(root, "tree")
os.makedirs(os.path.join(tr, "Test", "Test"))
tk = os.path.join(tr, "Test.KSC")
KscCollection(entries=["Test.KMP"]).save(tk)
k = KmpMultisample(name="Test", mno1=1)
k.zones.append(KmpZone(filename="MS001000.KSF", original_key=60, top_key=60))
k.save(os.path.join(tr, "Test", "Test.KMP"))
s1 = KsfSample(name="S1", sample_rate=44100)
s1.set_samples(pcm([1, 2, 3, 4, 5]))
s1.save(os.path.join(tr, "Test", "Test", "MS001000.KSF"))
mm = SampleEditorModel()
mm.open_collection(tk)
zn = next((n for n in enumerate_nodes(mm.roots) if n.zone_ref is not None and n.zone_ref[0].filename == "MS001000.KSF"), None)
check("setup-zone-found", zn is not None)
mm.select_node(zn)
check("setup-zone-selected", mm.has_zone_selected and mm.has_sample_loaded)
mm.new_multisample_in_collection("Other", 2)
check("rebuild-clears-zone-selection", not mm.has_zone_selected)
check("rebuild-clears-sample-selection", not mm.has_sample_loaded)
mm.save_selected_multisample()
check("rebuild-then-save-refuses-not-silently-succeeds", "No multisample selected" in mm.status_text)

# ---- RIFF hardening
data = bytearray(16)
data[0:4] = b"SMP1"
riff.write_u32be(data, 4, 0xFFFFFFF8)
chunks = riff.read_chunks(bytes(data))
check("riff-zero-step-terminates", len(chunks) >= 1)
check("riff-zero-step-payload-clamped", len(chunks[0][1]) <= 8)
data = bytearray(16)
data[0:4] = b"SMP1"
riff.write_u32be(data, 4, 0x80000000)
try:
    riff.read_chunks(bytes(data)); threw = False
except Exception:
    threw = True
check("riff-huge-length-does-not-throw", not threw)
payload = bytes([1, 2, 3, 4])
chunks = riff.read_chunks(riff.build_chunk("AAAA", payload) + riff.build_chunk("BBBB", payload))
check("riff-wellformed-unchanged", len(chunks) == 2 and chunks[0][0] == "AAAA" and chunks[1][0] == "BBBB" and chunks[1][1] == payload)

# ---- SNO1 allocation
hd = os.path.join(root, "harden")
os.makedirs(hd)
hk = KsfSample(name="HARDEN", sample_rate=44100, flags=0x81, sno1=41)
hk.set_samples(pcm([1, 2, 3, 4, 5, 6, 7, 8]))
hp_ = os.path.join(hd, "HARDEN.KSF")
hk.save(hp_)
check("readsno1-matches-full-open", KsfSample.read_sno1(hp_) == 41)
check("readsno1-matches-reopen", KsfSample.open(read(hp_)).sno1 == 41)
check("readsno1-null-for-non-ksf", KsfSample.read_sno1(os.path.join(hd, "nope.KSF")) is None)
check("nextfree-seeds-past-disk", next_free_sno1(hd) == 42)
al = Sno1Allocator(hd)
a, b, c = al.next(), al.next(), al.next()
check("allocator-seeded-past-disk", a == 42)
check("allocator-distinct-consecutive", b == 43 and c == 44)

# ---- path guard
gr = os.path.join(tempfile.gettempdir(), "kronos_guard_root")


def rejects(name):
    try:
        ensure_under(gr, os.path.join(gr, name), name)
        return False
    except IOError:
        return True


def accepts(name):
    try:
        return len(ensure_under(gr, os.path.join(gr, name), name)) > 0
    except IOError:
        return False


check("guard-rejects-parent-escape", rejects(os.path.join("..", "escaped.KSF")))
check("guard-rejects-deep-escape", rejects(os.path.join("..", "..", "escaped.KSF")))
check("guard-rejects-rooted", rejects(r"C:\Windows\escaped.KSF"))
check("guard-accepts-leaf", accepts("NORMAL.KSF"))
check("guard-accepts-subfolder", accepts(os.path.join("MS000", "NORMAL.KSF")))
check("guard-accepts-roundtrip", accepts(os.path.join("MS000", "..", "NORMAL.KSF")))

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
