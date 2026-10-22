"""Drum Kit and Wave Sequence conversion. Both objects are laid out identically on Kronos and Nautilus, so only
ROM sample references are remapped."""
from __future__ import annotations

from Conversion.data import RomWaveformMap
from Conversion.profiles import DRUM_KIT_SIZE, WAVE_SEQ_SIZE
from Conversion.result import ConversionResult
from Conversion.zones import remap_drum_kit, remap_wave_seq
from Objects.object_body import iter_drum_kit_sample_refs
from Tools.exs_option_index import ExsOptionIndex


def _convert(body: bytes, size: int, label: str, remap, rom_map: RomWaveformMap, exs: ExsOptionIndex) -> ConversionResult:
    if len(body) != size:
        return ConversionResult(None, [], [f"Kronos {label} body is {len(body)} bytes, expected {size}."])
    remapped, unmapped = remap(body, rom_map, exs)
    if unmapped:
        return ConversionResult(None, [], [f"{u.where}: {u.reason}" for u in unmapped])
    return ConversionResult(remapped)


def convert_drum_kit(body: bytes, rom_map: RomWaveformMap, exs: ExsOptionIndex) -> ConversionResult:
    """A zone whose sample has no Nautilus equivalent is switched off and reported; a kit that would be left with
    no sound at all is refused."""
    if len(body) != DRUM_KIT_SIZE:
        return ConversionResult(None, [], [f"Kronos Drum Kit body is {len(body)} bytes, expected {DRUM_KIT_SIZE}."])
    remapped, unmapped = remap_drum_kit(body, rom_map, exs, switch_off_unmapped=True)
    if not unmapped:
        return ConversionResult(remapped)
    before = {n for n, _, _, _ in iter_drum_kit_sample_refs(body)}
    after = {n for n, _, _, _ in iter_drum_kit_sample_refs(remapped)}
    if not after:
        return ConversionResult(None, [], [f"Every sample in this kit has no Nautilus equivalent ({len(unmapped)} zones), "
                                           "so nothing would play."])
    shown = [f"{u.where}: {u.reason}" for u in unmapped[:5]]
    more = f" and {len(unmapped) - 5} more" if len(unmapped) > 5 else ""
    warnings = [f"{len(unmapped)} zone(s) switched off because their sample has no Nautilus equivalent - "
                + "; ".join(shown) + more + "."]
    silent = sorted(before - after)
    if silent:
        warnings.append("Notes now silent: " + ", ".join(str(n) for n in silent[:20])
                        + (f" and {len(silent) - 20} more" if len(silent) > 20 else "") + ".")
    return ConversionResult(remapped, warnings)


def convert_wave_seq(body: bytes, rom_map: RomWaveformMap, exs: ExsOptionIndex) -> ConversionResult:
    return _convert(body, WAVE_SEQ_SIZE, "Wave Sequence", remap_wave_seq, rom_map, exs)
