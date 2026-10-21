"""Port of C# PcgConversionSelfTests (conversion half). Run from the repo root: python tests/test_conversion.py"""
import json
import os
import sys
import tempfile

os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_conv_test_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Conversion import karma
from Conversion.combi import TimbreProgramRefs, convert_combi
from Conversion.data import (ARP_ENCODING_FIXED, ARP_ENCODING_NORMAL, ARP_PATTERN_SIZE, GePatternEntry,
                             KarmaGeCatalog, KarmaGePatternLibrary, ProgramRefMap, RomWaveformMap)
from Conversion.media import convert_drum_kit, convert_wave_seq
from Conversion.program import convert_program
from Conversion.profiles import ProgramKind
from Conversion.zones import remap_drum_kit, remap_program_hd1, remap_wave_seq
from Data.pcg_file import WIRE_SIZE_EXI, WIRE_SIZE_HD1
from Tools.exs_option_index import ExsOptionIndex, raw_uuid_bytes
from Tools.sample_reference_walker import iter_program_sample_zone_refs
from Objects.object_body import iter_drum_kit_sample_refs, iter_wave_seq_sample_refs

KORG_MS_PREFIX = bytes([0x4B, 0x4F, 0x52, 0x47, 0, 0, 0, 0, 0, 0, 0, 0, 0x4D, 0x53, 0x00])
EXS377 = "EXs377\nKApro Film Atmos & Pads\n377\n2,uuid:e0ec1421-4c0f-49e8-9feb-83f74fc9fce8,EXs377 KApro Film Atmos & Pads"

fails = []


def check(name, cond):
    if not cond:
        fails.append(name)


def hd1_zone(nn, number, raw_uuid=None):
    b = bytearray(WIRE_SIZE_HD1)
    t = 2774
    b[t] = 1
    if raw_uuid is None:
        b[t + 1:t + 16] = KORG_MS_PREFIX
        b[t + 16] = nn
    else:
        b[t + 1:t + 17] = raw_uuid
    b[t + 18], b[t + 19] = number & 0xFF, number >> 8
    return bytes(b)


def drum_kit(nn, sample_id):
    b = bytearray(38424)
    z = 24
    b[z] = 1
    b[z + 1:z + 16] = KORG_MS_PREFIX
    b[z + 16] = nn
    b[z + 18], b[z + 19] = sample_id & 0xFF, sample_id >> 8
    return bytes(b)


def wave_seq(nn, select):
    b = bytearray(2216)
    s = 40
    b[s] = 0
    b[s + 1:s + 16] = KORG_MS_PREFIX
    b[s + 16] = nn
    b[s + 18], b[s + 19] = select & 0xFF, select >> 8
    return bytes(b)


def rom(*entries):
    return RomWaveformMap.from_entries([dict(section=s, kronos_index=k, nautilus_index=n, name="x", **({"audio": a} if a else {}))
                                        for s, k, n, a in entries])


no_exs = ExsOptionIndex.from_catalog("{}")
rmap = rom(("ROM_MULTISAMPLES", 9, 20, None))
ge_cat = KarmaGeCatalog.from_entries([{"ge_number": 47, "name": "Comp/Lead Generated-Riff"}])
HD1, EXI = ProgramKind.HD1, ProgramKind.EXI

# ---- RomWaveformMap
check("rom-map-hit", rmap.try_map("ROM_MULTISAMPLES", 9) == 20)
check("rom-map-miss", rmap.try_map("ROM_MULTISAMPLES", 1234) is None and rmap.try_map("ROM_DRUMSAMPLES", 9) is None)
audio = rom(("ROM_MULTISAMPLES", 9, 20, "differs"), ("ROM_MULTISAMPLES", 10, 21, "exact"))
check("rom-map-audio-flag", not audio.is_audio_identical("ROM_MULTISAMPLES", 9) and audio.is_audio_identical("ROM_MULTISAMPLES", 10)
      and rmap.is_audio_identical("ROM_MULTISAMPLES", 9))
r = convert_program(hd1_zone(0, 9), HD1, audio, no_exs, KarmaGeCatalog())
check("rom-map-approx-warning", any("audio differs" in w and "#9" in w for w in r.warnings))

# ---- zone remapper
src = hd1_zone(0, 9)
out, un = remap_program_hd1(src, rmap, no_exs)
check("remap-rom-hit", next(iter_program_sample_zone_refs(out))[3] == 20 and not un)
exp = bytearray(src); exp[2774 + 18] = out[2774 + 18]; exp[2774 + 19] = out[2774 + 19]
check("remap-preserves-other-bytes", out == bytes(exp))
_, un = remap_program_hd1(hd1_zone(0, 9999), rmap, no_exs)
check("remap-rom-miss", len(un) == 1 and "9999" in un[0].reason and "ROM_MULTISAMPLES" in un[0].reason)
ram = hd1_zone(2, 42)
out, un = remap_program_hd1(ram, rmap, no_exs)
check("remap-ram-passthrough", out == ram and not un)
_, un = remap_program_hd1(hd1_zone(4, 5), rmap, no_exs)
check("remap-legacy-exs-refuses", len(un) == 1 and "EXs1" in un[0].reason)
known = ExsOptionIndex.from_catalog(json.dumps({"377": EXS377}))
_, un = remap_program_hd1(hd1_zone(0, 3, raw_uuid_bytes("e0ec1421-4c0f-49e8-9feb-83f74fc9fce8")), rmap, known)
check("remap-raw-uuid-known-exs-refuses", len(un) == 1 and "KApro Film Atmos & Pads" in un[0].reason)
unk = bytes((i * 7 + 1) & 0xFF for i in range(16))
raw = hd1_zone(0, 3, unk)
out, un = remap_program_hd1(raw, rmap, no_exs)
check("remap-raw-uuid-unresolved-passthrough", out == raw and not un)

dk_map = rom(("ROM_DRUMSAMPLES", 5, 99, None))
out, un = remap_drum_kit(drum_kit(0, 5), dk_map, no_exs)
check("drumkit-remap-hit", next(iter_drum_kit_sample_refs(out))[3] == 99 and not un)
_, un = remap_drum_kit(drum_kit(0, 9999), dk_map, no_exs)
check("drumkit-remap-miss", len(un) == 1 and "9999" in un[0].reason and "ROM_DRUMSAMPLES" in un[0].reason)
out, un = remap_wave_seq(wave_seq(0, 9), rmap, no_exs)
check("waveseq-remap-hit", next(iter_wave_seq_sample_refs(out))[2] == 20 and not un)
_, un = remap_wave_seq(wave_seq(0, 9999), rmap, no_exs)
check("waveseq-remap-miss", len(un) == 1 and "9999" in un[0].reason and "ROM_MULTISAMPLES" in un[0].reason)
check("media-wrong-size-refused", convert_drum_kit(b"x", dk_map, no_exs).body is None and convert_wave_seq(b"x", rmap, no_exs).body is None)
check("media-convert-ok", convert_drum_kit(drum_kit(0, 5), dk_map, no_exs).body is not None)

# ---- KARMA stripper
filled = bytes((i * 17 + 3) & 0xFF for i in range(WIRE_SIZE_HD1))
stripped, was = karma.strip_karma_fields(filled)
check("karma-strip-reports", was)
check("karma-strip-zeroes-range", not any(stripped[karma.KARMA_START:karma.KARMA_END + 1]))
check("karma-strip-preserves-edges", stripped[karma.KARMA_START - 1] == filled[karma.KARMA_START - 1]
      and stripped[karma.KARMA_END + 1] == filled[karma.KARMA_END + 1] and stripped[0] == filled[0] and stripped[10] == filled[10])
check("karma-strip-noop-blank", not karma.strip_karma_fields(bytes(WIRE_SIZE_HD1))[1])
chord = bytearray(WIRE_SIZE_HD1); chord[2558] = chord[2562] = 0xFF
cs, was = karma.strip_karma_fields(bytes(chord))
check("karma-chord-bits", was and cs[2558] & 0b0001_1000 == 0 and cs[2558] & 0b1110_0111 == 0b1110_0111
      and cs[2562] & 0b0111_0000 == 0 and cs[2562] & 0b0000_1111 == 0b0000_1111)
nochord = bytearray(WIRE_SIZE_HD1); nochord[2558] = 0b1110_0111; nochord[2562] = 0b0000_1111
check("karma-chord-not-reported", not karma.strip_karma_fields(bytes(nochord))[1])

ge_body = bytearray(WIRE_SIZE_HD1); ge_body[1814] = 47
w = karma.describe_ge_assignment(bytes(ge_body), ge_cat)
check("ge-warning-known", w and "#47" in w and "Comp/Lead Generated-Riff" in w)
ge_body[1814] = 200
w = karma.describe_ge_assignment(bytes(ge_body), ge_cat)
check("ge-warning-unknown", w and "#200" in w and "name unknown" in w)
check("ge-warning-zero", karma.describe_ge_assignment(bytes(WIRE_SIZE_HD1), ge_cat) is None)

# ---- program pipeline
full = bytearray(hd1_zone(0, 9)); full[1814] = 47; full[1400] = 0xAB
ok = convert_program(bytes(full), HD1, rmap, no_exs, ge_cat)
check("conv-success", ok.body is not None and not ok.refusals)
check("conv-warnings", any("#47" in w and "Comp/Lead Generated-Riff" in w for w in ok.warnings)
      and any("KARMA performance data" in w for w in ok.warnings) and any("zero-filled" in w for w in ok.warnings))
if ok.body:
    as_kronos = bytearray(len(full))
    as_kronos[:2552] = ok.body[:2552]
    as_kronos[2558:] = ok.body[2552:]
    check("conv-zone-remapped", next(iter_program_sample_zone_refs(bytes(as_kronos)))[3] == 20)
    check("conv-karma-zeroed", ok.body[1400] == 0)
    check("conv-size", len(ok.body) == WIRE_SIZE_HD1 - 6)

lay_src = bytes((i * 7 + 3) & 0xFF for i in range(WIRE_SIZE_EXI))
lay_tmpl = bytes([0x5A]) * (WIRE_SIZE_EXI - 6)
lb = convert_program(lay_src, EXI, rmap, no_exs, ge_cat, lay_tmpl)
check("relayout", lb.body and len(lb.body) == 4954 and lb.body[0] == lay_src[0] and lb.body[1279] == lay_src[1279]
      and lb.body[1280] == 0x5A and lb.body[2551] == 0x5A and lb.body[2553] == lay_src[2559] and lb.body[4953] == lay_src[4959])
check("relayout-no-zero-fill-warning", not any("zero-filled" in w for w in lb.warnings))
bad = convert_program(lay_src, EXI, rmap, no_exs, ge_cat, bytes(3700))
check("relayout-bad-template", bad.body is None and len(bad.refusals) == 1)

# HD-1 supplied as a 4960-byte PCG slot must still get its zones remapped (length alone can't tell the kinds apart)
slot = hd1_zone(0, 9) + bytes(4960 - WIRE_SIZE_HD1)
slot_r = convert_program(slot, HD1, rmap, no_exs, ge_cat)
check("hd1-pcg-slot-remapped", slot_r.body is not None and len(slot_r.body) == 3700)
slot_bad = convert_program(hd1_zone(0, 9999) + bytes(4960 - WIRE_SIZE_HD1), HD1, rmap, no_exs, ge_cat)
check("hd1-pcg-slot-refuses-unmapped", slot_bad.body is None and "9999" in slot_bad.refusals[0])
check("program-wrong-size-refused", convert_program(b"x" * 100, HD1, rmap, no_exs, ge_cat).body is None)


def scene_pn(b, n):
    o = karma.SCENE_BASE + n * karma.SCENE_STRIDE
    return b[o] | b[o + 1] << 8


arp_src = bytearray(WIRE_SIZE_HD1); arp_src[1814] = 25
arp_t = bytes([0x5A]) * (WIRE_SIZE_HD1 - 6)
d = convert_program(bytes(arp_src), HD1, rmap, no_exs, ge_cat, arp_t).body
check("arp-default-scene1", scene_pn(d, 0) == 4 and scene_pn(d, 1) == 0x5A5A and scene_pn(d, 3) == 0x5A5A)
a = convert_program(bytes(arp_src), HD1, rmap, no_exs, ge_cat, arp_t, karma.ArpOptions(program_scenes=0b1111)).body
check("arp-all-scenes", all(scene_pn(a, n) == 4 for n in range(4)))
none = convert_program(bytes(arp_src), HD1, rmap, no_exs, ge_cat, arp_t, karma.ArpOptions(program_scenes=0)).body
check("arp-no-scenes", scene_pn(none, 0) == 0x5A5A)
check("arp-warning", any("#25" in w and "P4" in w and "Scene 1" in w
                         for w in convert_program(bytes(arp_src), HD1, rmap, no_exs, ge_cat, arp_t).warnings))
check("arp-ge-map", [karma.preset_for_ge(g) for g in (26, 24, 22, 23, 25, 47)] == [0, 1, 2, 3, 4, None])
check("arp-unmapped-ge-untouched", scene_pn(ok.body or bytes(3700), 0) == 0)

pat = bytearray(ARP_PATTERN_SIZE); pat[0] = ord("T"); pat[25] = 8
lib = KarmaGePatternLibrary.parse(KarmaGePatternLibrary.pack([
    GePatternEntry(47, ARP_ENCODING_FIXED, 3, 0.95, bytes(pat)), GePatternEntry(48, ARP_ENCODING_NORMAL, 4, 0.50, bytes(pat))]))
e47 = lib.for_ge(47)
check("ge-library-roundtrip", len(lib) == 2 and e47.mode == ARP_ENCODING_FIXED and e47.steps_per_quarter == 3
      and abs(e47.score - 0.95) < 1e-9 and e47.pattern[0] == ord("T"))
check("ge-library-rejects-garbage", KarmaGePatternLibrary.parse(bytes(10)) is None)
emb = KarmaGePatternLibrary.from_catalog()
up = emb.for_ge(26)
check("ge-library-embedded", len(emb) > 0 and up and len(up.pattern) == ARP_PATTERN_SIZE and up.score > 0.9)
tsrc = bytearray(WIRE_SIZE_HD1); tsrc[1814] = 47
ws = convert_program(bytes(tsrc), HD1, rmap, no_exs, ge_cat, arp_t, karma.ArpOptions(transcribed_pattern_slot=9), lib)
nb = ws.body
check("ge-transcribed-scene1", scene_pn(nb, 0) == 2053 + 9 and scene_pn(nb, 1) == 0x5A5A)
check("ge-transcribed-settings", (nb[2442] >> 2) & 7 == 4 and nb[2444] == 100 and nb[2445] == 0x81)
check("ge-transcribed-returned", len(ws.arp_patterns) == 1 and ws.arp_patterns[0][0] == 9 and ws.arp_patterns[0][1][0] == ord("T"))
ns = convert_program(bytes(tsrc), HD1, rmap, no_exs, ge_cat, arp_t, None, lib)
check("ge-transcribed-needs-slot", not ns.arp_patterns and scene_pn(ns.body, 0) == 0x5A5A and any("no bank-B slot" in w for w in ns.warnings))
tsrc[1814] = 48
ls = convert_program(bytes(tsrc), HD1, rmap, no_exs, ge_cat, arp_t, karma.ArpOptions(transcribed_pattern_slot=9), lib)
check("ge-transcribed-threshold", not ls.arp_patterns and scene_pn(ls.body, 0) == 0x5A5A)

cs_src = bytearray(WIRE_SIZE_HD1); cs_src[1814] = 26; cs_src[1304], cs_src[1305] = 0x70, 0x2B; cs_src[1306] = 0b1100_0000
tm = bytes(WIRE_SIZE_HD1 - 6)
nb = convert_program(bytes(cs_src), HD1, rmap, no_exs, ge_cat, tm).body
check("karma-common-carried", nb[1350] == 0x70 and nb[1351] == 0x2B and nb[1349] & 0b11 == 0b11)
cs_src[1814] = 47
nb2 = convert_program(bytes(cs_src), HD1, rmap, no_exs, ge_cat, tm).body
check("arp-switch-only-when-assigned", nb2[1349] & 0b10 == 0 and nb2[1349] & 1 == 1)

ref = convert_program(hd1_zone(0, 9999), HD1, rmap, no_exs, ge_cat)
check("refusal", ref.body is None and len(ref.refusals) == 1 and "9999" in ref.refusals[0])

# ---- combi
kc = bytearray((i * 13 + 5) & 0xFF for i in range(7810))


def module(m, ge, in_ch, out_ch, top, bottom):
    k = 1814 + 744 * m
    kc[k], kc[k + 1], kc[k + 2], kc[k + 3], kc[k + 5], kc[k + 6] = ge & 0xFF, ge >> 8, in_ch, out_ch, top, bottom


module(0, 26, 0x10, 3, 100, 20); module(1, 24, 0x80 | 2, 5, 90, 30); module(2, 47, 0, 0, 0, 0); module(3, 0, 0, 0, 0, 0)
kc[1304], kc[1305], kc[1306] = 0xE0, 0x2E, 0b1000_0000
for i in (4832, 4833, 4835, 4837, 4796):
    kc[i] = 0xFF
kc[4798] = 0x42
ct = bytes([0x5A]) * 5640
cr = convert_combi(bytes(kc), ge_cat, ct)
cb = cr.body
check("combi-layout", len(cb) == 5640 and cb[0] == kc[0] and cb[1267] == kc[1267] and cb[1268] == 0x5A
      and cb[2620] == kc[4790] and cb[5639] == kc[7809])
check("combi-timbre-shift", cb[2632] == kc[4802] and cb[2633] == kc[4803] and cb[2632 + 188] == kc[4802 + 188])


def pnc(n, off):
    return cb[2440 + 44 * n + off] | cb[2440 + 44 * n + off + 1] << 8


check("combi-arp-a-b", pnc(0, 0) == 0 and pnc(0, 16) == 1 and pnc(1, 0) == 0x5A5A)
check("combi-channels-zones", cb[2452] == 3 and cb[2453] == 0x10 and cb[2468] == 5 and cb[2469] == 2
      and cb[2612] == 100 and cb[2613] == 20 and cb[2616] == 90 and cb[2617] == 30)
check("combi-karma-common", cb[1350] == 0xE0 and cb[1351] == 0x2E and cb[1349] & 0b11 == 0b10)
check("combi-kronos-bits", cb[2662] == kc[4832] & 0x87 and cb[2663] == kc[4833] & 0x9F and cb[2665] == kc[4835]
      and cb[2667] == kc[4837] & 0x7F and cb[2626] == 0x5A and cb[2631] == 0x5A)
check("combi-module-c-dropped", any("Module C" in w and "#47" in w and "dropped" in w for w in cr.warnings))
cc = convert_combi(bytes(kc), ge_cat, ct, karma.ArpOptions(combi_arp_a_source=2, combi_arp_b_source=None))
check("combi-module-choice", any("Module A" in w and "dropped" in w for w in cc.warnings)
      and any("Module C" in w and "no translation" in w for w in cc.warnings))
ref_map = ProgramRefMap.from_entries([dict(kronos_bank=0, kronos_index=5, kronos_name="Grand", nautilus_bank=17,
                                           nautilus_index=9, nautilus_name="Grand N", source="korg")])
kr = bytearray(kc)
for t in range(16):
    o = 4802 + 188 * t
    kr[o], kr[o + 1], kr[o + 2] = 5, 0, 0x20 if t < 2 else 0
rm = convert_combi(bytes(kr), ge_cat, ct, None, None, TimbreProgramRefs.FACTORY_EQUIVALENTS, ref_map).body
check("combi-ref-remapped", rm[2632] == 9 and rm[2633] == 17 and rm[2632 + 188] == 9 and rm[2632 + 2 * 188] == 5)
keep = convert_combi(bytes(kr), ge_cat, ct, None, None, TimbreProgramRefs.KEEP_SLOTS, ref_map).body
check("combi-ref-keep", keep[2632] == 5 and keep[2633] == 0)
ed = convert_combi(bytes(kr), ge_cat, ct, None, None, TimbreProgramRefs.FACTORY_EQUIVALENTS, ref_map, lambda b, i: "My Edit")
check("combi-ref-edited", ed.body[2632] == 5 and any("My Edit" in w for w in ed.warnings))
check("combi-wrong-size", convert_combi(bytes(100), ge_cat).body is None)

# ---- Nautilus PCG writer / reader
from Conversion.nautilus_pcg import (open_nautilus_pcg, write_single_drum_kit, write_single_program,
                                     write_single_wave_seq)
named = bytearray(ok.body); named[:24] = b"ROUNDTRIP TEST".ljust(24, b" ")
pf = write_single_program(bytes(named), is_exi=False)
check("writer-header", pf[:4] == b"KORG" and pf[4] == 0xDD and pf[5] == 0)
check("writer-pcg1-size", int.from_bytes(pf[20:24], "big") == len(pf) - 0x1C)
rt = open_nautilus_pcg(pf)
check("writer-roundtrip", rt and not rt.rejected_banks and not rt.checksum_warnings and len(rt.objects) == 1
      and rt.objects[0].body == bytes(named) and rt.objects[0].name == "ROUNDTRIP TEST" and not rt.objects[0].is_exi)
check("writer-exi-tag", open_nautilus_pcg(write_single_program(lay_tmpl, is_exi=True)).objects[0].is_exi)
dkf = open_nautilus_pcg(write_single_drum_kit(drum_kit(0, 5)))
check("writer-drumkit", dkf and len(dkf.objects) == 1 and dkf.objects[0].body == drum_kit(0, 5) and not dkf.checksum_warnings)
wsf = open_nautilus_pcg(write_single_wave_seq(wave_seq(0, 9)))
check("writer-waveseq", wsf and len(wsf.objects) == 1 and wsf.objects[0].body == wave_seq(0, 9) and not wsf.checksum_warnings)
check("reader-rejects-kronos", open_nautilus_pcg(b"KORG\x68\x00" + bytes(20)) is None)
REAL_NAUT = "Z:/Nautilus Research/PCG Info/FACTORY_extracted/FACTORY/PRELOAD.PCG"
if os.path.exists(REAL_NAUT):
    real = open_nautilus_pcg(open(REAL_NAUT, "rb").read())
    progs = [o for o in real.objects if o.obj_type == 0]
    check("real-nautilus-read", real and not real.checksum_warnings and not real.rejected_banks and len(progs) > 100
          and all(o.name for o in progs[:5]))

# ---- bundled data loads
check("data-rom-map", len(RomWaveformMap.from_catalog()) > 1000)
check("data-ref-map", len(ProgramRefMap.from_catalog()) > 800)
check("data-ge-names", len(KarmaGeCatalog.from_catalog()) > 2000)

if fails:
    print("FAILED:", fails)
    sys.exit(1)
print("ALL PASS")
