"""Converts Kronos objects and writes them into consecutive Nautilus slots over SysEx.

Order is fixed for safety: read every target slot, convert, back every pre-image up to one .syx, only then write
(arp patterns first, so no Program ever points at a pattern that isn't there), store each touched bank once, and
re-read to verify. A target the conversion can't use (wrong Program bank type, refused conversion, past the end of
the bank) is skipped with a reason; it never aborts the others."""
from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Callable, Dict, List, Optional, Set, Tuple

import Data.nautilus_sysex as nsx
import Data.object_types as object_types
from Conversion import karma
from Conversion.combi import convert_combi
from Conversion.data import (KarmaGeCatalog, KarmaGePatternLibrary, ProgramRefMap, RomWaveformMap,
                             nautilus_timbre_bank_for_wire)
from Conversion.media import convert_drum_kit, convert_wave_seq
from Conversion.profiles import NAUTILUS, ProgramKind, COMBI_NAUTILUS_SIZE
from Conversion.program import convert_program
from Conversion.result import ConversionResult
from Data.librarian_sysex import OBJ_COMBI, OBJ_DRUM_KIT, OBJ_PROGRAM, OBJ_SONG_TIMBRE, OBJ_WAVE_SEQ
from Tools.exs_option_index import ExsOptionIndex

CONVERTIBLE_TYPES = (OBJ_PROGRAM, OBJ_COMBI, OBJ_DRUM_KIT, OBJ_WAVE_SEQ, OBJ_SONG_TIMBRE)
SONG_SLOTS = 200   # Nautilus Song Timbre Sets: one bank (0), 200 songs (probed 2026-10-22; slot 200 does not answer)

# Undocumented Nautilus Arp Pattern object (556 bytes); bank 0x40 = user bank B (B000-B127).
OBJ_ARP_PATTERN, ARP_BANK_B, ARP_BANK_B_SLOTS = 0x19, 0x40, 128


@dataclass
class ConvertItem:
    obj_type: int
    name: str
    body: bytes                 # Kronos wire body (Program: 3706 HD-1 / 4960 EXi)
    is_exi: bool = False
    native: bool = False        # body is already a Nautilus body (a Nautilus .SNG / dump): copied, not converted
    src_ref: Optional[Tuple[int, int]] = None   # Program: (Kronos Combi Timbre bank byte, slot) it was read from


@dataclass
class ConvertTables:
    rom_map: RomWaveformMap
    exs: ExsOptionIndex
    ge_catalog: KarmaGeCatalog
    ref_map: ProgramRefMap
    ge_patterns: Optional[KarmaGePatternLibrary] = None

    @classmethod
    def load(cls) -> "ConvertTables":
        return cls(RomWaveformMap.from_catalog(), ExsOptionIndex.from_catalog(), KarmaGeCatalog.from_catalog(),
                   ProgramRefMap.from_catalog(), KarmaGePatternLibrary.from_catalog())


# SysExService.write_object / store_bank return this when no Reply arrived (timeout or lost link). It is NOT a
# refusal: the instrument may or may not have acted on the message.
NO_REPLY = -1


@dataclass
class ItemReport:
    name: str
    dest: str
    status: str                 # "written" | "refused" | "failed" | "unverified" (stored, but the re-read differs)
    messages: List[str] = field(default_factory=list)


def bank_label(obj_type: int, bank: int) -> str:
    """Nautilus user banks continue past its factory A-F as G.. (wire 0x40 = G, 0x4C = S), where Kronos says U-A.."""
    if obj_type == OBJ_SONG_TIMBRE:
        return "Songs"
    if obj_type in (OBJ_PROGRAM, OBJ_COMBI) and 0x40 <= bank <= 0x4D:
        return chr(71 + bank - 0x40)
    if obj_type == OBJ_ARP_PATTERN:
        return "arp pattern bank B"
    return object_types.label_for(obj_type, bank)


def type_name(obj_type: int) -> str:
    return "Song Timbre Set" if obj_type == OBJ_SONG_TIMBRE else object_types.REGISTRY[obj_type].display_name


def slot_count(obj_type: int, bank: int) -> int:
    return SONG_SLOTS if obj_type == OBJ_SONG_TIMBRE else object_types.slot_count(obj_type, bank)


def dest_label(obj_type: int, bank: int, slot: int) -> str:
    if obj_type == OBJ_SONG_TIMBRE:
        return f"Song {slot:03d}"
    return f"{bank_label(obj_type, bank)}{slot:03d}" if obj_type in (OBJ_PROGRAM, OBJ_COMBI) \
        else f"{bank_label(obj_type, bank)}:{slot:03d}"


EXI_DEST = -1   # `dest` key for EXi Programs when they go to a different bank than HD-1 Programs


def dest_key(item: ConvertItem, dest: Dict[int, Tuple[int, int]]) -> int:
    return EXI_DEST if item.obj_type == OBJ_PROGRAM and item.is_exi and EXI_DEST in dest else item.obj_type


def program_kind_of(item: ConvertItem) -> ProgramKind:
    return ProgramKind.EXI if item.is_exi else ProgramKind.HD1


class _ArpSlots:
    """Hands out bank-B slots for transcribed GE patterns, one per distinct GE, from `start` upward."""

    def __init__(self, start: Optional[int], tables: ConvertTables) -> None:
        self.next = start
        self.tables = tables
        self.by_ge: Dict[int, int] = {}

    def slot_for(self, ge: int) -> Optional[int]:
        """None when the GE has a preset, no usable transcription, or the bank is full."""
        lib = self.tables.ge_patterns
        if self.next is None or not ge or karma.preset_for_ge(ge) is not None or lib is None:
            return None
        entry = lib.for_ge(ge)
        if entry is None or entry.score < karma.ArpOptions().min_transcription_score:
            return None
        if ge not in self.by_ge:
            if self.next >= ARP_BANK_B_SLOTS:
                return None
            self.by_ge[ge] = self.next
            self.next += 1
        return self.by_ge[ge]

    def options_for(self, item: ConvertItem) -> karma.ArpOptions:
        if item.obj_type == OBJ_PROGRAM:
            return karma.ArpOptions(transcribed_pattern_slot=self.slot_for(karma.read_ge_number(item.body) or 0))
        if item.obj_type in (OBJ_COMBI, OBJ_SONG_TIMBRE) and len(item.body) == 7810:
            base = karma.ArpOptions()
            slots = []
            for source in (base.combi_arp_a_source, base.combi_arp_b_source):
                k = 1814 + 744 * source
                slots.append(self.slot_for(item.body[k] | (item.body[k + 1] << 8)))
            if slots[0] is not None and slots[0] == slots[1]:
                slots[1] = None   # one pattern can't serve both arpeggiators
            return karma.ArpOptions(transcribed_pattern_slot=slots[0], transcribed_pattern_slot_b=slots[1])
        return karma.ArpOptions()


class PushReports(list):
    """The per-item ItemReports, plus the arp bank-B slots that were actually written (for the next default)."""

    def __init__(self) -> None:
        super().__init__()
        self.arp_used: List[int] = []


def plan_arp_slots(items: List[ConvertItem], tables: ConvertTables, arp_start: Optional[int]) -> List[int]:
    """The bank-B slots push_converted would write for these items (an upper bound: a refused conversion frees its)."""
    arp = _ArpSlots(arp_start, tables)
    for item in items:
        arp.options_for(item)
    return sorted(arp.by_ge.values())


def _connection_problem(service, still_valid: Optional[Callable[[], bool]]) -> Optional[str]:
    """Why it is no longer safe to send this push's messages to `service`, or None. A reconnect may be to a different
    instrument (SysExService.stop() resets the codec to Kronos), so nothing is sent once the link is not the same
    live Nautilus connection the user confirmed."""
    if getattr(service, "is_nautilus", True) is False:
        return "the connection is no longer a Nautilus"
    if getattr(service, "can_dump", True) is False:
        return "the SysEx link is down"
    if still_valid is not None and not still_valid():
        return "the instrument connection changed"
    return None


def push_converted(service, items: List[ConvertItem], dest: Dict[int, Tuple[int, int]], tables: ConvertTables,
                   backup_path: str, progress: Callable[[str], None] = lambda m: None,
                   arp_start: Optional[int] = None,
                   still_valid: Optional[Callable[[], bool]] = None) -> PushReports:
    """`dest` maps object type -> (wire bank, first slot); EXI_DEST adds a separate bank for EXi Programs. `arp_start` = first bank-B slot for transcribed arp
    patterns (None = don't use transcriptions). `service` needs dump_object_parsed, backup_objects, write_object
    and store_bank (SysExService, already switched to the Nautilus codec). `still_valid` is checked, with the
    service's own family/link state, before every phase and write; once it fails nothing more is sent.
    When the service offers hold_dump_gate() the whole push runs inside it, so the perf loop and name pulls do not
    interleave their requests and replies with ours."""
    reports = PushReports()
    hold = getattr(service, "hold_dump_gate", None)
    with (hold() if hold is not None else contextlib.nullcontext()):
        _push(service, items, dest, tables, backup_path, progress, arp_start, still_valid, reports)
    return reports


def _push(service, items: List[ConvertItem], dest: Dict[int, Tuple[int, int]], tables: ConvertTables,
          backup_path: str, progress: Callable[[str], None], arp_start: Optional[int],
          still_valid: Optional[Callable[[], bool]], reports: PushReports) -> None:
    def guard() -> Optional[str]:
        return _connection_problem(service, still_valid)

    pending: List[Tuple[ItemReport, SimpleNamespace, bytes, Set[int]]] = []   # report, op, pre-image, arp slots needed
    next_slot = {t: s for t, (_, s) in dest.items()}
    placed = _placements(items, dest)
    arp = _ArpSlots(arp_start, tables)
    arp_patterns: Dict[int, bytes] = {}

    for item in items:
        key = dest_key(item, dest)
        bank, _ = dest[key]
        slot = next_slot[key]
        next_slot[key] += 1
        where = dest_label(item.obj_type, bank, slot)
        rep = ItemReport(item.name, where, "refused")
        reports.append(rep)
        if slot >= slot_count(item.obj_type, bank):
            rep.messages.append(f"Past the last slot of bank {bank_label(item.obj_type, bank)}.")
            continue
        problem = guard()
        if problem:
            rep.status = "failed"
            rep.messages.append(f"Not read: {problem}.")
            continue
        progress(f"Reading {where}")
        target = service.dump_object_parsed(item.obj_type, bank, slot, no_response_ms=6000)
        if target is None:
            rep.status = "failed"
            rep.messages.append("The instrument did not answer for the target slot.")
            continue
        if item.obj_type == OBJ_PROGRAM:
            want = NAUTILUS.wire_size(program_kind_of(item))
            if len(target.body) != want:
                kind = "EXi" if len(target.body) == NAUTILUS.wire_exi else "HD-1"
                rep.messages.append(f"{bank_label(OBJ_PROGRAM, bank)} is a {kind} bank; this is a "
                                    f"{program_kind_of(item).value.upper()} Program - pick a matching bank.")
                continue
        if item.obj_type == OBJ_SONG_TIMBRE and len(target.body) != COMBI_NAUTILUS_SIZE:
            rep.messages.append(f"Song {slot:03d} is {len(target.body)} bytes on the instrument, not a "
                                f"{COMBI_NAUTILUS_SIZE}-byte Song Timbre Set.")
            continue
        if item.obj_type == OBJ_SONG_TIMBRE:
            old = target.body[:24].split(b"\x00")[0].decode("ascii", "replace").rstrip()
            if not re.fullmatch(r"NEW SONG \d{3}", old):
                rep.messages.append(f'Replaces the song "{old}"; its previous content is in the backup.')
        result = _convert(item, target.body, tables, arp.options_for(item), placed)
        rep.messages += result.warnings
        if result.body is None:
            rep.messages += result.refusals
            continue
        needed = {s for s, _ in result.arp_patterns}
        arp_patterns.update(dict(result.arp_patterns))
        op = SimpleNamespace(obj=item.obj_type, bank=bank, index=slot, version=target.version, body=result.body)
        rep.status = "failed"   # until verified
        pending.append((rep, op, target.body, needed))

    if not pending:
        return

    arp_ops: Dict[int, Tuple[SimpleNamespace, bytes]] = {}
    arp_failed: Dict[int, str] = {}
    for s, pattern in sorted(arp_patterns.items()):
        if guard():
            arp_failed[s] = f"Not read: {guard()}."
            continue
        progress(f"Reading arp pattern slot B{s:03d}")
        cur = service.dump_object_parsed(OBJ_ARP_PATTERN, ARP_BANK_B, s, no_response_ms=6000)
        if cur is None:
            arp_failed[s] = f"The instrument did not answer for arp pattern slot B{s:03d}."
            continue
        arp_ops[s] = (SimpleNamespace(obj=OBJ_ARP_PATTERN, bank=ARP_BANK_B, index=s, version=cur.version, body=pattern),
                      cur.body)

    backups = [SimpleNamespace(obj=op.obj, bank=op.bank, index=op.index, version=op.version, body=pre)
               for _, op, pre, _ in pending]
    backups += [SimpleNamespace(obj=op.obj, bank=op.bank, index=op.index, version=op.version, body=pre)
                for op, pre in arp_ops.values()]
    aborted = guard()
    if aborted:
        for rep, _, _, _ in pending:
            rep.messages.append(f"Not written: {aborted}.")
        return
    service.backup_objects(backups, backup_path)
    problem = _verify_backup(backup_path, backups)
    if problem:
        for rep, _, _, _ in pending:
            rep.messages.append(f"Backup could not be verified ({problem}); nothing was written.")
        return
    progress(f"Backed up and verified {len(backups)} target slot(s) in {backup_path}")

    for s, (op, pre) in sorted(arp_ops.items()):
        aborted = aborted or guard()
        if aborted:
            arp_failed[s] = f"Not written: {aborted}."
            continue
        progress(f"Writing arp pattern B{s:03d}")
        code = service.write_object(op)
        if code == NO_REPLY:
            state = _state_after_no_reply(service, op, pre)
            if state == "unchanged":
                arp_failed[s] = f"No reply writing arp pattern B{s:03d}; a re-read shows the slot unchanged."
            elif state == "unknown":
                arp_failed[s] = (f"No reply writing arp pattern B{s:03d}; it may or may not have changed. "
                                 f"Backup: {backup_path}")
        elif code != 0:
            arp_failed[s] = f"Arp pattern write to B{s:03d} refused by the instrument (reply {code})."
    if any(s not in arp_failed for s in arp_ops):
        aborted = aborted or guard()
        if aborted:
            committed, note = False, (f"Not stored: {aborted}; the patterns are only in volatile memory of the "
                                      f"original instrument. Backup: {backup_path}")
        else:
            progress("Storing arp pattern bank B")
            committed, note = _store_checked(service, OBJ_ARP_PATTERN, ARP_BANK_B)
        if committed is not True:
            for s in arp_ops:
                arp_failed.setdefault(s, f"Arp bank B: {note}")
    for s, (op, _) in arp_ops.items():
        if s in arp_failed:
            continue
        back = service.dump_object_parsed(OBJ_ARP_PATTERN, ARP_BANK_B, s, no_response_ms=6000)
        if back is None or back.body != op.body:
            arp_failed[s] = f"Arp pattern B{s:03d} did not read back as written."

    written: List[Tuple[ItemReport, SimpleNamespace]] = []
    unknown: List[Tuple[ItemReport, SimpleNamespace, bytes]] = []   # no reply to the write, state unproven
    for rep, op, pre, needed in pending:
        bad = [arp_failed[s] for s in sorted(needed) if s in arp_failed]
        if bad:
            rep.messages += bad + ["Not written: it depends on an arp pattern that is missing."]
            continue
        aborted = aborted or guard()
        if aborted:
            rep.messages.append(f"Not written: {aborted}.")
            continue
        progress(f"Writing {rep.dest}")
        code = service.write_object(op)
        if code == 0:
            written.append((rep, op))
        elif code == NO_REPLY:
            state = _state_after_no_reply(service, op, pre)
            if state == "applied":
                rep.messages.append("No reply to the write, but a re-read matches what was sent.")
                written.append((rep, op))
            elif state == "unchanged":
                rep.messages.append("No reply to the write; a re-read shows the slot unchanged.")
            else:
                rep.messages.append("No reply to the write and the slot could not be confirmed; it may or may not "
                                    f"have changed. Backup: {backup_path}")
                unknown.append((rep, op, pre))
        else:
            rep.messages.append(f"Write refused by the instrument (reply {code}).")

    # A bank is Stored only for slots that were proven written; an unknown slot never triggers a Store itself.
    committed_banks: Set[Tuple[int, int]] = set()
    failed_banks: Dict[Tuple[int, int], str] = {}
    for obj, bank in sorted({(op.obj, op.bank) for _, op in written}):
        aborted = aborted or guard()
        if aborted:
            committed, note = False, (f"Not stored: {aborted}. The slot was written to the volatile memory of the "
                                      f"original instrument only; the previous contents are in {backup_path}.")
        else:
            progress(f"Storing bank {bank_label(obj, bank)}")
            committed, note = _store_checked(service, obj, bank)
        if committed:
            committed_banks.add((obj, bank))
            if note:
                for rep, op in written:
                    if (op.obj, op.bank) == (obj, bank):
                        rep.messages.append(note)
        else:
            failed_banks[(obj, bank)] = note
            for rep, op in written:
                if (op.obj, op.bank) == (obj, bank):
                    rep.messages.append(note)
    if failed_banks and committed_banks:
        done = ", ".join(bank_label(o, b) for o, b in sorted(committed_banks))
        for rep, op in written:
            if (op.obj, op.bank) in failed_banks:
                rep.messages.append(f"Bank(s) already stored: {done}. The previous contents of every target slot are "
                                    f"in {backup_path}.")
    written = [(r, o) for r, o in written if (o.obj, o.bank) in committed_banks]

    for rep, op in written:
        if aborted:
            rep.messages.append(f"Not re-read: {aborted}.")
            continue
        back = service.dump_object_parsed(op.obj, op.bank, op.index, no_response_ms=6000)
        if back is not None and back.body == op.body:
            rep.status = "written"
        else:
            # A Nautilus re-reads HD-1/EXi Programs, Combis and arp patterns byte for byte after Store (live,
            # 2026-10-22), so a difference there is real.
            rep.status = "unverified"
            rep.messages.append("Stored, but the re-read " + (
                "got no reply." if back is None else "differs from what was sent (the instrument may normalise bodies)."))
    for rep, op, pre in unknown:
        if aborted:
            continue
        state = _state_after_no_reply(service, op, pre)
        if state == "applied" and (op.obj, op.bank) in committed_banks:
            rep.status = "written"
            rep.messages.append("Verified by re-read after the reply was lost.")
        elif state == "applied":
            rep.messages.append("The slot holds the new content but only in volatile memory (its bank was not Stored).")
        elif state == "unchanged":
            rep.messages.append("A later re-read shows the slot unchanged.")
    reports.arp_used = sorted(s for s in arp_ops if s not in arp_failed)


def _verify_backup(path: str, backups) -> Optional[str]:
    """None when the file re-parses (Nautilus codec) to exactly the pre-images, in order; else why not. The backup
    is the only way back from a bad conversion, so no write happens on an unverified one."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as ex:
        return f"cannot be read: {ex}"
    parts = raw.split(b"\xf7")
    if parts[-1]:
        return "it ends mid-record"
    got = []
    for chunk in parts[:-1]:
        d = nsx.parse_object_dump(chunk + b"\xf7")
        if d is None:
            return "it holds an unreadable record"
        got.append((d.obj, d.bank, d.index, d.body))
    want = [(b.obj, b.bank, b.index, b.body) for b in backups]
    if len(got) != len(want):
        return f"it holds {len(got)} of {len(want)} objects"
    if got != want:
        return "its contents differ from the pre-images"
    return None


def _state_after_no_reply(service, op, pre: bytes) -> str:
    """What a slot looks like after a write whose Reply never arrived: "applied", "unchanged" or "unknown"."""
    back = service.dump_object_parsed(op.obj, op.bank, op.index, no_response_ms=6000)
    if back is None:
        return "unknown"
    if back.body == op.body:
        return "applied"
    return "unchanged" if back.body == pre else "unknown"


def _store_checked(service, obj: int, bank: int) -> Tuple[Optional[bool], str]:
    """Store a bank. (True|False|None, note): committed, not committed, or unknown. A lost Reply stays unknown:
    the bank digest reflects the live edit state, not storage, so it cannot say whether the Store committed."""
    code = service.store_bank(obj, bank)
    if code == 0:
        return True, ""
    if code != NO_REPLY:
        return False, f"Store Bank failed (reply {code}); the object is only in volatile memory."
    # Store Bank commits the WHOLE bank, including unrelated unsaved edits in other slots (Nautilus, reboot test
    # 2026-10-22: a slot written before the Store survived, one written after did not). Reads cannot show it, since
    # unstored edits are served from live memory. A lost Store reply cannot be resolved remotely either (the
    # func-0x37 digest changes on a write, before any Store), so the user is told to check the instrument.
    return None, "No reply to Store Bank; the object may or may not be stored."


@dataclass
class RestoreResult:
    ok: bool
    messages: List[str] = field(default_factory=list)


def restore_from_backup(service, backup_path: str,
                        progress: Callable[[str], None] = lambda m: None) -> RestoreResult:
    """Replays a push's .syx backup (Nautilus codec) and re-Stores every bank it touches, putting the previous
    contents of the target slots back. The file is parsed completely before the first write. A bank whose writes
    were not all acknowledged is not Stored. Not wired to any menu yet.
    Verified on a Nautilus for Program, Combi and arp-pattern slots (2026-10-22)."""
    msgs: List[str] = []
    try:
        with open(backup_path, "rb") as f:
            raw = f.read()
    except OSError as ex:
        return RestoreResult(False, [f"Cannot read the backup: {ex}"])
    ops = []
    parts = raw.split(b"\xf7")
    if parts[-1]:
        return RestoreResult(False, ["The backup ends mid-record (truncated); nothing was written."])
    for chunk in parts:
        if not chunk:
            continue
        d = nsx.parse_object_dump(chunk + b"\xf7")
        if d is None:
            return RestoreResult(False, ["The backup holds an unreadable record; nothing was written."])
        ops.append(SimpleNamespace(obj=d.obj, bank=d.bank, index=d.index, version=d.version, body=d.body))
    if not ops:
        return RestoreResult(False, ["The backup is empty; nothing was written."])
    bad_banks: Set[Tuple[int, int]] = set()
    for op in ops:
        progress(f"Restoring {bank_label(op.obj, op.bank)} slot {op.index}")
        code = service.write_object(op)
        if code != 0:
            bad_banks.add((op.obj, op.bank))
            msgs.append(f"Write of {bank_label(op.obj, op.bank)} slot {op.index} not acknowledged (reply {code}).")
    ok = not bad_banks
    for obj, bank in sorted({(o.obj, o.bank) for o in ops} - bad_banks):
        committed, note = _store_checked(service, obj, bank)
        if committed is not True:
            ok = False
            msgs.append(f"Bank {bank_label(obj, bank)}: {note}")
    for obj, bank in sorted(bad_banks):
        msgs.append(f"Bank {bank_label(obj, bank)} was not Stored.")
    for op in ops:
        if (op.obj, op.bank) in bad_banks:
            continue
        back = service.dump_object_parsed(op.obj, op.bank, op.index, no_response_ms=6000)
        if back is None or back.body != op.body:
            ok = False
            msgs.append(f"{bank_label(op.obj, op.bank)} slot {op.index} did not read back as the backup.")
    if ok:
        msgs.append(f"Restored {len(ops)} object(s) from {backup_path}.")
    return RestoreResult(ok, msgs)


def _placements(items: List[ConvertItem], dest: Dict[int, Tuple[int, int]]) -> Dict[Tuple[int, int], Tuple[int, int]]:
    """Where each Program of this run will be written, as Combi Timbre references: a Combi converted alongside its
    Programs must point at them there, not at the slot numbers it had on the Kronos."""
    placed: Dict[Tuple[int, int], Tuple[int, int]] = {}
    next_slot = {t: s for t, (_, s) in dest.items()}
    for item in items:
        key = dest_key(item, dest)
        bank, _ = dest[key]
        slot = next_slot[key]
        next_slot[key] += 1
        if item.obj_type != OBJ_PROGRAM or item.src_ref is None or item.native:
            continue
        byte = nautilus_timbre_bank_for_wire(bank)
        if byte is not None and slot < slot_count(OBJ_PROGRAM, bank):
            placed[item.src_ref] = (byte, slot)
    return placed


def _convert(item: ConvertItem, template: bytes, tables: ConvertTables, options: karma.ArpOptions,
             placed: Optional[Dict[Tuple[int, int], Tuple[int, int]]] = None):
    t = tables
    if item.obj_type == OBJ_PROGRAM:
        return convert_program(item.body, program_kind_of(item), t.rom_map, t.exs, t.ge_catalog, template,
                               options, t.ge_patterns)
    if item.native:
        return ConversionResult(item.body) if len(item.body) == COMBI_NAUTILUS_SIZE else ConversionResult(
            None, [], [f"A Nautilus Song Timbre Set is {COMBI_NAUTILUS_SIZE} bytes, not {len(item.body)}."])
    if item.obj_type in (OBJ_COMBI, OBJ_SONG_TIMBRE):
        return convert_combi(item.body, t.ge_catalog, template, options, t.ge_patterns, ref_map=t.ref_map, placed=placed)
    if item.obj_type == OBJ_DRUM_KIT:
        return convert_drum_kit(item.body, t.rom_map, t.exs)
    return convert_wave_seq(item.body, t.rom_map, t.exs)
