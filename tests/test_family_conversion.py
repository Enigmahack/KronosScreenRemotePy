"""Kronos <-> Nautilus: family-aware accessors, PCG loading, conversion on entry (both directions), the Librarian's
cached per-device conversion. Real-file parts skip when the Nautilus factory PCG is absent. Run from the repo root."""
import os
import sys
import tempfile
from unittest.mock import MagicMock

os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_family_test_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from Conversion.auto import convert_object
from Conversion.combi_reverse import convert_combi_to_kronos
from Conversion.data import (RomWaveformMap, init_combi_template, init_program_template, kronos_timbre_bank_for_obj,
                             nautilus_timbre_bank_for_wire)
from Conversion.push import ConvertTables
from Data.librarian_sysex import (OBJ_COMBI, OBJ_DRUM_KIT, OBJ_PROGRAM, OBJ_WAVE_SEQ, combi_timbre_ref,
                                  iter_combi_timbre_refs, set_combi_timbre_ref)
from Data.pcg_file import FAMILY_KRONOS, FAMILY_NAUTILUS, body_family, open_pcg, wire_body_from_pcg_entry
from Tools.dependency_scanner import walk_object_references

fails = []


def check(name, cond):
    if not cond:
        fails.append(name)


# ---- Combi Timbre accessors speak Kronos numbering on a Nautilus body
n = bytearray(5640)
set_combi_timbre_ref(n, 0, 18, 10)      # Kronos U-A
set_combi_timbre_ref(n, 1, 31, 127)     # Kronos U-GG
set_combi_timbre_ref(n, 2, 5, 3)        # factory bank: unchanged
check("naut-raw-byte", n[2633] == 17 and n[2633 + 188] == 30 and n[2633 + 376] == 5)
check("naut-roundtrip", [combi_timbre_ref(bytes(n), t) for t in range(3)] == [(18, 10), (31, 127), (5, 3)])
k = bytearray(7810)
set_combi_timbre_ref(k, 0, 18, 10)
check("kronos-raw-byte", k[4803] == 18 and combi_timbre_ref(bytes(k), 0) == (18, 10))
check("naut-iter-16", len(list(iter_combi_timbre_refs(bytes(n)))) == 16)
check("bank-byte-helpers", kronos_timbre_bank_for_obj(0x40) == 18 and nautilus_timbre_bank_for_wire(0x40) == 17
      and nautilus_timbre_bank_for_wire(0x4C) == 29 and kronos_timbre_bank_for_obj(0x10) is None)
check("body-family", body_family(OBJ_COMBI, 5640) == FAMILY_NAUTILUS and body_family(OBJ_PROGRAM, 4960) == FAMILY_KRONOS
      and body_family(OBJ_PROGRAM, 3700) == FAMILY_NAUTILUS and body_family(OBJ_DRUM_KIT, 38424) is None)

# ---- Program / Combi round trips through the templates
T = ConvertTables.load()
for exi in (True, False):
    kp = bytearray(init_program_template(False, exi))
    kp[:24] = b"Roundtrip".ljust(24, b"\0")
    kp[100], kp[900] = 0x5A, 0x33                                   # in the shared head
    kp[3000] = 0x44                                                 # in the shared tail
    kp = bytes(kp)
    r = convert_object(OBJ_PROGRAM, kp, exi, FAMILY_KRONOS, FAMILY_NAUTILUS, T)
    want = 4954 if exi else 3700
    check(f"k2n-program-size-{exi}", r.body is not None and len(r.body) == want)
    back = convert_object(OBJ_PROGRAM, r.body, exi, FAMILY_NAUTILUS, FAMILY_KRONOS, T)
    check(f"n2k-program-size-{exi}", back.body is not None and len(back.body) == len(kp))
    check(f"program-head-roundtrip-{exi}", back.body[:1280] == kp[:1280])
    check(f"program-tail-roundtrip-{exi}", back.body[2558:] == kp[2558:])
    check(f"program-kronos-block-from-init-{exi}",
          back.body[1280:2558] == init_program_template(False, exi)[1280:2558])
    pcg_slot = kp if exi else kp + bytes(4960 - 3706)
    check(f"k2n-accepts-pcg-slot-{exi}", convert_object(OBJ_PROGRAM, pcg_slot, exi, FAMILY_KRONOS, FAMILY_NAUTILUS, T).body == r.body)
check("same-family-passthrough", convert_object(OBJ_PROGRAM, b"x", True, FAMILY_KRONOS, FAMILY_KRONOS, T).body == b"x")
check("wrong-size-refused", convert_object(OBJ_PROGRAM, b"x", True, FAMILY_NAUTILUS, FAMILY_KRONOS, T).body is None)

kc = bytearray(init_combi_template(False))
kc[:24] = b"Roundtrip Combi".ljust(24, b"\0")
for t, (bank, num) in enumerate([(18, 126), (24, 125), (31, 127)]):   # slots the factory map does not cover
    o = 4802 + t * 188
    kc[o], kc[o + 1], kc[o + 2] = num, bank, 0x20
nc = convert_object(OBJ_COMBI, bytes(kc), False, FAMILY_KRONOS, FAMILY_NAUTILUS, T)
check("k2n-combi", nc.body is not None and len(nc.body) == 5640)
check("k2n-combi-refs-same-wire-bank",
      [(r.ref.bank, r.ref.number) for r in walk_object_references(OBJ_COMBI, nc.body)][:3] == [(0x40, 126), (0x46, 125), (0x4D, 127)])
bc = convert_combi_to_kronos(nc.body, init_combi_template(False), None)   # no factory map: only the bank shift
check("n2k-combi", bc.body is not None and len(bc.body) == 7810
      and [combi_timbre_ref(bc.body, t) for t in range(3)] == [(18, 126), (24, 125), (31, 127)])

# ---- an unused Oscillator 2 (Single mode) does not block a Program
rm = RomWaveformMap.from_entries([{"section": "ROM_MULTISAMPLES", "kronos_index": 5, "nautilus_index": 9, "name": "a",
                                   "nautilus_name": "a", "source": "audio", "audio": "content"}])
check("reverse-map", rm.reversed().try_map("ROM_MULTISAMPLES", 9) == 5 and rm.reversed().try_map("ROM_MULTISAMPLES", 5) is None
      and rm.reversed().is_audio_identical("ROM_MULTISAMPLES", 9))

# ---- real Nautilus factory PCG
PRELOAD = os.environ.get("KRONOS_NAUTILUS_PRELOAD", "Z:/Nautilus Research/PCG Info/FACTORY_extracted/FACTORY/PRELOAD.PCG")
if not os.path.exists(PRELOAD):
    print("SKIP real Nautilus PCG: not found:", PRELOAD)
else:
    pcg = open_pcg(open(PRELOAD, "rb").read())
    check("naut-pcg-opens", pcg is not None and pcg.family == FAMILY_NAUTILUS and not pcg.rejected_banks)
    progs = {(e.bank.obj_bank, e.index): e.name for e in pcg.objects if e.obj_type == OBJ_PROGRAM}
    total = hit = 0
    for e in pcg.objects:
        if e.obj_type == OBJ_COMBI:
            for r in walk_object_references(OBJ_COMBI, e.body):
                if r.ref.bank >= 0x40:
                    total += 1
                    hit += (r.ref.bank, r.ref.number) in progs
    check("naut-combi-user-refs-resolve", total > 500 and hit == total)
    sample = [e for e in pcg.objects if e.obj_type == OBJ_PROGRAM][:200:7]
    ok = 0
    for e in sample:
        r = convert_object(OBJ_PROGRAM, wire_body_from_pcg_entry(OBJ_PROGRAM, e), e.is_exi, FAMILY_NAUTILUS, FAMILY_KRONOS, T)
        ok += r.body is not None and len(r.body) == (4960 if e.is_exi else 3706) and r.body[:24] == e.body[:24]
    check("naut-programs-mostly-convert", ok >= len(sample) * 0.6)
    ws = [e for e in pcg.objects if e.obj_type == OBJ_WAVE_SEQ][:60]
    check("naut-waveseqs-convert", sum(convert_object(OBJ_WAVE_SEQ, e.body, False, FAMILY_NAUTILUS, FAMILY_KRONOS, T).body is not None
                                      for e in ws) > len(ws) // 2)

# ---- the Librarian converts a PCG of the other family for the connected instrument, once, and can bypass it
from PySide6.QtWidgets import QApplication
app = QApplication([])
import Data.pcg_file as pf
from Models.app_settings import AppSettings
import Views.librarian_shell_window as L


class Fake:
    can_dump = True

    def __init__(self, nautilus):
        self.is_nautilus = nautilus

    def __getattr__(self, name):
        return MagicMock()

    def cached_bank_names(self, *a, **k):
        return {}


kp = bytearray(init_program_template(False, True))
kp[:24] = b"Window Program".ljust(24, b"\0")
kcb = bytearray(init_combi_template(False))
kcb[:24] = b"Window Combi".ljust(24, b"\0")
blob = pf._KORG_HEADER + pf._make_bank_chunk(b"MBK1", 1, 4960, 0x20000, [bytes(kp)]) \
    + pf._make_bank_chunk(b"CBK1", 1, 7810, 0x20000, [bytes(kcb)])
kpcg = open_pcg(blob)
check("synthetic-kronos-pcg", kpcg is not None and kpcg.family == FAMILY_KRONOS and len(kpcg.objects) == 2)

w = L.LibrarianShellWindow("1.2.3.4", Fake(True), parent=None, settings=AppSettings())
w._pcg, w._pcg_source_label = kpcg, "synthetic"
w._refresh_pcg_tree()
check("status-mentions-conversion", "converted to the Nautilus format" in w._pcg_status_label.text())
addr_p = next(a for a in w._pcg_by_addr if a[0] == OBJ_PROGRAM)
addr_c = next(a for a in w._pcg_by_addr if a[0] == OBJ_COMBI)
check("window-converts-program", len(w._pcg_resolve_content(*addr_p)) == 4954)
check("window-converts-combi", len(w._pcg_resolve_content(*addr_c)) == 5640)
check("window-raw-bypass", len(w._pcg_resolve_content(*addr_p, raw=True)) == 4960)
check("window-conversion-cached", len(w._pcg_conv_cache) == 2)
w._service = Fake(False)
check("window-no-conversion-same-family", len(w._pcg_resolve_content(*addr_p)) == 4960)
import dataclasses
w._service = Fake(True)
w._pcg = dataclasses.replace(kpcg, family=FAMILY_NAUTILUS)
check("manual-convert-blocked-for-nautilus-pcg", "already in the Nautilus format" in (w._convert_block_reason("pcg") or ""))

if fails:
    print("FAILED:", fails)
    sys.exit(1)
print("ALL PASS")
