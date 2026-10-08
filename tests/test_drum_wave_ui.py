"""Drum Kit / Wave Sequence through the real Librarian window against a duck-typed fake instrument:
pull -> tree roots/leaves, properties readout, rename -> push, swap with Program osc-zone repointing,
PCG import -> Merge -> Auto-Fill (dependency order + reference resolution).
Run from the repo root: python tests/test_drum_wave_ui.py"""
import hashlib
import os
import sys
import tempfile
import time

tmp = tempfile.mkdtemp(prefix="kr_dw_ui_")
os.environ["KRONOS_DATA_DIR"] = tmp
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unittest.mock import MagicMock
from PySide6.QtWidgets import QApplication, QMessageBox

app = QApplication([])

from Models.app_settings import AppSettings
from Data import pcg_file
from Data.librarian_sysex import (
    OBJ_DRUM_KIT, OBJ_PROGRAM, OBJ_WAVE_SEQ, PROGRAM_HD1_WIRE_SIZE, iter_program_zone_refs,
    set_program_zone_number,
)
import Data.object_types as ot
import Objects.object_body as ob
from Data.librarian_model import ObjLoc
import Views.librarian_shell_window as L


def wait(cond, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


def named(size, name):
    b = bytearray(size)
    b[0:24] = name.encode().ljust(24)
    return bytes(b)


def hd1_program(name, zones):
    b = bytearray(named(PROGRAM_HD1_WIRE_SIZE, name))
    b[2558] = 4   # Drums-mode oscillator
    for osc, zone, ms_type, number in zones:
        base = (2774, 3240)[osc] + zone * 22
        b[base] = ms_type
        set_program_zone_number(b, osc, zone, number)
    return bytes(b)


class Fake:
    def __init__(self):
        self.can_dump = True
        self.writes, self.stores = [], []
        lin44 = ot.drum_kit_loc_to_linear(0x40, 4)
        self.objs = {
            (OBJ_PROGRAM, 0x40): {7: (5, hd1_program("DrumProg", [(0, 0, 1, lin44)]))},
            (OBJ_DRUM_KIT, 0): {3: (3, named(ob.DRUM_KIT_BODY_SIZE, "Rock Kit"))},
            (OBJ_DRUM_KIT, 0x40): {4: (3, named(ob.DRUM_KIT_BODY_SIZE, "Funk Kit")),
                                   5: (3, named(ob.DRUM_KIT_BODY_SIZE, "Jazz Kit"))},
            (OBJ_WAVE_SEQ, 0): {1: (1, named(ob.WAVE_SEQ_BODY_SIZE, "Pad Seq"))},
        }

    def bank_digest(self, obj, bank):
        return hashlib.sha1(f"{obj}:{bank}:{sorted(self.objs.get((obj, bank), {}))}".encode()).digest()

    backfill = False   # a real instrument has no empty slot: anything unwritten holds an INIT object

    def dump_object_parsed(self, obj, bank, number, no_response_ms=3000):
        o = self.objs.get((obj, bank), {}).get(number)
        if o is None and self.backfill:
            size, ver, nm = {OBJ_PROGRAM: (PROGRAM_HD1_WIRE_SIZE, 5, "Init Program"), OBJ_DRUM_KIT: (ob.DRUM_KIT_BODY_SIZE, 3, "Init Drum Kit"),
                             OBJ_WAVE_SEQ: (ob.WAVE_SEQ_BODY_SIZE, 1, "Init Wave Sequence")}[obj]
            o = (ver, named(size, nm))
        if o is None:
            return None
        d = MagicMock(); d.version, d.body = o
        return d

    def write_object(self, op):
        self.writes.append((op.obj, op.bank, op.index, op.version, op.body))
        self.objs.setdefault((op.obj, op.bank), {})[op.index] = (op.version, op.body)
        return 0

    def store_bank(self, obj, bank):
        self.stores.append((obj, bank)); return 0

    def backup_objects(self, ops, path): return None
    def sync_names(self, **k): return None
    def cached_bank_names(self, *a, **k): return {}
    def __getattr__(self, name): return MagicMock()


fake = Fake()
w = L.LibrarianShellWindow("1.2.3.4", fake, parent=None, settings=AppSettings())
assert wait(lambda: not w._probe_running)
QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)

# ── 1. pull ──────────────────────────────────────────────────────────────────
w._set_sync_mode(L.SYNC_PULL_ONLY)
w._start_sync(); assert wait(lambda: not w._busy, 120), "pull"
assert w._index.get(OBJ_DRUM_KIT, 0x40, 4).display_name == "Funk Kit"
assert w._index.get(OBJ_WAVE_SEQ, 0, 1).display_name == "Pad Seq"
assert w._index.get(OBJ_DRUM_KIT, 0, 3).version == 3
roots = {w._tree_local.topLevelItem(i).text(0).split("  ")[0].split(" (")[0]: w._tree_local.topLevelItem(i)
         for i in range(w._tree_local.topLevelItemCount())}
assert list(roots)[:5] == ["Programs", "Combis", "Set Lists", "Drum Kits", "Wave Sequences"], list(roots)
dk_banks = [roots["Drum Kits"].child(i).text(0) for i in range(roots["Drum Kits"].childCount())]
assert dk_banks == ["INT", "U-A"], dk_banks
leaf = roots["Drum Kits"].child(1).child(0)
assert leaf.text(0).startswith("Funk Kit") and leaf.data(0, 0x100)[:4] == ("local", OBJ_DRUM_KIT, 0x40, 4)
print("pull + tree ok:", dk_banks, "|", leaf.text(0))

# ── 2. readout / init / catalog ──────────────────────────────────────────────
lines, rows = w._object_body_readout(OBJ_DRUM_KIT, named(ob.DRUM_KIT_BODY_SIZE, "x"))
assert lines and "Populated sample zones: 0" in lines[0]
cat = w._build_local_catalog()
sites = cat.referrers_of(ObjLoc(OBJ_DRUM_KIT, 0x40, 4))
assert [(r.kind, r.ref_bank, r.ref_index) for r in sites] == [("osc_zone", 0x40, 7)], sites
assert not cat.referrers_of(ObjLoc(OBJ_DRUM_KIT, 0x40, 5))
assert w._is_init_body(OBJ_DRUM_KIT, named(ob.DRUM_KIT_BODY_SIZE, "Init Drum Kit"))
print("readout + referrers ok")

# ── 3. rename -> dirty -> 2-way push writes a Drum Kit with its own version byte ──────────
loc = ObjLoc(OBJ_DRUM_KIT, 0x40, 5)
entry = w._index.get(*(loc.obj_type, loc.bank, loc.number))
new_body = L._name_writer(OBJ_DRUM_KIT)(w._blobs.get(entry.current_hash), "Jazz Kit v2")
w._write_local_body_edit(loc, new_body, 'name to "Jazz Kit v2"')
assert w._index.get(OBJ_DRUM_KIT, 0x40, 5).is_dirty
w._set_sync_mode(L.SYNC_TWO_WAY)
w._start_sync(); assert wait(lambda: not w._busy, 120), "push"
dk_writes = [x for x in fake.writes if x[0] == OBJ_DRUM_KIT]
assert [(x[1], x[2], x[3]) for x in dk_writes] == [(0x40, 5, 3)], dk_writes
assert (OBJ_DRUM_KIT, 0x40) in fake.stores and len(dk_writes[0][4]) == ob.DRUM_KIT_BODY_SIZE
assert not w._index.get(OBJ_DRUM_KIT, 0x40, 5).is_dirty
print("rename + push ok: wrote", [(hex(x[1]), x[2], f"v{x[3]}") for x in dk_writes], "stored", fake.stores)

# ── 4. swap two Drum Kits: the Program's osc zone must follow ────────────────────────────
a, b = ObjLoc(OBJ_DRUM_KIT, 0x40, 4), ObjLoc(OBJ_DRUM_KIT, 0x40, 5)
assert w._swap_local_at(a, b)
prog = w._blobs.get(w._index.get(OBJ_PROGRAM, 0x40, 7).current_hash)
zone0 = [n for o, z, t, n in iter_program_zone_refs(prog) if (o, z) == (0, 0)][0]
assert zone0 == ot.drum_kit_loc_to_linear(0x40, 5), zone0   # was U-A:004 -> now follows the kit to U-A:005
assert w._index.get(OBJ_DRUM_KIT, 0x40, 5).display_name == "Funk Kit"
assert w._index.get(OBJ_DRUM_KIT, 0x40, 4).display_name == "Jazz Kit v2"
print("swap ok: zone now", zone0)

# ── 5. PCG -> Merge -> Auto-Fill: Drum Kit lands in a Drum Kit bank, Program resolves to it ──
def chunk(tag, size, bank_id, recs):
    return pcg_file._make_bank_chunk(tag, len(recs), size, bank_id, recs)


kit_a, kit_b = named(ob.DRUM_KIT_BODY_SIZE, "PCG Kit A"), named(ob.DRUM_KIT_BODY_SIZE, "PCG Kit B")
wseq = named(ob.WAVE_SEQ_BODY_SIZE, "PCG Wave Seq")
pcg_prog = bytearray(hd1_program("PcgDrumProg", [(0, 0, 1, 1)]))   # linear 1 = INT:001 = "PCG Kit B"
pcg_prog = bytes(pcg_prog) + bytes(pcg_file.PCG_SLOT_SIZE - len(pcg_prog))
blob = bytearray(pcg_file._KORG_HEADER)
blob += chunk(b"DBK1", ob.DRUM_KIT_BODY_SIZE, 0, [kit_a, kit_b])
blob += chunk(b"WBK1", ob.WAVE_SEQ_BODY_SIZE, 0, [wseq])
blob += chunk(b"PBK1", pcg_file.PCG_SLOT_SIZE, 0x04, [pcg_prog])
w._pcg = pcg_file.open_pcg(bytes(blob))
w._pcg_source_label = "synthetic.pcg"
w._refresh_pcg_tree()
pcg_roots = [w._tree_pcg.topLevelItem(i).text(0) for i in range(w._tree_pcg.topLevelItemCount())]
assert "Drum Kits" in pcg_roots and "Programs" in pcg_roots, pcg_roots
w._tree_pcg.expandAll()
prog_root = next(w._tree_pcg.topLevelItem(i) for i in range(w._tree_pcg.topLevelItemCount())
                 if w._tree_pcg.topLevelItem(i).text(0) == "Programs")
w._tree_pcg.setCurrentItem(prog_root.child(0).child(0))
w._pull_pcg_selected_into_merge()
types_in_merge = sorted(e.obj_type for e in w._merge.entries)
assert types_in_merge == [OBJ_PROGRAM, OBJ_DRUM_KIT], types_in_merge   # program + its auto-pulled kit
w._auto_fill_to_library()
assert wait(lambda: not w._auto_fill_queue and not w._auto_fill_timer.isActive(), 60)
placed_kit = w._index.find_by_content_hash(OBJ_DRUM_KIT, ob.__dict__.get("_h", "") or
                                           L.BlobStore.compute_hash(kit_b))
assert placed_kit is not None, "Drum Kit B was not placed"
kit_bank, kit_slot = placed_kit
assert ot.is_read_only(OBJ_DRUM_KIT, kit_bank) is False
new_prog_loc = next(k for k in w._index.entries if k.startswith(f"{OBJ_PROGRAM}:") and
                    w._index.entries[k].display_name == "PcgDrumProg")
new_prog = w._blobs.get(w._index.entries[new_prog_loc].current_hash)
placed_zone = [n for o, z, t, n in iter_program_zone_refs(new_prog) if (o, z) == (0, 0)][0]
assert placed_zone == ot.drum_kit_loc_to_linear(kit_bank, kit_slot), (placed_zone, kit_bank, kit_slot)
print("pcg -> merge -> auto-fill ok: kit at", ObjLoc(OBJ_DRUM_KIT, kit_bank, kit_slot).label(),
      "| program zone repointed to linear", placed_zone)

# the version byte of a PCG-imported object must be the CURRENT OS's (C# saw Reply Code 3 on real
# hardware from a placeholder version) — Drum Kit 3, Wave Sequence 1 — and is what reaches the wire
assert w._index.get(OBJ_DRUM_KIT, kit_bank, kit_slot).version == 3
# a Wave Sequence staged alone from the PCG
w._tree_pcg.expandAll()
ws_root = next(w._tree_pcg.topLevelItem(i) for i in range(w._tree_pcg.topLevelItemCount())
               if w._tree_pcg.topLevelItem(i).text(0) == "Wave Sequences")
w._tree_pcg.setCurrentItem(ws_root.child(0).child(0))
w._pull_pcg_selected_into_merge()
w._auto_fill_to_library()
assert wait(lambda: not w._auto_fill_queue and not w._auto_fill_timer.isActive(), 60)
ws_at = w._index.find_by_content_hash(OBJ_WAVE_SEQ, L.BlobStore.compute_hash(wseq))
assert ws_at is not None and w._index.get(OBJ_WAVE_SEQ, *ws_at).version == 1, ws_at
fake.writes.clear(); fake.stores.clear()
fake.backfill = True
w._set_sync_mode(L.SYNC_TWO_WAY)
w._start_sync(); assert wait(lambda: not w._busy, 120), "push 2"
by_type = {x[0]: x for x in fake.writes}
assert by_type[OBJ_DRUM_KIT][3] == 3 and by_type[OBJ_WAVE_SEQ][3] == 1, [(x[0], x[3]) for x in fake.writes]
assert len(by_type[OBJ_DRUM_KIT][4]) == ob.DRUM_KIT_BODY_SIZE and len(by_type[OBJ_WAVE_SEQ][4]) == ob.WAVE_SEQ_BODY_SIZE
print("pcg-imported versions on the wire ok:", [(x[0], f"v{x[3]}") for x in fake.writes if x[0] in (4, 5)])

# deleting a Drum Kit: no blank template exists for obj04, so the delete must fall back to
# erase_body.build (an init-named body of the full wire size) rather than silently doing nothing
victim = ObjLoc(OBJ_DRUM_KIT, 0, 3)                       # "Rock Kit", pulled from the instrument
def find_leaf(tree, payload):
    stack = [tree.topLevelItem(k) for k in range(tree.topLevelItemCount())]
    while stack:
        it = stack.pop()
        if it.data(0, 0x100) == payload:
            return it
        stack.extend(it.child(k) for k in range(it.childCount()))
    return None


w._tree_local.expandAll()
w._tree_local.setCurrentItem(find_leaf(w._tree_local, ("local", OBJ_DRUM_KIT, 0, 3)))
sel = w._tree_local.currentItem().data(0, 0x100)
assert sel[1:] == (OBJ_DRUM_KIT, 0, 3), sel
w._toggle_delete_local_selected()
ent = w._index.get(OBJ_DRUM_KIT, 0, 3)
assert ent is not None and ent.pending_delete, "delete did not stage"
erased = w._blobs.get(ent.current_hash)
assert len(erased) == ob.DRUM_KIT_BODY_SIZE and ob.is_init(OBJ_DRUM_KIT, erased), (len(erased), erased[:24])
fake.writes.clear()
w._start_sync(); assert wait(lambda: not w._busy, 120), "push 3"
erase_writes = [x for x in fake.writes if (x[0], x[1], x[2]) == (OBJ_DRUM_KIT, 0, 3)]
assert len(erase_writes) == 1 and ob.is_init(OBJ_DRUM_KIT, erase_writes[0][4]) and erase_writes[0][3] == 3
assert w._index.get(OBJ_DRUM_KIT, 0, 3) is None or not w._index.get(OBJ_DRUM_KIT, 0, 3).pending_delete
print("delete -> erase_body fallback -> pushed init kit ok")

w._closed = True
print("ALL PASS")
