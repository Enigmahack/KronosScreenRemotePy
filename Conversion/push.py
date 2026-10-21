"""Converts Kronos objects and writes them into consecutive Nautilus slots over SysEx.

Order is fixed for safety: read every target slot, convert, back every pre-image up to one .syx, only then write
(arp patterns first, so no Program ever points at a pattern that isn't there), store each touched bank once, and
re-read to verify. A target the conversion can't use (wrong Program bank type, refused conversion, past the end of
the bank) is skipped with a reason; it never aborts the others."""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Callable, Dict, List, Optional, Set, Tuple

import Data.object_types as object_types
from Conversion import karma
from Conversion.combi import convert_combi
from Conversion.data import KarmaGeCatalog, KarmaGePatternLibrary, ProgramRefMap, RomWaveformMap
from Conversion.media import convert_drum_kit, convert_wave_seq
from Conversion.profiles import NAUTILUS, ProgramKind
from Conversion.program import convert_program
from Data.librarian_sysex import OBJ_COMBI, OBJ_DRUM_KIT, OBJ_PROGRAM, OBJ_WAVE_SEQ
from Tools.exs_option_index import ExsOptionIndex

CONVERTIBLE_TYPES = (OBJ_PROGRAM, OBJ_COMBI, OBJ_DRUM_KIT, OBJ_WAVE_SEQ)

# Undocumented Nautilus Arp Pattern object (556 bytes); bank 0x40 = user bank B (B000-B127).
OBJ_ARP_PATTERN, ARP_BANK_B, ARP_BANK_B_SLOTS = 0x19, 0x40, 128


@dataclass
class ConvertItem:
    obj_type: int
    name: str
    body: bytes                 # Kronos wire body (Program: 3706 HD-1 / 4960 EXi)
    is_exi: bool = False


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


@dataclass
class ItemReport:
    name: str
    dest: str
    status: str                 # "written" | "refused" | "failed"
    messages: List[str] = field(default_factory=list)


def bank_label(obj_type: int, bank: int) -> str:
    """Nautilus user banks continue past its factory A-F as G.. (wire 0x40 = G, 0x4C = S), where Kronos says U-A.."""
    if obj_type in (OBJ_PROGRAM, OBJ_COMBI) and 0x40 <= bank <= 0x4D:
        return chr(71 + bank - 0x40)
    return object_types.label_for(obj_type, bank)


def dest_label(obj_type: int, bank: int, slot: int) -> str:
    return f"{bank_label(obj_type, bank)}{slot:03d}" if obj_type in (OBJ_PROGRAM, OBJ_COMBI) \
        else f"{bank_label(obj_type, bank)}:{slot:03d}"


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
        if item.obj_type == OBJ_COMBI and len(item.body) == 7810:
            base = karma.ArpOptions()
            slots = []
            for source in (base.combi_arp_a_source, base.combi_arp_b_source):
                k = 1814 + 744 * source
                slots.append(self.slot_for(item.body[k] | (item.body[k + 1] << 8)))
            if slots[0] is not None and slots[0] == slots[1]:
                slots[1] = None   # one pattern can't serve both arpeggiators
            return karma.ArpOptions(transcribed_pattern_slot=slots[0], transcribed_pattern_slot_b=slots[1])
        return karma.ArpOptions()


def push_converted(service, items: List[ConvertItem], dest: Dict[int, Tuple[int, int]], tables: ConvertTables,
                   backup_path: str, progress: Callable[[str], None] = lambda m: None,
                   arp_start: Optional[int] = None) -> List[ItemReport]:
    """`dest` maps object type -> (wire bank, first slot). `arp_start` = first bank-B slot for transcribed arp
    patterns (None = don't use transcriptions). `service` needs dump_object_parsed, backup_objects, write_object
    and store_bank (SysExService, already switched to the Nautilus codec)."""
    reports: List[ItemReport] = []
    pending: List[Tuple[ItemReport, SimpleNamespace, bytes, Set[int]]] = []   # report, op, pre-image, arp slots needed
    next_slot = {t: s for t, (_, s) in dest.items()}
    arp = _ArpSlots(arp_start, tables)
    arp_patterns: Dict[int, bytes] = {}

    for item in items:
        bank, _ = dest[item.obj_type]
        slot = next_slot[item.obj_type]
        next_slot[item.obj_type] += 1
        where = dest_label(item.obj_type, bank, slot)
        rep = ItemReport(item.name, where, "refused")
        reports.append(rep)
        if slot >= object_types.slot_count(item.obj_type, bank):
            rep.messages.append(f"Past the last slot of bank {object_types.label_for(item.obj_type, bank)}.")
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
        result = _convert(item, target.body, tables, arp.options_for(item))
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
        return reports

    arp_ops: Dict[int, Tuple[SimpleNamespace, bytes]] = {}
    arp_failed: Dict[int, str] = {}
    for s, pattern in sorted(arp_patterns.items()):
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
    service.backup_objects(backups, backup_path)
    progress(f"Backed up {len(backups)} target slot(s) to {backup_path}")

    for s, (op, _) in sorted(arp_ops.items()):
        progress(f"Writing arp pattern B{s:03d}")
        code = service.write_object(op)
        if code != 0:
            arp_failed[s] = f"Arp pattern write to B{s:03d} refused by the instrument (reply {code})."
    if any(s not in arp_failed for s in arp_ops):
        progress("Storing arp pattern bank B")
        code = service.store_bank(OBJ_ARP_PATTERN, ARP_BANK_B)
        if code != 0:
            for s in arp_ops:
                arp_failed.setdefault(s, f"Store Bank failed for arp bank B (reply {code}).")
    for s, (op, _) in arp_ops.items():
        if s in arp_failed:
            continue
        back = service.dump_object_parsed(OBJ_ARP_PATTERN, ARP_BANK_B, s, no_response_ms=6000)
        if back is None or back.body != op.body:
            arp_failed[s] = f"Arp pattern B{s:03d} did not read back as written."

    written = []
    for rep, op, _, needed in pending:
        bad = [arp_failed[s] for s in sorted(needed) if s in arp_failed]
        if bad:
            rep.messages += bad + ["Not written: it depends on an arp pattern that is missing."]
            continue
        progress(f"Writing {rep.dest}")
        code = service.write_object(op)
        if code != 0:
            rep.messages.append(f"Write refused by the instrument (reply {code}).")
            continue
        written.append((rep, op))
    for obj, bank in sorted({(op.obj, op.bank) for _, op in written}):
        progress(f"Storing bank {object_types.label_for(obj, bank)}")
        code = service.store_bank(obj, bank)
        if code != 0:
            for rep, op in written:
                if (op.obj, op.bank) == (obj, bank):
                    rep.messages.append(f"Store Bank failed (reply {code}); the object is only in volatile memory.")
            written = [(r, o) for r, o in written if (o.obj, o.bank) != (obj, bank)]
    for rep, op in written:
        back = service.dump_object_parsed(op.obj, op.bank, op.index, no_response_ms=6000)
        if back is not None and back.body == op.body:
            rep.status = "written"
        else:
            rep.messages.append("Written, but the re-read did not match what was sent.")
    return reports


def _convert(item: ConvertItem, template: bytes, tables: ConvertTables, options: karma.ArpOptions):
    t = tables
    if item.obj_type == OBJ_PROGRAM:
        return convert_program(item.body, program_kind_of(item), t.rom_map, t.exs, t.ge_catalog, template,
                               options, t.ge_patterns)
    if item.obj_type == OBJ_COMBI:
        return convert_combi(item.body, t.ge_catalog, template, options, t.ge_patterns, ref_map=t.ref_map)
    if item.obj_type == OBJ_DRUM_KIT:
        return convert_drum_kit(item.body, t.rom_map, t.exs)
    return convert_wave_seq(item.body, t.rom_map, t.exs)
