"""Drum Kit and Wave Sequence conversion, either direction. Both objects are laid out identically on Kronos and
Nautilus, so only ROM sample references are remapped (the `*_to_kronos` functions take the forward map and invert it)."""
from __future__ import annotations

from Conversion.data import RomWaveformMap
from Conversion.profiles import DRUM_KIT_SIZE, WAVE_SEQ_SIZE
from Conversion.result import ConversionResult
from Conversion.zones import remap_drum_kit, remap_wave_seq
from Objects.object_body import iter_drum_kit_sample_refs
from Tools.exs_option_index import ExsOptionIndex


def _convert(body: bytes, size: int, label: str, remap, rom_map, exs: ExsOptionIndex,
             source: str = "Kronos") -> ConversionResult:
    if len(body) != size:
        return ConversionResult(None, [], [f"{source} {label} body is {len(body)} bytes, expected {size}."])
    remapped, unmapped = remap(body, rom_map, exs)
    if unmapped:
        return ConversionResult(None, [], [f"{u.where}: {u.reason}" for u in unmapped])
    return ConversionResult(remapped)


def convert_drum_kit(body: bytes, rom_map, exs: ExsOptionIndex, source: str = "Kronos") -> ConversionResult:
    """A zone whose sample has no equivalent on the other instrument is switched off and reported; a kit that would
    be left with no sound at all is refused."""
    target = getattr(rom_map, "target_name", "Nautilus")
    if len(body) != DRUM_KIT_SIZE:
        return ConversionResult(None, [], [f"{source} Drum Kit body is {len(body)} bytes, expected {DRUM_KIT_SIZE}."])
    remapped, unmapped = remap_drum_kit(body, rom_map, exs, switch_off_unmapped=True)
    if not unmapped:
        return ConversionResult(remapped)
    before = {n for n, _, _, _ in iter_drum_kit_sample_refs(body)}
    after = {n for n, _, _, _ in iter_drum_kit_sample_refs(remapped)}
    if not after:
        return ConversionResult(None, [], [f"Every sample in this kit has no {target} equivalent ({len(unmapped)} zones), "
                                           "so nothing would play."])
    shown = [f"{u.where}: {u.reason}" for u in unmapped[:5]]
    more = f" and {len(unmapped) - 5} more" if len(unmapped) > 5 else ""
    warnings = [f"{len(unmapped)} zone(s) switched off because their sample has no {target} equivalent - "
                + "; ".join(shown) + more + "."]
    silent = sorted(before - after)
    if silent:
        warnings.append("Notes now silent: " + ", ".join(str(n) for n in silent[:20])
                        + (f" and {len(silent) - 20} more" if len(silent) > 20 else "") + ".")
    return ConversionResult(remapped, warnings)


def convert_wave_seq(body: bytes, rom_map, exs: ExsOptionIndex, source: str = "Kronos") -> ConversionResult:
    """A step whose waveform has no equivalent plays a piano sound instead; the rest of the sequence is kept."""
    target = getattr(rom_map, "target_name", "Nautilus")
    if len(body) != WAVE_SEQ_SIZE:
        return ConversionResult(None, [], [f"{source} Wave Sequence body is {len(body)} bytes, expected {WAVE_SEQ_SIZE}."])
    warnings: list = []
    remapped, unmapped = remap_wave_seq(body, rom_map, exs, substitute=True, notes=warnings)
    steps = [u.where for u in unmapped]
    if steps:
        warnings.append(f"The waveform for {', '.join(steps[:12])}{' and ' + str(len(steps) - 12) + ' more' if len(steps) > 12 else ''}"
                        f" was not found on the {target} and has been substituted by a piano sound, so the sequence may "
                        "not match perfectly.")
    return ConversionResult(remapped, warnings, substitutions=[f"{u.where}: {u.reason}" for u in unmapped])


def convert_drum_kit_to_kronos(body: bytes, rom_map: RomWaveformMap, exs: ExsOptionIndex) -> ConversionResult:
    return convert_drum_kit(body, rom_map.reversed(), exs, "Nautilus")


def convert_wave_seq_to_kronos(body: bytes, rom_map: RomWaveformMap, exs: ExsOptionIndex) -> ConversionResult:
    return convert_wave_seq(body, rom_map.reversed(), exs, "Nautilus")
