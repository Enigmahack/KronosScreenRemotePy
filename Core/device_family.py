"""DeviceFamily — which physical Kronos/Nautilus family the daemon's MODEL
command reports for the current connection. Port of C#'s
`Core/DeviceFamily.cs`.

Resolved once per connection, from a real daemon round trip (see
Views/main_window.py's `_fetch_device_family`), distinct from
`StreamReceiver.stream_fmt` — the stream's native pixel format/bpp
(INDEX8 on Kronos vs RGB565LE on Nautilus) is a separate, protocol-level
signal read from the handshake itself, cheaper (no extra round trip) but
not authoritative the way MODEL's own FAMILY field is.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional, Tuple


class DeviceFamily(Enum):
    """UNKNOWN covers "never connected yet" and "MODEL query failed/timed
    out" — both treated the same as KRONOS everywhere this matters, matching
    C#'s own DeviceFamily.cs comment."""
    UNKNOWN = "Unknown"
    KRONOS = "Kronos"
    NAUTILUS = "Nautilus"

    @staticmethod
    def parse(family_str: Optional[str]) -> "DeviceFamily":
        if not family_str:
            return DeviceFamily.UNKNOWN
        upper = family_str.strip().upper()
        if upper == "NAUTILUS":
            return DeviceFamily.NAUTILUS
        if upper == "KRONOS":
            return DeviceFamily.KRONOS
        return DeviceFamily.UNKNOWN


def parse_kv(resp: Optional[str]) -> Dict[str, str]:
    """Space-separated KEY=VALUE tokens (the MODEL reply) -> dict. Tolerates any order and unknown keys."""
    kv: Dict[str, str] = {}
    for part in (resp or "").split():
        if "=" in part:
            k, _, v = part.partition("=")
            kv[k] = v
    return kv


def _int(v: Optional[str]) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None


@dataclass(frozen=True)
class ModelInfo:
    """Everything the daemon's MODEL command reports (docs/api.md MODEL). Fields a daemon is too old to send are
    None / empty: BOARD, PANEL_HWVER, CPUS/CORES/THREADS and STREAM_FMT/STREAM_GEOM arrived in 3.0.2."""
    family: DeviceFamily = DeviceFamily.UNKNOWN
    model: str = ""
    fb_bpp: Optional[int] = None
    board: str = ""
    panel_hwver: Optional[int] = None
    cpus: Optional[int] = None
    cores: Optional[int] = None
    threads: Optional[int] = None
    stream_fmt: str = ""
    stream_geom: Optional[Tuple[int, int]] = None
    family_raw: str = ""

    @staticmethod
    def parse(resp: Optional[str]) -> "ModelInfo":
        kv = parse_kv(resp)
        geom = None
        w, sep, h = kv.get("STREAM_GEOM", "").partition("x")
        if sep and _int(w) and _int(h):
            geom = (int(w), int(h))
        return ModelInfo(
            family=DeviceFamily.parse(kv.get("FAMILY")), model=kv.get("MODEL", ""), fb_bpp=_int(kv.get("FB_BPP")),
            board=kv.get("BOARD", ""), panel_hwver=_int(kv.get("PANEL_HWVER")), cpus=_int(kv.get("CPUS")),
            cores=_int(kv.get("CORES")), threads=_int(kv.get("THREADS")), stream_fmt=kv.get("STREAM_FMT", ""),
            stream_geom=geom, family_raw=kv.get("FAMILY", ""))

    @property
    def stream_is_rgb565(self) -> Optional[bool]:
        return None if not self.stream_fmt else self.stream_fmt.upper() == "RGB565LE"


def _selftest() -> None:
    k = ModelInfo.parse("FAMILY=KRONOS MODEL=KRONOS2 FB_BPP=8 CPUS=4 CORES=2 THREADS=2 STREAM_FMT=INDEX8 STREAM_GEOM=800x600")
    assert (k.family, k.model, k.fb_bpp, k.cpus, k.cores, k.threads) == (DeviceFamily.KRONOS, "KRONOS2", 8, 4, 2, 2)
    assert k.stream_geom == (800, 600) and k.stream_is_rgb565 is False
    n = ModelInfo.parse("FAMILY=NAUTILUS MODEL=NAUTILUS_AT FB_BPP=16 BOARD=N3160TM-ITX-K PANEL_HWVER=2 CPUS=4 CORES=4 "
                        "THREADS=1 STREAM_FMT=RGB565LE STREAM_GEOM=800x480")
    assert n.family is DeviceFamily.NAUTILUS and n.model == "NAUTILUS_AT" and n.board == "N3160TM-ITX-K"
    assert n.panel_hwver == 2 and n.stream_geom == (800, 480) and n.stream_is_rgb565 is True
    old = ModelInfo.parse("FAMILY=KRONOS MODEL=KRONOS2")          # pre-3.0.2 daemon: only the first fields
    assert old.cpus is None and old.stream_geom is None and old.stream_is_rgb565 is None and old.board == ""
    assert ModelInfo.parse(None).family is DeviceFamily.UNKNOWN
    assert ModelInfo.parse("FAMILY=NAUTILUS STREAM_GEOM=garbage").stream_geom is None
    print("device_family self-test OK")


if __name__ == "__main__":
    _selftest()
