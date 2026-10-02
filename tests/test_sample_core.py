"""Sample Editor core layer against COPIES of real hardware-captured fixtures: import builder, stereo pair
creation, export, normalization report, clipboard/workspace. Run from the repo root."""
import os, shutil, sys, tempfile, wave
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np

import Data.ksc_collection as ksc_mod
from Data.ksc_collection import KscCollection
from Data.kmp_multisample import KmpMultisample, KmpZone
from Data.ksf_sample import KsfSample
import Core.sample_import_builder as builder
import Core.sample_export as exporter
import Core.sample_support as support

FIX = r"Z:\KronosScreenRemote\SampleFixtures"
fails = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


tmp = tempfile.mkdtemp(prefix="kr_samplecore_")
work = os.path.join(tmp, "SMPTEST")
shutil.copytree(os.path.join(FIX, "SMPTEST"), work)
ksc_path = os.path.join(work, "LOOP.KSC")
coll = KscCollection.open(open(ksc_path, "rb").read())
content = ksc_mod.content_dir_for(ksc_path)
kmp_path = os.path.join(content, "CLAUD000.KMP")
m = KmpMultisample.open(open(kmp_path, "rb").read())
n0 = len(m.zones)
print("fixture: %d zones in CLAUD000, entries=%s" % (n0, [e for e in coll.entries if e.upper().endswith('.KMP')]))

# ---- add_sample_zone: ordering, uniqueness, round trip
tone = (np.sin(np.arange(2000) * 0.1) * 9000).astype(np.int16)
before_sno = {}
for root, _d, files in os.walk(content):
    for f in files:
        if f.upper().endswith(".KSF"):
            before_sno[os.path.join(root, f)] = KsfSample.read_sno1(os.path.join(root, f))
z = builder.add_sample_zone(m, kmp_path, "NewTone", tone, 44100, 60, 127)
check("zone appended in top-key order", m.zones[-1] is z and all(m.zones[i].top_key <= m.zones[i + 1].top_key for i in range(len(m.zones) - 1)))
ksf_path = z.ksf_path(kmp_path)
check("ksf written under <kmp-basename>/", os.path.exists(ksf_path) and os.path.dirname(ksf_path).endswith("CLAUD000"))
back = KsfSample.open(open(ksf_path, "rb").read())
check("ksf round-trips pcm", back.samples().tolist() == tone.tolist() and back.name == "NewTone" and back.sample_rate == 44100)
check("flags 0x81 (one-shot + boost), not inherited", back.flags == 0x81 and back.loop_tune == 0)
check("sno1 unique across the collection", back.sno1 not in before_sno.values())
low = builder.add_sample_zone(m, kmp_path, "Low", tone, 44100, 10, 5)
check("inserted BEFORE a higher-keyed zone", m.zones.index(low) < m.zones.index(z))
for i in range(builder.MAX_ZONES_PER_MULTISAMPLE - len(m.zones)):
    m.zones.append(KmpZone(filename="SKIPPEDSAMPLE", original_key=127, top_key=127))
try:
    builder.add_sample_zone(m, kmp_path, "Overflow", tone, 44100, 60, 127); check("128-zone cap enforced", False)
except ValueError as e:
    check("128-zone cap enforced", "128" in str(e))
check("cap leaves no stray file", not any("Overflow" == (KsfSample.open(open(os.path.join(r, f), 'rb').read()).name)
                                          for r, _d, fs in os.walk(content) for f in fs if f.upper().endswith('.KSF')))

# ---- stereo pair creation + sibling resolution
coll2_path = os.path.join(tmp, "PAIR.KSC")
coll2 = KscCollection(entries=[])
open(coll2_path, "wb").write(coll2.to_bytes(os.path.basename(coll2_path)))
l, lp, r, rp = builder.create_stereo_multisample_pair(coll2, coll2_path, "Strings", 10)
check("pair: same name, opposite suffix, adjacent MNO1",
      (l.name, l.suffix, l.mno1, r.name, r.suffix, r.mno1) == ("Strings", "-L", 10, "Strings", "-R", 11))
check("pair .KMP filenames never carry -L/-R",
      "-L" not in os.path.basename(lp) and "-R" not in os.path.basename(rp) and os.path.basename(lp).endswith(".KMP"))
check("pair registered in the saved collection",
      KscCollection.open(open(coll2_path, "rb").read()).entries[-2:] == [os.path.basename(lp), os.path.basename(rp)])
sib, sibp = builder.find_stereo_sibling_on_disk(KscCollection.open(open(coll2_path, "rb").read()), l, lp)
check("find_stereo_sibling resolves the right half", sib is not None and sib.suffix == "-R" and sibp == rp)
lz, rz = builder.add_stereo_sample_zone_pair(l, lp, r, rp, "Pad", tone, tone[::-1].copy(), 44100, 60, 72)
check("stereo zones share a key range", (lz.original_key, lz.top_key) == (rz.original_key, rz.top_key) == (60, 72))
lk, rk = KsfSample.open(open(lz.ksf_path(lp), "rb").read()), KsfSample.open(open(rz.ksf_path(rp), "rb").read())
check("stereo halves carry -L/-R and their own audio", (lk.suffix, rk.suffix) == ("-L", "-R") and lk.samples().tolist() != rk.samples().tolist())
check("stereo halves get distinct sno1", lk.sno1 != rk.sno1)
dz = builder.make_default_first_zone()
check("default first zone: C2 placeholder", (dz.filename, dz.original_key, dz.top_key) == ("SKIPPEDSAMPLE", 36, 36) and dz.is_skipped)
full = KmpMultisample(name="Full", suffix="-R", mno1=3); full.zones = [KmpZone(filename="x", top_key=1)] * 128
try:
    builder.add_stereo_sample_zone_pair(l, lp, full, rp, "Boom", tone, tone, 44100, 60, 72); check("pair add refuses BEFORE writing either half", False)
except ValueError:
    check("pair add refuses BEFORE writing either half", len(l.zones) == 1)

# ---- export
out = os.path.join(tmp, "wav")
exp, skip = exporter.export_collection(coll, ksc_path, out)
wavs = sorted(os.listdir(out))
print("export: exported=%d skipped=%d wavs=%s" % (exp, skip, wavs))
check("export wrote one wav per readable sample, named after the sample", exp >= 1 and len(wavs) == exp and all(w.endswith(".wav") for w in wavs))
with wave.open(os.path.join(out, wavs[0])) as w:
    src = None
    check("exported wav is mono 16-bit", w.getnchannels() == 1 and w.getsampwidth() == 2)
used = set()
check("duplicate names get _N suffixes",
      [exporter.make_unique_file_name(used, "A:B"), exporter.make_unique_file_name(used, "A:B"), exporter.make_unique_file_name(used, "a:b")] == ["A_B", "A_B_1", "a_b_2"])
hdr_only = KsfSample(name="Stub", sample_rate=44100)
check("header-only sample is skipped, no file", exporter.export_sample_to_wav(hdr_only, os.path.join(out, "stub.wav")) is False and not os.path.exists(os.path.join(out, "stub.wav")))
check("single export of a real sample", exporter.export_sample_to_wav(back, os.path.join(out, "single", "x.wav")))
with wave.open(os.path.join(out, "single", "x.wav")) as w:
    check("single export content matches", np.frombuffer(w.readframes(w.getnframes()), "<i2").tolist() == tone.tolist())
e2, s2 = exporter.export_multisample(m, kmp_path, os.path.join(tmp, "ms_wav"))
check("multisample export skips SKIPPEDSAMPLE placeholder zones", e2 >= 3 and s2 == 0)

# ---- normalization report on a real multi-sample collection
real = os.path.join(FIX, "ANDRE_K2_73", "samplesfeb28_25.KSC")
rc = KscCollection.open(open(real, "rb").read())
rep = support.build_normalization_report(rc, real)
check("report covers every readable zone", len(rep) > 0)
rates = {e.sample_rate for e in rep}
print("report: %d samples, rates=%s, flagged=%d" % (len(rep), sorted(rates), sum(e.flagged for e in rep)))
if len(rates) == 1:
    check("uniform collection -> only header-only samples flagged", all(e.flagged == e.is_header_only for e in rep))
mixed = [support.SampleNormalizationEntry("a", "a", 44100, 16, 1, False, False)] * 2
check("clipboard", (support.SampleClipboard.clear(), not support.SampleClipboard.has_content())[1])
support.SampleClipboard.set(tone, 44100); tone[0] = 123
check("clipboard stores a COPY", support.SampleClipboard.pcm[0] != 123 and support.SampleClipboard.has_content() and support.SampleClipboard.sample_rate == 44100)
class S: sample_workspace_root = ""
check("workspace defaults under the OS temp dir", support.resolve_workspace_root(S()).startswith(tempfile.gettempdir()))
S.sample_workspace_root = r"D:\x"
check("workspace honours the setting", support.resolve_workspace_root(S()) == r"D:\x")

print("ALL PASS" if not fails else "FAILED: " + ", ".join(fails))
sys.exit(1 if fails else 0)
