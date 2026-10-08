"""`.KSC` — sample collection manifest (plain CRLF text, NOT chunked). Port of
C#'s Core/Sample/KscCollection.cs.

Header `#KORG Script Version 1.0` / `#v2` / `#uuid:<uuid>`, then one plain
line per owned .KMP/.KSF filename, then (REQUIRED) one `#>User.0.2.<filename>`
line per entry above, same order — omitting that block produces a file that
loads with no error but shows zero content in the sample browser (see
kronosology/docs/interfaces/ksc_kmp_ksf_file_format.md §1.2).

Read path stays permissive (must be able to open a real _UserBank.KSC for
inspection). to_bytes()/save() (normal/"mFieldA==false" mode, doc §1.3)
refuse to target a _UserBank.KSC-suffixed filename — that mode's own
plain-filename-list + #uuid: format is real Kronos-generated output only.
Use to_user_bank_bytes()/save_user_bank() for the dedicated writer.
"""
from __future__ import annotations

import os
import pathlib
import uuid as uuid_mod
from dataclasses import dataclass, field
from typing import List, Optional

import Data.korg_riff_chunk as riff
import Data.ksf_sample as ksf_sample_mod
import Data.kmp_multisample as kmp_mod
from Models.storage import atomic_write_bytes


@dataclass
class KscCollection:
    bank_uuid: Optional[str] = None
    entries: List[str] = field(default_factory=list)
    path: Optional[str] = None

    @staticmethod
    def open(data: bytes) -> "KscCollection":
        text = data.decode("ascii", errors="replace")
        lines = text.split("\r\n")
        k = KscCollection()

        # header lines 0/1 are fixed literals; line 2 is #uuid: (not always
        # present — e.g. never in a _UserBank.KSC).
        if len(lines) > 2 and lines[2].startswith("#uuid:"):
            k.bank_uuid = lines[2][len("#uuid:"):]
            i = 3
        else:
            i = 2
        while i < len(lines) and len(lines[i]) > 0 and not lines[i].startswith("#"):
            k.entries.append(lines[i])
            i += 1
        return k

    def to_bytes(self, target_file_name: Optional[str] = None) -> bytes:
        # Fall back to path's own filename when the caller omits
        # target_file_name, so a bare to_bytes() on a collection whose path
        # is already _UserBank.KSC-suffixed can't silently bypass the guard.
        if target_file_name is None and self.path is not None:
            target_file_name = os.path.basename(self.path)
        if target_file_name is not None and target_file_name.upper().endswith("_USERBANK.KSC"):
            raise ValueError(
                "Refusing to write a _UserBank.KSC via normal-mode to_bytes()/save() — that "
                "format (plain filename list + #uuid:) is real instrument-generated output only. "
                "Use to_user_bank_bytes()/save_user_bank() for the dedicated #>>uuid:-reference format.")

        if self.bank_uuid is None:
            self.bank_uuid = gen_bank_uuid()
        lines = ["#KORG Script Version 1.0", "#v2", f"#uuid:{self.bank_uuid}"]
        lines.extend(self.entries)
        lines.extend(f"#>User.0.2.{e}" for e in self.entries)
        return ("\r\n".join(lines) + "\r\n").encode("ascii", errors="replace")

    def save(self, path: Optional[str] = None) -> None:
        path = path or self.path
        if path is None:
            raise ValueError("no path given and none stored")
        data = self.to_bytes(os.path.basename(path))
        atomic_write_bytes(pathlib.Path(path), data)
        self.path = path

    def to_user_bank_bytes(self) -> bytes:
        """Writes the OWN-BANK case of the _UserBank.KSC "reference-export"
        format (doc §1.3, mFieldA==true): one "#>>uuid:<bank>.MS<n>.1.0.<name>"
        line per .KMP entry and one "#>>uuid:<bank>.DS<n>.1.0.<name>" line per
        bare-.KSF entry (n = 0-based, counted separately per type —
        positional/emission-order, NOT tied to a multisample's own mno1 or a
        sample's own sno1), followed by one closing
        "#>uuid:<bank>.<MsCount>.<DsCount>.<own .KSC base filename>" summary
        line. <name> is each referenced .KMP/.KSF's own 24-byte NAME chunk
        re-encoded (space-padded, suffix right-aligned), not a
        trimmed/re-derived string.

        Deliberately does NOT attempt to replicate a real Eva-generated
        _UserBank.KSC's full contents — a real one reflects whatever the
        Kronos sampling engine currently has resident in RAM at Save time,
        which this app has no way to observe. This writer only ever emits
        this collection's own on-disk entries.
        """
        if self.path is None:
            raise ValueError("to_user_bank_bytes needs path set to resolve entry files")
        if self.bank_uuid is None:
            raise ValueError("to_user_bank_bytes needs bank_uuid set")

        content_dir = content_dir_for(self.path)
        ms_lines: List[str] = []
        ds_lines: List[str] = []
        for entry in self.entries:
            entry_path = os.path.join(content_dir, entry)
            if entry.upper().endswith(".KMP"):
                with open(entry_path, "rb") as f:
                    kmp = kmp_mod.KmpMultisample.open(f.read())
                if kmp is None:
                    raise ValueError(f"not a recognizable .KMP: {entry}")
                name = riff.encode_name_field(kmp.name, kmp.suffix, 24).decode("ascii", errors="replace")
                ms_lines.append(f"#>>uuid:{self.bank_uuid}.MS{len(ms_lines)}.1.0.{name}")
            elif entry.upper().endswith(".KSF"):
                with open(entry_path, "rb") as f:
                    ksf = ksf_sample_mod.KsfSample.open(f.read())
                if ksf is None:
                    raise ValueError(f"not a recognizable .KSF: {entry}")
                name = riff.encode_name_field(ksf.name, ksf.suffix, 24).decode("ascii", errors="replace")
                ds_lines.append(f"#>>uuid:{self.bank_uuid}.DS{len(ds_lines)}.1.0.{name}")

        lines = ["#KORG Script Version 1.0", "#v2"]
        lines.extend(ms_lines)
        lines.extend(ds_lines)
        base = os.path.splitext(os.path.basename(self.path))[0]
        lines.append(f"#>uuid:{self.bank_uuid}.{len(ms_lines)}.{len(ds_lines)}.{base}")
        return ("\r\n".join(lines) + "\r\n").encode("ascii", errors="replace")

    @property
    def user_bank_path(self) -> str:
        """<ksc-dir>/<ksc-basename>_UserBank.KSC — the sibling path a real
        Kronos always places this file at, next to the normal .KSC sharing
        the same #uuid:."""
        base = os.path.splitext(os.path.basename(self.path or ""))[0]
        return os.path.join(os.path.dirname(self.path or ""), base + "_UserBank.KSC")

    def save_user_bank(self) -> None:
        with open(self.user_bank_path, "wb") as f:
            f.write(self.to_user_bank_bytes())

    @staticmethod
    def for_folder(ksc_path: str) -> "KscCollection":
        """Build a fresh manifest by scanning <ksc-dir>/<ksc-basename>/ for
        .KMP/.KSF files — the "generate .KSC for this folder" operation."""
        content_dir = content_dir_for(ksc_path)
        entries: List[str] = []
        if os.path.isdir(content_dir):
            for name in sorted(os.listdir(content_dir)):
                if name.upper().endswith(".KMP") or name.upper().endswith(".KSF"):
                    entries.append(name)
        return KscCollection(path=ksc_path, entries=entries)


def gen_bank_uuid() -> str:
    """Standard generated UUID per the PCG bank-UUID scheme: byte 15 bit 0
    must be 0 for the mono/"bank identity" form."""
    b = bytearray(uuid_mod.uuid4().bytes)
    b[15] &= 0xFE
    return str(uuid_mod.UUID(bytes=bytes(b)))


def content_dir_for(ksc_path: str) -> str:
    """<ksc-dir>/<ksc-basename>/ — the collection's own content folder,
    holding every .KMP/.KSF this .KSC's plain filename entries reference."""
    return os.path.join(os.path.dirname(ksc_path), os.path.splitext(os.path.basename(ksc_path))[0])


def next_free_sno1(content_dir: str) -> int:
    """Smallest sno1 not currently used by any .KSF anywhere under
    content_dir (scanned recursively). A .KSF's own SNO1 chunk is what a
    real Kronos's bulk-.KSC-import actually reads (doc §1.6); leaving it at
    the field's default (0) makes the import silently drop all but one of
    the identically-numbered zones' audio. Every new sample this app writes
    must get a real, collection-unique value.

    Disk-scanning (not an in-memory counter) so it stays correct across app
    restarts and multi-session edits — sno1 isn't otherwise held in memory
    (KmpZone/KmpMultisample never carry it)."""
    max_seen: Optional[int] = None
    if os.path.isdir(content_dir):
        for root, _dirs, files in os.walk(content_dir):
            for name in files:
                if not name.upper().endswith(".KSF"):
                    continue
                sno = ksf_sample_mod.KsfSample.read_sno1(os.path.join(root, name))
                if sno is not None and (max_seen is None or sno > max_seen):
                    max_seen = sno
    return 0 if max_seen is None else max_seen + 1


class Sno1Allocator:
    """One disk scan for a whole batch of imports, handing out consecutive
    ids from it. next_free_sno1 has to stay a disk scan (sno1 is not held in
    memory anywhere), but calling it per-import is O(imports x existing .KSF
    files) — seeding once and incrementing gives the same uniqueness
    guarantee for a batch at the cost of a single scan."""

    def __init__(self, content_dir: str):
        self._next = next_free_sno1(content_dir)

    def next(self) -> int:
        value = self._next
        self._next += 1
        return value
