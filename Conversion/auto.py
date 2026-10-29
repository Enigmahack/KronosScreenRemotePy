"""Converts one object from the instrument family it came from to the family of the connected instrument, with Init
templates standing in for the target slot (the Librarian converts when an object enters the library, before any slot
is chosen). Kronos -> Nautilus is the live-verified direction; Nautilus -> Kronos is NOT verified on a Kronos."""
from __future__ import annotations

from typing import Callable, Optional

from Conversion import karma
from Conversion.combi import convert_combi
from Conversion.combi_reverse import convert_combi_to_kronos
from Conversion.data import init_combi_template, init_program_template
from Conversion.media import (convert_drum_kit, convert_drum_kit_to_kronos, convert_wave_seq,
                              convert_wave_seq_to_kronos)
from Conversion.profiles import ProgramKind
from Conversion.program import convert_program
from Conversion.program_reverse import convert_program_to_kronos
from Conversion.push import ConvertTables
from Conversion.result import ConversionResult
from Data.librarian_sysex import OBJ_COMBI, OBJ_DRUM_KIT, OBJ_PROGRAM, OBJ_SONG_TIMBRE, OBJ_WAVE_SEQ
from Data.pcg_file import FAMILY_KRONOS, FAMILY_NAUTILUS

CONVERTIBLE = (OBJ_PROGRAM, OBJ_COMBI, OBJ_SONG_TIMBRE, OBJ_DRUM_KIT, OBJ_WAVE_SEQ)


def convert_object(obj_type: int, body: bytes, is_exi: bool, src_family: str, dst_family: str,
                   tables: ConvertTables,
                   program_name: Optional[Callable[[int, int], Optional[str]]] = None) -> ConversionResult:
    """`body` is a wire body (a Program may also be a PCG slot). Same family: returned as is.

    `program_name(Timbre bank byte, slot)` names the Program a Combi Timbre points at in the source set (None when the
    set does not hold it). A Timbre is re-pointed to the other instrument's factory Program only while that name still
    matches the factory Program; a user's own Program of the same slot keeps its place in the same wire bank."""
    if src_family == dst_family:
        return ConversionResult(body)
    to_nautilus = dst_family == FAMILY_NAUTILUS and src_family == FAMILY_KRONOS
    to_kronos = dst_family == FAMILY_KRONOS and src_family == FAMILY_NAUTILUS
    if not (to_nautilus or to_kronos):
        return ConversionResult(None, [], [f"Cannot convert {src_family} to {dst_family}."])
    kind = ProgramKind.EXI if is_exi else ProgramKind.HD1
    if obj_type == OBJ_PROGRAM:
        if to_nautilus:
            return convert_program(body, kind, tables.rom_map, tables.exs, tables.ge_catalog,
                                   init_program_template(True, is_exi), karma.ArpOptions(), tables.ge_patterns)
        return convert_program_to_kronos(body, kind, tables.rom_map, tables.exs, init_program_template(False, is_exi),
                                         init_program_template(True, is_exi))
    if obj_type in (OBJ_COMBI, OBJ_SONG_TIMBRE):
        if to_nautilus:
            return convert_combi(body, tables.ge_catalog, init_combi_template(True), karma.ArpOptions(),
                                 tables.ge_patterns, ref_map=tables.ref_map, kronos_program_name=program_name)
        return convert_combi_to_kronos(body, init_combi_template(False), tables.ref_map, program_name)
    if obj_type == OBJ_DRUM_KIT:
        return (convert_drum_kit if to_nautilus else convert_drum_kit_to_kronos)(body, tables.rom_map, tables.exs)
    if obj_type == OBJ_WAVE_SEQ:
        return (convert_wave_seq if to_nautilus else convert_wave_seq_to_kronos)(body, tables.rom_map, tables.exs)
    return ConversionResult(None, [], ["This kind of object is not converted between instruments."])
