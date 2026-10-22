"""Kronos Song Timbre Sets from a .syx of func-0x73 Object Dumps (what `SysExService.backup_bank_to_syx` writes)."""
from __future__ import annotations

from typing import List, Tuple

from Conversion.push import ConvertItem
from Data import librarian_sysex as kx
from Data.librarian_sysex import OBJ_SONG_TIMBRE

SONG_OBJECT = 0x18          # Song = Song Timbre Set (7810 bytes) + 2 reserved + Song Control
TIMBRE_SET_SIZE = 7810


def read_song_timbre_sets(path: str) -> Tuple[List[ConvertItem], List[str]]:
    """(items, notes). Whole-Song dumps contribute their Timbre Set; anything else in the file is reported, not guessed at."""
    with open(path, "rb") as f:
        raw = f.read()
    items: List[ConvertItem] = []
    notes: List[str] = []
    for chunk in raw.split(b"\xf7"):
        if not chunk:
            continue
        d = kx.parse_object_dump(chunk + b"\xf7")
        if d is None:
            notes.append("Skipped a record that is not a Kronos object dump (a Nautilus dump, or a damaged file).")
            continue
        if d.obj not in (OBJ_SONG_TIMBRE, SONG_OBJECT):
            notes.append(f"Skipped object type 0x{d.obj:02X} at index {d.index}: not a Song Timbre Set.")
            continue
        if len(d.body) < TIMBRE_SET_SIZE:
            notes.append(f"Skipped song {d.index}: {len(d.body)} bytes, expected at least {TIMBRE_SET_SIZE}.")
            continue
        body = bytes(d.body[:TIMBRE_SET_SIZE])
        name = body[:24].split(b"\x00")[0].decode("ascii", "replace").rstrip()
        items.append(ConvertItem(OBJ_SONG_TIMBRE, f"{name} (song {d.index})", body))
    return items, notes
