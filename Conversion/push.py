"""Converts objects of the other instrument family and writes them into consecutive slots of the connected instrument
(Kronos -> Nautilus or Nautilus -> Kronos) over SysEx. The target is the service's family (`is_nautilus`); an item marked
`native` is already in the target's format and is copied. Arp patterns exist only on the Nautilus.

Order is fixed for safety: read every target slot, convert, back every pre-image up to one .syx, only then write
(arp patterns first, so no Program ever points at a pattern that isn't there; then Drum Kits and Wave Sequences; then
Programs, each whose oscillator zones were re-pointed at a Drum Kit / Wave Sequence of this run only once that object
was written and Stored; then the Combis / Song Timbre Sets, likewise for the Programs their Timbres point at), store
each touched bank, and re-read to verify. A target the conversion can't use (wrong Program bank type, refused conversion, past the end of
the bank) is skipped with a reason; it never aborts the others."""
from __future__ import annotations

import contextlib
import dataclasses
import re
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Callable, Dict, List, Optional, Set, Tuple

import Data.librarian_sysex as ksx
import Data.nautilus_sysex as nsx
import Data.object_types as object_types
from Conversion import karma
from Conversion.combi import convert_combi
from Conversion.combi_reverse import convert_combi_to_kronos
from Conversion.data import (KarmaGeCatalog, KarmaGePatternLibrary, ProgramRefMap, RomWaveformMap,
                             UnknownProgramIdentity, init_program_template, kronos_timbre_bank_for_obj,
                             nautilus_timbre_bank_for_wire)
from Conversion.media import convert_drum_kit, convert_drum_kit_to_kronos, convert_wave_seq, convert_wave_seq_to_kronos
from Conversion.profiles import KRONOS, NAUTILUS, ProgramKind, COMBI_KRONOS_SIZE, COMBI_NAUTILUS_SIZE
from Conversion.program import convert_program
from Conversion.program_reverse import convert_program_to_kronos
from Conversion.result import ConversionResult
from Data.librarian_model import apply_ref_patch, iter_program_media_refs
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
    body: bytes                 # wire body of the source family (the other instrument), or of the target when native
    is_exi: bool = False
    native: bool = False        # body is already in the target instrument's format: copied, not converted
    # Where the object was read from, as its referrers address it. Program: (source Combi Timbre bank byte, slot);
    # Drum Kit / Wave Sequence: (object bank, slot), turned into the linear number a Program oscillator zone uses.
    src_ref: Optional[Tuple[int, int]] = None
    program_name: Optional[Callable[[int, int], Optional[str]]] = None   # Combi: names the Programs its Timbres use


@dataclass
class ConvertTables:
    rom_map: RomWaveformMap
    exs: ExsOptionIndex
    ge_catalog: KarmaGeCatalog
    ref_map: ProgramRefMap
    ge_patterns: Optional[KarmaGePatternLibrary] = None
    # Not a table: the user's choice (Settings > Librarian) for a Program reference whose source slot is not known.
    unknown_identity: UnknownProgramIdentity = UnknownProgramIdentity.ASSUME_FACTORY

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


def _nautilus_target(service) -> bool:
    """The connected instrument's family, for labels: Nautilus unless the service says it is a Kronos."""
    return bool(getattr(service, "is_nautilus", True))


def bank_label(obj_type: int, bank: int, nautilus: bool = True) -> str:
    """Nautilus names: Programs A-F then G.. (wire 0x40 = G, 0x4C = S), Combis A-G then H.. (0x40 = H); Kronos says I-A / U-A.."""
    if obj_type == OBJ_SONG_TIMBRE:
        return "Songs"
    if nautilus and obj_type in (OBJ_PROGRAM, OBJ_COMBI) and (0x40 <= bank <= 0x4D or bank <= 6):
        return object_types.label_for(obj_type, bank, True)
    if obj_type == OBJ_ARP_PATTERN:
        return "arp pattern bank B"
    return object_types.label_for(obj_type, bank)


def type_name(obj_type: int) -> str:
    return "Song Timbre Set" if obj_type == OBJ_SONG_TIMBRE else object_types.REGISTRY[obj_type].display_name


def slot_count(obj_type: int, bank: int) -> int:
    return SONG_SLOTS if obj_type == OBJ_SONG_TIMBRE else object_types.slot_count(obj_type, bank)


def dest_label(obj_type: int, bank: int, slot: int, nautilus: bool = True) -> str:
    if obj_type == OBJ_SONG_TIMBRE:
        return f"Song {slot:03d}"
    label = bank_label(obj_type, bank, nautilus)
    return f"{label}{slot:03d}" if obj_type in (OBJ_PROGRAM, OBJ_COMBI) else f"{label}:{slot:03d}"


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

    def slot_for(self, ge: Optional[int]) -> Optional[int]:
        """None when the module is unused, the GE has a preset, no usable transcription, or the bank is full."""
        lib = self.tables.ge_patterns
        if self.next is None or ge is None or karma.preset_for_ge(ge) is not None or lib is None:
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
        if item.native:
            return karma.ArpOptions()   # copied as is: no KARMA to translate
        if item.obj_type == OBJ_PROGRAM:
            return karma.ArpOptions(transcribed_pattern_slot=self.slot_for(karma.module_ge(item.body)))
        if item.obj_type in (OBJ_COMBI, OBJ_SONG_TIMBRE) and len(item.body) == 7810:
            base = karma.ArpOptions()
            slots = []
            for source in (base.combi_arp_a_source, base.combi_arp_b_source):
                slots.append(self.slot_for(karma.module_ge(item.body, 1814 + 744 * source, combi=True)))
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


def _connection_problem(service, still_valid: Optional[Callable[[], bool]],
                        expect_nautilus: Optional[bool] = None) -> Optional[str]:
    """Why it is no longer safe to send this push's messages to `service`, or None. A reconnect may be to a different
    instrument (SysExService.stop() resets the codec to Kronos), so nothing is sent once the link is not the same
    live connection, to the same instrument family, the user confirmed."""
    if expect_nautilus is not None and bool(getattr(service, "is_nautilus", True)) != expect_nautilus:
        return f"the connection is no longer a {'Nautilus' if expect_nautilus else 'Kronos'}"
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
    and store_bank (SysExService, already switched to the target's codec). `still_valid` is checked, with the
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
    nautilus = _nautilus_target(service)
    profile, timbre_set_size = (NAUTILUS, COMBI_NAUTILUS_SIZE) if nautilus else (KRONOS, COMBI_KRONOS_SIZE)

    def guard() -> Optional[str]:
        return _connection_problem(service, still_valid, nautilus)

    pending: List[Tuple[ItemReport, SimpleNamespace, bytes, Set[int]]] = []   # report, op, pre-image, arp slots needed
    slots = _plan_slots(items, dest)
    placed, media, placed_at = _placements(items, slots, nautilus)
    arp = _ArpSlots(arp_start if nautilus else None, tables)
    arp_patterns: Dict[int, bytes] = {}
    uses: Dict[int, Set[tuple]] = {}   # id(report) -> typed placement keys its references were re-pointed at

    for i, item in enumerate(items):
        bank, slot = slots[i]
        where = dest_label(item.obj_type, bank, slot, nautilus)
        rep = ItemReport(item.name, where, "refused")
        reports.append(rep)
        if slot >= slot_count(item.obj_type, bank):
            rep.messages.append(f"Past the last slot of bank {bank_label(item.obj_type, bank, nautilus)}.")
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
            want = profile.wire_size(program_kind_of(item))
            if len(target.body) != want:
                kind = "EXi" if len(target.body) == profile.wire_exi else "HD-1"
                rep.messages.append(f"{bank_label(OBJ_PROGRAM, bank, nautilus)} is a {kind} bank; this is a "
                                    f"{program_kind_of(item).value.upper()} Program - pick a matching bank.")
                continue
        if item.obj_type == OBJ_SONG_TIMBRE and len(target.body) != timbre_set_size:
            rep.messages.append(f"Song {slot:03d} is {len(target.body)} bytes on the instrument, not a "
                                f"{timbre_set_size}-byte Song Timbre Set.")
            continue
        if item.obj_type == OBJ_SONG_TIMBRE:
            old = target.body[:24].split(b"\x00")[0].decode("ascii", "replace").rstrip()
            if not re.fullmatch(r"NEW SONG \d{3}", old):
                rep.messages.append(f'Replaces the song "{old}"; its previous content is in the backup.')
        used = _UsedPlacements(placed)
        result = _convert(item, target.body, tables, arp.options_for(item), used, nautilus)
        uses[id(rep)] = {("program", k) for k in used.hits}
        rep.messages += result.warnings
        if result.body is None:
            rep.messages += result.refusals
            continue
        if item.obj_type == OBJ_PROGRAM and media:
            body, hits = _repoint_media(result.body, media)
            if hits:
                result = dataclasses.replace(result, body=body)
                uses[id(rep)] |= hits
                rep.messages.append("Oscillator zones re-pointed to the Drum Kit / Wave Sequence written in this run: "
                                    + ", ".join(dest_label(*placed_at[k][1:], nautilus) for k in sorted(hits)) + ".")
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
    problem = _verify_backup(backup_path, backups, nsx if nautilus else ksx)
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
    committed_banks: Set[Tuple[int, int]] = set()
    failed_banks: Dict[Tuple[int, int], str] = {}

    def write_and_store(entries) -> None:
        """Writes `entries`, then Stores each bank one of them was proven written to. A bank is Stored only for
        slots that were proven written; an unknown slot never triggers a Store itself."""
        nonlocal aborted
        phase_written: List[Tuple[ItemReport, SimpleNamespace]] = []
        for rep, op, pre, needed in entries:
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
                phase_written.append((rep, op))
            elif code == NO_REPLY:
                state = _state_after_no_reply(service, op, pre)
                if state == "applied":
                    rep.messages.append("No reply to the write, but a re-read matches what was sent.")
                    phase_written.append((rep, op))
                elif state == "unchanged":
                    rep.messages.append("No reply to the write; a re-read shows the slot unchanged.")
                else:
                    rep.messages.append("No reply to the write and the slot could not be confirmed; it may or may "
                                        f"not have changed. Backup: {backup_path}")
                    unknown.append((rep, op, pre))
            else:
                rep.messages.append(f"Write refused by the instrument (reply {code}).")
        written.extend(phase_written)
        for obj, bank in sorted({(op.obj, op.bank) for _, op in phase_written}):
            aborted = aborted or guard()
            if aborted:
                committed, note = False, (f"Not stored: {aborted}. The slot was written to the volatile memory of "
                                          f"the original instrument only; the previous contents are in "
                                          f"{backup_path}.")
            else:
                progress(f"Storing bank {bank_label(obj, bank, nautilus)}")
                committed, note = _store_checked(service, obj, bank)
            if committed:
                committed_banks.add((obj, bank))
            else:
                failed_banks[(obj, bank)] = note
            if note:
                for rep, op in phase_written:
                    if (op.obj, op.bank) == (obj, bank):
                        rep.messages.append(note)

    def committed_only(entries) -> list:
        """The entries whose re-pointed references all landed: every object of this run they now point at is committed
        (written AND its bank Stored). Otherwise a reference would point at whatever that slot still holds."""
        committed_slots = {(op.obj, op.bank, op.index) for _, op in written if (op.obj, op.bank) in committed_banks}
        out = []
        for entry in entries:
            rep = entry[0]
            missing = []
            for key in sorted(uses[id(rep)]):
                i, obj, bank, slot = placed_at[key]
                if (obj, bank, slot) in committed_slots:
                    continue
                prep = reports[i]
                reason = prep.messages[-1] if prep.messages else prep.status
                missing.append(f'{type_name(obj)} "{prep.name}" ({prep.dest}) was not written and stored '
                               f'({prep.status}: {reason})')
            if missing:
                rep.messages += missing + ["Not written: an object it uses failed in this run, so its references "
                                           "would point at whatever that slot still holds."]
                progress(f"Skipped {rep.dest} ({rep.name}): {'; '.join(missing)}")
                continue
            out.append(entry)
        return out

    # Phase 1: Drum Kits and Wave Sequences. Phase 2: Programs, gated on the Drum Kits / Wave Sequences their zones were
    # re-pointed at. Phase 3: Combis / Song Timbre Sets, gated on the Programs their Timbres were re-pointed at.
    # Split by type, so no bank is Stored in two phases (a later failed Store must not hide behind an earlier one).
    media_types, late = (OBJ_DRUM_KIT, OBJ_WAVE_SEQ), (OBJ_COMBI, OBJ_SONG_TIMBRE)
    write_and_store([e for e in pending if e[1].obj in media_types])
    write_and_store(committed_only([e for e in pending if e[1].obj not in media_types + late]))
    write_and_store(committed_only([e for e in pending if e[1].obj in late]))

    if failed_banks and committed_banks:
        done = ", ".join(bank_label(o, b, nautilus) for o, b in sorted(committed_banks))
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


def _verify_backup(path: str, backups, codec=nsx) -> Optional[str]:
    """None when the file re-parses (with the target's codec) to exactly the pre-images, in order; else why not. The backup
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
        d = codec.parse_object_dump(chunk + b"\xf7")
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
                        progress: Callable[[str], None] = lambda m: None,
                        still_valid: Optional[Callable[[], bool]] = None) -> RestoreResult:
    """Replays a push's .syx backup (Nautilus codec) and re-Stores every bank it touches, putting the previous
    contents of the target slots back. The file is parsed completely before the first write. A bank whose writes
    were not all acknowledged is not Stored. Not wired to any menu yet.
    Verified on a Nautilus for Program, Combi and arp-pattern slots (2026-10-22)."""
    msgs: List[str] = []
    nautilus = _nautilus_target(service)   # bank names in the messages follow the connected instrument
    try:
        with open(backup_path, "rb") as f:
            raw = f.read()
    except OSError as ex:
        return RestoreResult(False, [f"Cannot read the backup: {ex}"])
    problem = _connection_problem(service, still_valid)
    if problem:   # the backup is Nautilus-encoded; a reconnect may have reset the service to the Kronos codec
        return RestoreResult(False, [f"Not restored: {problem}; nothing was written."])
    ops = []
    parts = raw.split(b"\xf7")
    if parts[-1]:
        return RestoreResult(False, ["The backup ends mid-record (truncated); nothing was written."])
    for chunk in parts:
        if not chunk:
            continue
        d = getattr(service, "_codec", nsx).parse_object_dump(chunk + b"\xf7")
        if d is None:
            return RestoreResult(False, ["The backup holds an unreadable record; nothing was written."])
        ops.append(SimpleNamespace(obj=d.obj, bank=d.bank, index=d.index, version=d.version, body=d.body))
    if not ops:
        return RestoreResult(False, ["The backup is empty; nothing was written."])
    bad_banks: Set[Tuple[int, int]] = set()
    for op in ops:
        problem = _connection_problem(service, still_valid)
        if problem:
            bad_banks.add((op.obj, op.bank))
            msgs.append(f"Not restored {bank_label(op.obj, op.bank, nautilus)} slot {op.index}: {problem}.")
            continue
        progress(f"Restoring {bank_label(op.obj, op.bank, nautilus)} slot {op.index}")
        code = service.write_object(op)
        if code != 0:
            bad_banks.add((op.obj, op.bank))
            msgs.append(f"Write of {bank_label(op.obj, op.bank, nautilus)} slot {op.index} not acknowledged (reply {code}).")
    ok = not bad_banks
    for obj, bank in sorted({(o.obj, o.bank) for o in ops} - bad_banks):
        committed, note = _store_checked(service, obj, bank)
        if committed is not True:
            ok = False
            msgs.append(f"Bank {bank_label(obj, bank, nautilus)}: {note}")
    for obj, bank in sorted(bad_banks):
        msgs.append(f"Bank {bank_label(obj, bank, nautilus)} was not Stored.")
    for op in ops:
        if (op.obj, op.bank) in bad_banks:
            continue
        back = service.dump_object_parsed(op.obj, op.bank, op.index, no_response_ms=6000)
        if back is None or back.body != op.body:
            ok = False
            msgs.append(f"{bank_label(op.obj, op.bank, nautilus)} slot {op.index} did not read back as the backup.")
    if ok:
        msgs.append(f"Restored {len(ops)} object(s) from {backup_path}.")
    return RestoreResult(ok, msgs)


class _UsedPlacements(dict):
    """The run's placements, recording which ones a conversion actually re-pointed a Timbre at (convert_combi tests
    `(bank, index) in placed` before using an entry), so a Combi is only written once those Programs are committed."""

    def __init__(self, placed) -> None:
        super().__init__(placed)
        self.hits: Set[Tuple[int, int]] = set()

    def __contains__(self, key) -> bool:
        hit = super().__contains__(key)
        if hit:
            self.hits.add(key)
        return hit


def _plan_slots(items: List[ConvertItem], dest: Dict[int, Tuple[int, int]]) -> List[Tuple[int, int]]:
    """(wire bank, slot) each item is written to: consecutive slots per destination."""
    next_slot = {t: s for t, (_, s) in dest.items()}
    out = []
    for item in items:
        key = dest_key(item, dest)
        out.append((dest[key][0], next_slot[key]))
        next_slot[key] += 1
    return out


def _linear(obj_type: int, bank: int, slot: int) -> Optional[int]:
    if obj_type == OBJ_DRUM_KIT:
        return object_types.drum_kit_loc_to_linear(bank, slot)
    return object_types.wave_seq_loc_to_linear(bank, slot)


def _placements(items: List[ConvertItem], slots: List[Tuple[int, int]], nautilus: bool = True):
    """Where this run writes the objects other objects of the run may reference, so a referrer converted alongside them
    points at them there, not at the slot numbers they had on the Kronos. Returns (programs, media, placed_at):
    programs: Combi Timbre reference (bank byte, slot) -> the same at the destination;
    media: (Drum Kit / Wave Sequence type, linear number a Program oscillator zone holds) -> the destination's;
    placed_at: typed key ("program", ref) / ("media", type, linear) -> (item index, type, wire bank, slot)."""
    programs: Dict[Tuple[int, int], Tuple[int, int]] = {}
    media: Dict[Tuple[int, int], int] = {}
    placed_at: Dict[tuple, Tuple[int, int, int, int]] = {}
    for i, (item, (bank, slot)) in enumerate(zip(items, slots)):
        if item.src_ref is None or item.native or slot >= slot_count(item.obj_type, bank):
            continue
        if item.obj_type == OBJ_PROGRAM:
            byte = nautilus_timbre_bank_for_wire(bank) if nautilus else kronos_timbre_bank_for_obj(bank)
            if byte is not None:
                programs[item.src_ref] = (byte, slot)
                placed_at[("program", item.src_ref)] = (i, OBJ_PROGRAM, bank, slot)
        elif item.obj_type in (OBJ_DRUM_KIT, OBJ_WAVE_SEQ):
            src, dst = _linear(item.obj_type, *item.src_ref), _linear(item.obj_type, bank, slot)
            if src is not None and dst is not None:
                media[(item.obj_type, src)] = dst
                placed_at[("media", item.obj_type, src)] = (i, item.obj_type, bank, slot)
    return programs, media, placed_at


def _repoint_media(body: bytes, media: Dict[Tuple[int, int], int]) -> Tuple[bytes, Set[tuple]]:
    """Points a converted Program's Drum Kit / Wave Sequence zones at where this run writes those objects."""
    out, hits = bytearray(body), set()
    for site, obj_type, number in iter_program_media_refs(body):
        dst = media.get((obj_type, number))
        if dst is not None:
            apply_ref_patch(out, 'osc_zone', site, 0, dst)
            hits.add(("media", obj_type, number))
    return bytes(out), hits


def _convert(item: ConvertItem, template: bytes, tables: ConvertTables, options: karma.ArpOptions,
             placed: Optional[Dict[Tuple[int, int], Tuple[int, int]]] = None, to_nautilus: bool = True):
    """`template` is the target slot's current body: it supplies what the source format has no counterpart for."""
    t = tables
    if item.native:
        # Already in the target's format: copied, never converted; it must be the size the target slot holds (a
        # Program slot's size also says HD-1 vs EXi).
        return ConversionResult(item.body) if len(item.body) == len(template) else ConversionResult(
            None, [], [f"A {type_name(item.obj_type)} here must be {len(template)} bytes, not {len(item.body)}."])
    if not to_nautilus:
        if item.obj_type == OBJ_PROGRAM:
            return convert_program_to_kronos(item.body, program_kind_of(item), t.rom_map, t.exs, template,
                                             init_program_template(True, item.is_exi))
        if item.obj_type in (OBJ_COMBI, OBJ_SONG_TIMBRE):
            return convert_combi_to_kronos(item.body, template, t.ref_map, item.program_name, t.unknown_identity,
                                           placed)
        if item.obj_type == OBJ_DRUM_KIT:
            return convert_drum_kit_to_kronos(item.body, t.rom_map, t.exs)
        return convert_wave_seq_to_kronos(item.body, t.rom_map, t.exs)
    if item.obj_type == OBJ_PROGRAM:
        return convert_program(item.body, program_kind_of(item), t.rom_map, t.exs, t.ge_catalog, template,
                               options, t.ge_patterns)
    if item.obj_type in (OBJ_COMBI, OBJ_SONG_TIMBRE):
        return convert_combi(item.body, t.ge_catalog, template, options, t.ge_patterns, ref_map=t.ref_map, placed=placed,
                             kronos_program_name=item.program_name, unknown_identity=t.unknown_identity)
    if item.obj_type == OBJ_DRUM_KIT:
        return convert_drum_kit(item.body, t.rom_map, t.exs)
    return convert_wave_seq(item.body, t.rom_map, t.exs)
