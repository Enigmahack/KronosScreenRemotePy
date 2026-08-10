"""
Derived reset-to-INIT erase body — port of Core/LocalLibrary/EraseBody.cs.

FALLBACK blank-body builder for a committed pending-delete, used only when no
REAL captured blank template is available (offline AND never captured — see
blank_template_store.py, which prefers the instrument's own blank bytes over
this derived one). This derives a best-effort blank from the object's
existing body rather than synthesizing one from scratch.

The Kronos SysEx protocol has NO "delete object" command — func 0x73 always
writes a body, and every Program/Combi/Set-List slot on the instrument always
contains something — so the only way to make a slot "empty" on hardware is to
overwrite it with a blank/initialized object and Store it.

Every erase body is DERIVED from the slot's existing (valid) body: same wire
length and structure, only the identity fields cleared. That keeps the result
a body the instrument is guaranteed to accept. Set-List erase is a true empty
(all 128 slot names + comments blanked, matching SetListData.IsEmpty's own
blank-name test); Program/Combi erase is a reset-to-INIT identity (name and
category cleared, and a Combi's timbre references cleared so it points at
nothing) rather than a factory-accurate INIT patch set, which isn't available
offline.

Deliberately does NOT rewrite each Set List slot's performance type/bank/index
reference: the slot format has no "unassigned" encoding, so zeroing a ref just
makes it point at Combi INT-A:001 rather than "nothing" — more misleading, not
less. Leaving the original references in place while blanking the names is the
least-invasive "empty" this protocol allows.
"""
from __future__ import annotations

import librarian_sysex as lsx
import object_body as ob
from librarian_sysex import _TIMBRE_COUNT as TIMBRE_COUNT


def build(obj_type: int, existing_body: bytes) -> bytes:
    """Port of EraseBody.Build."""
    if obj_type == lsx.OBJ_SET_LIST:
        return _build_setlist(existing_body)
    if obj_type == lsx.OBJ_COMBI:
        return _build_combi(existing_body)
    if obj_type == lsx.OBJ_PROGRAM:
        return _build_program(existing_body)
    return bytes(existing_body)


def _build_setlist(existing: bytes) -> bytes:
    body = ob.write_setlist_name(existing, "")
    for slot in range(ob.SLOT_COUNT):
        body = ob.write_setlist_slot_name(body, slot, "")
        body = ob.write_setlist_slot_comments(body, slot, "")
    return body


def _build_combi(existing: bytes) -> bytes:
    body = ob.write_combi_name(existing, "INIT COMBI")
    body = ob.write_combi_category(body, 0, 0)
    mutable = bytearray(body)
    for t in range(TIMBRE_COUNT):
        lsx.set_combi_timbre_ref(mutable, t, func33_bank=0, number=0)
    return bytes(mutable)


def _build_program(existing: bytes) -> bytes:
    body = ob.write_program_name(existing, "INIT PROGRAM")
    return ob.write_program_category(body, 0, 0)


# ── Self-test (python erase_body.py) ─────────────────────────────────────────

def _selftest() -> None:
    import sys

    import kronos_sysex as ksx

    fails: list = []

    def check(name, cond):
        if not cond:
            fails.append(name)

    # Program: 4960-byte EXi-sized fake body, name at 0 (24 bytes), category
    # bytes per object_body.py's own offsets.
    prog = bytearray(b"\x00" * 4960)
    prog[0:24] = ob._pad_ascii("My Program", 24)
    erased = build(lsx.OBJ_PROGRAM, bytes(prog))
    check("program-length-preserved", len(erased) == len(prog))
    check("program-name-is-init", ksx._ascii_trim(erased, 0, 24) == "INIT PROGRAM")
    info = ob.parse_program_body(erased)
    check("program-category-cleared", info.category == 0 and info.sub_category == 0)

    # Combi: name at 0, timbre refs nonzero before erase, all-zero after.
    combi = bytearray(b"\x00" * 7810)
    combi[0:24] = ob._pad_ascii("My Combi", 24)
    for t in range(TIMBRE_COUNT):
        lsx.set_combi_timbre_ref(combi, t, func33_bank=5, number=9)
    erased = build(lsx.OBJ_COMBI, bytes(combi))
    check("combi-length-preserved", len(erased) == len(combi))
    check("combi-name-is-init", ksx._ascii_trim(erased, 0, 24) == "INIT COMBI")
    info = ob.parse_combi_body(erased)
    check("combi-category-cleared", info.category == 0 and info.sub_category == 0)
    check("combi-timbres-cleared",
          all(lsx.combi_timbre_ref(erased, t) == (0, 0) for t in range(TIMBRE_COUNT)))

    # Set List: name blanked, all slot names + comments blanked, length preserved.
    setlist = bytearray(b"\x00" * 69416)
    setlist[0:24] = ob._pad_ascii("My Set List", 24)
    setlist = ob.write_setlist_slot_name(bytes(setlist), 3, "Some Slot")
    setlist = ob.write_setlist_slot_comments(setlist, 3, "notes")
    erased = build(lsx.OBJ_SET_LIST, setlist)
    check("setlist-length-preserved", len(erased) == len(setlist))
    check("setlist-name-blank", ksx._ascii_trim(erased, 0, 24) == "")
    slot = ob.parse_setlist_slot(erased, 3)
    check("setlist-slot-name-blank", slot is not None and slot.name == "")

    if fails:
        print("FAIL:", ", ".join(fails))
        sys.exit(1)
    print("erase_body self-test: OK")


if __name__ == "__main__":
    _selftest()
