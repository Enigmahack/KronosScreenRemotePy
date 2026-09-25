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

from enum import Enum
from typing import Optional


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
