import sys, glob, collections, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from Data.pcg_file import open_pcg, wire_body_from_pcg_entry
from Data.librarian_sysex import OBJ_PROGRAM
import Tools.sample_reference_walker as sw, Tools.dependency_scanner as ds

files = sorted(glob.glob(r"Z:\PCG EXAMPLES\**\*.pcg", recursive=True) + glob.glob(r"Z:\PCG EXAMPLES\**\*.PCG", recursive=True))
files += sorted(glob.glob(r"Z:\KronosScreenRemote\SampleFixtures\**\*.pcg", recursive=True))
files = files[:25]
print(len(files), "pcg files", flush=True)
tot = collections.Counter(); drum = 0; progs = 0; sample_progs = 0; ex = {}; bad = 0
for f in files:
    try:
        pcg = open_pcg(open(f, 'rb').read())
    except Exception:
        bad += 1; continue
    if pcg is None:
        bad += 1; continue
    for e in pcg.objects:
        if e.obj_type != OBJ_PROGRAM:
            continue
        b = wire_body_from_pcg_entry(OBJ_PROGRAM, e)
        if b is None:
            continue
        progs += 1
        rows = sw.walk(OBJ_PROGRAM, b)
        if rows:
            sample_progs += 1
        for r in rows:
            tot[r.bucket] += 1
            ex.setdefault(r.bucket, (os.path.basename(f), e.name, r.description))
        drum += sum(1 for _ in ds.walk_display_references(OBJ_PROGRAM, b) if _.ref_kind == "drum track")
print("unreadable:", bad, "| programs:", progs, "| with sample deps:", sample_progs)
print("bucket rows:", dict(tot), "| drum-track refs:", drum)
for k, v in ex.items():
    print(" ", k, "->", v)
