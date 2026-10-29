from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class ConversionResult:
    """`body` is None exactly when `refusals` is non-empty: a refusal never yields a partial body.
    `arp_patterns` are (bank-B slot, 556-byte pattern) pairs the caller must write for the body to play.
    `substitutions` lists the sample zones that had no equivalent on the other instrument and play the default piano
    instead; the body is otherwise complete, so they are also summarised in `warnings`."""
    body: Optional[bytes]
    warnings: List[str] = field(default_factory=list)
    refusals: List[str] = field(default_factory=list)
    arp_patterns: List[Tuple[int, bytes]] = field(default_factory=list)
    substitutions: List[str] = field(default_factory=list)   # zones that now play the default piano: "where: why"
