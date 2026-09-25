# CLAUDE.md

Guidance for Claude Code working in this repository. Read this before starting
any parity/bug-fixing work here.

## What this is

`KronosScreenRemotePy` is a Python/Qt (PySide6) port of `../KronosScreenRemote`
(C#/WPF) — a remote-control client for a Korg Kronos/Nautilus synthesizer. It
streams the instrument's screen over a proprietary TCP protocol, forwards
touch/keyboard/button input back to it, and manages the instrument's Local
Library (Programs/Combis/Set Lists) and sample content over FTP.

**Three sibling repos matter for this work:**
- `../KronosScreenRemote` — the C# app. **Source of truth for behavior**: what
  a feature is supposed to do, what it's called, where it lives in the menu.
- `../KronosScreenRemoteDaemon/docs/api.md` — the wire-protocol reference (the
  daemon that runs on the Kronos itself). **Source of truth for the protocol**:
  outranks both apps when they disagree. Every ctrl-port command, the stream
  handshake, frame format, etc. is documented here byte-for-byte.
- `/home/share/kronosology/docs/` — hardware reverse-engineering docs, ground-
  truth decompiled from the Kronos's own firmware. For anything involving the
  on-disk sample/library file formats (`.KSC`/`.KMP`/`.KSF`, `.PCG`), read
  `kronosology/docs/interfaces/ksc_kmp_ksf_file_format.md` (and its
  `pcg_file_format.md` sibling) **before** trusting the C# port's own
  comments — the C# port was itself written from these docs, and they contain
  hard-won hardware landmines (silent data loss, unit hangs) that aren't
  visible from reading code alone.

## Working method that has actually worked here

This branch spent a long stretch fixing real bugs by comparing Python against
C# line-by-line rather than trusting either codebase's own comments/docs.
Repeat this pattern:

1. **Fork a read-only audit** comparing a specific Python file/feature against
   its real C# counterpart, hunting for *concrete correctness bugs* (wrong
   protocol bytes, dead code, a control that changes UI state but never
   reaches the daemon/disk) — not a feature inventory. Be explicit that it's
   audit-only and must not edit/commit anything, and ask it to confirm "no
   edits made" in its final message. (One fork this session ignored a softer
   "report only" instruction and committed a fix anyway — the fix happened to
   be correct after independent verification, but don't rely on that.)
2. **Independently verify every finding** before fixing it — read the actual
   C# source yourself, don't trust the fork's paraphrase. Twice this session
   a flagged "bug" turned out to be wrong on closer reading: once a
   mischaracterized code path, and once — more importantly — **a whole
   feature (the Palette Editor) that C# has actually retired**
   (`KronosScreenRemote/Views/MainWindow.xaml.cs:49-60`: `EffectiveOverrides`
   always returns empty, the menu item is unconditionally hidden). Building
   the "missing" undo/redo for it would have been real, wasted effort. Before
   implementing a fix for something flagged as missing, check whether the C#
   feature itself is still live and reachable — an `=> alwaysEmpty`/
   `Visibility.Collapsed`-unconditionally pattern in C# means it's retired,
   not a parity gap.
3. **Never trust a self-authored status report.** The branch's early history
   includes a prior agent's "Phase 1-6" work that claimed "100% C# parity
   achieved!" while shipping literal `# TODO: pass` stubs wired to real menu
   items, and invented an entire local audio-workstation feature set with no
   connection to the real app. Those docs were deleted (`adb8d72`). Don't
   write new ones either — this file gets updated in place instead (see
   below), and commit messages carry the "why," not standalone status files.
4. **Verify against real data, not just synthetic fixtures.** For file-format
   work, `KronosScreenRemote/SampleFixtures/` and `PCG EXAMPLES`-style
   directories hold real, hardware-captured files. Round-trip your parser
   against those, not just hand-built test cases.
5. **Full-tree regression after every change**: import every module in every
   package (`Core/Models/Views/Data/Objects/Rendering/Commands/Tools/Utils`)
   headless (`QT_QPA_PLATFORM=offscreen`), and construct `MainWindow`. Catches
   reorg/import breakage cheaply before anything else.

## Environment quirks

- `.venv/bin/python3` in this repo is a broken CIFS-symlink placeholder (this
  tree lives on an SMB/CIFS share) — don't trust `source .venv/bin/activate`
  to do anything. The system `/usr/bin/python3` (3.11) already has PySide6,
  numpy, etc. installed and is what actually gets used; verify with `which
  python3` if in doubt. `pip install` into the venv doesn't work either
  (no working pip); if you need a package not already installed (this
  session wanted `pyftpdlib` for real-FTP-server testing and didn't have it),
  fall back to a duck-typed fake backend against the real calling code
  instead of blocking on package installation.
- `git push` fails in this environment (no credentials) — commit locally,
  the user pushes.
- The harness blocks plain `git rm`/destructive deletes as "irreversible
  local destruction" without explicit user confirmation, even for confirmed-
  dead, git-tracked files. Ask first.

## Current state (as of the last work on this file — check `git log` for
anything more recent than this)

The `ParityUpdate` branch (53 commits ahead of `main` as of this writing) has
had two focused work passes:

**Pass 1 — base functionality + Phase 1-3 correctness.** The stream protocol
was fundamentally broken (v3 handshake, v2 frame-body parsing — fixed,
`5c7c7e1`), several dialogs wired to real menus did nothing when clicked
(`09a2c1e`), device-family gating was entirely missing for Bank Select/VGA
Mirror/numpad routing (`c81eeab`, `f10cc61`), touch calibration wasn't synced
to the unit at all (`0d76df5`), the pixel-fallback mode/boot/Help detectors
were silently dead from a wrong resource path (`0897c80`), and the File
Manager had zero protection against renaming/deleting a Kronos's top-level
storage volume — a real bricking risk (`c4bd70a`). Dead Phase 2/3 duplicate
modules and stale self-authored status docs were removed (`4ff17dd`,
`adb8d72`). Full details, including what was audited and found clean
(Settings window all 9 tabs, Help window, control-surface button tokens,
SysEx Tool, Performance window, Local Library/Librarian): see git log for
this range, or ask a session with access to this project's Claude Code
memory (`/root/.claude/projects/*/memory/project_parityupdate_branch_state.md`
if you have access to it — a fresh environment won't).

**Pass 2 — real Sample Editor + further UI sweep.** The existing "Sample
Editor" was a generic local-WAV multi-track editor with zero connection to
the Kronos, FTP, or the real sample format — replaced with a real one:
- `Data/korg_riff_chunk.py`, `ksf_sample.py`, `kmp_multisample.py`,
  `ksc_collection.py`, `ksf_pcm.py`, `sample_path_guard.py` — the real
  `.KSC`/`.KMP`/`.KSF` binary/text format, ported byte-for-byte from C#'s
  `Core/Sample/*.cs` and cross-checked against
  `kronosology/docs/interfaces/ksc_kmp_ksf_file_format.md`. **Verified
  against all 92 `.KSF` + 16 `.KMP` real, hardware-captured fixture files**
  under `KronosScreenRemote/SampleFixtures/` — every one parses and
  round-trips byte-identical. (`0be8ba8`)
- `Core/sample_ftp.py` — FTP pull/push of a `.KSC` + its whole dependency
  closure, atomic both directions, reusing `Tools/file_manager.py`'s
  `_FtpWorker` and its safety guards. (`88de969`)
- `Views/sample_editor_window.py` — real window: browse the Kronos over FTP,
  pull a collection, view/edit a sample's name/loop points/flags, push back
  (only what was actually edited — never an untouched or header-only file).
  Wired into the real Tools menu + Command Palette. (`5824937`)
- A further UI-behavior sweep fixed a Ctrl+scroll zoom bug (wrong bounds vs.
  every other zoom trigger, `8b3606e`) and three real macro-recorder bugs
  (a latent crash-on-first-keystroke, inability to capture held keys/chords,
  wrong default replay speed, `8fc3f84`).

Everything above has actual verification behind it (real-fixture round-trips,
fake-FTP-backend integration tests exercising the real window code, targeted
unit tests for each fix) — not just "it compiles."

## What's left

Roughly in priority order, none of this started:

1. **Sample Editor is intentionally a first slice.** C#'s `SampleEditorWindow`
   is 2500+ lines; this port covers the hard, safety-critical part (format +
   transfer) and a minimal real UI. Deferred, in the C# reference at
   `Views/SampleWaveformControl.cs`, `SampleWaveformRulerControl.cs`,
   `SampleKeymapControl.cs`, `SamplePanControl.cs`, `SampleVolumeControl.cs`,
   `SampleVuMeterControl.cs`, `CreateMultisampleDialog.xaml.cs`,
   `InsertSilenceDialog.xaml.cs`, `SampleNormalizationReportWindow.xaml.cs`:
   - Waveform drawing + interactive selection/trim/fade editing
   - Multisample zone creation/splitting UI (currently can only edit fields
     on zones that already exist — no way to add a new zone/sample from this
     tool)
   - DSP effects preview
   - Normalization report
   - Create Multisample / Insert Silence dialogs
   Before writing any of this, re-read `ksc_kmp_ksf_file_format.md` for the
   specific operation — it documents a hardware landmine for nearly every one
   of these (e.g. §1.6/§3.2's `SNO1`-uniqueness requirement is exactly what a
   "create new zone" flow needs to get right, or it silently drops audio on a
   real unit).

2. **The Phase 4-6 "fake" audio/DSP code is still present, untouched.**
   `Core/sample_editor.py`, `Core/audio_engine.py`, `Views/waveform_display.py`,
   `Core/audio_effects.py`, `Core/audio_dsp.py`, `Core/audio_recorder.py`,
   `Core/audio_playback.py`, `Core/audio_sample_player.py`,
   `Core/sample_batch_processor.py`, `Core/presets.py`,
   `Views/audio_controls.py`, `Views/track_list.py`,
   `Views/cue_region_panel.py`, `Views/effect_preview_panel.py` — a generic
   local multi-track workstation invented without checking the real C# scope
   (which is ~100 lines: a VU meter). Still reachable from the Tools menu's
   "Testing & Diagnostics" submenu (itself not a real C# menu). The user's
   direction has been to leave this alone rather than delete it — it's a
   real but overbuilt attempt at *something* (possibly repurposable for
   Sample Editor DSP work above), not yet decided. Don't delete without
   asking; don't build on top of it without verifying it against C# first
   either (nothing in it has been checked for correctness).

3. **Confirmed-dead code not yet removed** (low priority, low risk, needs
   the same explicit-confirmation dance as any deletion in this harness):
   `Views/connection_dialogs.py`'s `ConnectionFailedDialog`/`LoginDialog`,
   `Views/property_dialogs.py`'s `UnresolvedDependenciesDialog`,
   `Views/file_dialogs.py`'s `RemoteFilePickerDialog`/
   `RemoteSampleBrowserDialog` — all zero-caller, each duplicating a real,
   working implementation elsewhere (the real FTP login flow is
   `_FtpLoginDialog` in `main_window.py`; the real file picker/dependency
   dialog are `librarian_shell_window.py`'s own `_RemoteFilePickerDialog`/
   `_UnresolvedDependenciesDialog`).

4. **Never attempted, whole project**: pixel-level visual comparison against
   the C# app. Everything verified so far is code-level/behavioral/protocol-
   level. Nobody has run both apps side by side and compared what's actually
   on screen.

5. **Smaller known gaps, not yet fixed** (low priority, each independently
   verified real but narrow-impact):
   - `Utils/key_map.py`: Right Shift/Ctrl/Alt send the same Linux keycode as
     Left (Qt doesn't distinguish them without a native-scancode check,
     unlike WPF's distinct `Key.RightShift` etc.) — documented as a known
     limitation in a comment, not implemented.
   - `Views/connection_dialogs.py`'s dead `LoginDialog` doesn't enforce the
     real 1-64-byte non-empty-username rule from `docs/api.md` §3.2 — only
     matters if a future session wires it up instead of deleting it.
   - No dedicated "reveal password" widget (C#'s `RevealablePasswordBox`) —
     password fields are presumably plain masked `QLineEdit`s. Cosmetic.

6. **General UI sweep was real but not exhaustive.** Covered: menu structure
   and placement, Settings (all tabs), Help, control-surface tokens, SysEx
   Tool, Performance window, Palette Editor (confirmed retired, correctly
   inert), Calibration mode, zoom/window-sizing, Image Adjustments, macro
   recorder, dead-dialog reconfirmation. Not specifically re-checked this
   pass: File Manager drag-drop interaction details, the zoom loupe's
   follow-cursor behavior in detail, Set List color assignment, any dialog
   not named above. If picking this up, the fork-audit pattern in "Working
   method" above is the efficient way to keep sweeping.

## Testing without real hardware

No Kronos/Nautilus is reachable from this environment. Patterns that worked:

- **Protocol-level**: build a minimal fake TCP server replaying exact bytes
  from `docs/api.md`'s worked examples (stream handshake, frame envelopes) or
  hand-built wire-format bytes, and drive the real `StreamReceiver`/
  `CtrlClient` classes against it.
- **FTP-level**: no working FTP server package was available in this
  environment (`pyftpdlib` uninstallable — see venv note above). Built a
  duck-typed fake object matching `Tools/file_manager.py`'s `_FtpWorker`
  method signatures, backed by real local file I/O, and monkey-patched
  `Tools.file_manager._FtpWorker` so the *real* window/closure code runs
  unmodified against it. This caught real bugs (e.g. a caller passing the
  wrong argument order) that testing the underlying functions in isolation
  wouldn't have.
- **File-format level**: always test against real fixture files
  (`KronosScreenRemote/SampleFixtures/`, `PCG EXAMPLES`) in addition to
  synthetic ones — round-trip byte-identity against real hardware-captured
  data is the strongest evidence a port is correct.
