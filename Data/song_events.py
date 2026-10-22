"""Song events: the 8-byte records in a Song's tracks (`SongEvent.txt`).

Each event is one big-endian 64-bit word whose top byte is the kind. Bit positions below are inclusive [hi:lo] of that word
(byte M07 = bits 63-56 ... byte M00 = bits 7-0). Bits the document leaves unnamed are kept in `reserved`, so
`encode(decode(raw)) == raw` for every event, and an unknown kind round-trips as raw bytes."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

BAR, PAT, TRK_END, NOTE, POLY_PRESS, CONTROL, PROGRAM, CH_PRESS, PITCH_BEND, EXCL, EXCL_DATA, EOX = (
    0x01, 0x02, 0x03, 0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x07, 0x08)
TEMPO = 0x0B   # kind 0x0B in the MASTER track only

KIND_NAMES = {BAR: "Bar", PAT: "Pat", TRK_END: "TrkEnd", NOTE: "Note", POLY_PRESS: "PolyPress", CONTROL: "ControlChg",
              PROGRAM: "ProgramChg", CH_PRESS: "ChPress", PITCH_BEND: "PitchBend", EXCL: "Excl",
              EXCL_DATA: "ExclData", EOX: "EOX"}

_Spec = Tuple[Tuple[str, int, int], ...]
_TICK = ("tick", 15, 0)
_UNFIXED = ("unfixed", 48, 48)
_SPECS: Dict[int, _Spec] = {
    BAR: (("meter", 39, 32), ("size", 31, 16), ("meas", 15, 0)),
    PAT: (("pat_meas", 39, 32), ("pat_no", 31, 16), ("meas", 15, 0)),
    TRK_END: (("meas", 15, 0),),
    NOTE: (("key", 47, 40), ("vel", 39, 32), ("length", 31, 16), _TICK),
    POLY_PRESS: (("val", 31, 24), ("key", 23, 16), _TICK),
    CONTROL: (_UNFIXED, ("last_val", 39, 32), ("val", 31, 24), ("number", 23, 16), _TICK),
    PROGRAM: (_UNFIXED, ("last_bank", 47, 40), ("last_prog", 39, 32), ("bank", 31, 24), ("prog", 23, 16), _TICK),
    CH_PRESS: (_UNFIXED, ("last_val", 31, 24), ("val", 23, 16), _TICK),
    PITCH_BEND: (_UNFIXED, ("last_high", 47, 40), ("last_low", 39, 32), ("high", 31, 24), ("low", 23, 16), _TICK),
    EXCL: (("enable", 49, 49), _UNFIXED, ("last_value", 39, 16), _TICK),
    EXCL_DATA: (("data", 55, 0),),
    EOX: (),
}
_MASTER_SPECS: Dict[int, _Spec] = {
    BAR: _SPECS[BAR], TRK_END: _SPECS[TRK_END],
    TEMPO: (_UNFIXED, ("number", 39, 32), ("tempo", 31, 16), _TICK),
}
EVENT_SIZE = 8


def _mask(hi: int, lo: int) -> int:
    return ((1 << (hi - lo + 1)) - 1) << lo


@dataclass(frozen=True)
class Event:
    kind: int
    fields: Dict[str, int] = field(default_factory=dict)
    reserved: int = 0             # every bit not covered by `fields` or the kind byte
    master: bool = False

    @property
    def name(self) -> str:
        if self.master and self.kind == TEMPO:
            return "TempoChg"
        return KIND_NAMES.get(self.kind, f"0x{self.kind:02X}")

    @property
    def known(self) -> bool:
        return self.kind in (_MASTER_SPECS if self.master else _SPECS)


def decode(raw: bytes, master: bool = False) -> Event:
    word = int.from_bytes(raw, "big")
    kind = word >> 56
    spec = (_MASTER_SPECS if master else _SPECS).get(kind)
    if spec is None:
        return Event(kind, {}, word & ((1 << 56) - 1), master)
    fields = {n: (word & _mask(hi, lo)) >> lo for n, hi, lo in spec}
    covered = 0xFF << 56
    for _, hi, lo in spec:
        covered |= _mask(hi, lo)
    return Event(kind, fields, word & ~covered & ((1 << 64) - 1), master)


def encode(ev: Event) -> bytes:
    spec = (_MASTER_SPECS if ev.master else _SPECS).get(ev.kind)
    word = (ev.kind << 56) | ev.reserved
    for n, hi, lo in spec or ():
        word |= (ev.fields[n] << lo) & _mask(hi, lo)
    return word.to_bytes(EVENT_SIZE, "big")


def decode_all(payload: bytes, master: bool = False) -> List[Event]:
    """`payload` is the event area of an MTE1/EVM1 chunk (after its 12-byte count/size/0 prefix)."""
    if len(payload) % EVENT_SIZE:
        raise ValueError(f"{len(payload)} bytes is not a whole number of 8-byte events.")
    return [decode(payload[i:i + EVENT_SIZE], master) for i in range(0, len(payload), EVENT_SIZE)]


def encode_all(events: List[Event]) -> bytes:
    return b"".join(encode(e) for e in events)


def _self_test() -> None:
    note = bytes.fromhex("0900415502450001")
    ev = decode(note)
    assert ev.name == "Note" and ev.fields == {"key": 0x41, "vel": 0x55, "length": 0x245, "tick": 1}, ev
    assert encode(ev) == note and ev.reserved == 0
    bar = bytes.fromhex("0100000000" + "0a" + "0000")
    assert decode(bar).fields == {"meter": 0, "size": 10, "meas": 0} and encode(decode(bar)) == bar
    tempo = decode(bytes.fromhex("0b00006b04b00010"), master=True)
    assert tempo.name == "TempoChg" and tempo.fields["tempo"] == 0x04B0 and tempo.fields["tick"] == 0x10
    unknown = bytes.fromhex("7f11223344556677")
    assert not decode(unknown).known and encode(decode(unknown)) == unknown
    print("song_events self-test OK")


if __name__ == "__main__":
    _self_test()
