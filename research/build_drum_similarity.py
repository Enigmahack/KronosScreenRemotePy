#!/usr/bin/env python3
"""Closest-sounding ROM drum sample on the other instrument, from the decrypted audio (not from names).

    python research/build_drum_similarity.py <ROM_PCM dir> Resources/Conversion/DrumSampleSimilarity.json

<ROM_PCM dir> is "Nautilus Research/Assets/ROM_PCM" (Kronos/Bank00 and Nautilus/Bank00 pulled from the units, decrypted with
scripts/decrypt_rom_pcm.py of that folder). Samples that already have an exact audio match (NautilusRomWaveformMap.json /
NautilusSampleEvidence.json) are NOT listed: the converter uses those first.

Feature of one sample (48 kHz mono, peak-normalised, starting at its onset): log band energies (24 log-spaced bands, 60 Hz-20 kHz) of
four windows (0-30, 30-120, 120-500, 500-2000 ms), the RMS decay over ten log-spaced windows, and the log length.
Distance = RMS difference of the band/decay vector + 2 * |log length ratio|.
Check (2026-11-11): for the 720 Kronos drum samples that have an exact Nautilus twin the nearest neighbour is that twin (or an
identical block) 706 times; for the 668 without one, the nearest neighbour has the same name category in 99% of the cases below distance 5.

Tiers: "close" < 3 <= "approx" < 5; above that the nearest sample of the SAME name category (leading letters: BD, SD, TOM ...)
("category", at any distance: a sound of the same kind beats a silent zone); "nearest" when the target has no sample of that name category (or the source has none), so no zone stays silent.
Output: {"kronos_to_nautilus": {"<kronos idx>": [nautilus idx, distance, tier]}, "nautilus_to_kronos": {...}}"""
import glob, json, os, re, sys
from pathlib import Path
import numpy as np

SR = 48000
EDGES = np.geomspace(60, 20000, 25)
CLOSE, APPROX, CATEGORY_MAX = 3.0, 5.0, 1e9
TIERS = ("close", "approx", "category", "nearest")


def bands(x):
    if len(x) < 64:
        x = np.pad(x, (0, 64 - len(x)))
    n = min(1 << int(np.ceil(np.log2(len(x)))), 16384)
    seg = x[:n]
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg)), n))
    f = np.fft.rfftfreq(n, 1 / SR)
    return np.array([(spec[(f >= EDGES[i]) & (f < EDGES[i + 1])] ** 2).sum() for i in range(len(EDGES) - 1)])


def features(a):
    a = a.astype(np.float32) / 32768.0
    pk = np.abs(a).max() if len(a) else 0
    if pk < 1e-5:
        return None
    a = a / pk
    on = int(np.argmax(np.abs(a) > 0.1))
    length, a = len(a) / SR, a[on:]
    vec = np.array([bands(a[int(s * SR):int(e * SR)]) if len(a[int(s * SR):int(e * SR)]) > 32 else np.zeros(24)
                    for s, e in ((0, .03), (.03, .12), (.12, .5), (.5, 2.0))])
    vec = 10 * np.log10(vec / (vec.sum() + 1e-12) + 1e-9)
    t = np.geomspace(0.005, 3.0, 11)
    env = [20 * np.log10(np.sqrt((a[int(t[i] * SR):int(t[i + 1] * SR)] ** 2).mean()) + 1e-5) if len(a[int(t[i] * SR):int(t[i + 1] * SR)]) else -100.0
           for i in range(10)]
    return np.concatenate([vec.ravel(), np.array(env) * 0.3]), np.log(length)


def category(name):
    m = re.match(r"[A-Za-z]+", name.strip())
    return m.group(0).upper() if m else ""


def load(rom_dir, unit):
    sys.path.insert(0, str(Path(rom_dir).parent.parent / "scripts"))
    from parse_bank_metadata import parse_metadata
    from decrypt_rom_pcm import ImageSet, decrypt_block
    d = os.path.join(rom_dir, unit, "Bank00")
    meta = parse_metadata(open(os.path.join(d, "BankMetaData.img"), "rb").read())
    imgs = ImageSet(sorted(glob.glob(os.path.join(d, "Samples*.img"))))
    out = []
    for ds in meta["drumsamples"]:
        bi = meta["samples"][ds["sample"]]["block"]
        blk = meta["blocks"][bi]
        out.append((ds["name"].strip(), features(np.frombuffer(decrypt_block(bi, blk, imgs.read_block_raw(blk)), dtype="<i2"))))
    return out


def nearest(src, dst, skip):
    di = [j for j, (_, f) in enumerate(dst) if f is not None]
    DV = np.stack([dst[j][1][0] for j in di])
    DL = np.array([dst[j][1][1] for j in di])
    dcat = np.array([category(dst[j][0]) for j in di])
    out, tally = {}, {t: 0 for t in TIERS}
    for i, (name, f) in enumerate(src):
        if f is None or i in skip:
            continue
        d = np.sqrt(((DV - f[0]) ** 2).mean(1)) + 2.0 * np.abs(DL - f[1])
        o = int(np.argmin(d))
        tier = "close" if d[o] < CLOSE else "approx" if d[o] < APPROX else None
        if tier is None:
            cat = category(name)
            same = np.where(dcat == cat)[0] if cat else []
            if len(same):
                o = int(same[np.argmin(d[same])])
                tier = "category" if d[o] < CATEGORY_MAX else None
            else:
                tier = "nearest"      # the target has no sample of this kind at all: the closest sound of any kind
        if tier:
            out[str(i)] = [di[o], round(float(d[o]), 3), tier]
            tally[tier] += 1
    return out, tally


def main(rom_dir, out_path):
    here = Path(__file__).resolve().parent.parent / "Resources" / "Conversion"
    exact = json.load(open(here / "NautilusRomWaveformMap.json"))
    k_done = {e["kronos_index"] for e in exact if e["section"] == "ROM_DRUMSAMPLES"}
    n_done = {e["nautilus_index"] for e in exact if e["section"] == "ROM_DRUMSAMPLES"}
    for e in json.load(open(here / "NautilusSampleEvidence.json")):
        if e["section"] == "ROM_DRUMSAMPLES" and e["kronos_bank"] == "Bank00" and e["nautilus_index"] is not None:
            k_done.add(e["kronos_index"]); n_done.add(e["nautilus_index"])
    K, N = load(rom_dir, "Kronos"), load(rom_dir, "Nautilus")
    k2n, t1 = nearest(K, N, k_done)
    n2k, t2 = nearest(N, K, n_done)
    print("Kronos", len(K), "exact", len(k_done), "-> similar", t1, "| Nautilus", len(N), "exact", len(n_done), "-> similar", t2)
    json.dump({"kronos_to_nautilus": k2n, "nautilus_to_kronos": n2k}, open(out_path, "w"), separators=(",", ":"))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(*sys.argv[1:])
