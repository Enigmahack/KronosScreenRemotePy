"""The Song Library: Songs kept on this computer, from .SNG files and from instruments.

A song that carries its events (read from a .SNG) is stored as a single-song .SNG, so the files in the library are ordinary
Korg song files. A song read over SysEx has only its Timbre Set and Song Control (Korg disabled the SysEx event dump); it is
stored as the whole-Song object body and can supply Timbre Sets but cannot be exported to a .SNG.
Entries are keyed by a hash of their stored bytes, so adding the same song twice adds it once. Audio files (.WAV) referenced
by a song's audio tracks are not stored."""
from __future__ import annotations

import hashlib
import json
import logging
import pathlib
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import Data.song_events as ev
from Models.storage import atomic_write_bytes, atomic_write_text
from Data.sng_file import MODEL_KRONOS, MODEL_NAUTILUS, read_sng
from Data.sng_writer import SngBuildError, build_sng, parts_of
from Data.song_model import SongData, from_sng_song, from_whole_body

log = logging.getLogger(__name__)

KIND_SNG, KIND_OBJECT = "sng", "object"
_EXT = {KIND_SNG: ".SNG", KIND_OBJECT: ".song"}
_KIND_OF_SUFFIX = {".sng": KIND_SNG, ".song": KIND_OBJECT}


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
        # The Song Library window imports on a worker thread while the table reads entries().
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        """Read index.json. A damaged index is never treated as an empty library: it is kept aside as
        index.json.corrupt-<time> and the index is rebuilt from the song files on disk, so the next
        add() cannot overwrite it with a list that has lost every earlier song. A missing index with
        song files present (deleted by hand, or a crash before the first save) is rebuilt the same way."""
        self.recovered_note: Optional[str] = None
        try:
            text = self._index_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            if self._song_files():
                self._rebuild("index.json was missing")
            else:
                self._entries = {}
            return
        except OSError as ex:
            # Unreadable right now (locked, permissions): do not rebuild over it, and do not let a
            # later save replace it either - the library is read-only for this session.
            log.error("Song Library index unreadable: %s", ex)
            self._entries = {}
            self._read_only = f"The Song Library index could not be read ({ex}); nothing will be saved."
            return
        try:
            raw = json.loads(text)
            self._entries = {e["id"]: SongEntry(**e) for e in raw.get("songs", [])
                             if (self.root / (e["id"] + _EXT.get(e.get("kind"), ""))).exists()}
        except (ValueError, KeyError, TypeError, AttributeError) as ex:
            aside = self._index_path.with_name(
                f"index.json.corrupt-{time.strftime('%Y%m%d_%H%M%S')}-{uuid.uuid4().hex[:6]}")
            try:
                self._index_path.replace(aside)
                log.error("Song Library index damaged (%s); kept as %s, rebuilding from the song files", ex, aside.name)
            except OSError as move_ex:
                try:
                    shutil.copy2(self._index_path, aside)
                    log.error("Song Library index damaged (%s); copied to %s (could not move it: %s)",
                              ex, aside.name, move_ex)
                except OSError as copy_ex:
                    # No copy of the damaged index exists anywhere else: rebuild in memory only, and never
                    # save over it this session.
                    log.error("Song Library index damaged (%s) and could not be kept aside: %s", ex, copy_ex)
                    self._read_only = (f"The damaged Song Library index could not be kept aside ({copy_ex}); "
                                       f"nothing will be saved until it can.")
            self._rebuild(f"index.json was damaged and was kept as {aside.name}")

    def _song_files(self) -> List[pathlib.Path]:
        try:
            files = list(self.root.iterdir())
        except OSError:
            return []
        # Skip the atomic writer's hidden ".<name>.<uuid>.tmp" files and anything set aside.
        return sorted(p for p in files if p.is_file() and not p.name.startswith(".")
                      and p.suffix.lower() in _KIND_OF_SUFFIX)

    def _rebuild(self, why: str) -> None:
        """Re-create every entry from the .SNG / .song files in the folder. Their names are the entry ids, so
        nothing is lost but the import source and date (shown as "recovered" and the file's time). A file
        that will not parse is left where it is and listed in the log, never deleted."""
        entries: Dict[str, SongEntry] = {}
        skipped: List[str] = []
        for path in self._song_files():
            sid, kind = path.stem, _KIND_OF_SUFFIX[path.suffix.lower()]
            try:
                model = int(sid.split("-", 1)[0], 16)
                data = path.read_bytes()
                if kind == KIND_OBJECT:
                    song = from_whole_body(data)
                else:
                    sng = read_sng(data)
                    song = from_sng_song(sng.songs[0], sng.model)
                added = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(path.stat().st_mtime))
            except Exception as ex:                  # noqa: BLE001 - one bad file must not stop the rest
                log.warning("Song Library rebuild: skipped %s: %s", path.name, ex)
                skipped.append(path.name)
                continue
            entries[sid] = SongEntry(sid, song.name, model, kind, "recovered", added, **_describe(song))
        self._entries = entries
        self.recovered_note = (f"The Song Library was rebuilt from its files ({why}): {len(entries)} song(s) "
                               f"recovered" + (f", {len(skipped)} file(s) could not be read" if skipped else "")
                               + ".")
        log.warning("%s", self.recovered_note)
        try:
            self._save()
        except OSError as ex:
            log.error("Song Library: could not save the rebuilt index: %s", ex)

    _read_only: Optional[str] = None

    def _save(self) -> None:
        if self._read_only:
            raise OSError(self._read_only)
        self.root.mkdir(parents=True, exist_ok=True)
        text = json.dumps({"songs": [asdict(e) for e in self._entries.values()]}, indent=1)
        atomic_write_text(self._index_path, text)

    def entries(self) -> List[SongEntry]:
        with self._lock:
            return list(self._entries.values())

    def path_of(self, entry_id: str) -> pathlib.Path:
        with self._lock:
            e = self._entries[entry_id]
        return self.root / (e.id + _EXT[e.kind])

    def add(self, songs: Sequence[SongData], source: str) -> Tuple[List[str], List[str]]:
        """(ids added, notes). A song already in the library is skipped; one that cannot be stored is reported.
        Building the song files is done outside the lock (it is the slow part), so entries() stays
        responsive while a worker thread imports."""
        if self._read_only:
            raise OSError(self._read_only)
        self.root.mkdir(parents=True, exist_ok=True)
        added: List[str] = []
        notes: List[str] = []
        for song in songs:
            try:
                if song.events is not None:
                    kind, data = KIND_SNG, build_sng(song.model, [parts_of(song)])
                else:
                    kind, data = KIND_OBJECT, song.whole_body()
            except (SngBuildError, ValueError) as ex:
                notes.append(f"{song.name or '(unnamed)'}: cannot be stored: {ex}")
                continue
            sid = f"{song.model:02x}-{hashlib.sha1(data).hexdigest()[:16]}"
            with self._lock:
                if sid in self._entries or sid in added:
                    notes.append(f"{song.name or '(unnamed)'}: already in the library.")
                    continue
                atomic_write_bytes(self.root / (sid + _EXT[kind]), data)
                self._entries[sid] = SongEntry(sid, song.name, song.model, kind, source,
                                               time.strftime("%Y-%m-%dT%H:%M:%S"), **_describe(song))
            if song.events is not None and song.events.audio:
                notes.append(f"{song.name}: plays audio tracks; its .WAV files are not stored.")
            added.append(sid)
        if added:
            with self._lock:
                self._save()
        return added, notes

    def get(self, entry_id: str) -> SongData:
        with self._lock:
            e = self._entries[entry_id]
        data = self.path_of(entry_id).read_bytes()
        if e.kind == KIND_OBJECT:
            return from_whole_body(data)
        sng = read_sng(data)
        return from_sng_song(sng.songs[0], sng.model)

    def remove(self, ids: Sequence[str]) -> int:
        with self._lock:
            return self._remove(ids)

    def _remove(self, ids: Sequence[str]) -> int:
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
            with self._lock:
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
