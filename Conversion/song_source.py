"""Songs from a file the user picked: a .SNG (Kronos or Nautilus, with all its MIDI data) or a .syx of Object Dumps (what
`SysExService.backup_bank_to_syx` writes: Song Timbre Sets 0x02, whole Songs 0x18, either instrument's framing).

`read_songs` returns `SongData` (Data.song_model); `convert_items` turns the Timbre Sets into Convert-to-Nautilus work items.
Only the Timbre Set and the name travel over SysEx: the events exist only in a .SNG, so a Song Control is never sent."""
from __future__ import annotations

from typing import List, Tuple

import Data.librarian_sysex as kx
import Data.nautilus_sysex as nsx
from Conversion.push import ConvertItem
from Data.librarian_sysex import OBJ_SONG_TIMBRE
from Data.sng_file import MODEL_KRONOS, MODEL_NAUTILUS, SngError, read_sng
from Data.song_model import CONTROL_SIZE, TIMBRE_SET_SIZE, SongData, SongError, from_sng_song, from_whole_body

SONG_OBJECT = 0x18          # Song = Song Timbre Set + Song Control in one object


def read_songs(path: str) -> Tuple[List[SongData], List[str]]:
    """(songs, notes). Anything that cannot be used is reported in `notes`, never guessed at."""
    with open(path, "rb") as f:
        raw = f.read()
    if raw[:4] == b"KORG":
        try:
            sng = read_sng(raw)
        except SngError as ex:
            return [], [f"Not a usable .SNG file: {ex}"]
        if sng.model not in TIMBRE_SET_SIZE:
            return [], [f"Unknown instrument type 0x{sng.model:02X} in the .SNG file."]
        try:
            return [from_sng_song(s, sng.model) for s in sng.songs], []
        except SongError as ex:
            return [], [f"Not a usable .SNG file: {ex}"]
    songs: List[SongData] = []
    notes: List[str] = []
    for chunk in raw.split(b"\xf7"):
        if not chunk:
            continue
        msg = chunk + b"\xf7"
        d, model = kx.parse_object_dump(msg), MODEL_KRONOS
        if d is None:
            d, model = nsx.parse_object_dump(msg), MODEL_NAUTILUS
        if d is None and len(msg) > 2 and msg[0] == 0xF0 and msg[1] != 0x42:
            notes.append(f"Skipped a SysEx message from another maker (manufacturer 0x{msg[1]:02X}"
                         + (", Yamaha: a DX7/DX-series voice or bank" if msg[1] == 0x43 else "") + "); this is not a Korg song dump.")
        elif d is None:
            notes.append("Skipped a record that is not an object dump.")
        elif d.obj == SONG_OBJECT:
            try:
                songs.append(from_whole_body(d.body))
            except SongError as ex:
                notes.append(f"Skipped song {d.index}: {ex}")
        elif d.obj == OBJ_SONG_TIMBRE:
            if len(d.body) != TIMBRE_SET_SIZE[model]:
                notes.append(f"Skipped song {d.index}: {len(d.body)} bytes is not a Song Timbre Set.")
            else:
                songs.append(SongData(model, d.body, bytes(CONTROL_SIZE)))
        else:
            notes.append(f"Skipped object type 0x{d.obj:02X} at index {d.index}: not a song.")
    return songs, notes


def convert_items(songs: List[SongData]) -> List[ConvertItem]:
    return [ConvertItem(OBJ_SONG_TIMBRE, s.name or "(unnamed)", s.timbre_set, native=s.model == MODEL_NAUTILUS)
            for s in songs]


def read_song_timbre_sets(path: str) -> Tuple[List[ConvertItem], List[str]]:
    songs, notes = read_songs(path)
    return convert_items(songs), notes
