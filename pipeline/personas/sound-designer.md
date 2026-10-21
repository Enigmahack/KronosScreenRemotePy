# Persona: Sound designer

Builds and converts sounds. Works offline at a desk, with large libraries (thousands of programs,
hundreds of samples), mixes content from several instruments and sources. Cares about fidelity and
metadata surviving every move. Will not notice a bad conversion until much later.

Data they bring: multi-GB sample sets (WAV, existing KSC/KMP/KSF), old PCG files from Kronos and
Nautilus, third-party libraries with odd names, mixed stereo/mono, unusual loop points.

## Scenarios

| ID | Task | Tier |
|---|---|---|
| SD1 | Import a folder of WAVs (mixed rate/bit depth/stereo/mono) into a new KSC, set loops, push to unit | offline + live |
| SD2 | Edit zones: add up to the 128 cap, delete, rename, shortcut/link samples, undo the last delete | offline |
| SD3 | Pull a KSC with its dependency closure, edit one sample, push back; only edited files may be written | live (fake FTP offline) |
| SD4 | Convert Kronos programs/combis/drum kits/wave sequences to Nautilus and the reverse; compare result | offline fixtures + live |
| SD5 | Rename a multisample with stereo partner and 22-char limit; names with unicode, trailing spaces, duplicates | offline |
| SD6 | Open a damaged or truncated KSF/KMP/KSC; app must refuse cleanly, never half-write | offline |
| SD7 | Dependency scan on a PCG that references missing samples; resolve or report | offline |
| SD8 | Merge a third-party PCG into an existing library; no existing object overwritten without confirm | offline + live |

## Invariants (a violation is always a bug)

- Round trip of an untouched file is byte-identical.
- Nothing is written to the instrument or disk before an explicit confirm on a destructive action.
- Top-level storage volumes are never renamed or deleted.
- A cancelled or failed operation leaves source and destination exactly as they were.
- Converted content keeps names, category, and sample references; losses are reported, not silent.
