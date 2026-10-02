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

**Pass 3 — re-audit found C# had moved on substantially (2026-09-19..23), plus
Phase 1 of the resulting catch-up plan.** A full re-audit against the C# repo
found it had shipped a large batch of new work Python had no knowledge of:
zone-management + waveform-rendering overhaul in the Sample Editor, FTP
folder-management additions, and — the largest item — real, live (not stub)
**Nautilus device-family UI support** (swapped right-panel skin, connect-time
device detection via the daemon's `MODEL` command, button/wheel remapping),
plus a generalized MIDI/SysEx transport to go with it. See "What's left"
below for the phased catch-up plan this produced. Phase 1 (low-risk items)
landed this pass (`535f359`): a real reveal-password widget
(`Views/revealable_password_edit.py`, replacing C#'s dual-PasswordBox/TextBox
hack with a plain `QLineEdit.EchoMode` toggle), double-click-slider-reset and
a "Default window size" dropdown in Settings (which needed window geometry
persistence added — Python had none at all before this), Sample
Editor/Librarian keybind entries, `ConnectionFailedDialog` wired into the
*initial* connect-failure path only (deliberately **not** the background
auto-reconnect loop — that's a Python-only feature, C#'s own docs say "does
not auto-reconnect", so a modal on every unattended retry would be a
regression), and removal of the confirmed-dead `LoginDialog`,
`UnresolvedDependenciesDialog`, and `file_dialogs.py`'s two picker dialogs
(all zero-caller, each duplicating a real implementation elsewhere).

Phase 2 (Sample Editor zone management + waveform) landed the same pass,
across five commits:
- Zone management (`db5544d`): the tree was fully read-only before this —
  now has Add Zone (128-zone cap, mirrored onto an in-sync stereo sibling),
  Delete Zone (zero-zone guard on both halves of a stereo pair, single-slot
  "Undo Delete Zone" rather than a full Ctrl+Z/redo stack — no undo stack
  exists anywhere else in this codebase, and C#'s own version needs live-
  object-identity tree-patching machinery this simpler window doesn't
  have), Remove Sample (deletes the real audio file + matching bare
  repository `.ksf` entries — the actual bug C#'s own version fixed;
  previously only the reference was cleared), and Rename (sample + whole
  multisample, 22-char UI cap, stereo-partner mirroring, multisample
  rename moves the `.kmp` file + zone folder immediately). New
  `Core/sample_link_resolver.py` (read-side stub-to-real-audio resolution)
  and `Data/kmp_multisample.py`'s `find_stereo_sibling()`. "Link" (assign
  an existing sample to a zone, sharing audio via SNO1) is deferred — it
  needs a "pick an existing sample" UI that doesn't exist yet.
- `Views/sample_keymap_control.py` (`10e8280`): visual piano-keyboard
  keymap, two-pass zone-bar paint implemented correctly from the start
  (see the module's own docstring for the black-key-boundary bug this
  avoids — C# had to patch it after the fact). Click-to-select only;
  boundary-drag resize and zone-bar reorder-drag are not ported (Add Zone
  is a toolbar action, not a drag gesture, so nothing depends on them).
- FTP folder management in the Load/Push browser (`be8ead5`): New Folder/
  Rename/Delete/Refresh on `_RemoteKscBrowserDialog`, reusing
  `Tools/file_manager.py`'s `_FtpWorker` methods and top-level-volume
  safety guards rather than rebuilding. "Properties" wasn't ported — the
  Python class named `FtpPropertiesDialog` is an FTP-connection-settings
  dialog, a real naming collision with C#'s item-properties viewer of the
  same name, not something reusable for this.
- `Core/waveform_pyramid.py` + `Views/sample_waveform_control.py`
  (`9acd550`): real waveform display, resolving the reuse-vs-rewrite
  question on `Views/waveform_display.py` (user confirmed: write fresh —
  that file remains untouched, still its own separate dead-code question).
  Min/max mip-map (numpy `reduceat`, verified fold-vs-direct-computation
  correct at every level) backs draggable Sample-Start/Loop-Start/Loop-End
  markers with live-preview-until-release drag semantics and abandoned-
  drag revert (Qt's `QEvent.UngrabMouse`, the equivalent of C#'s
  `OnLostMouseCapture` hook). Fade/DSP preview, the ruler sub-control, and
  stereo-pair pan/zoom sync are not ported.
- Targeted bug checks: the C# reverse-playback-freeze bug doesn't apply
  yet (no audio playback engine exists here — flagged as a must-not-
  reintroduce constraint for whenever one is added). The C# calibration-
  mode MIDI-disconnect crash was root-caused to an NAudio/winmm GC-
  finalizer race specific to that native handle; checked Python's actual
  transport close path (`Core/midi_bridge.py:stop()` — a plain
  `socket.close()` in a try/except, no finalizer, no native handle) and
  confirmed the bug class doesn't apply to this architecture at all.

Phase 3 (Nautilus device-family UI + MIDI/SysEx transport generalization)
is partially done, across six more commits:
- `Core/device_family.py` + connect-time `MODEL`-command detection
  (`8114109`): real `DeviceFamily` enum, `_fetch_device_family()` (a real
  connect-time state transition, not just the one-off ad-hoc dialog query
  that already existed), and `_apply_device_family_ui()` — the single hook
  every device-family-gated branch below runs through, called after both
  the cheap `stream_fmt` guess and the authoritative `MODEL` response.
- Left-panel collapse, Hide Value Input forced-on, footer FF/Rewind fade
  (`04c5865`): three of C#'s `ApplyDeviceFamilyUi()` branches that only
  needed existing widgets. Verified the "Full" vs "Focused" layout-preset
  interaction specifically — Nautilus force-hides the left panel in both,
  but Kronos's own Focused-mode rail-expand state (a separate mechanism
  C# has no equivalent of) survives untouched.
- `NautilusToken()` wire-token remap (`2b03d82`, extended `a2cbb4d`): Seq
  Locate/Rewind/Forward/Pause + Tap Tempo send real Nautilus scan codes
  (MS1/MP7/MP8/MS3/NUM9), wired into the footer buttons, keybind dispatch,
  and command palette. **Seq Record/Start/Save are now remapped too**
  (`a2cbb4d`): ported C#'s `SeqTransportViewModel` swap logic — Record→MS2,
  Start/Stop→MP6 (swapped with Pause's MS3 per C#'s own 2026-09-22 hardware
  correction), Save→EXIT (Nautilus front-panel "F" is wired to EXIT and
  that QA slot is programmed as Write/Save there, while Kronos fires the
  same REC/WRITE press for both Record and Save) — same three call sites
  as the rest of this remap (footer buttons, keybind dispatch, command
  palette).
- Window re-fit on connect (`a659eef`): only when the window is at a
  known preset scale (Small/Medium/Large/Huge) — a custom or "Last
  Used"-restored size is left alone rather than guessed at, since Python
  has no continuous scale-tracking to safely reapply the way C#'s
  `_currentScale` does.
- Escape-key routing fix (`808b0b3`), **completed `16102c5`**: no longer
  raw-forwarded to the daemon while keyboard-captured on Kronos (matches
  C#'s device-family exclusion), and captured-mode Escape now runs the
  same precedence chain as the uncaptured path instead of doing nothing.
  `MainWindow._handle_escape()` (new, `Views/main_window.py`) ports C#'s
  `MainWindow.Input.cs:389-408`: fullscreen-exit, then drag-cancel (new
  `FrameWidget.cancel_drag()` — mirrors C#'s cancel-drag block, including
  sending a bare TOUCH_UP for a pending-only drag with no prior
  TOUCH_DOWN, matching C# exactly), then zoom-off, then BUTTON EXIT
  (repeat-guarded; the earlier three branches are naturally idempotent on
  key-repeat since the state they check clears after firing once). Called
  from both `keyPressEvent`'s uncaptured path and `eventFilter`'s
  captured-on-Kronos path. C#'s palette-editor precedence steps are
  deliberately not ported — that feature is retired in C# itself.
- **MIDI/SysEx codec parameterization** (`48479a7`): `Data/nautilus_sysex.py`
  (new) ports `Networking/NautilusSysEx.cs` — the 6-byte Nautilus Exclusive
  Header (`F0 42 3g 00 01 5D`, vs. Kronos's 4-byte `F0 42 3g 68`) for the
  same hardware-verified primitive slice as C#'s `IKorgSysExCodec`: object
  dump request/write/parse (0x72/0x73), store bank (0x76), dump bank
  (0x77), bank digest request/parse (0x37/0x38), reply parse (0x24).
  `Data/librarian_sysex.py` gained two Kronos-side counterparts that didn't
  exist yet (`dump_bank_request` bytes form, `parse_reply`) so both modules
  expose an identical function surface — a "codec" is just one of these two
  modules, selected by reference, no interface/class needed.
  `Tools/sysex_dump_collector.py`'s `SysExDumpCollector` now takes a
  `codec` (default Kronos) and uses `codec.parse_object_dump`/
  `codec.parse_reply` instead of hardcoded header-byte matching in both
  `collect()` and `collect_per_object_names()`, plus codec-aware
  `object_dump_request()`/`dump_bank_request()` wrapper methods (mirroring
  C#'s `SysExDumpCollector.ObjectDumpRequest`/`DumpBankRequest`) so
  `SysExService` never touches the codec's byte builders directly.
  `SysExService.set_device_family(is_nautilus)` (new, port of C#'s
  `SetDeviceFamily`) selects the codec, is a no-op if unchanged, and
  rebuilds `_dump` on the current bridge when it does change; reset to
  Kronos on every `start()`/`stop()` (a reconnect may be a different
  instrument). Wired into `_apply_device_family_ui()` (the single hook
  every other device-family branch runs through) — plus one extra call
  right after `_sysex_service.start()`, since `start()`'s own reset would
  otherwise silently undo the guess `_apply_device_family_ui()` set just
  before it (that first call always runs pre-`start()` in
  `_apply_new_receiver`; only the *second* call, once the async `MODEL`
  reply resolves, reliably lands after). Live-stream mode/performance/name
  decode (`_on_raw_message`) deliberately stays Kronos-only regardless — a
  real, documented gap on Nautilus, matching C#'s own scoping decision
  (unmatched Nautilus messages there simply fall through unparsed rather
  than misparsing). Same for func 0x7C Change Program Bank Type — not part
  of the codec-verified slice on either side. Verified via self-tests in
  both codec modules (`python -m Data.librarian_sysex` /
  `python -m Data.nautilus_sysex`) plus a fake-bridge round-trip exercising
  the real `SysExDumpCollector.collect()`/`collect_per_object_names()`
  against both codecs (no real hardware reachable from this environment —
  see "Testing without real hardware" below).
- **Right-panel swap** (`6385ed5`, resource-path bug fix `4e0c4c6`): the last big Phase 3 item,
  unblocked once the user pointed at the actual asset directory
  (`../KronosScreenRemote/Resources/Images/`) and `Views/NautilusRightPanel.xaml`
  for layout — no more "no visual reference" excuse. `Rendering/control_surface.py`
  (previously a single Kronos-only 800×600 custom-painted `QWidget`) now
  holds BOTH skins in one widget/class, switched at runtime by
  `set_device_family()`, rather than porting C#'s two-separate-UserControls-
  swapped-by-visibility approach — there's no tree of real child widgets
  here to swap, just one custom paintEvent. Nautilus's button layout
  (`_NAUT_BUTTON_DEFS`) is `NautilusRightPanel.xaml`'s 12 `Margin="l,t,r,b"`
  values hand-converted to `(x,y,w,h)` in its native 1024×771 canvas; its
  scale strategy is genuinely different from Kronos's uniform-letterbox
  Viewbox — width-only (`scale = width/1024`, crops/gaps top-bottom, never
  letterboxes left-right) — ported into a new `_scale_and_offset()`/
  `_skin_dims()`/`_skin_wheel_rect()` trio the paint/hit-test code both
  read from. New `_Btn.hold_lit` (Nautilus A-F: lit only while physically
  held, set/cleared directly in mousePress/ReleaseEvent — a different
  mechanism from Kronos's toggle/radio_group/external-`set_active` paths,
  which stay unchanged). Wire tokens in `Views/main_window.py`'s
  `_CTRL_BTN_CMD` (`NAUT_*` entries): A-F/Exit/Enter/Inc/Dec are direct
  mode=0 sends; **MODE/PAGE deliberately reuse Combi's/Program's own
  `(cmd, mode-index)` tuples byte-for-byte** (matching C#'s
  `WireCommand(_nautilusRightPanel.BTN_Mode, "Mode Combi")`) so they get
  the identical boot-gate/pending-mode/sysex-refresh bookkeeping Kronos's
  own mode buttons get — verified this actually sends `BUTTON COMBI` and
  sets `_pending_mode=2` on press+release. Their LIT state is NOT wired
  through that bookkeeping at all, though (C#'s `ApplyNautilusLamps` is
  independent of `WireButtons`/`SetModeButton`) — new
  `_apply_nautilus_lamps()` mirrors the daemon's `STATE` poll
  `MODE_LIT`/`PAGE_LIT` fields (docs/api.md, Nautilus-only, 3.1.0+/3.1.1+)
  straight onto `NAUT_MODE`/`NAUT_PAGE`'s `active` flag, called on every
  `STATE` poll (even mid-boot, matching C#'s call ordering) and cleared to
  `None`/unlit on disconnect. Both button tables' buttons stay registered
  in `self._btns`/`_btn_map` at ALL times regardless of which skin is
  active/drawn — verified `set_mode()` still harmlessly lights the
  invisible Kronos "Combi" button while the Nautilus skin is showing, so
  reusing its mode index for `NAUT_MODE` doesn't need any special-casing.
  **Found and fixed a real, previously-undiscovered bug while building
  this**: `_res()`'s resource-path resolution (`Path(__file__).parent /
  "Resources" / "Images"`) was off by one directory level in FOUR places
  (`Rendering/control_surface.py`, `Rendering/overlay_renderer.py`,
  `Views/main_window.py`'s left-panel slider widget, `Models/storage.py`'s
  embedded `cal_data.json` fallback) — every `.exists()` guard silently
  swallowed the miss, so the Kronos control-surface panel has been
  rendering with ZERO images (no background, no wheel, no buttons) this
  whole branch. Confirmed via headless probe before the fix, confirmed
  fixed after. This is also the first actual pixel-level visual
  verification this branch has had (see "Never attempted" item below,
  now partially attempted) — both skins render correctly against the real
  hardware photo/asset backgrounds, buttons aligned to their physical
  positions; screenshots taken during this session confirm it visually,
  not just structurally.

Mode-select menu swap: done in Pass 4 (see below). Phase 3 is complete.

**Pass 4 — 2026-10-02 re-audit against the C# tree (including its uncommitted working
copy) and the first parity work from it.** Bucketing rule used: *retired in C#* (not a gap),
*Python-only*, *missing*, *out of scope* (Nautilus PCG conversion, every headless `--flag`
in `App.xaml.cs`, USB-direct MIDI incl. `MidiTransport`/`UsbMidiDeviceName`). Verified
retired in C#, do NOT port: Warp Mode, Check for Updates, Report Issue, Palette Editor, the
Tools-menu Input Tester entry **and** Settings' "Test Inputs" button (both hidden; Python's
Input Tester is therefore reachable nowhere, user decision). Done this pass:
- Labels: "MIDI Monitor…", "Disable/Enable Remote Typing"; Nautilus Mode Select item set
  (Mode/Page/A–F → `_NAUT_MODE_ITEMS`, status-bar mode menu disabled on Nautilus like C#);
  Escape-EXIT button flash; Button Injector moved to Settings > Debug (shown only while debug
  logging is ticked), "Testing & Diagnostics" submenu no longer built (its audio/EQ/device-info
  dialogs are now unreachable but untouched — see item 2 below).
- Calibration now matches C# 990d1d0: stored **only on the unit** (no `cal_data.json`, no baked-in
  factory mesh; `load_cal`/`save_cal` deleted), `CAL_SET` reply is read and mapped to C#'s four
  failure messages, saves are serialized and async (blocking form for the exit prompt), grid change
  confirms then saves, R/X/S/Ctrl+Z/Ctrl+Y/Calibrate-key semantics, bias-dot edits save immediately,
  overlay + click mapping use the `(w-1)/(h-1)` convention and the frame's real geometry (the old
  overlay hard-coded 800×600, wrong on Nautilus), node hit-test in screen space.
- Settings model: ~20 persisted fields added (`_SCALAR_FIELDS` in `Models/storage.py`), recent
  hosts cap 5 + per-host credentials, VU device persisted, Settings tabs reordered to C#'s, Input
  Mapping split out of Debug, Settings > Librarian gained Full-sync-on-launch / Force destructive
  write.
- **Librarian sync rework (found a real bug):** the plain "Sync Library" click used to run
  push-only `commit_changes` — it pulled only when "Force Full Sync" was ticked. Now one Sync
  button with a 2-Way / Pull Only / Push Only dropdown (persisted), the separate Commit button is
  gone; SysEx-off probe + banner + Re-check; `PullResult.aborted` (4 silent banks → give up);
  conflict banner + Resolve Conflicts ("keep mine" re-baselines then clears); warning banner;
  Pull Only discards edits **and** delete-flags behind a confirm, before pulling; Push Only offers
  the overwrite retry after a refusal/conflict; `force_destructive_write` skips only the conflict
  pre-scan (`build_changeset`) and arms per-bank digests so the per-write staleness check doesn't
  abort against an allowed-stale baseline; launch pull; "Local Library" → "Keyboard Library" in
  user-visible text; Expand/Collapse in the three tree context menus. Tests: self-tests in
  `Data/changeset_sync.py` and `Data/library_pull_pipeline.py` were extended; the window itself
  was driven end-to-end against a duck-typed fake Kronos (`KRONOS_DATA_DIR` isolates it from the
  real library — always set it when constructing `LibrarianShellWindow` headless).

Live-verified 2026-10-02: Kronos 192.168.100.15 (connect, family detect, unit calibration load, CAL_SET round-trip
with exact restore, 35-bank digest sweep, SysEx probe); Nautilus 192.168.100.26 (MODEL/STATE/VERSION answer, daemon
3.1.2; its stream login differs from the Kronos' — Nautilus-side stream/Librarian checks still pending its credentials).

**Object Dependencies panel (done 2026-10-02).** `Data/librarian_dependencies.py` (pure row logic: Local /
PCG / Merge collectors, red staged-gap rows, lazy "More Info" children), `Tools/sample_reference_walker.py`
(Program sample banks: HD-1 zones + EXi MOD-7/STR-1; ported with C#'s own self-test cases),
`Tools/exs_option_index.py` + `Resources/ExsCatalog.json` (EXs name catalog), and
`dependency_scanner.walk_display_references` (adds the Program Drum Track). UI: coloured rows, legend,
More Info dialog, "Search a PCG for this object…" (one file, every listed gap, one undo step), red section
persists across selection changes / tree rebuilds. **C# bug found, user decided "port the intended
behavior":** C#'s sample-bank name resolution never fires — `SampleDependencyRow.Key` is `"Exs|exs12"` but
`ResolveSampleDescription` expects bare `exs12`/hex (the one-line C# fix: strip the `Bucket|` prefix); Python
resolves correctly. Known gaps vs C#, all tracked: (a) **Drum Kit / Wave Sequence object types don't exist in
Python** (registry, pull, panes, PCG view, `KronosBanks` linear maps) so a Program's oscillator-zone refs to
them and their own sample refs aren't shown — this is the biggest remaining Librarian-model gap;
(b) the Program Drum Track ref is display-only: the *resolution* paths (merge pull, repoint, the push
referential REFUSE check) still use `walk_object_references`, which has no drum-track encoder — adding it
there without a `RefKind`-style dispatch would write at the wrong offsets, so port it as an enum first.

**Tests live in `tests/`** (run from the repo root, `python tests/<name>.py`): `regress.py` (import every
module + construct MainWindow), `test_cal.py`, `test_librarian.py` / `test_launch.py` (the whole sync UI
against a duck-typed fake Kronos; `KRONOS_DATA_DIR` isolates them), `test_deps_ui.py` (needs the real PCGs
under `Z:\PCG EXAMPLES`), `test_sysex_service.py` (SysExService vs a fake bridge), `real_pcg_walk.py`, `live_midi*.py` (read-only live
checks of the MIDI service / window), and `live_unit.py <host> [--user U --pass P] [--caltest]` —
live check against a real unit (the `--caltest` round-trip always restores the original calibration in a
`finally`). Module self-tests: `python -m Data.librarian_dependencies | Tools.sample_reference_walker |
Tools.dependency_scanner | Data.changeset_sync | Data.library_pull_pipeline`. Nautilus 192.168.100.26 login
is `nautilus`/`nautilus`; Kronos 192.168.100.15 uses the repo's `settings.json` login.

**Remaining from the Pass 4 audit, in priority order** (C# `file` references are the truth):
1. *Librarian*: `WindowPlacements` for non-main windows; File Manager "Set Default Start Folder" (field
   exists); Drum Kit / Wave Sequence object types (see above); drum-track in the resolution paths.
2. *(done 2026-10-02) MIDI/SysEx tab + behavior.* `Core/sysex_service.py` gained the perf-metadata loop
   (`refresh_now()` is now a 300 ms-debounced wake; proactive polling on a fixed interval; poll-on-changes
   for undecodable Program Changes; a bank-digest 0x38 push always refreshes), the per-change name pull
   (150 ms debounce, skips while a bulk dump holds the gate or the name is cached), and
   `value_slider_changed` (CC# from settings; CC 0/32 can never be shadowed). MainWindow: `_apply_midi_settings`
   (also on Settings OK / Import), live Monitor-MIDI start/stop while connected, hardware slider mirror
   (`KronosValueSliderPanel.follow_value`, ignored while dragging, never echoes VSLIDER back), faded
   MIDI Monitor menu + clickable footer MIDI cluster, OUT CH persisted. Settings > MIDI/SysEx tab (C#'s
   transport combo / USB device status are omitted: USB-direct is out of scope). Verified live on the Kronos
   (probe, perf loop, proactive @2 s, name pull, Monitor toggle through the real MainWindow). **Not verified
   live:** the hardware VALUE-slider CC — 25–30 s of listening saw no CC/PC from the Kronos at all (slider not
   moved, or CC transmit is off in GLOBAL › MIDI); re-run `tests/live_midi.py <host> --listen 30` /
   `tests/live_midi_raw.py <host> 25` while moving it. **Nautilus:** SysEx reads "unavailable" there because the
   probe is the Kronos-format mode request — identical to C# (its live-stream decode is Kronos-only by
   design); the bridge itself connects. Still unported from C#: the storage-change counters
   (`StorageChangeCountFor` / `ExpectDigestReply`) that the Librarian's post-write step 3b reads.
3. *Sample Editor — model + window DONE 2026-10-02; self-test port and visual pass remain.* Plan: port C#'s `SampleEditorViewModel` (4,592 lines)
   as a UI-independent `Core/sample_editor_model.py`, port its self-tests (`Core/Sample/SamplePhase*SelfTests.cs`
   — each pins a real hardware-observed bug), then rebuild `Views/sample_editor_window.py` on the model (the
   current window is a first slice with model logic embedded in it). **Dependencies decision (user, 2026-10-02):**
   `soxr` for resampling (added, `requirements.txt` created), in-house WSOLA for tempo/pitch (no SoundTouch wheel
   exists), Qt `QAudioDecoder` for MP3/MP4 (verified on real mp3/m4a/8-ch mp4), `sounddevice` WASAPI with
   `auto_convert=True` for playback (any declared rate, like C#'s WasapiOut). **Done + tested (pure layer):**
   `Core/sample_dsp.py` (all effects + tempo/pitch), `Core/sample_edit_undo.py` (byte-capped snapshot stack +
   zone-list stack), `Utils/midi_note_name.py`, `Core/audio_import.py`, `Core/sample_export.py`,
   `Core/sample_import_builder.py` (+ the disk-based `find_stereo_sibling_on_disk`; `Data/kmp_multisample.
   find_stereo_sibling` is the *loaded-cache* variant), `Core/sample_support.py` (clipboard, workspace root,
   normalization report), `Core/sample_playback.py` (one-shot / looping / reverse providers, pan, boost, meters;
   smoke-tested on the real device). Tests: `python -m Core.sample_dsp | Core.sample_edit_undo | Core.audio_import |
   Core.sample_playback | Utils.midi_note_name`, `tests/test_sample_core.py`. Note: C#'s pan code is `cos/sin`
   (centre = -3 dB per channel) although its comment says "unity at centre" — the code is ported. **Window done:** `Views/sample_editor_window.py` (+ `sample_waveform_control.py`, `sample_keymap_control.py`,
   `sample_small_controls.py`, `sample_editor_dialogs.py`, `sample_editor_widgets.py`, `sample_remote_source.py`), Settings > Sample Editor
   tab and a Help section; tests `tests/test_sample_ui_controls.py`, `tests/test_sample_window.py`. Live-verified on the Kronos:
   pull of a Kronos-authored collection, and a byte-identical push->pull round trip in `/SSD2/CLAUDETEST`. **Self-test port DONE** (every C# `Core/Sample/*SelfTests.cs` except `ExsOptionFile`, which belongs to the Librarian; tests `tests/test_sample_phase*.py`, `test_sample_stereo_transcode_hardening.py`, `test_sample_remote.py`, `test_sample_format.py`; negative-controlled by disabling stereo mirroring). **Still to do:** a side-by-side visual pass against the C# app, hardware audition check. **Model:**
   `Core/sample_editor_model/` — `core.py` (state/tree/select/stereo/dirty/rename), `edit.py` (effects, clipboard, move tool,
   tempo/pitch, multi-domain undo/redo), `markers.py` (set_marker/loop/flags/Use-Zero), `zones.py` (zone add/delete/
   move/reorder, import, link/assign, stereo pairs, multisample create/delete), `io.py` (save, export, Save As, remote
   pull/push via a duck-typed source), `play.py` (transport/piano-key), `model.py` assembles `SampleEditorModel`. Tests:
   `tests/test_sample_model_edit.py`, `tests/test_sample_model_zones.py` (real SMPTEST fixture copies). Gotchas: zone-adding
   methods rebuild the tree (selection is dropped — re-find the node with `_node_for_path`); `KmpZone` equality is by
   VALUE so always use `zone_index`/`zone_in`; the model's `dispatch` hook must be set by the window to marshal the
   audio-thread `on_stopped` onto the GUI thread.
4. Status-bar wrap + menu-driven minimum width + live shortcut labels in menus (990d1d0),
   re-fit window on family switch, keybind defaults diff (C# Zoom In/Out default to none; Python
   defaults them to =/-), command-palette entry diff incl. `NautilusModeF`, Help window text diff.
5. Port C#'s self-test suites as the parity evidence (DataSafety, SyncPipeline, MergeAutoFill,
   CrossPanePlacement, LibrarianUndo, SamplePhase*).

Note: `Core/sample_editor.py` (legacy fake workstation) shadows nothing now because the model package is `Core/sample_editor_model/`.

## What's left

**Nautilus PCG format-conversion (`Core/Pcg/Nautilus/`,
`Core/Pcg/NautilusConversion/` in C#) is explicitly OUT OF SCOPE** — confirmed
CLI-only in C# (`--nautilus-convert-*` flags), never reachable from its own
GUI, so not a screen-remote client feature.

Phases 1–3 (above) are done; the live to-do list is the Pass 4 "Remaining" list right above.
C# itself calls the whole Nautilus area preliminary (its own
`Documentation/nautilus_button_mapping.md` is marked unverified against
real hardware — no rooted Nautilus unit even on the C# team's side yet),
so treat this last item the same way: port the C# structure/behavior
faithfully, don't invent new hardware
assumptions where the source itself is unverified. Defer C#'s USB-direct
MIDI transport path (`MidiDeviceIdentity.cs`, no daemon link at all) unless
asked — Python has no USB-direct transport today, only the TCP bridge.

**Older, still-relevant items:**

1. **Sample Editor is still not a full port of C#'s 2500+-line
   `SampleEditorWindow`, but the hard/safety-critical parts plus zone
   management and waveform editing are now real** (Phase 2, above). What's
   left, in the C# reference at `SamplePanControl.cs`,
   `SampleVolumeControl.cs`, `SampleVuMeterControl.cs`,
   `CreateMultisampleDialog.xaml.cs`, `InsertSilenceDialog.xaml.cs`,
   `SampleNormalizationReportWindow.xaml.cs`, and the fade/DSP-preview part
   of `SampleWaveformControl.cs`:
   - DSP effects preview + fade editing (must not start without resolving
     item 2 below first — this is exactly where someone would be tempted to
     reach for that code)
   - Normalization report
   - Create Multisample / Insert Silence dialogs
   - "Link" a zone to an existing sample's audio (sharing via SNO1) — the
     read-side resolver (`Core/sample_link_resolver.py`) and every data-layer
     primitive it needs already exist; this needs a "pick an existing
     sample" UI that doesn't exist yet
   - Pan/volume controls, VU meter, stereo-pair pan/zoom-window sync in the
     waveform view
   Before writing any of this, re-read `ksc_kmp_ksf_file_format.md` for the
   specific operation — it documents a hardware landmine for nearly every one
   of these.

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

3. ~~Confirmed-dead code~~ — resolved in Pass 3 (`535f359`): the dead
   duplicates were deleted, and `ConnectionFailedDialog` was revived and
   wired into the initial connect-failure path (see Pass 3 above) rather
   than deleted, since C# itself revived/redesigned its own version in the
   same time window this session was auditing against.

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
   - ~~No dedicated "reveal password" widget~~ — resolved in Pass 3
     (`Views/revealable_password_edit.py`).

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
