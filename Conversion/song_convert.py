"""Kronos <-> Nautilus conversion of Songs, and of whole .SNG files.

What changes: the Song Timbre Set (Conversion.combi / Conversion.combi_reverse), the model byte, the BMT1 object version.
What is carried unchanged: the MIDI/master/pattern/audio events (identical format on both), Song Control (same size and
layout on both; its per-track Event Offset fields are unit-assigned and are rewritten by the instrument on load), BMT2, the
RGN1 audio-region table and the header's tail list. Songs that use audio tracks keep referencing their .WAV files, which a
.SNG does not contain. Program Change events keep their bank/program numbers."""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

import Data.song_events as ev
from Conversion.combi import convert_combi
from Conversion.combi_reverse import convert_combi_to_kronos
from Conversion.profiles import COMBI_KRONOS_SIZE, COMBI_NAUTILUS_SIZE
from Conversion.push import ConvertTables
from Data.sng_file import MODEL_KRONOS, MODEL_NAUTILUS, SngError, read_sng
from Data.sng_writer import SongParts, build_sng, parts_from_sng
from Data.song_model import SongData, from_sng_song

_RESOURCES = Path(__file__).resolve().parent.parent / "Resources" / "Conversion"
MODEL_NAMES = {MODEL_KRONOS: "Kronos", MODEL_NAUTILUS: "Nautilus"}


def _template(model: int) -> bytes:
    name = {MODEL_KRONOS: "KronosInitCombination.bin", MODEL_NAUTILUS: "NautilusInitSongTimbreSet.bin"}[model]
    return (_RESOURCES / name).read_bytes()


def convert_timbre_set(timbre_set: bytes, source: int, target: int, tables: ConvertTables,
                       template: Optional[bytes] = None) -> Tuple[Optional[bytes], List[str], List[str]]:
    """(body | None, warnings, refusals). `template` is a target-model body supplying the settings the source lacks;
    by default the bundled Init template of that model."""
    if source == target:
        return timbre_set, [], []
    if source == MODEL_KRONOS:
        r = convert_combi(timbre_set, tables.ge_catalog, template or _template(MODEL_NAUTILUS), None,
                          tables.ge_patterns, ref_map=tables.ref_map)
    else:
        r = convert_combi_to_kronos(timbre_set, template or _template(MODEL_KRONOS), tables.ref_map)
    return r.body, r.warnings, r.refusals


def convert_song(song: SongData, target: int, tables: ConvertTables) -> Tuple[Optional[SongData], List[str], List[str]]:
    body, warnings, refusals = convert_timbre_set(song.timbre_set, song.model, target, tables)
    if body is None:
        return None, warnings, refusals
    if song.events is not None and song.model != target:
        changes = sum(1 for t in song.events.tracks for e in t if e.kind == ev.PROGRAM)
        if changes:
            warnings.append(f"{changes} Program Change event(s) keep their bank and program numbers; check that those "
                            f"Programs exist on the {MODEL_NAMES[target]}.")
        if song.events.audio or song.events.patterns:
            warnings.append("The song has audio or pattern tracks: its .WAV audio files are not part of a .SNG and must "
                            "be copied to the same locations.")
    return SongData(target, body, song.control, song.events), warnings, []


def convert_sng(data: bytes, target: int, tables: ConvertTables,
                only: Optional[List[int]] = None) -> Tuple[Optional[bytes], List[str], List[str]]:
    """Converts a whole .SNG (or just the songs listed in `only`). (file bytes | None, warnings, refusals): a song that
    cannot be converted refuses the whole file, never a partial one."""
    try:
        sng = read_sng(data)
    except SngError as ex:
        return None, [], [f"Not a usable .SNG file: {ex}"]
    if sng.model not in MODEL_NAMES:
        return None, [], [f"Unknown instrument type 0x{sng.model:02X}."]
    picks = list(range(len(sng.songs))) if only is None else only
    if not picks:
        return None, [], ["No songs were selected."]
    parts: List[SongParts] = []
    warnings: List[str] = []
    for i in picks:
        try:
            song = from_sng_song(sng.songs[i], sng.model)
        except ValueError as ex:   # SongError
            return None, warnings, [f"Song {i}: not a usable song: {ex}"]
        new, w, refused = convert_song(song, target, tables)
        warnings += [f"Song {i} ({song.name}): {m}" for m in w]
        if new is None:
            return None, warnings, [f"Song {i} ({song.name}): {m}" for m in refused]
        p = parts_from_sng(sng.songs[i])
        parts.append(SongParts(new.control, new.timbre_set, p.bmt2, p.trk1))
    rgn = sng.root.find("RGN1")
    keep_regions = rgn is not None and len(picks) == len(sng.songs)
    if rgn is not None and not keep_regions:
        warnings.append("The audio-region table (RGN1) was left out because only some songs were chosen; songs that "
                        "play audio tracks need it.")
    return (build_sng(target, parts, rgn.payload if keep_regions else None, sng.header.tail if keep_regions else ()),
            warnings, [])
