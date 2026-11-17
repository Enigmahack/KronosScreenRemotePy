"""One running record of what every conversion between instruments changed, dropped, substituted or assumed.

Every entry point (a PCG of the other family converted on entry, Convert to Nautilus, Convert a .SNG file, Song Library
export) adds the complete messages of each object it converted or refused, so nothing a converter reported is lost to
a log filter or a closed dialog. UI-free."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List, Sequence, Set, Tuple


@dataclass(frozen=True)
class ReportEntry:
    source: str              # "PCG on entry", "Convert to Nautilus", "Convert .SNG", "Song Library export"
    name: str
    status: str              # "converted", "refused", "written", "failed", ...
    messages: Tuple[str, ...]
    when: str = field(default="", compare=False)


class ConversionReport:
    def __init__(self) -> None:
        self.entries: List[ReportEntry] = []
        self._seen: Set[ReportEntry] = set()

    def __len__(self) -> int:
        return len(self.entries)

    def add(self, source: str, name: str, status: str, messages: Sequence[str]) -> bool:
        """False when the same entry is already recorded (a PCG object re-converted after a cache drop)."""
        e = ReportEntry(source, name or "(unnamed)", status, tuple(messages), time.strftime("%H:%M:%S"))
        if e in self._seen:
            return False
        self._seen.add(e)
        self.entries.append(e)
        return True

    def clear(self) -> None:
        self.entries.clear()
        self._seen.clear()

    def text(self) -> str:
        lines = []
        for e in self.entries:
            lines.append(f"{e.when}  [{e.source}] {e.name}: {e.status}")
            lines += [f"    - {m}" for m in e.messages] or ["    (nothing changed beyond the format)"]
        return "\n".join(lines)
