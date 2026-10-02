"""Port of C# SampleRemoteSelfTests + FtpPathSafetySelfTests against a duck-typed remote source: pull populates
the tree, push refuses header-only / dirty / never-pulled samples, cancelled pull keeps the tree, FTP top-level
and length guards."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("KRONOS_DATA_DIR", tempfile.mkdtemp(prefix="kr_data_"))
import numpy as np

from Core.sample_editor_model.model import SampleEditorModel
from Core.sample_editor_model.io import RemotePullResult, RemotePushResult
from Core.sample_editor_model.tree import enumerate_nodes
from Data.ksc_collection import KscCollection
from Data.kmp_multisample import KmpMultisample, KmpZone
from Data.ksf_sample import KsfSample
from Tools.file_manager import _is_top_level_ftp_path, _MAX_REMOTE_PATH_LENGTH, FtpPathTooLongError

fails = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


class FakeSource:
    def __init__(self, picked=None, remote_map=None, result=None):
        self._result = result or RemotePullResult(picked, "", dict(remote_map or {}))
        self.pulled_filter = None
        self.pushed = []

    def pick_and_pull(self, ext, local_root):
        self.pulled_filter = ext
        return self._result

    def push(self, local, remote):
        self.pushed.append((local, remote))
        return RemotePushResult("pushed")


def find_zone(m, fn):
    return next((n for n in enumerate_nodes(m.roots) if n.zone_ref is not None and n.zone_ref[0].filename == fn), None)


root = tempfile.mkdtemp(prefix="kr_remote_")
os.makedirs(os.path.join(root, "Test", "Test"))
ksc = os.path.join(root, "Test.KSC")
KscCollection(entries=["Test.KMP"]).save(ksc)
kmp = os.path.join(root, "Test", "Test.KMP")
k = KmpMultisample(name="Test", mno1=1)
k.zones += [KmpZone(filename="MS001000.KSF", original_key=60, top_key=60),
            KmpZone(filename="MS001001.KSF", original_key=61, top_key=61)]
k.save(kmp)
d = os.path.join(root, "Test", "Test")
k1, k2 = os.path.join(d, "MS001000.KSF"), os.path.join(d, "MS001001.KSF")
s1 = KsfSample(name="S1", sample_rate=44100)
s1.set_samples(np.array([1, 2, 3, 4, 5], dtype=np.int16))
s1.save(k1)
KsfSample(name="S2").save(k2)
rmap = {ksc: "/ClaudeTest/Test.KSC", kmp: "/ClaudeTest/Test/Test.KMP",
        k1: "/ClaudeTest/Test/Test/MS001000.KSF", k2: "/ClaudeTest/Test/Test/MS001001.KSF"}

m = SampleEditorModel()
fake = FakeSource(ksc, rmap)
m.pull_collection_from_kronos(fake)
check("pull-uses-ksc-extension-filter", fake.pulled_filter == ".KSC")
check("pull-populates-tree", len(m.roots) > 0)
m.select_node(find_zone(m, "MS001000.KSF"))
check("pull-sample-loaded", m.has_sample_loaded and not m.sample_is_header_only)
m.push_selected_sample(fake)
check("push-clean-sample-succeeds", len(fake.pushed) == 1)
check("push-clean-sample-right-remote-path", len(fake.pushed) == 1 and fake.pushed[0][1] == "/ClaudeTest/Test/Test/MS001000.KSF")
m.select_node(find_zone(m, "MS001001.KSF"))
check("push-header-only-sample-selected", m.sample_is_header_only)
m.push_selected_sample(fake)
check("push-refuses-header-only", len(fake.pushed) == 1)
m.select_node(find_zone(m, "MS001000.KSF"))
m.apply_sample_edits(22050, True, 0, 0, 0)
m.push_selected_sample(fake)
check("push-refuses-dirty-sample", len(fake.pushed) == 1)

m = SampleEditorModel()
m.open_collection(ksc)
m.select_node(find_zone(m, "MS001000.KSF"))
fake = FakeSource(ksc, rmap)
m.push_selected_sample(fake)
check("push-refuses-never-pulled", len(fake.pushed) == 0)

m = SampleEditorModel()
m.open_collection(ksc)
before = len(m.roots)
m.pull_collection_from_kronos(FakeSource(result=RemotePullResult(None, "Load from Kronos cancelled.")))
check("pull-cancel-sets-status", m.status_text == "Load from Kronos cancelled.")
check("pull-cancel-keeps-previous-tree", len(m.roots) == before)

# ---- FTP path safety
check("toplevel-ssd-bare", _is_top_level_ftp_path("/SSD1"))
check("toplevel-ssd-trailing-slash", _is_top_level_ftp_path("/SSD1/"))
check("toplevel-root-itself", _is_top_level_ftp_path("/"))
check("not-toplevel-one-deep", not _is_top_level_ftp_path("/SSD1/Samples"))
check("not-toplevel-several-deep", not _is_top_level_ftp_path("/SSD1/Samples/Kit/MS001000.KSF"))
at_limit = "/" + "a" * 244
check("fits-exactly-at-limit", len(at_limit) == 245 == _MAX_REMOTE_PATH_LENGTH)
check("toolong-message-names-the-length", "246" in str(FtpPathTooLongError(at_limit + "a")))

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
