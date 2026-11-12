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
Stereo pairs (checked 2026-11-11): the Nautilus stores a stereo drum sample as two adjacent drum samples "X-L", "X-R"; the Kronos
keeps the MONO sum ((L + R) >> 1) as one sample: 173 Kronos drum samples are byte-identical to the sum of a Nautilus pair. So a pair is
matched as one sample by its mono sum (exact hash first, then the same fingerprint search). A pair target is entered as the "-L" index
with a 4th element 1, meaning the zone must be flagged stereo (byte 15 bit 0 of its bank id, the way the factory kits reference a stereo
sample); a Nautilus "-L"/"-R" zone is looked up by its pair's sum and becomes a mono Kronos sample.
Multisamples (for Wave Sequence steps): the same search, one representative sample per multisample (the zone whose top key is the
first one at or above middle C), names compared by their first word; written as "kronos_to_nautilus_ms" / "nautilus_to_kronos_ms"
[other idx, distance, tier]. Only multisamples with no entry in the ROM waveform map / evidence are listed.
Output: {"kronos_to_nautilus": {"<kronos idx>": [nautilus idx, distance, tier, stereo]}, "nautilus_to_kronos": {"<nautilus idx>": [kronos idx, distance, tier, right_half]}}"""
import glob, hashlib, json, os, re, sys
from pathlib import Path
import numpy as np

SR = 48000
EDGES = np.geomspace(60, 20000, 25)
CLOSE, APPROX, CATEGORY_MAX = 3.0, 5.0, 1e9
TIERS = ("exact", "close", "approx", "category", "nearest")


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
    """[(name, features, sha1 of the PCM, pcm length)] and, for the Nautilus, {L index: (mix features, mix sha1)}."""
    sys.path.insert(0, str(Path(rom_dir).parent.parent / "scripts"))
    from parse_bank_metadata import parse_metadata
    from decrypt_rom_pcm import ImageSet, decrypt_block
    d = os.path.join(rom_dir, unit, "Bank00")
    meta = parse_metadata(open(os.path.join(d, "BankMetaData.img"), "rb").read())
    imgs = ImageSet(sorted(glob.glob(os.path.join(d, "Samples*.img"))))
    pcm = []
    for ds in meta["drumsamples"]:
        bi = meta["samples"][ds["sample"]]["block"]
        blk = meta["blocks"][bi]
        pcm.append((ds["name"].strip(), np.frombuffer(decrypt_block(bi, blk, imgs.read_block_raw(blk)), dtype="<i2")))
    out = [(n, features(a), hashlib.sha1(a.tobytes()).hexdigest()) for n, a in pcm]
    pairs = {}
    for i in range(len(pcm) - 1):
        (n1, l), (n2, r) = pcm[i], pcm[i + 1]
        if n1.endswith("-L") and n2 == n1[:-2] + "-R" and len(l) == len(r):
            mix = ((l.astype(np.int32) + r) >> 1).astype(np.int16)
            pairs[i] = (features(mix), hashlib.sha1(mix.tobytes()).hexdigest())
    return out, pairs


def nearest(queries, cands, skip):
    """queries / cands: [(name, features)] with an id each; returns {query id: [cand id, dist, tier]}."""
    cid = [c for c, (_, f) in cands if f is not None]
    DV = np.stack([f[0] for c, (_, f) in cands if f is not None])
    DL = np.array([f[1] for c, (_, f) in cands if f is not None])
    dcat = np.array([category(n) for c, (n, f) in cands if f is not None])
    out, tally = {}, {t: 0 for t in TIERS}
    for q, (name, f) in queries:
        if f is None or q in skip:
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
            out[q] = [cid[o], round(float(d[o]), 3), tier]
            tally[tier] += 1
    return out, tally


def load_multisamples(rom_dir, unit):
    """[(name, features)] per ROM multisample, from its middle zone."""
    sys.path.insert(0, str(Path(rom_dir).parent.parent / "scripts"))
    from parse_bank_metadata import parse_metadata
    from decrypt_rom_pcm import ImageSet, decrypt_block
    d = os.path.join(rom_dir, unit, "Bank00")
    meta = parse_metadata(open(os.path.join(d, "BankMetaData.img"), "rb").read())
    imgs = ImageSet(sorted(glob.glob(os.path.join(d, "Samples*.img"))))
    out = []
    for ms in meta["multisamples"]:
        zs = meta["zones"][ms["first_zone"]:ms["first_zone"] + ms["zone_count"]]
        if not zs:
            out.append((ms["name"].strip(), None)); continue
        z = next((z for z in zs if z["top_key?"] >= 60), zs[-1])
        bi = meta["samples"][z["sample"]]["block"]
        blk = meta["blocks"][bi]
        out.append((ms["name"].strip(), features(np.frombuffer(decrypt_block(bi, blk, imgs.read_block_raw(blk)), dtype="<i2"))))
    return out


def main(rom_dir, out_path):
    here = Path(__file__).resolve().parent.parent / "Resources" / "Conversion"
    exact = json.load(open(here / "NautilusRomWaveformMap.json"))
    k_done = {e["kronos_index"] for e in exact if e["section"] == "ROM_DRUMSAMPLES"}
    n_done = {e["nautilus_index"] for e in exact if e["section"] == "ROM_DRUMSAMPLES"}
    for e in json.load(open(here / "NautilusSampleEvidence.json")):
        if e["section"] == "ROM_DRUMSAMPLES" and e["kronos_bank"] == "Bank00" and e["nautilus_index"] is not None:
            k_done.add(e["kronos_index"]); n_done.add(e["nautilus_index"])
    (K, _), (N, pairs) = load(rom_dir, "Kronos"), load(rom_dir, "Nautilus")
    is_half = {i for p in pairs for i in (p, p + 1)}
    k_hash = {}
    for i, (_, _, h) in enumerate(K):
        k_hash.setdefault(h, i)
    k2n, n2k, t1 = {}, {}, {t: 0 for t in TIERS}
    t2 = dict(t1)
    # 1. a pair whose mono sum is byte-identical to a Kronos sample
    for p, (_, h) in pairs.items():
        k = k_hash.get(h)
        if k is not None and k not in k_done:
            k2n.setdefault(str(k), [p, 0.0, "exact", 1]); t1["exact"] += 1
        if k is not None:
            n2k[str(p)], n2k[str(p + 1)] = [k, 0.0, "exact"], [k, 0.0, "exact"]; t2["exact"] += 2
    # 2. the rest by fingerprint: Nautilus candidates are the single samples and the pair sums (a pair is one candidate, the -L index)
    n_cands = [(i, (n, f)) for i, (n, f, _) in enumerate(N) if i not in is_half] + [(p, (N[p][0][:-2], pairs[p][0])) for p in pairs]
    sim, tl = nearest([(i, (n, f)) for i, (n, f, _) in enumerate(K)], n_cands, k_done | {int(k) for k in k2n})
    for k, v in sim.items():
        k2n[str(k)] = v + [1 if v[0] in pairs else 0]
    for t, c in tl.items(): t1[t] += c
    # Nautilus -> Kronos: a half is queried by its pair's sum; Kronos candidates are single samples
    q = [(i, (n, pairs[i][0] if i in pairs else pairs[i - 1][0] if i - 1 in pairs else f)) for i, (n, f, _) in enumerate(N)]
    sim, tl = nearest(q, [(i, (n, f)) for i, (n, f, _) in enumerate(K)], n_done | {int(k) for k in n2k})
    for k, v in sim.items():
        n2k[str(k)] = v
    for t, c in tl.items(): t2[t] += c
    for k, v in n2k.items():
        v.append(1 if int(k) - 1 in pairs else 0)     # 4th element 1: the RIGHT half of a pair (its "-L" is the sample before it)
    print("Kronos", len(K), "exact", len(k_done), "-> similar", t1, "| Nautilus", len(N), "exact", len(n_done), "-> similar", t2)
    km_done = {e["kronos_index"] for e in exact if e["section"] == "ROM_MULTISAMPLES"}
    nm_done = {e["nautilus_index"] for e in exact if e["section"] == "ROM_MULTISAMPLES"}
    for e in json.load(open(here / "NautilusSampleEvidence.json")):
        if e["section"] == "ROM_MULTISAMPLES" and e["kronos_bank"] == "Bank00" and e["nautilus_index"] is not None:
            km_done.add(e["kronos_index"]); nm_done.add(e["nautilus_index"])
    KM, NM = load_multisamples(rom_dir, "Kronos"), load_multisamples(rom_dir, "Nautilus")
    k2n_ms, t3 = nearest(list(enumerate(KM)), list(enumerate(NM)), km_done)
    n2k_ms, t4 = nearest(list(enumerate(NM)), list(enumerate(KM)), nm_done)
    print("multisamples Kronos", len(KM), "exact", len(km_done), "->", t3, "| Nautilus", len(NM), "exact", len(nm_done), "->", t4)
    json.dump({"kronos_to_nautilus": k2n, "nautilus_to_kronos": n2k, "kronos_to_nautilus_ms": k2n_ms, "nautilus_to_kronos_ms": n2k_ms}, open(out_path, "w"), separators=(",", ":"))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(*sys.argv[1:])
