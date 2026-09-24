"""
Central design tokens for Kronos ScreenRemote.

Single source of truth for colours, typography and spacing so the UI reads as
one system instead of ~150 ad-hoc inline literals. Pure strings — no Qt import —
so this module is safe to
import from anywhere, including at load time.

Usage:
    import Utils.theme as T
    lbl.setStyleSheet(f"color: {T.TEXT_DIM}; font-size: {T.FS_BODY}px;")

Prefer semantic tokens (TEXT_DIM, OK, ERROR) over raw hex. When a colour role is
missing, add a token here rather than inlining a new literal.
"""
from __future__ import annotations

# ── Backgrounds (elevation layers — kept deliberately few) ──────────────────
BG          = "#1A1A1A"   # window / status bar / dominant surface
PANEL       = "#212121"   # slightly raised panel
PANEL_ALT   = "#2A2A2A"   # buttons, inputs, headers (most raised)
INSET       = "#131313"   # sunken areas (log/console text views)

# ── Borders ─────────────────────────────────────────────────────────────────
BORDER      = "#333333"   # hairlines, separators
BORDER_STRONG = "#444444" # button/input outlines

# ── Text (one ramp of greys, by role — replaces #888/#666/#556/#555/#444) ───
TEXT        = "#CCCCCC"    # primary body text
TEXT_DIM    = "#888888"    # secondary / labels carrying a live value
TEXT_IDLE   = "#555555"    # dormant affordances (idle caret / count)
TEXT_FAINT  = "#444444"    # near-invisible (disabled, idle dot)

# ── Accent ──────────────────────────────────────────────────────────────────
ACCENT      = "#88AADD"    # links, headings, current-mode, active hint
ACCENT_DEEP = "#1A4E6E"    # accent fill / selection background

# ── Semantic status (fold the 8 greens / 7 reds / 6 ambers into these) ──────
OK          = "#55C155"    # success indicator (dots, bars)
OK_TEXT     = "#88DD88"    # success text on dark (higher contrast)
WARN        = "#CCAA33"    # in-progress / caution
ERROR       = "#CC4444"    # failure indicator
ERROR_TEXT  = "#DD6666"    # failure text on dark

# MIDI traffic indicators (RX green / TX red; dim tint when idle) — from the C#.
MIDI_RX      = "#4CAF50"
MIDI_RX_DIM  = "#2A3A2A"
MIDI_TX      = "#AF4C50"
MIDI_TX_DIM  = "#3A2A2A"

# ── Typography ──────────────────────────────────────────────────────────────
# Families are stylesheet font-family strings (with fallbacks). FONT_UI_FAMILY
# is the bare family for QFont(...) construction.
FONT_UI         = "'Segoe UI', Arial, sans-serif"
FONT_UI_FAMILY  = "Segoe UI"
# Single family (not a QSS list — see FONT_SYMBOL note). Consolas is the proven
# mono on the Windows target; used verbatim across the original code.
FONT_MONO       = "Consolas"
# Footer glyph icons: a bare single family for QFont(...) — NOT a QSS list.
# Qt's QSS parser does not reliably honour comma-separated font-family lists
# (that made the icons render as tofu), so icons get their font via setFont().
FONT_SYMBOL     = "Segoe UI Symbol"

# Type ramp (px — matches the codebase's existing px convention)
FS_CAPTION = 10   # captions, faint hints
FS_SMALL   = 11   # dense readouts / status bar text
FS_BODY    = 12   # default body
FS_H2      = 14   # sub-headings
FS_H1      = 16   # headings / prominent values
FS_ICON    = 15   # footer glyph icons (out-size body text so they read as icons)

# ── Spacing ─────────────────────────────────────────────────────────────────
PAD_TIGHT  = 2
PAD        = 4
PAD_WIDE   = 8


def app_stylesheet() -> str:
    """Application-wide QSS baseline applied once in main().

    Rules here cascade *with* per-widget ``setStyleSheet`` (they merge per
    property, they don't replace each other), so e.g. ``QLabel#footerIcon``
    keeps its size even when a dynamic setter changes only ``color``.
    """
    return f"""
    QToolTip {{
        color: {TEXT};
        background-color: {PANEL_ALT};
        border: 1px solid {BORDER_STRONG};
        padding: 3px;
    }}
    QStatusBar {{
        background: {BG};
        color: {TEXT_DIM};
    }}
    QStatusBar::item {{ border: none; }}
    /* NOTE: footer glyph icons are sized via QFont in code (see _icon in
       main_window), NOT via a QSS font rule here — a QSS font-family list was
       rendering them as tofu, and a QStatusBar font-size would cascade over the
       icon QFont. Text labels set their own font-size explicitly. */
    /* Unified button baseline. Windows that previously hand-rolled button
       styles now reference these same tokens so everything converges. */
    QPushButton {{
        background-color: {PANEL_ALT};
        color: {TEXT};
        border: 1px solid {BORDER_STRONG};
        border-radius: 3px;
        padding: 4px 10px;
    }}
    QPushButton:hover  {{ background-color: #333333; border-color: #555555; }}
    QPushButton:pressed{{ background-color: {ACCENT_DEEP}; }}
    QPushButton:disabled {{ color: {TEXT_IDLE}; border-color: {BORDER}; }}
    """
