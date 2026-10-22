"""The Song Library: Songs kept on this computer, from .SNG files and from instruments.

A song that carries its events (read from a .SNG) is stored as a single-song .SNG, so the files in the library are ordinary
Korg song files. A song read over SysEx has only its Timbre Set and Song Control (Korg disabled the SysEx event dump); it is
stored as the whole-Song object body and can supply Timbre Sets but cannot be exported to a .SNG.
Entries are keyed by a hash of their stored bytes, so adding the same song twice adds it once. Audio files (.WAV) referenced
by a song's audio tracks are not stored."""
from __future__ import annotations

import hashlib
import json
import pathlib
import time
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import Data.song_events as ev
from Data.sng_file import MODEL_KRONOS, MODEL_NAUTILUS, read_sng
from Data.sng_writer import SngBuildError, build_sng, parts_of
from Data.song_model import SongData, from_sng_song, from_whole_body

KIND_SNG, KIND_OBJECT = "sng", "object"
_EXT = {KIND_SNG: ".SNG", KIND_OBJECT: ".song"}


@dataclass
class SongEntry:
    id: str
    name: str
    model: int
    kind: str
    source: str
    added_utc: str
    tracks: int = 0
    notes: int = 0
    bars: int = 0
    has_audio: bool = False

    @property
    def has_events(self) -> bool:
        return self.kind == KIND_SNG


def _describe(song: SongData) -> Dict[str, object]:
    e = song.events
    if e is None:
        return {}
    return {"tracks": sum(1 for t in e.tracks if len(t) > 1),
            "notes": sum(1 for t in e.tracks for x in t if x.kind == ev.NOTE),
            "bars": sum(1 for x in e.master if x.kind == ev.BAR),
            "has_audio": bool(e.audio)}


class SongLibrary:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = pathlib.Path(root)
        self._index_path = self.root / "index.json"
        self._entries: Dict[str, SongEntry] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self._index_path.read_text(encoding="utf-8"))
            self._entries = {e["id"]: SongEntry(**e) for e in raw.get("songs", [])
                             if (self.root / (e["id"] + _EXT.get(e.get("kind"), ""))).exists()}
        except (OSError, ValueError, KeyError, TypeError):
            self._entries = {}

    def _save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        text = json.dumps({"songs": [asdict(e) for e in self._entries.values()]}, indent=1)
        tmp = self._index_path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self._index_path)

    def entries(self) -> List[SongEntry]:
        return list(self._entries.values())

    def path_of(self, entry_id: str) -> pathlib.Path:
        e = self._entries[entry_id]
        return self.root / (e.id + _EXT[e.kind])

    def add(self, songs: Sequence[SongData], source: str) -> Tuple[List[str], List[str]]:
        """(ids added, notes). A song already in the library is skipped; one that cannot be stored is reported."""
        self.root.mkdir(parents=True, exist_ok=True)
        added: List[str] = []
        notes: List[str] = []
        for song in songs:
            if song.events is not None:
                kind, data = KIND_SNG, build_sng(song.model, [parts_of(song)])
            else:
                kind, data = KIND_OBJECT, song.whole_body()
            sid = f"{song.model:02x}-{hashlib.sha1(data).hexdigest()[:16]}"
            if sid in self._entries:
                notes.append(f"{song.name or '(unnamed)'}: already in the library.")
                continue
            path = self.root / (sid + _EXT[kind])
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
            self._entries[sid] = SongEntry(sid, song.name, song.model, kind, source,
                                           time.strftime("%Y-%m-%dT%H:%M:%S"), **_describe(song))
            if song.events is not None and song.events.audio:
                notes.append(f"{song.name}: plays audio tracks; its .WAV files are not stored.")
            added.append(sid)
        if added:
            self._save()
        return added, notes

    def get(self, entry_id: str) -> SongData:
        e = self._entries[entry_id]
        data = self.path_of(entry_id).read_bytes()
        if e.kind == KIND_OBJECT:
            return from_whole_body(data)
        sng = read_sng(data)
        return from_sng_song(sng.songs[0], sng.model)

    def remove(self, ids: Sequence[str]) -> int:
        n = 0
        for sid in ids:
            e = self._entries.pop(sid, None)
            if e is not None:
                try:
                    (self.root / (e.id + _EXT[e.kind])).unlink()
                except OSError:
                    pass
                n += 1
        if n:
            self._save()
        return n

    def export_sng(self, ids: Sequence[str], target_model: int, tables) -> Tuple[Optional[bytes], List[str], List[str]]:
        """One .SNG holding the chosen songs, converted to `target_model`. (bytes | None, warnings, refusals)."""
        from Conversion.song_convert import convert_song
        warnings: List[str] = []
        parts = []
        for sid in ids:
            e = self._entries[sid]
            if not e.has_events:
                return None, warnings, [f"{e.name}: came over SysEx, so it has no MIDI data and cannot be written to a .SNG."]
            new, w, refused = convert_song(self.get(sid), target_model, tables)
            warnings += [f"{e.name}: {m}" for m in w]
            if new is None:
                return None, warnings, [f"{e.name}: {m}" for m in refused]
            try:
                parts.append(parts_of(new))
            except SngBuildError as ex:
                return None, warnings, [f"{e.name}: {ex}"]
        if not parts:
            return None, warnings, ["No songs were chosen."]
        if any(self._entries[s].has_events and self.get(s).events.audio for s in ids):
            warnings.append("Some songs play audio tracks: copy their .WAV files to the instrument separately; the "
                            "audio-region table is not part of an exported library song.")
        return build_sng(target_model, parts), warnings, []
