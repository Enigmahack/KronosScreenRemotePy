"""Drum Kit and Wave Sequence conversion. Both objects are laid out identically on Kronos and Nautilus, so only
ROM sample references are remapped."""
from __future__ import annotations

from Conversion.data import RomWaveformMap
from Conversion.profiles import DRUM_KIT_SIZE, WAVE_SEQ_SIZE
from Conversion.result import ConversionResult
from Conversion.zones import remap_drum_kit, remap_wave_seq
from Tools.exs_option_index import ExsOptionIndex


def _convert(body: bytes, size: int, label: str, remap, rom_map: RomWaveformMap, exs: ExsOptionIndex) -> ConversionResult:
    if len(body) != size:
        return ConversionResult(None, [], [f"Kronos {label} body is {len(body)} bytes, expected {size}."])
    remapped, unmapped = remap(body, rom_map, exs)
    if unmapped:
        return ConversionResult(None, [], [f"{u.where}: {u.reason}" for u in unmapped])
    return ConversionResult(remapped)


def convert_drum_kit(body: bytes, rom_map: RomWaveformMap, exs: ExsOptionIndex) -> ConversionResult:
    return _convert(body, DRUM_KIT_SIZE, "Drum Kit", remap_drum_kit, rom_map, exs)


def convert_wave_seq(body: bytes, rom_map: RomWaveformMap, exs: ExsOptionIndex) -> ConversionResult:
    return _convert(body, WAVE_SEQ_SIZE, "Wave Sequence", remap_wave_seq, rom_map, exs)
