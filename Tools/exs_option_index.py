r"""
Name index built from the shipped EXs product catalog (Resources/ExsCatalog.json) — port of
Core/Sample/ExsOptionIndex.cs + ExsOptionFile.Parse. Answers "what sample bank does this
byte-decoded EXs<N> or raw-UUID bank actually mean" for the Object Dependencies panel.

BOTH lookups are needed: EXs1-126 use the legacy KORG/MS-prefixed form (decodable to a bare number
with no lookup), but EXs127+ AND every genuine 3rd-party/user bank share the SAME raw-16-byte-UUID
form on disk — the only way to name a raw UUID is to find an option file whose own line-4 <id>
names that exact UUID. A hit IDENTIFIES a product; it is NOT evidence the pack is installed on the
connected instrument, and callers must not word it as if it were.

A hand-dropped `exs_catalog.json` in the data directory wins over the bundled copy (so a user can
pick up packs released later); one that won't parse never shadows the good bundled copy.
"""
from __future__ import annotations

import json
import logging
import pathlib
from typing import Dict, Optional

log = logging.getLogger(__name__)

_BUNDLED = pathlib.Path(__file__).resolve().parent.parent / "Resources" / "ExsCatalog.json"


def raw_uuid_bytes(uuid: Optional[str]) -> Optional[bytes]:
    """The 16 bytes an option file's 'uuid:<uuid>' id has inside a PCG body: the UUID's own
    string/RFC byte order (NOT Windows GUID byte-swapping — that would miss every 3rd-party
    bank, i.e. the whole EXs127+ population)."""
    if uuid is None:
        return None
    hexs = uuid.replace("-", "")
    if len(hexs) != 32:
        return None
    try:
        return bytes.fromhex(hexs)
    except ValueError:
        return None


def parse_option_file(number: int, text: str) -> Optional[tuple]:
    """(name, uuid_id|None) from one Sxxx option file, or None. Line 2 = friendly name;
    line 4 = '2,<id>,<long name>' where <id> is 'uuid:<uuid>' for a 3rd-party pack."""
    lines = text.replace("\r\n", "\n").split("\n")
    if len(lines) < 2:
        return None
    name = lines[1].strip()
    if not name:
        return None
    uuid_id = None
    if len(lines) > 3:
        fields = lines[3].split(",")
        if len(fields) >= 2 and fields[1].lower().startswith("uuid:"):
            uuid_id = fields[1][5:].strip()
    return name, uuid_id


class ExsOptionIndex:
    def __init__(self) -> None:
        self._by_number: Dict[int, str] = {}
        self._by_uuid_hex: Dict[str, str] = {}
        self.from_override_file = False

    def __len__(self) -> int:
        return len(self._by_number)

    count = property(__len__)

    def name_for_exs_number(self, number: int) -> Optional[str]:
        return self._by_number.get(number)

    def name_for_uuid_hex(self, hex_key: str) -> Optional[str]:
        """`hex_key` must be masked like sample_reference_walker.dedup_key (byte 15 bit 0 cleared)."""
        return self._by_uuid_hex.get(hex_key.upper())

    @staticmethod
    def override_path() -> pathlib.Path:
        import Models.storage as storage
        return storage.data_dir() / "exs_catalog.json"

    @classmethod
    def from_catalog(cls, json_text: Optional[str] = None) -> "ExsOptionIndex":
        index = cls()
        entries: Optional[Dict[str, str]]
        if json_text is not None:
            entries = cls._deserialize(json_text)
        else:
            entries = cls._deserialize(cls._read(cls.override_path))
            index.from_override_file = entries is not None
            if entries is None:
                entries = cls._deserialize(cls._read(lambda: _BUNDLED))
        if entries is None:
            return index
        for key, text in entries.items():
            try:
                number = int(key)
            except ValueError:
                continue
            parsed = parse_option_file(number, text)
            if parsed is None:
                continue
            name, uuid_id = parsed
            index._by_number[number] = name
            raw = raw_uuid_bytes(uuid_id)
            if raw is not None:
                masked = bytearray(raw)
                masked[15] &= 0xFE   # mono/stereo flag — same mask as dedup_key
                index._by_uuid_hex[masked.hex().upper()] = name
        return index

    @staticmethod
    def _read(path_fn) -> Optional[str]:
        try:
            p = path_fn()
            return p.read_text(encoding="utf-8") if p.exists() else None
        except Exception as e:
            log.warning("[exs-catalog] read failed: %s", e)
            return None

    @staticmethod
    def _deserialize(text: Optional[str]) -> Optional[Dict[str, str]]:
        if text is None:
            return None
        try:
            d = json.loads(text)
            return d if isinstance(d, dict) else None
        except Exception as e:
            log.warning("[exs-catalog] unreadable catalog: %s", e)
            return None
