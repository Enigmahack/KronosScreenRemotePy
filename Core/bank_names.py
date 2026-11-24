"""Bank names per instrument family: one class per family, the Nautilus one overriding only what differs.

Bank bytes are object-dump (header) encoding. Add a new instrument by subclassing `KronosBankNames` and overriding
the hooks that differ; every caller goes through `for_family`.
"""
from __future__ import annotations

_A = ord("A")
USER_BASE = 0x40      # object-bank byte of the first user bank, every type, both families


def extra_bank_label(n: int) -> str:
    """User bank n (0-based) past the banks the instrument has. Korg's NAUTILUS_MIDI_SysEx.txt *2 lists Program banks
    A-F (0-5) and G-T (0x40-0x4D) only, Drum Kit / Wave Seq user banks 0x40-0x4D, and the MIDI bank-select table
    also ends at T; measured on a Nautilus, bank bytes 0x4E-0x60 answer with bank 0x4C's digest. A Nautilus .PCG
    can still hold more (Programs through 0x20021, Wave Sequences through 0x20012): they are read from the file
    and placed into real banks, never written to an address of their own."""
    return f"File bank {n + 1}"


class KronosBankNames:
    """Kronos: Programs I-A..I-G, GM, g(1)..g(d), U-A..U-G, U-AA..U-GG; Combis I-A..I-G, U-A..U-G."""

    PROGRAM_INTERNAL = 7          # object bytes 0..n-1
    COMBI_INTERNAL = 7
    PROGRAM_LAST_USER = 0x4D      # highest user-bank byte the instrument itself has
    COMBI_LAST_USER = 0x46
    MEDIA_LAST_USER = 0x4D        # Drum Kits and Wave Sequences

    # -- hooks a subclass overrides ------------------------------------------------------------------------------
    def internal(self, i: int) -> str:
        return f"I-{chr(_A + i)}"

    def user_program(self, i: int) -> str:
        """i = 0-based user bank (object byte - 0x40)."""
        if i <= 6:
            return f"U-{chr(_A + i)}"
        c = chr(_A + i - 7)
        return f"U-{c}{c}"

    user_combi = user_program
    user_media = user_program

    def has_gm_banks(self) -> bool:
        return True

    # -- labels --------------------------------------------------------------------------------------------------
    def program(self, ob: int) -> str:
        if 0 <= ob < self.PROGRAM_INTERNAL:
            return self.internal(ob)
        if self.has_gm_banks():
            if ob == 0x10:
                return "GM"
            if 0x11 <= ob <= 0x19:
                return f"g({ob - 0x10})"
            if ob == 0x1A:
                return "g(d)"
        return self._user_or_extra(ob, self.PROGRAM_LAST_USER, self.user_program)

    def combi(self, ob: int) -> str:
        if 0 <= ob < self.COMBI_INTERNAL:
            return self.internal(ob)
        return self._user_or_extra(ob, self.COMBI_LAST_USER, self.user_combi)

    def drum_kit(self, ob: int) -> str:
        if ob == 0:
            return "INT"
        if ob == 0x10:
            return "GM"
        return self._user_or_extra(ob, self.MEDIA_LAST_USER, self.user_media)

    def wave_seq(self, ob: int) -> str:
        if ob == 0:
            return "INT"
        return self._user_or_extra(ob, self.MEDIA_LAST_USER, self.user_media)

    @staticmethod
    def _user_or_extra(ob: int, last_user: int, user) -> str:
        if USER_BASE <= ob <= last_user:
            return user(ob - USER_BASE)
        if last_user < ob <= 0x7F:
            return extra_bank_label(ob - USER_BASE)
        return f"?{ob:02X}"


class NautilusBankNames(KronosBankNames):
    """Nautilus (NAUTILUS_MIDI_SysEx.txt *2): Programs A-F then G-T (0x40-0x4D); Combis A-G then H-N (0x40-0x46).
    No I-x / U-x banks, no GM / g(n) Program banks.
    Drum Kits / Wave Sequences are NOT overridden: NAUTILUS_MIDI_SysEx.txt lists their user banks as USER-A..G, AA..GG
    (linear map U-A00..U-GG31), the same as the Kronos."""

    PROGRAM_INTERNAL = 6          # A-F

    def internal(self, i: int) -> str:
        return chr(_A + i)

    def user_program(self, i: int) -> str:
        return chr(_A + 6 + i)    # G..T

    def user_combi(self, i: int) -> str:
        return chr(_A + 7 + i)    # H..N

    def has_gm_banks(self) -> bool:
        return False


KRONOS = KronosBankNames()
NAUTILUS = NautilusBankNames()


_active: KronosBankNames = KRONOS


def set_active(nautilus: bool) -> None:
    """Names for the connected instrument; SysExService.set_device_family() is the only caller. Labels asked for
    without an explicit family (`label_for(..., nautilus=None)`) use this."""
    global _active
    _active = NAUTILUS if nautilus else KRONOS


def for_family(nautilus: "bool | None" = None) -> KronosBankNames:
    """None = the connected instrument's names."""
    if nautilus is None:
        return _active
    return NAUTILUS if nautilus else KRONOS
