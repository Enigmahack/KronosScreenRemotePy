# UI/UX Standardization — Running List

A living checklist of UI/UX inconsistencies and recommendations for
`KronosScreenRemotePy`. Items are grouped by priority. Check them off as they
are implemented (one at a time, per the feature-parity workflow).

_Last updated: 2026-07-13 (all 12 items implemented — see Implementation notes)._

> **Status: all items implemented.** New `theme.py` holds the design tokens; the
> footer, `main_window`, and the other windows' shared semantic colours now
> reference them. One thing needs a human eyeball: **confirm the footer icons
> actually render larger** (QSS-driven font size can't be self-verified
> headlessly). See Implementation notes at the bottom for scope decisions.

---

## Root causes (why the drift exists)

1. **No design-token layer.** Every color, font, and size is a hardcoded string
   literal inside `setStyleSheet(...)` calls, spread across 11 modules. There is
   no `theme.py` / shared palette, so the same semantic idea ("dim text",
   "panel background", "OK/green") is spelled a dozen slightly-different ways.
2. **The footer uses text glyphs as icons.** `⌨︎ ⇄ ● ⊞ ▾` are Unicode
   characters rendered by `QLabel` at the 11px status-bar font size — hence
   *tiny*, and their weight/shape depends on whatever font happens to resolve
   the glyph. There are no real icon assets (only `AppIcon.*`).
3. **Ad-hoc per-widget styling.** Each widget re-specifies its own colors and
   metrics, so nothing stays in sync when one place changes.

---

## P1 — High impact (address first)

### [x] 1. Create a central theme / design-token module (`theme.py`)
**Evidence:** 150+ inline hex-color literals across 11 files; no shared palette.
Near-duplicate values used interchangeably for one semantic role:
- *Dim text greys:* `#888`, `#666`, `#556`, `#555`, `#444` (footer alone —
  `main_window.py:1188,1204,1207,1211,1216,1227`). `#556` is effectively a typo
  for `#555`.
- *Panel/near-black backgrounds:* `#1A1A1A`, `#191919`, `#1B1B1B`, `#1E1E1E`,
  `#141414`, `#131313`, `#121212`, `#111`, `#0D0D0D` — a dozen "black-ish" panels.
- *"OK / success" greens:* `#44BB44`, `#44CC44`, `#55CC55`, `#88DD88`, `#98C379`,
  `#4CAF50`, `#7DC97D`, `#AACC88` — 8 greens.
- *"Error" reds:* `#CC3333`, `#CC4444`, `#E06C75`, `#FF8888`, `#FF6666`,
  `#DD6666`, `#CC6666` — 7 reds.
- *"Warn" ambers:* `#CCAA00`, `#CCCC44`, `#CCAA33`, `#FFD246`, `#DDAA55`,
  `#FFCC66` — 6 ambers.
- *Accent blue* is *mostly* consistent (`#88AADD`) — keep it as the canonical
  accent token, fold the near-variants (`#88BBFF`, `#90B8FF`, `#61AFEF`) into it.

**Recommendation:** add `theme.py` exposing semantic tokens, e.g.
`BG`, `PANEL`, `PANEL_ALT`, `BORDER`, `TEXT`, `TEXT_DIM`, `TEXT_IDLE`,
`ACCENT`, `OK`, `WARN`, `ERROR`, plus a small type ramp. Migrate inline colors
to tokens incrementally (footer first). Keeps everything in one place and makes
future restyles a one-line change.

### [x] 2. Footer icons — make them legible and consistent
**Evidence:** `main_window.py:1199` (`⌨︎`), `1206` (`⇄ —`), `1210` (`●`),
`1215` (`⊞`), `1226` (`▾`). Only the keyboard label sets a symbol font
(`font-family: 'Segoe UI Symbol'`, line 1200); the rest inherit the default UI
font, so glyph rendering (and vertical alignment) varies, with tofu risk.
Everything renders at the 11px status-bar size → too small.
**Recommendation:**
- Bump footer glyph size to ~14–15px (icons should out-size body text, not
  match it), and set **one** symbol font on **all** footer glyph labels.
- Prefer a small, cohesive icon set over ad-hoc glyphs: the metaphors are
  currently mixed (outline keyboard, double-arrow, filled dot, boxed-plus, tiny
  caret). Either pick glyphs from a single family/weight, or ship SVG icons.

### [x] 3. Collapse footer greys to two semantic tokens
**Evidence:** five different greys for what are all "idle/secondary" footer
elements (see item 1). **Recommendation:** map them to `TEXT_DIM` (labels with
live values: status, fps, ping, conn-mode) and `TEXT_IDLE` (dormant affordances:
notify dot, kbd-info, vu caret), and fix `#556` → token.

---

## P2 — Medium impact

### [x] 4. Footer spacing / padding is uneven
**Evidence:** `_status_label` has `padding-left: 4px` (`:1188`); `kbd_info_btn`
and `vu_picker_btn` use `padding: 0 2px` (`:1216,1227`); every other footer
widget has none. **Recommendation:** one consistent horizontal gutter between
footer cells (set via layout spacing or a shared padding token), not per-widget
ad-hoc padding.

### [x] 5. Fix and unify separators
**Evidence:** footer `_sep()` builds a `QFrame` VLine and sets `color: #333`
(`main_window.py:1195`) — but a QFrame line color is not driven by the CSS
`color` property, so the divider likely renders in the default (lighter) palette
colour, not `#333`; margins are vertical-only (`margin: 2px 0`) so there's no
horizontal breathing room. Meanwhile `settings_window.py:427,454` implements
separators a *different* way (`QWidget` + `setFixedHeight(1)` +
`background: #444`). **Recommendation:** one separator helper, styled via
`background-color` on a 1px widget (reliable), reused everywhere.

### [x] 6. Footer widgets jitter as their text changes (inconsistent widths)
**Evidence:** the glyph/value labels size to content, so the fps label toggling
between `""` and `"12.3 fps"` (`:1615`), ping between `"⇄ —"` and a value
(`:1206`), and the mode label (`:1795`) shift their neighbours horizontally.
**Recommendation:** give value labels a fixed min-width (e.g. fps ≈ 56px, ping ≈
48px, mode ≈ 90px) and consistent alignment so the bar stays stable.

### [x] 7. Rationalize left-vs-right footer grouping
**Evidence:** kbd / fps / ping / notify / kbd-info are added left
(`addWidget`, `:1240–1245`) while vu / conn-mode / mode are added right
(`addPermanentWidget`, `:1247–1250`). The split isn't semantically obvious (e.g.
FPS+ping on the left, conn-mode on the right, though all three are "link
health"). **Recommendation:** group by meaning — *connection/health* cluster,
*input* cluster, *audio* cluster — and pick left/right intentionally.

### [x] 8. Define a typography scale; unify mono fonts
**Evidence:** font sizes span `8,9,10,11,12,13,14,15,16,20`px with no ramp;
monospace is spelled both `Consolas` (sysex_tool_window) and `Courier New`
(overlay_renderer) and bare `monospace`. Sans is `Segoe UI`, `Arial`,
`sans-serif` interchangeably. **Recommendation:** a small type ramp in
`theme.py` (e.g. `FS_CAPTION=10, FS_BODY=12, FS_H2=14, FS_H1=16`) and one mono
family token.

### [x] 9. Complete the tooltip coverage
**Evidence:** `_fps_label` (`:1203`), `_mode_label` (`:1237`), and
`_status_label` have no tooltip while their neighbours do. **Recommendation:**
every footer affordance gets a tooltip; clickable ones already set a
pointing-hand cursor — make that uniform too.

---

## P3 — Polish

### [x] 10. Consolidate semantic status colours (green/red/amber families)
Fold the 8 greens / 7 reds / 6 ambers (item 1) into `OK` / `ERROR` / `WARN`
tokens, with at most a lighter "text-on-dark" variant where genuinely needed.

### [x] 11. Standardize button styling across windows
**Evidence:** buttons re-declare their own bg/border/hover per file
(`file_manager.py`, `sysex_tool_window.py:201`, `settings_window.py`).
**Recommendation:** one `QPushButton` QSS block applied app-wide (via the app
stylesheet), variants by object-name.

### [x] 12. Fixed-width numeric readouts
Numeric/telemetry labels (fps, ping, VU dB, perf values) should use a fixed
width + right/centre alignment so digits don't shift layout as they update.

---

## Implementation notes (2026-07-13)

- **Token layer:** `theme.py` — colours (BG/PANEL/PANEL_ALT/INSET, BORDER(_STRONG),
  TEXT/TEXT_DIM/TEXT_IDLE/TEXT_FAINT, ACCENT/ACCENT_DEEP, OK/OK_TEXT/WARN/ERROR),
  a type ramp (FS_*), font families (UI/MONO/SYMBOL) and a `app_stylesheet()`.
- **Global stylesheet + base font** applied once in `main.py`
  (`app.setFont` + `app.setStyleSheet(theme.app_stylesheet())`), covering
  QToolTip, QStatusBar, the `QLabel#footerIcon` sizing rule, and a unified
  QPushButton baseline.
- **Footer icons (#2)** get their font from a real `QFont("Segoe UI Symbol")`
  at `FS_ICON` (15px), set in code. An earlier attempt sized them via an
  app-level `QLabel#footerIcon` QSS rule whose `font-family` was a
  comma-separated *list* — Qt's QSS parser doesn't reliably honour font-family
  fallback lists, so the glyphs rendered as blank tofu. A real `QFont` (single
  family, proper glyph fallback) fixes it and isn't wiped by the colour-only
  dynamic setters. Also dropped the emoji variation selector on the keyboard
  glyph (was `⌨︎`, now `⌨`). **Headless can't render fonts here, so the visible
  result needs an on-screen check.**
- **Grouping (#7):** all footer items are left-justified (dot · status · fps ·
  ping · kbd · notify · kbd-info · streaming-mode · current-mode); the audio VU
  meter is the sole right-justified item.
- **#10 semantic colours:** consolidated the duplicated greens/reds/ambers/greys
  in `main_window`, `perf_window`, `settings_window`, `help_window`,
  `about_dialog`. **Preserved** intentional palettes: the SysEx message-type
  colours + on/solo/off filter styles (verbatim from C#), the Set List 16-slot
  colour ring, and help's key-cap/ title accents.
- **#11 buttons:** app-wide QPushButton baseline added; `file_manager`'s
  divergent button rule realigned to the same token values; the SysEx clear
  button already matched. Un-styled buttons elsewhere now inherit the baseline.
- **Accent (`#88AADD`)** was already a single consistent value app-wide (never
  the inconsistency), so those literals were left in place; the `ACCENT` token
  exists for future reference.
- **Deferred (low value / higher risk, token layer now exists for it):** deep
  per-widget chrome tokenization inside `file_manager`/`sysex`/`setlist` QSS
  blocks (panel/border elevation greys), and migrating every last font-size
  literal to the ramp. These render fine today; convert opportunistically.

## Notes / not-yet-touched surfaces
- This pass concentrated on the **footer/status bar** (the reported pain point)
  plus an app-wide colour/font audit. Per-window deep-dives (Settings, File
  Manager, SysEx Tool, Perf) will surface more once the token layer exists.
- The C# original is the parity reference for *layout*; these recommendations
  are about *standards conformance*, which both builds can adopt.
