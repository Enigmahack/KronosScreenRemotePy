"""One Song, whichever way it arrived.

A .SNG song (Data.sng_file) carries everything: Song Control, Song Timbre Set and the MIDI/master/pattern/audio events. A SysEx
Song (object 0x18, or 0x02 + 0x08) carries only the Timbre Set and the Control: Korg disabled the SysEx event dump, so the
events can only come from a file. `SongData.events` is None for the SysEx form.

Whole-Song object 0x18 layout: Song Timbre Set, then (Kronos only) 2 reserved zero bytes, then Song Control.
Kronos 13 076 B = 7810 + 2 + 5264; Nautilus 10 904 B = 5640 + 5264."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from Data.sng_file import MODEL_KRONOS, MODEL_NAUTILUS, SngSong

CONTROL_SIZE = 5264
TIMBRE_SET_SIZE = {MODEL_KRONOS: 7810, MODEL_NAUTILUS: 5640}
RESERVED_GAP = {MODEL_KRONOS: 2, MODEL_NAUTILUS: 0}
WHOLE_SIZE = {m: TIMBRE_SET_SIZE[m] + RESERVED_GAP[m] + CONTROL_SIZE for m in TIMBRE_SET_SIZE}
_MODEL_BY_WHOLE = {size: m for m, size in WHOLE_SIZE.items()}


class SongError(ValueError):
    pass


@dataclass(frozen=True)
class SongData:
    model: int                          # MODEL_KRONOS | MODEL_NAUTILUS: whose layout `timbre_set`/`control` are in
    timbre_set: bytes
    control: bytes
    events: Optional[SngSong] = None    # the .SNG song this came from (tracks, patterns, audio); None for SysEx

    def __post_init__(self) -> None:
        if len(self.timbre_set) != TIMBRE_SET_SIZE.get(self.model, -1):
            raise SongError(f"A model 0x{self.model:02X} Song Timbre Set is {TIMBRE_SET_SIZE.get(self.model)} bytes, "
                            f"not {len(self.timbre_set)}.")
        if len(self.control) != CONTROL_SIZE:
            raise SongError(f"A Song Control is {CONTROL_SIZE} bytes, not {len(self.control)}.")

    @property
    def name(self) -> str:
        return self.timbre_set[:24].split(b"\x00")[0].decode("ascii", "replace").rstrip()

    @property
    def has_events(self) -> bool:
        return self.events is not None

    def whole_body(self) -> bytes:
        """The object-0x18 body."""
        return self.timbre_set + bytes(RESERVED_GAP[self.model]) + self.control

    def sysex_objects(self) -> Tuple[bytes, bytes]:
        """(object 0x02 body, object 0x08 body)."""
        return self.timbre_set, self.control


def from_whole_body(body: bytes) -> SongData:
    """Parses an object-0x18 body; the model follows from its length."""
    model = _MODEL_BY_WHOLE.get(len(body))
    if model is None:
        raise SongError(f"A whole Song is {' or '.join(str(s) for s in _MODEL_BY_WHOLE)} bytes, not {len(body)}.")
    sts = TIMBRE_SET_SIZE[model]
    gap = body[sts:sts + RESERVED_GAP[model]]
    if any(gap):
        raise SongError("The reserved bytes between the Timbre Set and the Song Control are not zero.")
    return SongData(model, body[:sts], body[sts + RESERVED_GAP[model]:])


def from_sysex_pair(timbre_set: bytes, control: bytes) -> SongData:
    """From object 0x02 and 0x08 bodies; the model follows from the Timbre Set's length."""
    model = next((m for m, n in TIMBRE_SET_SIZE.items() if n == len(timbre_set)), None)
    if model is None:
        raise SongError(f"A Song Timbre Set is {' or '.join(map(str, TIMBRE_SET_SIZE.values()))} bytes, "
                        f"not {len(timbre_set)}.")
    return SongData(model, timbre_set, control)


def from_sng_song(song: SngSong, model: int) -> SongData:
    return SongData(model, song.timbre_set, song.control, song)


def _self_test() -> None:
    k = SongData(MODEL_KRONOS, bytes(7810), bytes(5264))
    assert len(k.whole_body()) == 13076 and from_whole_body(k.whole_body()) == k
    n = SongData(MODEL_NAUTILUS, bytes(5640), bytes(range(256)) * 20 + bytes(144))
    assert len(n.whole_body()) == 10904 and from_whole_body(n.whole_body()) == n
    assert from_sysex_pair(*n.sysex_objects()) == n
    for bad in (lambda: from_whole_body(bytes(100)), lambda: SongData(MODEL_KRONOS, bytes(5640), bytes(5264)),
                lambda: from_whole_body(bytes(7810) + b"\x01\x00" + bytes(5264))):
        try:
            bad()
        except SongError:
            continue
        raise AssertionError("expected SongError")
    print("song_model self-test OK")


if __name__ == "__main__":
    _self_test()
