"""Port of C# SamplePhase11SelfTests + SamplePhase18SelfTests: multi-root open, expansion survives rebuild,
session-wide dirty survives navigation, multisample rename cascade (.KMP + folder move, stereo mirror,
deferred content, _UserBank.KSC), collision guard, KMP/KSF naming tiers."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("KRONOS_DATA_DIR", tempfile.mkdtemp(prefix="kr_data_"))
import numpy as np

from Core.sample_editor_model.model import SampleEditorModel
from Core.sample_editor_model.core import compute_kmp_base_name
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


def read(path):
    with open(path, "rb") as f:
        return f.read()


root = tempfile.mkdtemp(prefix="kr_phase11_")


def build_collection(name):
    ksc = os.path.join(root, f"{name}.KSC")
    os.makedirs(os.path.join(root, name, name))
    KscCollection(entries=[f"{name}.KMP"]).save(ksc)
    k = KmpMultisample(name=name, mno1=0)
    k.zones.append(KmpZone(filename="MS000000.KSF", original_key=60, top_key=60))
    k.save(os.path.join(root, name, f"{name}.KMP"))
    s = KsfSample(name=name, sample_rate=44100)
    s.set_samples(pcm([1, 2, 3, 4, 5]))
    s.save(os.path.join(root, name, name, "MS000000.KSF"))
    return ksc


def root_for(m, ksc):
    return next(r for r in m.roots if r.collection_ref[1] == ksc)


A, B = build_collection("PhaseA"), build_collection("PhaseB")

m = SampleEditorModel()
m.open_collection(A)
check("first-collection-opens-one-root", len(m.roots) == 1)
m.open_collection(B)
check("second-collection-adds-a-root-not-replaces", len(m.roots) == 2)
check("both-collections-still-present", {r.collection_ref[1] for r in m.roots} == {A, B})
m.open_collection(A)
check("reopening-a-collection-still-two-roots", len(m.roots) == 2)

m = SampleEditorModel()
m.open_collection(A)
r = root_for(m, A)
ms = r.children[0]
r.is_expanded = True
ms.is_expanded = True
m.select_node(ms.children[0])
m.add_placeholder_zone()
nr = root_for(m, A)
check("root-expansion-survives-rebuild", nr.is_expanded)
check("multisample-expansion-survives-rebuild", nr.children[0].is_expanded)

C = build_collection("PhaseC")
m = SampleEditorModel()
m.open_collection(C)
check("starts-with-no-unsaved-changes", not m.has_unsaved_changes)
zn = root_for(m, C).children[0].children[0]
m.select_node(zn)
m.apply_zone_edits(61, 61)
check("edit-marks-session-dirty", m.has_unsaved_changes)
m.select_node(None)
check("session-dirty-survives-navigation-away", m.has_unsaved_changes)
m.select_node(root_for(m, C).children[0].children[0])
m.save_selected_multisample()
check("save-clears-session-dirty", not m.has_unsaved_changes)

# ---- Phase 18: naming
check("basename-tier1-5plus3", compute_kmp_base_name("DaveTest", 0) == "DAVET000")
check("basename-tier1-pads-short-name", compute_kmp_base_name("Bo", 5) == "BO___005")
check("basename-tier2-4plus4", compute_kmp_base_name("DaveTest", 1028) == "DAVE1028")
check("basename-uppercases-and-sanitizes-non-alphanumeric", compute_kmp_base_name("beer-", 0) == "BEER_000")

k = KmpMultisample(name="Test", mno1=1028)
check("ksf-filename-tier2-empty", k.next_ksf_filename() == "M1028000.KSF")
k.zones.append(KmpZone(filename="M1028000.KSF"))
check("ksf-filename-tier2-second-zone", k.next_ksf_filename() == "M1028001.KSF")
check("free-zone-filename-tier2-skips-used-index", k.next_free_zone_filename() == "M1028001.KSF")

# ---- rename cascade
sroot = tempfile.mkdtemp(prefix="kr_phase18_")
ksc = os.path.join(sroot, "RenameTest.KSC")
os.makedirs(os.path.join(sroot, "RenameTest"))
coll = KscCollection(path=ksc)
coll.save(ksc)
left, lp, right, rp = create_stereo_multisample_pair(coll, ksc, "OldName", 0)


def write_real_zone(k, kmp_path, seed):
    fn = k.next_ksf_filename()
    k.zones.append(KmpZone(filename=fn, original_key=36, top_key=36))
    d = os.path.join(os.path.dirname(kmp_path), os.path.splitext(os.path.basename(kmp_path))[0])
    os.makedirs(d, exist_ok=True)
    s = KsfSample(name="OldName", sample_rate=44100)
    s.set_samples((np.arange(50) + seed).astype(np.int16))
    s.save(os.path.join(d, fn))


write_real_zone(left, lp, 1000)
write_real_zone(right, rp, 2000)
left.save(lp)
right.save(rp)
coll.save(ksc)

cd = os.path.join(sroot, "RenameTest")
old_lf, old_rf = os.path.join(cd, "OLDNA000"), os.path.join(cd, "OLDNA001")
check("fixture-old-left-ksf-exists", os.path.exists(os.path.join(old_lf, "MS000000.KSF")))
check("fixture-old-right-ksf-exists", os.path.exists(os.path.join(old_rf, "MS001000.KSF")))

m = SampleEditorModel()
m.open_collection(ksc)
ln = next((n for n in m.all_multisample_nodes() if os.path.normcase(n.multisample_ref[1]) == os.path.normcase(lp)), None)
check("fixture-left-node-found", ln is not None)
m.select_node(ln)
m.rename_selected_multisample("DaveTest")

nlk, nrk = os.path.join(cd, "DAVET000.KMP"), os.path.join(cd, "DAVET001.KMP")
nlf, nrf = os.path.join(cd, "DAVET000"), os.path.join(cd, "DAVET001")
check("old-left-kmp-gone", not os.path.exists(lp))
check("old-right-kmp-gone", not os.path.exists(rp))
check("new-left-kmp-present", os.path.exists(nlk))
check("new-right-kmp-present-stereo-sibling-mirrored", os.path.exists(nrk))
check("old-left-folder-gone", not os.path.isdir(old_lf))
check("old-right-folder-gone", not os.path.isdir(old_rf))
check("new-left-folder-present", os.path.isdir(nlf))
check("new-right-folder-present", os.path.isdir(nrf))
check("left-ksf-filename-unchanged-after-move", os.path.exists(os.path.join(nlf, "MS000000.KSF")))
check("right-ksf-filename-unchanged-after-move", os.path.exists(os.path.join(nrf, "MS001000.KSF")))

if os.path.exists(nlk):
    check("kmp-content-still-deferred-before-save", KmpMultisample.open(read(nlk)).name == "OldName")
else:
    check("kmp-content-still-deferred-before-save", False)
m.save_all_changes()
if os.path.exists(nlk):
    check("kmp-content-flushed-after-save-changes", KmpMultisample.open(read(nlk)).name == "DaveTest")
else:
    check("kmp-content-flushed-after-save-changes", False)

entries = [e.upper() for e in KscCollection.open(read(ksc)).entries]
check("ksc-entries-updated-to-new-filenames", "DAVET000.KMP" in entries)
check("ksc-entries-no-longer-list-old-filename", "OLDNA000.KMP" not in entries)
check("userbank-generated-on-save-changes", os.path.exists(os.path.join(sroot, "RenameTest_UserBank.KSC")))

# ---- collision guard
croot = tempfile.mkdtemp(prefix="kr_phase18c_")
ksc2 = os.path.join(croot, "Collision.KSC")
os.makedirs(os.path.join(croot, "Collision"))
KscCollection(path=ksc2).save(ksc2)
m2 = SampleEditorModel()
m2.open_collection(ksc2)
na = m2.new_multisample_in_collection("AAAAA", 0)
nb = m2.new_multisample_in_collection("BBBBB", 1)
check("collision-fixture-both-created", na is not None and nb is not None)
if na is not None and nb is not None:
    m2.select_node(nb)
    cdir = os.path.join(croot, "Collision")
    with open(os.path.join(cdir, "CCCCC001.KMP"), "wb") as f:
        f.write(bytes([1, 2, 3]))
    m2.rename_selected_multisample("CCCCC")
    check("collision-guard-refuses-when-target-file-exists", nb.multisample_ref[0].name == "BBBBB")
    check("collision-guard-leaves-original-kmp-in-place", os.path.exists(os.path.join(cdir, "BBBBB001.KMP")))

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
