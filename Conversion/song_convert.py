"""Kronos <-> Nautilus conversion of Songs, and of whole .SNG files.

What changes: the Song Timbre Set (Conversion.combi / Conversion.combi_reverse), the model byte, the BMT1 object version,
and the Program Change events' bank/program numbers (re-pointed like a Combi Timbre, in place: same size, so Song
Control's event offsets stay valid). What is carried unchanged: every other MIDI/master/pattern/audio event (identical
format on both; SysEx events are counted and reported, never rewritten), Song Control (same size and layout on both; its
per-track Event Offset fields are unit-assigned and are rewritten by the instrument on load), BMT2, the RGN1 audio-region
table and the header's tail list. Songs that use audio tracks keep referencing their .WAV files, which a .SNG does not
contain.

Program Change bank numbers (low 7 bits; bit 7 is kept as found) relate to the Combi Timbre bank byte b as INT 0-5 ->
122-127, GM / g(1)-g(d) 6-16 -> 0-10, user banks 17+ -> b - 5. Read from the four fixture .SNG files: every Nautilus
Program Change that matches its track's Timbre fits, as do the Kronos ones (user banks only).
HW-VERIFY: on a Kronos, a song whose Program Change selects an INT, GM and user-bank Program; check the bank bytes."""
from __future__ import annotations

import collections
import dataclasses
from pathlib import Path
from typing import Callable, Counter, List, Optional, Tuple

import Data.song_events as ev
from Conversion.combi import convert_combi
from Conversion.combi_reverse import convert_combi_to_kronos
from Conversion.data import factory_repoint, kronos_user_to_nautilus_bank, nautilus_user_to_kronos_bank
from Conversion.profiles import COMBI_KRONOS_SIZE, COMBI_NAUTILUS_SIZE
from Conversion.push import ConvertTables
from Data.sng_file import MODEL_KRONOS, MODEL_NAUTILUS, Node, SngError, read_sng
from Data.sng_writer import build_sng, parts_of
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
                          tables.ge_patterns, ref_map=tables.ref_map, unknown_identity=tables.unknown_identity)
    else:
        r = convert_combi_to_kronos(timbre_set, template or _template(MODEL_KRONOS), tables.ref_map,
                                    unknown_identity=tables.unknown_identity)
    return r.body, r.warnings, r.refusals


def _pc_to_timbre_bank(v: int) -> Optional[int]:
    if 122 <= v <= 127:
        return v - 122
    if 0 <= v <= 10:
        return v + 6
    if 12 <= v <= 26:
        return v + 5
    return None


def _timbre_to_pc_bank(b: int) -> Optional[int]:
    if 0 <= b <= 5:
        return 122 + b
    if 6 <= b <= 16:
        return b - 6
    if 17 <= b <= 31:
        return b - 5
    return None


def _map_program(bank: int, index: int, to_nautilus: bool, tables: ConvertTables) -> Tuple[int, int, str]:
    """(bank byte, index, how) for a Program reference in Combi Timbre numbering, as a converted Timbre would get it.
    A song never shows what its source slots hold, so a factory equivalent is always an assumption ("assumed"), used
    only when tables.unknown_identity says so; otherwise the slot is kept ("kept") with user banks shifted."""
    ref = tables.ref_map
    e = None
    if ref is not None and len(ref):
        e = ref.get(bank, index) if to_nautilus else ref.get_by_nautilus(bank, index)
    name = (e.kronos_name if to_nautilus else e.nautilus_name) if e is not None else None
    repoint, _assumed = factory_repoint(name, None, tables.unknown_identity)
    if repoint:
        if to_nautilus:
            return e.nautilus_bank, e.nautilus_index, "assumed"
        return e.kronos_bank, e.kronos_index, "assumed"
    shifted = (kronos_user_to_nautilus_bank if to_nautilus else nautilus_user_to_kronos_bank)(bank)
    return (bank if shifted is None else shifted), index, "kept"


def _convert_program_change(raw: bytes, to_nautilus: bool, tables: ConvertTables, how: Counter) -> bytes:
    e = ev.decode(raw)
    f = dict(e.fields)

    def one(bank_key: str, prog_key: str) -> str:
        v, p = f[bank_key], f[prog_key]
        b = _pc_to_timbre_bank(v & 0x7F)
        if b is None:
            return "unknown"
        nb, ni, kind = _map_program(b, p & 0x7F, to_nautilus, tables)
        nv = _timbre_to_pc_bank(nb)
        if nv is None:
            return "unknown"
        f[bank_key], f[prog_key] = (v & 0x80) | nv, (p & 0x80) | (ni & 0x7F)
        return kind

    how[one("bank", "prog")] += 1
    if not f["unfixed"]:   # the track's previous Program Change; meaningless while "unfixed" is set
        one("last_bank", "last_prog")
    return ev.encode(dataclasses.replace(e, fields=f))


def _rewrite_events(node: Node, fn: Callable[[bytes], bytes], parent: str = "") -> Node:
    """`node` with `fn` applied to every Program Change in its track (MTK1) and pattern (KTA1) event areas."""
    if node.children is None:
        if parent not in ("MTK1", "KTA1"):
            return node
        out = bytearray(node.payload)
        for i in range(12, len(out) - ev.EVENT_SIZE + 1, ev.EVENT_SIZE):
            if out[i] == ev.PROGRAM:
                out[i:i + ev.EVENT_SIZE] = fn(bytes(out[i:i + ev.EVENT_SIZE]))
        return Node(node.tag, node.dwx, bytes(out))
    return Node(node.tag, node.dwx, node.payload, [_rewrite_events(c, fn, node.tag) for c in node.children])


def _event_areas(node: Optional[Node], tag: str) -> Tuple[Tuple[ev.Event, ...], ...]:
    found = node.find(tag) if node is not None else None
    return tuple(tuple(ev.decode_all(c.payload[12:])) for c in (found.children or ())) if found is not None else ()


def _convert_events(song, to_nautilus: bool, tables: ConvertTables, warnings: List[str]):
    """The SngSong with its Program Changes re-pointed for the other instrument; reports them and its SysEx events."""
    how: Counter = collections.Counter()
    sdt = _rewrite_events(song.node, lambda raw: _convert_program_change(raw, to_nautilus, tables, how))
    trk = sdt.find("TRK1")
    mdt, adt = (trk.find("MDT1"), trk.find("ADT1")) if trk is not None else (None, None)
    tracks = _event_areas(mdt, "MTK1")
    patterns = tuple((pid, events) for (pid, _), events in zip(song.patterns, _event_areas(adt, "KTA1")))
    other = "Nautilus" if to_nautilus else "Kronos"
    if how["assumed"]:
        warnings.append(f"{how['assumed']} Program Change event(s) re-pointed to {other} factory equivalents, ASSUMING "
                        "the source slots hold factory Programs (a song does not show what they hold). Check them, or "
                        "choose 'Keep the slot numbers' in Settings > Librarian.")
    if how["kept"]:
        warnings.append(f"{how['kept']} Program Change event(s) kept their program numbers (user banks keep their "
                        f"position, U-A <-> G); those Programs must exist at the same {other} slots.")
    if how["unknown"]:
        warnings.append(f"{how['unknown']} Program Change event(s) use a bank number this converter does not know and "
                        "were left unchanged.")
    sysex = sum(1 for t in tracks + tuple(e for _, e in patterns) for e in t if e.kind == ev.EXCL)
    if sysex:
        warnings.append(f"{sysex} SysEx event(s) were carried over unchanged: Kronos and Nautilus SysEx messages differ "
                        f"(header and parameters), so on the {other} they may do nothing or something else.")
    return dataclasses.replace(song, node=sdt, tracks=tracks, patterns=patterns)


def convert_song(song: SongData, target: int, tables: ConvertTables) -> Tuple[Optional[SongData], List[str], List[str]]:
    body, warnings, refusals = convert_timbre_set(song.timbre_set, song.model, target, tables)
    if body is None:
        return None, warnings, refusals
    events = song.events
    if events is not None and song.model != target:
        events = _convert_events(events, target == MODEL_NAUTILUS, tables, warnings)
        if events.audio or events.patterns:
            warnings.append("The song has audio or pattern tracks: its .WAV audio files are not part of a .SNG and must "
                            "be copied to the same locations.")
    return SongData(target, body, song.control, events), warnings, []


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
        parts.append(parts_of(new))   # the converted song's own events (Program Changes re-pointed)
    rgn = sng.root.find("RGN1")
    keep_regions = rgn is not None and len(picks) == len(sng.songs)
    if rgn is not None and not keep_regions:
        warnings.append("The audio-region table (RGN1) was left out because only some songs were chosen; songs that "
                        "play audio tracks need it.")
    return (build_sng(target, parts, rgn.payload if keep_regions else None, sng.header.tail if keep_regions else ()),
            warnings, [])
