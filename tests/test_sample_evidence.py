"""Sample mapping from the audio comparison (Resources/Conversion/NautilusSampleEvidence.json) layered over the
hand-made ROM map, against the real bundled tables: trimmed and reversed matches, EXs banks that move into the
Nautilus ROM or onto a Nautilus EXs bank, the default piano for sounds with no equivalent, and the warnings."""
import json
import os
import sys
import tempfile

os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_evid_")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from Conversion.data import KORG_MS_PREFIX, ROM_DRUMSAMPLES, ROM_MULTISAMPLES, RomWaveformMap, _read_json
from Conversion.zones import remap_program_hd1
from Data.librarian_sysex import program_zone_reversed
from Tools.exs_option_index import ExsOptionIndex
from Tools.sample_reference_walker import iter_program_sample_zone_refs

failures = []


def check(name, ok):
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        failures.append(name)


BANK302 = bytes.fromhex("efd825c7cd1b443da08f7ee3b5d57b3e")
rmap = RomWaveformMap.from_catalog()
exs = ExsOptionIndex()


def program(zones, size=3706, base=2774):
    """zones: list of (osc, zone, uuid16, number) written into a Kronos (3706) or Nautilus (3700) HD-1 body."""
    b = bytearray(size)
    for osc, zone, uuid, number in zones:
        t = base + (0 if osc == 0 else 466) + zone * 22
        b[t] = 1
        b[t + 1:t + 17] = uuid
        b[t + 18], b[t + 19] = number & 0xFF, number >> 8
    b[(base - 216) if False else 2558 - (3706 - size)] = 1   # oscillator mode Double, so Oscillator 2 counts
    return bytes(b)


def rom(flag=0):
    return KORG_MS_PREFIX + bytes([flag])


def exs_ref(n, flag=0):
    return KORG_MS_PREFIX + bytes([((n + 1) << 1) | flag])


def refs(body):
    return list(iter_program_sample_zone_refs(body))


# ---- the map itself
base = _read_json("nautilus_rom_waveform_map.json", "NautilusRomWaveformMap.json")
manual = [e for e in base if e.get("source") == "manual"]
check("manual overrides are never replaced",
      manual and all(rmap.try_map(e["section"], e["kronos_index"]) == e["nautilus_index"] for e in manual))
t447 = rmap.lookup(ROM_MULTISAMPLES, 0, 447)
check("Glass Vox REV (a trimmed copy) maps to the forward sample, reversed", t447 and t447.index == 635 and t447.reverse)
t448 = rmap.lookup(ROM_MULTISAMPLES, 0, 448)
check("Syn-Air Vox (one trimmed zone) maps fully", t448 and t448.index == 636 and not t448.partial)
check("a normal sample is not flagged reversed", not rmap.lookup(ROM_MULTISAMPLES, 0, 197).reverse
      and rmap.lookup(ROM_MULTISAMPLES, 0, 198).reverse)
e1 = rmap.lookup(ROM_MULTISAMPLES, 1, 25)
check("Kronos EXs1 'Oboe-Vibrato' lives in the Nautilus ROM", e1 and e1.uuid is None and e1.index == 177)
e22 = rmap.lookup(ROM_MULTISAMPLES, 22, 0)
check("Kronos EXs22 maps to the Nautilus EXs bank", e22 and e22.uuid == BANK302 and e22.index == 0)
check("a Kronos EXs bank the Nautilus lacks has no target", rmap.lookup(ROM_MULTISAMPLES, 6, 0) is None)
check("drumsamples of an EXs bank map too", any(k[0] == ROM_DRUMSAMPLES for k in rmap._ext))

# ---- Kronos -> Nautilus Program
out, unmapped = remap_program_hd1(program([(0, 0, rom(), 447)]), rmap, exs)
z = refs(out)[0]
check("REV zone: forward sample, Zone MS Reverse set", z[3] == 635 and z[2] == rom() and program_zone_reversed(out, 0, 0)
      and not unmapped)
out, unmapped = remap_program_hd1(program([(0, 0, exs_ref(1, 1), 25)]), rmap, exs)
z = refs(out)[0]
check("EXs1 zone becomes a ROM zone and keeps the stereo flag", z[3] == 177 and z[2] == rom(1) and not unmapped)
out, unmapped = remap_program_hd1(program([(0, 0, exs_ref(22), 0)]), rmap, exs)
z = refs(out)[0]
check("EXs22 zone points at the Nautilus EXs bank", z[3] == 0 and z[2] == BANK302 and not unmapped)

notes = []
out, unmapped = remap_program_hd1(program([(0, 0, exs_ref(6), 3), (0, 1, rom(), 9999)]), rmap, exs, substitute=True,
                                  notes=notes)
check("no equivalent: default piano, both zones listed", len(unmapped) == 2 and all(r[3] == 2 and r[2] == rom() for r in refs(out)))
out, unmapped = remap_program_hd1(program([(0, 0, exs_ref(6), 3)]), rmap, exs)
check("without substitution the zone stays and is reported", len(unmapped) == 1 and refs(out)[0][3] == 3)

notes = []
remap_program_hd1(program([(0, 0, rom(), 447), (0, 1, rom(), 197)]), rmap, exs, notes=notes)
check("reversed and normal zones in one oscillator warn", any("mixes reversed" in n and "Oscillator 1" in n for n in notes))
notes = []
remap_program_hd1(program([(0, 0, rom(), 447), (1, 0, rom(), 197)]), rmap, exs, notes=notes)
check("reversed in one oscillator and normal in the other does not", not any("mixes reversed" in n for n in notes))

partial = next(e for e in json.load(open(os.path.join(os.path.dirname(__file__), "..", "Resources", "Conversion",
                                                       "NautilusSampleEvidence.json")))
               if e["kronos_bank"] != "Bank00" and e["match"] == "partial")
notes = []
out, unmapped = remap_program_hd1(program([(0, 0, exs_ref(int(partial["kronos_bank"][4:])), partial["kronos_index"])]),
                                  rmap, exs, notes=notes)
check("a partial multisample is used and flagged", not unmapped and any("may differ slightly" in n for n in notes))

# ---- Nautilus -> Kronos Program
back = rmap.reversed()
nbody = program([(0, 0, BANK302, 0)], size=3700, base=2768)
out, unmapped = remap_program_hd1(nbody, back, exs)
z = refs(out)[0]
check("Nautilus EXs bank maps back to Kronos EXs22", z[3] == 0 and z[2] == exs_ref(22) and not unmapped)
nbody = program([(0, 0, bytes.fromhex("65acc320645b477da46b00df2f2612aa"), 0)], size=3700, base=2768)
out, unmapped = remap_program_hd1(nbody, back, exs, substitute=True)
check("a Nautilus-only EXs bank has no Kronos equivalent", len(unmapped) == 1 and refs(out)[0][3] == 0
      and refs(out)[0][2] == rom())

# ---- Wave Sequences: an unmatched step plays the piano instead of refusing the sequence
from Conversion.media import convert_wave_seq
from Objects.object_body import iter_wave_seq_sample_refs


def wave_seq(steps):
    b = bytearray(2216)
    for i, (uuid, number) in enumerate(steps):
        s = 40 + i * 34
        b[s] = 0
        b[s + 1:s + 17] = uuid
        b[s + 18], b[s + 19] = number & 0xFF, number >> 8
    return bytes(b)


r = convert_wave_seq(wave_seq([(rom(), 447), (exs_ref(11), 95), (rom(), 197)]), rmap, exs)
steps = list(iter_wave_seq_sample_refs(r.body)) if r.body else []
check("wave sequence with an unmatched step converts", r.body is not None and not r.refusals and len(steps) >= 3)
check("matched steps remapped, the unmatched one plays ROM ms 2",
      steps and steps[0][2] == 635 and steps[2][2] == 318 and steps[1][2] == 2 and steps[1][1] == rom())
check("the user is told which step and why", len(r.substitutions) == 1 and r.substitutions[0].startswith("Step 2")
      and any("Step 2" in w and "substituted by a piano sound" in w and "may not match perfectly" in w for w in r.warnings))
r = convert_wave_seq(wave_seq([(rom(), 197)]), rmap, exs)
check("a clean sequence has no substitution warning", r.body is not None and not r.substitutions and not r.warnings)

print("ALL PASS" if not failures else "FAILED: %s" % failures)
sys.exit(1 if failures else 0)
