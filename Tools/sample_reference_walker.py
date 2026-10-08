r"""
Walks a Program body's own outgoing SAMPLE references (Sampling Mode/RAM, EXs, User Sample
Banks, EXi external PCM) — port of Core/LocalLibrary/SampleReferenceWalker.cs plus the
LibRefs.IterProgramSampleZoneRefs / IterExiPcmHighSlotCandidates iterators it depends on.

Separate from dependency_scanner on purpose: a sample reference is (16-byte Bank Identity,
numeric ID), NOT an ObjLoc. It points OUTSIDE the catalogued library at instrument/EXs/.KSC
filesystem content that can never be content-hashed, pulled, placed or repointed. Feeding it
into the object-reference walker would make every Program touching a non-ROM sample
permanently "unresolved". This walker is DISPLAY ONLY.

Drum Kit and Wave Sequence bodies carry sample refs too (Objects.object_body.
iter_drum_kit_sample_refs / iter_wave_seq_sample_refs); all three object types are walked here.

Byte layout hardware-confirmed 2026-08-27 against real Kronos-saved test programs (see the C#
file header for the corpus evidence):
  * HD-1 Program oscillator zone: [type/mode byte][16-byte Bank UUID][1 reserved][2-byte LE Number]
  * EXi MOD-7 / STR-1 (the only 2 of 9 engines with a PCM component): an EXi-internal encoding at a
    fixed absolute offset per engine, gated on the engine-type byte at 2857.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Tuple

from Data.librarian_sysex import OBJ_DRUM_KIT, OBJ_PROGRAM, OBJ_WAVE_SEQ
from Data.pcg_file import WIRE_SIZE_EXI, WIRE_SIZE_HD1

# Bucket names match the C# BankBucket enum (also what the panel's colour table keys on).
RAM = "SamplingModeRam"
EXS = "Exs"
USER = "UserOrThirdParty"
EXI_EXTERNAL = "ExiExternal"

ZONES_PER_OSC = 8
_OSC1_ZONE_BASE, _OSC2_ZONE_BASE, _ZONE_STRIDE, _ZONE_NUM_OFFSET = 2774, 3240, 22, 18
_OSC_MODE_OFFSET = 2558

_EXI_ALGORITHM_TYPE_OFFSET = 2857
_EXI_MOD7, _EXI_STR1 = 7, 4
_MOD7 = ("MOD-7", 3375, 3376, 3439)   # engine, flag, uuid, number offsets
_STR1 = ("STR-1", 3449, 3450, 3513)

# EXi's own "unpopulated slot" / ROM sentinel for the PCM OSC High slot.
EXI_PCM_HIGH_DEFAULT_BLOB = bytes([0xF4, 0x24, 0x75, 0x04, 0, 0, 0, 0, 0, 0, 0, 0xD0, 0x34, 0x05, 0x00, 0xB0])

# "KORG\0\0\0\0\0\0\0\0MS\0" — the legacy (byte-decodable) Bank Identity prefix; byte 15 carries the bank.
KORG_MS_PREFIX = bytes([0x4B, 0x4F, 0x52, 0x47, 0, 0, 0, 0, 0, 0, 0, 0, 0x4D, 0x53, 0x00])


@dataclass(frozen=True)
class SampleDependencyRow:
    description: str
    key: str          # dedup identity: bucket|bank identity (no count suffix)
    count: int
    bucket: str


def _is_legacy_form(uuid: bytes) -> bool:
    return len(uuid) == 16 and uuid[:15] == KORG_MS_PREFIX


def _is_all_zero(uuid: bytes) -> bool:
    return not any(uuid)


def dedup_key(uuid: bytes) -> str:
    """Byte 15 bit 0 is a mono/stereo flag, not part of bank identity — masked out."""
    masked = bytearray(uuid)
    masked[15] &= 0xFE
    return masked.hex().upper()


def _classify_legacy_uuid(uuid: bytes) -> Optional[Tuple[str, str, str]]:
    """(bucket, label, key), or None for ROM (callers skip it — 'don't care about ROM')."""
    if _is_legacy_form(uuid):
        legacy_bank = uuid[15] >> 1   # 0=ROM, 1=Sampling Mode (RAM), N+1=EXs<N>
        if legacy_bank == 0:
            return None
        if legacy_bank == 1:
            return RAM, "Sampling Mode (RAM)", "ram"
        n = legacy_bank - 1
        return EXS, f"EXs{n}", f"exs{n}"
    key = dedup_key(uuid)
    return USER, f"User/3rd-Party Sample Bank ({key[:12]}…)", key


def iter_program_sample_zone_refs(body: bytes) -> Iterator[Tuple[int, int, bytes, int]]:
    """HD-1 zone-type==1 ('Sample') zones that are NOT Drums-mode (osc mode 4/5 zones are Drum
    Kit object refs, reported elsewhere) — (osc, zone, uuid, number)."""
    if _OSC_MODE_OFFSET >= len(body):
        return
    osc_mode = body[_OSC_MODE_OFFSET] & 0x07
    for osc in range(2):
        base = _OSC1_ZONE_BASE if osc == 0 else _OSC2_ZONE_BASE
        for zone in range(ZONES_PER_OSC):
            type_off = base + zone * _ZONE_STRIDE
            num_off = type_off + _ZONE_NUM_OFFSET
            if num_off + 1 >= len(body):
                return
            if (body[type_off] & 0x03) != 1 or osc_mode in (4, 5):
                continue
            yield osc, zone, bytes(body[type_off + 1:type_off + 17]), body[num_off] | (body[num_off + 1] << 8)


def iter_exi_pcm_high_slot_candidates(body: bytes) -> Iterator[Tuple[str, bytes, int]]:
    """(engine, 16-byte blob, raw number) for an EXi Program whose engine is MOD-7 or STR-1 and
    whose PCM High slot has its 'MS Type' on-bit set."""
    if _EXI_ALGORITHM_TYPE_OFFSET >= len(body):
        return
    algo = body[_EXI_ALGORITHM_TYPE_OFFSET]
    site = _MOD7 if algo == _EXI_MOD7 else _STR1 if algo == _EXI_STR1 else None
    if site is None:
        return
    engine, flag_off, uuid_off, num_off = site
    if num_off + 1 >= len(body):
        return
    if (body[flag_off] & 0x01) == 0:
        return
    yield engine, bytes(body[uuid_off:uuid_off + 16]), body[num_off] | (body[num_off + 1] << 8)


def walk(obj_type: int, body: bytes) -> List[SampleDependencyRow]:
    """Port of SampleReferenceWalker.Walk."""
    if obj_type not in (OBJ_PROGRAM, OBJ_DRUM_KIT, OBJ_WAVE_SEQ):
        return []
    from Objects.object_body import is_init
    if is_init(obj_type, body):
        return []   # an untouched slot references nothing meaningful

    groups: Dict[str, Tuple[str, int, str]] = {}

    def add(bucket: str, label: str, key: str) -> None:
        gk = f"{bucket}|{key}"
        prev = groups.get(gk)
        groups[gk] = (label, prev[1] + 1, bucket) if prev else (label, 1, bucket)

    def add_legacy(uuid: bytes) -> None:
        if _is_all_zero(uuid):
            return   # nothing assigned — distinct from a real ROM legacy UUID
        c = _classify_legacy_uuid(uuid)
        if c is not None:
            add(*c)

    if obj_type == OBJ_DRUM_KIT:
        from Objects.object_body import iter_drum_kit_sample_refs
        for _n, _z, uuid, _i in iter_drum_kit_sample_refs(body):
            add_legacy(uuid)
    elif obj_type == OBJ_WAVE_SEQ:
        from Objects.object_body import iter_wave_seq_sample_refs
        for _st, uuid, _sel in iter_wave_seq_sample_refs(body):
            add_legacy(uuid)
    elif len(body) == WIRE_SIZE_HD1:
        for _o, _z, uuid, _n in iter_program_sample_zone_refs(body):
            add_legacy(uuid)
    elif len(body) == WIRE_SIZE_EXI:
        for engine, blob, raw in iter_exi_pcm_high_slot_candidates(body):
            if blob == EXI_PCM_HIGH_DEFAULT_BLOB:
                continue   # ROM / unassigned
            add(EXI_EXTERNAL,
                f"{engine} PCM OSC: External Sample Bank #{raw >> 4} "
                "(bank identity not decodable from PCG bytes alone)",
                f"{engine}|{blob.hex().upper()}")

    return [SampleDependencyRow(f"{label} ({count}x)" if count > 1 else label, gk, count, bucket)
            for gk, (label, count, bucket) in groups.items()]


# ── Self-test (python -m Tools.sample_reference_walker) — ported from
#    Core/LocalLibrary/SampleReferenceWalkerSelfTests.cs (the Program cases) ──────────────────

def _selftest() -> None:
    import sys
    from Objects.object_body import write_program_name

    fails: List[str] = []

    def check(name: str, cond: bool) -> None:
        if not cond:
            fails.append(name)

    def legacy_uuid(legacy_bank: int, stereo: int = 0) -> bytes:
        u = bytearray(16)
        u[:15] = KORG_MS_PREFIX
        u[15] = (legacy_bank << 1) | stereo
        return bytes(u)

    def write_zone(body: bytearray, osc: int, zone: int, ms_type: int, uuid: Optional[bytes], number: int) -> None:
        type_off = (_OSC1_ZONE_BASE if osc == 0 else _OSC2_ZONE_BASE) + zone * _ZONE_STRIDE
        body[type_off] = ms_type
        if uuid is not None:
            body[type_off + 1:type_off + 17] = uuid
        body[type_off + 18] = number & 0xFF
        body[type_off + 19] = (number >> 8) & 0xFF

    def program(name: str, size: int) -> bytearray:
        return bytearray(write_program_name(bytes(size), name))

    # HD-1: real Sample zone -> EXs, ROM skipped, RAM labelled, Wave-Sequence zone not a sample ref
    b = program("TEST HD1", WIRE_SIZE_HD1)
    write_zone(b, 0, 0, 1, legacy_uuid(18), 199)
    write_zone(b, 0, 1, 1, legacy_uuid(0), 0)
    write_zone(b, 0, 2, 1, legacy_uuid(1), 2)
    write_zone(b, 0, 3, 2, legacy_uuid(18), 5)
    rows = walk(OBJ_PROGRAM, bytes(b))
    check("hd1-exs-row-present", any("EXs17" in r.description for r in rows))
    check("hd1-exs-bucket", any("EXs17" in r.description and r.bucket == EXS for r in rows))
    check("hd1-rom-not-shown", not any("ROM" in r.description for r in rows))
    check("hd1-ram-row", any("Sampling Mode (RAM)" in r.description and r.bucket == RAM for r in rows))
    check("hd1-row-count-excludes-wave-seq-zone", len(rows) == 2)

    # HD-1 Drums-mode zone is a Drum Kit object ref, not a sample ref
    b = program("TEST DRUMS", WIRE_SIZE_HD1)
    b[_OSC_MODE_OFFSET] = 4
    write_zone(b, 0, 0, 1, legacy_uuid(18), 199)
    check("hd1-drums-zone-excluded", len(walk(OBJ_PROGRAM, bytes(b))) == 0)

    # raw (non-legacy) UUID -> User/3rd-party bucket; the mono/stereo bit dedupes
    raw = bytes([0x91, 0x66, 0xF8, 0x10, 0xD0, 0xC3, 0xBF, 0xB4, 0x6F, 0x89, 0x0A, 0x0C, 0x99, 0x6E, 0x0A, 0x02])
    sib = bytearray(raw); sib[15] |= 1
    b = program("TEST USR", WIRE_SIZE_HD1)
    write_zone(b, 0, 0, 1, raw, 2)
    write_zone(b, 0, 1, 1, bytes(sib), 3)
    rows = walk(OBJ_PROGRAM, bytes(b))
    check("raw-uuid-dedupes-stereo", len(rows) == 1)
    check("raw-uuid-labelled", len(rows) == 1 and "User/3rd-Party Sample Bank" in rows[0].description)
    check("raw-uuid-2x", len(rows) == 1 and "(2x)" in rows[0].description)
    check("raw-uuid-bucket", len(rows) == 1 and rows[0].bucket == USER)

    # EXi MOD-7 / STR-1, gated on the engine byte at 2857
    def exi(algo: int, flag_off: int, uuid_off: int, num_off: int, blob: bytes, raw_num: int) -> bytes:
        e = bytearray(WIRE_SIZE_EXI)
        e[2857] = algo
        e[flag_off] = 0xB1
        e[uuid_off:uuid_off + 16] = blob
        e[num_off] = raw_num & 0xFF
        e[num_off + 1] = raw_num >> 8
        return bytes(e)
    real_blob = bytes([0xA1, 0x9A, 0xB6, 0xAB, 0x66, 0x43, 0xF0, 0xA4, 0xA4, 0xCB, 0xF2, 0xB9, 0x4F, 0x4D, 0x34, 0x2E])
    rows = walk(OBJ_PROGRAM, exi(7, 3375, 3376, 3439, real_blob, 0x0C70))
    check("exi-mod7-real-ref", len(rows) == 1 and "MOD-7" in rows[0].description and "#199" in rows[0].description)
    check("exi-mod7-bucket", len(rows) == 1 and rows[0].bucket == EXI_EXTERNAL)
    check("exi-mod7-rom-excluded", len(walk(OBJ_PROGRAM, exi(7, 3375, 3376, 3439, EXI_PCM_HIGH_DEFAULT_BLOB, 0))) == 0)
    check("exi-non-pcm-engine-not-walked", len(walk(OBJ_PROGRAM, exi(3, 3375, 3376, 3439, real_blob, 0x0C70))) == 0)
    rows = walk(OBJ_PROGRAM, exi(4, 3449, 3450, 3513, real_blob, 0x0040))
    check("exi-str1-real-ref", len(rows) == 1 and "STR-1" in rows[0].description and "#4" in rows[0].description)

    # INIT objects reference nothing
    b = program("Init Program", WIRE_SIZE_HD1)
    write_zone(b, 0, 0, 1, legacy_uuid(18), 199)
    check("init-program-no-sample-rows", len(walk(OBJ_PROGRAM, bytes(b))) == 0)

    # non-Program types are out of scope here
    check("non-program-empty", walk(0x01, bytes(WIRE_SIZE_EXI)) == [])

    if fails:
        print("FAIL:", ", ".join(fails))
        sys.exit(1)
    print("sample_reference_walker self-test: OK")


if __name__ == "__main__":
    _selftest()
