"""Set List conversion Kronos <-> Nautilus (Conversion/setlist.py) and the stride/bank-aware Set List accessors.
Corpus parts use real hardware-captured PCGs and skip when absent. Run from the repo root."""
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from Conversion.auto import convert_object
from Conversion.push import ConvertTables
from Conversion.setlist import convert_setlist
from Data.librarian_sysex import (OBJ_COMBI, OBJ_PROGRAM, OBJ_SET_LIST, SETLIST_KRONOS_SIZE, SETLIST_NAUTILUS_SIZE,
                                  func33_to_obj_bank, set_setlist_slot_ref, setlist_slot_base, setlist_slot_ref,
                                  setlist_slot_pc)
from Data.pcg_file import FAMILY_KRONOS, FAMILY_NAUTILUS, open_pcg
from fixture_paths import PCG_EXAMPLES
from Tools.dependency_scanner import walk_object_references

fails = []


def check(name, cond):
    if not cond:
        fails.append(name)
        print("FAIL", name)


# ---- accessors: stride, comment offset, bank numbering
for size, stride in ((SETLIST_KRONOS_SIZE, 542), (SETLIST_NAUTILUS_SIZE, 543)):
    b = bytearray(size)
    check(f"stride-{stride}", setlist_slot_base(b, 1) - setlist_slot_base(b, 0) == stride)
    set_setlist_slot_ref(b, 0, 18, 5, type_=1)            # U-A / G, Program
    set_setlist_slot_ref(b, 127, 31, 9, type_=1)          # U-GG / T
    set_setlist_slot_ref(b, 1, 7, 2, type_=0)             # Combi U-A / G
    check(f"raw-{stride}", b[24 + 25] == 17 and b[24 + 127 * stride + 25] == 30 and b[24 + stride + 25] == 7)
    check(f"read-{stride}", (setlist_slot_ref(bytes(b), 0), setlist_slot_ref(bytes(b), 127),
                             setlist_slot_ref(bytes(b), 1)) == ((1, 18, 5), (1, 31, 9), (0, 7, 2)))

# ---- synthetic Kronos -> Nautilus -> Kronos
k = bytearray(SETLIST_KRONOS_SIZE)
k[:5] = b"MYSET"
for s in range(4):
    o = setlist_slot_base(k, s)
    k[o:o + 5] = b"SLOT%d" % s
    k[o + 24] |= 0b0011_0000                  # colour bits survive
    k[o + 25] |= 0b1010_0000                  # transpose bits survive
    k[o + 27], k[o + 28] = 6, 100
    k[o + 30:o + 40] = b"comment %d " % s
set_setlist_slot_ref(k, 0, 18, 10, type_=1)   # user Program, no factory map entry -> keeps position
set_setlist_slot_ref(k, 1, 7, 3, type_=0)
set_setlist_slot_ref(k, 2, 0, 200 - 1, type_=2)
k[24 + 128 * 542 + 3] = 0x10                  # band 3
k[24 + 128 * 542 + 12] = 2                    # slot style
res = convert_setlist(bytes(k), True)
check("k2n-body", res.body is not None and len(res.body) == SETLIST_NAUTILUS_SIZE)
n = res.body
check("k2n-name", n[:5] == b"MYSET")
for s in range(4):
    o = setlist_slot_base(n, s)
    check(f"k2n-comment-{s}", n[o + 31:o + 41] == b"comment %d " % s and n[o + 30] == 0)
    check(f"k2n-fields-{s}", n[o + 24] & 0b0011_0000 == 0b0011_0000 and n[o + 25] & 0xE0 == 0xA0
          and (n[o + 27], n[o + 28]) == (6, 100))
check("k2n-refs", [setlist_slot_ref(n, s) for s in range(3)] == [(1, 18, 10), (0, 7, 3), (2, 0, 199)])
check("k2n-footer", n[24 + 128 * 543 + 3] == 0x10 and n[24 + 128 * 543 + 12] == 2)
check("k2n-warns-combi-song", any("Combi slots 2" in w for w in res.warnings) and any("Song slots 3" in w for w in res.warnings))
back = convert_setlist(n, False)
check("n2k-roundtrip", back.body == bytes(k))
# Nautilus-only data is reported when it is dropped
nn = bytearray(n)
nn[setlist_slot_base(nn, 0) + 30] = 0x80 | 55
nn[24 + 128 * 543] = 0x01 | (20 << 1)
dropped = convert_setlist(bytes(nn), False)
check("n2k-dynamics-warn", any("Slot 1 Dynamics" in w for w in dropped.warnings))
check("n2k-trim-warn", any("Input Trim" in w for w in dropped.warnings) and dropped.body[24 + 128 * 542] == 1)
check("wrong-size-refused", convert_setlist(bytes(100), True).body is None
      and convert_setlist(bytes(SETLIST_KRONOS_SIZE), False).body is None)
hk = bytearray(SETLIST_KRONOS_SIZE)
hk[24 + 27] = 0x17
check("hold-clamp", convert_setlist(bytes(hk), True).body[24 + 27] == 0x16)

# ---- through the entry point and the shared tables
tables = ConvertTables.load()
r = convert_object(OBJ_SET_LIST, bytes(k), False, FAMILY_KRONOS, FAMILY_NAUTILUS, tables)
check("auto-k2n", r.body is not None and len(r.body) == SETLIST_NAUTILUS_SIZE)
check("auto-same", convert_object(OBJ_SET_LIST, bytes(k), False, FAMILY_KRONOS, FAMILY_KRONOS, tables).body == bytes(k))


# ---- real corpus
def pcgs(family):
    roots = [PCG_EXAMPLES] + ([os.path.join(os.path.dirname(PCG_EXAMPLES), "Nautilus Research", "PCG Info")]
                              if family == FAMILY_NAUTILUS else [])
    for root in roots:
        for f in glob.glob(os.path.join(root, "**", "*"), recursive=True):
            if f.lower().endswith(".pcg"):
                p = open_pcg(open(f, "rb").read())
                if p is not None and p.family == family:
                    yield f, p


def objects_by_addr(p, obj_type):
    return {(e.bank.obj_bank if e.bank is not None else 0, e.index): e for e in p.objects if e.obj_type == obj_type}


def dangling(body, programs, combis):
    """Slots whose reference points at nothing (an empty or missing slot) in the same PCG."""
    n = 0
    for r in walk_object_references(OBJ_SET_LIST, body):
        tbl = programs if r.ref.obj_type == OBJ_PROGRAM else combis
        if r.ref.bank < 0x10 and r.ref.obj_type == OBJ_PROGRAM and r.ref.bank > 5:
            continue                                  # GM / g(n): always present on the instrument
        e = tbl.get((r.ref.bank, r.ref.number))
        if e is None or not (e.name or "").strip():
            n += 1
    return n


for family, other in ((FAMILY_KRONOS, FAMILY_NAUTILUS), (FAMILY_NAUTILUS, FAMILY_KRONOS)):
    files = seen = refs = bad_roundtrip = dang = 0
    for f, p in pcgs(family):
        sls = [e for e in p.objects if e.obj_type == OBJ_SET_LIST]
        if not sls:
            continue
        if files >= 4:      # the whole corpus (122 files, 15,616 Set Lists) passes too but takes minutes: KRONOS_SETLIST_ALL=1
            if not os.environ.get("KRONOS_SETLIST_ALL"):
                break
        files += 1
        progs, combs = objects_by_addr(p, OBJ_PROGRAM), objects_by_addr(p, OBJ_COMBI)
        for e in sls:
            seen += 1
            refs += sum(1 for _ in walk_object_references(OBJ_SET_LIST, e.body))
            dang += dangling(e.body, progs, combs)
            res = convert_object(OBJ_SET_LIST, e.body, False, family, other, tables)
            if res.body is None or len(res.body) != (SETLIST_NAUTILUS_SIZE if other == FAMILY_NAUTILUS else SETLIST_KRONOS_SIZE):
                fails.append(f"corpus-convert {f} {e.index}")
                continue
            # names / comments survive, and a back-conversion matches the original wherever no map entry applied
            back = convert_object(OBJ_SET_LIST, res.body, False, other, family, tables)
            if back.body is None:
                fails.append(f"corpus-back {f} {e.index}")
                continue
            for s in range(128):
                a, b = setlist_slot_base(e.body, s), setlist_slot_base(back.body, s)
                if e.body[a:a + 24] != back.body[b:b + 24]:
                    bad_roundtrip += 1
    print(f"{family}: {files} file(s), {seen} set list(s), {refs} refs, {dang} dangling in the source, "
          f"{bad_roundtrip} slot names changed on a round trip")
    check(f"{family}-names", bad_roundtrip == 0)
    if files:
        check(f"{family}-dangling-low", dang <= max(5, refs // 200))

if fails:
    print("FAILED:", fails)
    sys.exit(1)
print("set list conversion: OK")
