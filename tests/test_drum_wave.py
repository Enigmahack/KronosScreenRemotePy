"""Drum Kit / Wave Sequence object-type tests (offline): registry, bodies, PCG import, referrers,
placement patching, pull-planner scope. Run from the repo root: python tests/test_drum_wave.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import Data.object_types as ot
import Objects.object_body as ob
import Objects.erase_body as erase
from Data import pcg_file
from Data.librarian_model import (
    BatchPlacement, LibraryCatalog, ObjLoc, encode_ref, plan_batch_move, resolve_sequential_fill,
    SequentialFillItem,
)
from Data.librarian_sysex import (
    OBJ_COMBI, OBJ_DRUM_KIT, OBJ_PROGRAM, OBJ_SET_LIST, OBJ_WAVE_SEQ, PROGRAM_HD1_WIRE_SIZE,
    ObjectDump, iter_program_zone_refs, program_drum_track_ref, program_drum_track_on,
    set_program_zone_number,
)
from Data.library_pull_pipeline import all_banks, slot_count_for

fails = []


def check(name, cond):
    if not cond:
        fails.append(name)


def hd1_program(zone_refs=(), osc_mode=4, drum_track=None):
    body = bytearray(PROGRAM_HD1_WIRE_SIZE)
    body[2558] = osc_mode
    for (osc, zone, ms_type, number) in zone_refs:
        base = (2774, 3240)[osc] + zone * 22
        body[base] = ms_type
        set_program_zone_number(body, osc, zone, number)
    if drum_track is not None:
        body[1295] |= 0x10
        body[2689], body[2688] = drum_track
    return bytes(body)


# ── pull scope ──────────────────────────────────────────────────────────────
banks = all_banks()
check("pull-includes-drumkit-int", any(b.obj_type == OBJ_DRUM_KIT and b.bank == 0 for b in banks))
check("pull-excludes-gm", not any(b.obj_type == OBJ_DRUM_KIT and b.bank == 0x10 for b in banks))
check("pull-wave-user-count", sum(1 for b in banks if b.obj_type == OBJ_WAVE_SEQ) == 15)
check("pull-program-first", banks[0].obj_type == OBJ_PROGRAM)
check("slot-counts", slot_count_for(OBJ_WAVE_SEQ, 0) == 150 and slot_count_for(OBJ_DRUM_KIT, 0x40) == 16)

# ── labels ──────────────────────────────────────────────────────────────────
check("label-dk", ObjLoc(OBJ_DRUM_KIT, 0x40, 4).label() == "U-A:004")
check("label-ws", ObjLoc(OBJ_WAVE_SEQ, 0, 33).label() == "INT:033")
check("label-setlist", ObjLoc(OBJ_SET_LIST, 0, 5).label() == "Set List 05")

# ── referrers: Drum Kit via osc zone (Drums-mode oscillator), Wave Seq via ms_type 2 ────────────
prog = hd1_program([(0, 0, 1, 44), (1, 2, 2, 33)], osc_mode=4)
cat = LibraryCatalog()
cat.add_program(ObjectDump(OBJ_PROGRAM, 0x40, 7, 5, prog))
dk_sites = cat.referrers_of(ObjLoc(OBJ_DRUM_KIT, 0x40, 4))        # linear 44
ws_sites = cat.referrers_of(ObjLoc(OBJ_WAVE_SEQ, 0, 33))
check("dk-referrer", [(r.kind, r.site) for r in dk_sites] == [("osc_zone", 0)])
check("ws-referrer", [(r.kind, r.site) for r in ws_sites] == [("osc_zone", 8 + 2)])
check("dk-not-in-melodic-mode", not LibraryCatalog().referrers_of(ObjLoc(OBJ_DRUM_KIT, 0x40, 4)))
prog_melodic = hd1_program([(0, 0, 1, 44)], osc_mode=0)   # ms_type 1 outside Drums mode = a plain sample
cat_m = LibraryCatalog()
cat_m.add_program(ObjectDump(OBJ_PROGRAM, 0x40, 8, 5, prog_melodic))
check("sample-zone-not-a-drumkit-ref", not cat_m.referrers_of(ObjLoc(OBJ_DRUM_KIT, 0x40, 4)))
# a Set List slot pointing at COMBI U-A:004 must never be reported as a referrer of Drum Kit U-A:004
sl = bytearray(24 + 542 * 128)
sl[24 + 24] = 0                  # slot 0: type 0 = Combi
sl[24 + 25] = 7                  # func33 bank 7 = Combi U-A
sl[24 + 26] = 4
cat.add_setlist(ObjectDump(OBJ_SET_LIST, 0, 3, 0, bytes(sl)))
check("setlist-combi-not-drumkit-referrer", len(cat.referrers_of(ObjLoc(OBJ_DRUM_KIT, 0x40, 4))) == 1)
check("setlist-combi-is-combi-referrer", len(cat.referrers_of(ObjLoc(OBJ_COMBI, 0x40, 4))) == 1)

# ── drum track (Program -> Program) ─────────────────────────────────────────
dt_prog = hd1_program(drum_track=(18, 9))     # func33 18 = Program U-A
check("drum-track-read", program_drum_track_on(dt_prog) and program_drum_track_ref(dt_prog) == (18, 9))
cat_d = LibraryCatalog()
cat_d.add_program(ObjectDump(OBJ_PROGRAM, 0x40, 1, 5, dt_prog))
check("drum-track-referrer", [r.kind for r in cat_d.referrers_of(ObjLoc(OBJ_PROGRAM, 0x40, 9))] == ["drum_track"])

# ── placement patches the zone to the NEW linear address ────────────────────
new_dk_body = bytes(ob.DRUM_KIT_BODY_SIZE)
placement = BatchPlacement(ObjLoc(OBJ_DRUM_KIT, 0x41, 2), ObjectDump(OBJ_DRUM_KIT, 0x40, 4, 3, new_dk_body),
                           "kit", src=ObjLoc(OBJ_DRUM_KIT, 0x40, 4))
plan = plan_batch_move(cat, OBJ_DRUM_KIT, [placement], {})
check("batch-no-refuse", not plan.is_refusable)
prog_write = [w for w in plan.writes if w.obj == OBJ_PROGRAM]
check("batch-patches-program", len(prog_write) == 1)
if prog_write:
    new_lin = ot.drum_kit_loc_to_linear(0x41, 2)
    zones = {(o, z): n for o, z, _t, n in iter_program_zone_refs(prog_write[0].body)}
    check("batch-zone-repointed", zones[(0, 0)] == new_lin == 40 + 16 + 2)
    check("batch-other-zone-untouched", zones[(1, 2)] == 33)
check("batch-no-setlist-write", not [w for w in plan.writes if w.obj == OBJ_SET_LIST])

# GM drum kit bank refuses; slot past end of bank refuses
gm = plan_batch_move(LibraryCatalog(), OBJ_DRUM_KIT,
                     [BatchPlacement(ObjLoc(OBJ_DRUM_KIT, 0x10, 0), ObjectDump(OBJ_DRUM_KIT, 0, 0, 3, new_dk_body), "k")], {})
check("gm-refused", gm.is_refusable)
past = plan_batch_move(LibraryCatalog(), OBJ_DRUM_KIT,
                       [BatchPlacement(ObjLoc(OBJ_DRUM_KIT, 0x40, 16), ObjectDump(OBJ_DRUM_KIT, 0, 0, 3, new_dk_body), "k")], {})
check("past-bank-refused", past.is_refusable)

# unencodable target (GM bank has linear entries, a nonexistent slot does not)
check("encode-unencodable", encode_ref("osc_zone", ObjLoc(OBJ_DRUM_KIT, 0x40, 20)) is None)

# ── sequential fill honours per-bank slot counts ────────────────────────────
items = [SequentialFillItem(ObjLoc(OBJ_DRUM_KIT, 0x40, 0), ObjectDump(OBJ_DRUM_KIT, 0x40, 0, 3, new_dk_body))
         for _ in range(20)]
placed, pending = resolve_sequential_fill(items, OBJ_DRUM_KIT, 0x40, 0)
check("fill-user-bank-caps-at-16", len(placed) == 16 and len(pending) == 4)
placed, pending = resolve_sequential_fill(items, OBJ_DRUM_KIT, 0, 0)
check("fill-int-bank-fits-20", len(placed) == 20 and not pending)

# ── erase / init ────────────────────────────────────────────────────────────
dk = bytearray(ob.DRUM_KIT_BODY_SIZE)
dk[0:24] = b"Rock Kit".ljust(24)
erased = erase.build(OBJ_DRUM_KIT, bytes(dk))
check("erase-dk-init", ob.is_init(OBJ_DRUM_KIT, erased) and len(erased) == len(dk))
ws = bytearray(ob.WAVE_SEQ_BODY_SIZE)
check("erase-ws-init", ob.is_init(OBJ_WAVE_SEQ, erase.build(OBJ_WAVE_SEQ, bytes(ws))))

# ── PCG import: DBK1 / WBK1 chunks ──────────────────────────────────────────
def chunk(tag, count, size, bank_id, name):
    recs = []
    for i in range(count):
        r = bytearray(size)
        r[0:len(f"{name}{i}")] = f"{name}{i}".encode()
        recs.append(bytes(r))
    return pcg_file._make_bank_chunk(tag, count, size, bank_id, recs)


blob = bytearray(pcg_file._KORG_HEADER)
blob += chunk(b"DBK1", 3, ob.DRUM_KIT_BODY_SIZE, 0, "KIT")
blob += chunk(b"DBK1", 2, ob.DRUM_KIT_BODY_SIZE, 0x20001, "UKIT")
blob += chunk(b"WBK1", 150, ob.WAVE_SEQ_BODY_SIZE, 0, "WSEQ")
blob += chunk(b"DBK1", 1, ob.DRUM_KIT_BODY_SIZE, 0x2000E, "BAD")
pcg = pcg_file.open_pcg(bytes(blob))
check("pcg-opens", pcg is not None)
if pcg:
    dks = [e for e in pcg.objects if e.obj_type == OBJ_DRUM_KIT]
    wss = [e for e in pcg.objects if e.obj_type == OBJ_WAVE_SEQ]
    check("pcg-dk-count", len(dks) == 5)
    check("pcg-dk-banks", {e.bank.obj_bank for e in dks} == {0, 0x41})
    check("pcg-dk-name", any(e.name == "UKIT1" and e.bank.obj_bank == 0x41 and e.index == 1 for e in dks))
    check("pcg-ws-150", len(wss) == 150 and wss[0].bank.obj_bank == 0)
    check("pcg-bad-bank-rejected", len(pcg.rejected_banks) == 1 and pcg.rejected_banks[0].bank_id_raw == 0x2000E)
    check("pcg-wire-body-passthrough", pcg_file.wire_body_from_pcg_entry(OBJ_DRUM_KIT, dks[0]) == dks[0].body)

if fails:
    print("FAIL:", fails)
    sys.exit(1)
print("test_drum_wave OK")
