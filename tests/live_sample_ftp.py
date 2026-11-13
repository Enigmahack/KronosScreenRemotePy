"""Live, WRITES the instrument's FTP (only inside a NEW scratch folder this script creates and deletes again):
Core.sample_ftp push order and name guards on real hardware.
  1. push_closure() of a real fixture collection (SampleFixtures/SMPTEST/LOOP.KSC: 2 .KMP, 2 .KSF, _UserBank.KSC)
     into <parent>/<new folder>, logging every FTP call: checks each final rename (.part -> real name) lands
     child-first (every .KSF, then every .KMP, then the .KSC files);
  2. pull() it back into a temp dir and compares every pulled file byte for byte with the source;
  3. lists the folder tree (LIST -a) for '.part' / '.old' leftovers;
  4. pushes copies whose first zone filename is crafted ('..\\x', '/etc/x') into another new folder: must be refused
     with NOTHING sent (no STOR/RNTO/MKD) and the folder must not exist afterwards;
  5. deletes the scratch folder (and the parent too when this script created it) and confirms it is gone.
usage: live_sample_ftp.py <host> <user> <password> <parent dir, e.g. /SSD2/CLAUDETEST_K>"""
import os, sys, shutil, tempfile, time, glob
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import Core.sample_ftp as sftp
from Data.kmp_multisample import KmpMultisample
from Data.ksc_collection import KscCollection
from Tools.file_manager import _FtpWorker
from tests.fixture_paths import SAMPLE_FIXTURES

host, user, pw, parent = sys.argv[1:5]
parent = parent.rstrip("/")
stamp = time.strftime("%H%M%S")
scratch = f"{parent}/SFTP_{stamp}"
refused_dir = f"{parent}/SFTP_{stamp}_BAD"
src_root = os.path.join(SAMPLE_FIXTURES, "SMPTEST")
work = tempfile.mkdtemp(prefix="kr_livesftp_")
local = os.path.join(work, "src"); os.makedirs(local)
for p in ("LOOP.KSC", "LOOP_UserBank.KSC"):
    shutil.copy2(os.path.join(src_root, p), local)
shutil.copytree(os.path.join(src_root, "LOOP"), os.path.join(local, "LOOP"))

ftp = _FtpWorker(host, 21, user, pw); ftp.connect()
calls = []
for name in ("upload", "rename", "mkdir", "delete_file", "delete_dir", "download"):
    orig = getattr(ftp, name)
    def mk(orig, name):
        def f(*a, **k):
            calls.append((name,) + tuple(a)); return orig(*a, **k)
        return f
    setattr(ftp, name, mk(orig, name))

def exists(path):
    par, nm = path.rsplit("/", 1)
    return any(e.name == nm for e in ftp.list_dir(par or "/", include_hidden=True))
def walk(path):
    out = []
    for e in ftp.list_dir(path, include_hidden=True):
        out.append((e.full_path, e.is_directory, e.size))
        if e.is_directory:
            out += walk(e.full_path)
    return out

made_parent = False
result = {}
try:
    if not exists(parent):
        ftp.mkdir(parent); made_parent = True
    assert not exists(scratch) and not exists(refused_dir)
    # 1. push
    ksc = os.path.join(local, "LOOP.KSC")
    t0 = time.time()
    fails = sftp.push_closure(ftp, ksc, KscCollection.open(open(ksc, "rb").read()), scratch, on_progress=None)
    print(f"push failures: {fails}  ({time.time() - t0:.1f}s)")
    finals = [c[2] for c in calls if c[0] == "rename" and c[1].endswith(".part")]
    stors = [c[2] for c in calls if c[0] == "upload"]
    print("STOR order :", [s.rsplit("/", 1)[-1] for s in stors])
    print("RNTO order :", [f.replace(scratch, "") for f in finals])
    kind = lambda p: {".KSF": 0, ".KMP": 1, ".KSC": 2}[p[-4:].upper()]
    ranks = [kind(f) for f in finals]
    result["push_ok"] = fails == []
    result["child_first"] = ranks == sorted(ranks) and ranks.count(0) == 2 and ranks.count(1) == 2 and ranks.count(2) == 2
    # 2. pull back and compare
    pulled = os.path.join(work, "pull")
    entry, remote_map, pfails = sftp.pull(ftp, f"{scratch}/LOOP.KSC", pulled)
    print("pull failures:", pfails, "files:", len(remote_map))
    same = []
    for lp, rp in remote_map.items():
        rel = rp[len(scratch) + 1:].replace("/", os.sep)
        a, b = open(lp, "rb").read(), open(os.path.join(local, rel), "rb").read()
        same.append(a == b); print("   ", rel, "IDENTICAL" if a == b else "DIFFERS")
    ub = os.path.join(work, "ub.KSC"); ftp.download(f"{scratch}/LOOP_UserBank.KSC", ub)
    same.append(open(ub, "rb").read() == open(os.path.join(local, "LOOP_UserBank.KSC"), "rb").read())
    print("    LOOP_UserBank.KSC (RETR directly)", "IDENTICAL" if same[-1] else "DIFFERS")
    result["roundtrip"] = pfails == [] and len(remote_map) == 5 and all(same)
    # 3. leftovers
    tree = walk(scratch)
    left = [p for p, d, s in tree if p.endswith(".part") or p.endswith(".old")]
    print("remote tree:", [(p.replace(scratch, ""), s) for p, d, s in tree])
    result["no_leftovers"] = left == []
    # 4. crafted names
    for bad in ("..\\x", "/etc/x"):
        bdir = os.path.join(work, "bad" + str(len(bad))); shutil.copytree(local, bdir)
        kp = os.path.join(bdir, "LOOP", "CLAUD000.KMP")
        m = KmpMultisample.open(open(kp, "rb").read()); m.zones[0].filename = bad
        open(kp, "wb").write(m.to_bytes())
        assert KmpMultisample.open(open(kp, "rb").read()).zones[0].filename == bad
        n = len(calls)
        bk = os.path.join(bdir, "LOOP.KSC")
        bf = sftp.push_closure(ftp, bk, KscCollection.open(open(bk, "rb").read()), refused_dir)
        sent = [c for c in calls[n:] if c[0] in ("upload", "rename", "mkdir", "delete_file")]
        print(f"crafted {bad!r}: messages={bf} ftp calls={sent} folder exists={exists(refused_dir)}")
        result[f"refused {bad}"] = (not sent and not exists(refused_dir)
                                    and any("refused" in m_ for m_ in bf) and "Nothing was uploaded" in bf[-1])
finally:
    for d in (scratch, refused_dir):
        try:
            if exists(d):
                ftp.delete_dir(d)
        except Exception as ex:
            print("CLEANUP FAILED", d, ex)
    if made_parent:
        try:
            if not ftp.list_dir(parent, include_hidden=True):
                ftp.delete_dir(parent)
        except Exception as ex:
            print("CLEANUP FAILED", parent, ex)
    result["cleaned"] = not exists(scratch) and not exists(refused_dir) and (not made_parent or not exists(parent))
    print("parent listing after cleanup:", [e.name for e in ftp.list_dir(parent.rsplit("/", 1)[0] or "/", include_hidden=True)
                                             if e.name.startswith(parent.rsplit("/", 1)[-1])],
          [e.name for e in (ftp.list_dir(parent, include_hidden=True) if exists(parent) else [])])
    print("RESULT", result)
    ftp.disconnect()
    shutil.rmtree(work, ignore_errors=True)
