"""What an EXi Program conversion carries over unchanged and must report.

An EXi Program runs two engines (EXi1, EXi2; Algorithm Type at 2857 / 3909 in the Kronos layout, Prog_EXi_Common.txt),
each any of the EXi models. Their parameter blocks are copied byte for byte, which is right for everything except the
settings that name content the two instruments number differently:
  - MOD-7 / STR-1 PCM oscillators reference a ROM or external multisample in an EXi-internal encoding that is not decoded
    (Tools/sample_reference_walker.iter_exi_pcm_high_slot_candidates finds them).
  - SGX-2 Piano Type (u16 at the start of the engine block, 2908 / 3960) selects a piano set. Kronos corpus Programs use
    0-63 in groups of 16 (0-15 German D, 16-31 Japanese C, 48-63 Berlin); the Nautilus factory PRELOAD.PCG uses only
    64-127 (72-79 Italian, 80-95 "German2", 96-111 uprights, 112-127 small/prepared grands), e.g. Kronos "Virtual
    Insanity Piano" 15 is Nautilus 68. So the number is not portable and no mapping is known.
EP-1 Model Type uses 0-6 on both instruments' factory content and is carried over."""
from __future__ import annotations

from typing import List

from Tools.sample_reference_walker import (EXI2_SHIFT, EXI_PCM_HIGH_DEFAULT_BLOB, iter_exi_pcm_high_slot_candidates,
                                           kronos_layout_view)

_ALGORITHM_TYPE = 2857
_ENGINE_BLOCK = 2908   # EXi1 engine parameters (Kronos layout); EXi2's are EXI2_SHIFT further
_SGX2 = 8
_KRONOS_PIANO_GROUPS = {0: "German D", 1: "Japanese C", 3: "Berlin"}


def exi_notes(body: bytes, source: str, target: str) -> List[str]:
    """Warnings for an EXi Program body (either layout) converted from `source` to `target` (instrument names)."""
    view = kronos_layout_view(body)
    notes = []
    for engine, blob, raw in iter_exi_pcm_high_slot_candidates(body):
        which = "ROM" if blob == EXI_PCM_HIGH_DEFAULT_BLOB else "external sample bank"
        # HW-VERIFY: convert a MOD-7 / STR-1 Program whose PCM oscillator is on and listen on the target: whether the
        # number (#raw >> 4 here) uses the HD-1 ROM numbering, so it could be remapped like an HD-1 zone.
        notes.append(f"{engine if engine.startswith('EXi2') else 'EXi1 ' + engine} PCM oscillator ({which} multisample "
                     f"#{raw >> 4}) was carried over unchanged: EXi PCM references are not converted, so it may play a "
                     f"different waveform on the {target}. Check it by ear.")
    for n, shift in ((1, 0), (2, EXI2_SHIFT)):
        o = _ENGINE_BLOCK + shift
        if o + 1 >= len(view) or view[_ALGORITHM_TYPE + shift] != _SGX2:
            continue
        piano = view[o] | (view[o + 1] << 8)
        group = _KRONOS_PIANO_GROUPS.get(piano // 16) if source == "Kronos" else None
        # HW-VERIFY: which Nautilus Piano Type numbers load the German / Italian / Berlin sets (the owner confirms the
        # Nautilus has those three, not the Austrian one); with that, Kronos 0-63 could be mapped.
        notes.append(f"EXi{n} SGX-2 Piano Type {piano}{f' ({group})' if group else ''} was carried over unchanged, but the "
                     f"Kronos and Nautilus number their piano sets differently (Kronos factory Programs use 0-63, "
                     f"Nautilus 64-127): choose the piano again on the {target}.")
    return notes
