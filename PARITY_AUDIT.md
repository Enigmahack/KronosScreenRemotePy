# C# -> Python parity audit

Dated 2026-10-16. C# reference: `Z:\KronosScreenRemote`. Findings come from code reading by read-only audit
passes; rows marked "name match" were matched by function names and docstrings, not line by line. Re-verify a
row before building on it. `CLAUDE.md` is the running history; this file is the gap list.

Status: **Ported** / **Partial** / **Missing** / **Absent** (deliberately not ported) / **Python-only**.

## Librarian and MIDI/SysEx (ranked by gap size)

| # | Area | Status | Note |
|---|---|---|---|
| 1 | C# self-test suites as evidence | Partial | No Python counterpart: `DumpGateSelfTests`, `MidiTransportCoordinator*`, `ObjectBodySelfTests`, `LocalEditOpsSelfTests`, `LocalCutCopyPasteSelfTests`, `CrossPanePlacementSelfTests`, `PlacementStalenessGateSelfTests`, `DataSafetySelfTests` (436 lines), `DependencyResolutionSelfTests`, `LibrarianDependencyCacheSelfTests`, `PcgPaneLoadSelfTests`, `PcgSearchFilterSelfTests`, `LibrarianSettingsApplySelfTests`, `ScreenSessionSelfTests`, `StreamReceiverV3SelfTests`. Partly covered: `SyncPipeline`, `SyncCancellation`, `LibrarianUndo`, `LocalLibrary`, `MergeCache`. |
| 2 | USB-direct MIDI transport, `MidiTransportCoordinator` | Absent | Python has the daemon's 9875 TCP bridge only; Settings has no transport combo. |
| 3 | `LocalEditOps` | Partial | Behaviour exists but is embedded in `Views/librarian_shell_window.py` (4900 lines) and `Data/librarian_model.py`; nothing is unit-testable headless. Candidate for extraction into `Data/local_edit_ops.py`. |
| 4 | Storage-change counters (`StorageChangeCountFor`, `ExpectDigestReply`) | Missing | The post-write "unexpected storage push" check (Librarian step 3b) does not run. Documented in `CLAUDE.md`. |
| 5 | `SysExService` extras | Partial | No match for `Reset`, `NotifyUserActivity`, `DumpBankBulkAsync`, `RequestProgramBankTypesAsync`. |
| 6 | `PcgChecksumWarning` | Missing | `Data/pcg_file.py` has no checksum validation; C# warns. Matters for the converter's PCG writer, which must produce valid checksums. |
| 7 | Librarian menubar | Partial | Python uses buttons plus shortcuts (Ctrl+Z/X/C/V, F2, Del); no File/Edit menubar. Context menus ported. C# "Properties..." is "More Info..." in Python. |
| 8 | `LibraryPullPipeline` per-object/bank/mode pulls | Partial | `PullAsync` ported; `PullObject/Bank/ModeAsync` not found as named functions. |
| 9 | Typed `ObjectBody` classes | Ported (functions) | `Objects/object_body.py` covers name, category, set-list slot, drum-kit and wave-seq refs. |
| 10 | SyncPipeline | Ported, differs | Python is stricter: pushes need live pre-image backups; `force_destructive_write` only skips the conflict pre-scan. |
| 11 | MIDI Monitor window | Ported | `DecodeMidi` is device-family aware in C#, Kronos-only in Python. Auto-scroll checkbox unverified. |
| 12 | CLI dev tools (`--dump-pcg-*`, `--nautilus-*`, `--karma-ge-*`, `--sample-*`) | Absent | Python equivalents are ad hoc (`tests/live_*.py`, module `_selftest`s). No single self-test runner. |
| 13 | Nautilus PCG conversion | **In progress** | See `Conversion/` and the converter section below. |

Also ported (name match): DumpGate, OpLog, BlankTemplateStore, LibrarianUndo, MergeCache, LocalLibrary store/index,
DependencyScanner, SampleReferenceWalker, SessionDependencyClipboard, ObjectTypeRegistry, SysExDumpCollector,
Nautilus and Kronos SysEx codecs.

## Sample Editor and File Manager

The Sample Editor is close to full parity. **`CLAUDE.md` "What's left" item 1 is stale**: fade/DSP preview (never
existed in C#), normalization report, Create Multisample, Insert Silence, Link/Assign, pan/volume/VU, and stereo
pan/zoom sync are all present.

| Feature | Status | Note |
|---|---|---|
| Tempo/pitch | Partial, intentional | C# uses SoundTouch; Python uses WSOLA + soxr (user decision). Output differs; no A/B comparison done. |
| Tree-selection self-test, `ExsOptionFile` self-test | Missing | Minor. |
| Legacy fake workstation (`Core/sample_editor.py`, `audio_*`, `presets.py`, ...) | Python-only | Dead code per `CLAUDE.md`; do not delete without asking. |
| File Manager "Set Default Start Folder" + `ResolveStartFolder` | Missing | Setting field exists but nothing reads or writes it. |
| File Manager drag dwell-to-navigate, edge auto-scroll | Missing | UX only. |
| File Manager rubber-band select | Partial | Qt gives it for free; end-on-leave behaviour unverified. |
| Item Properties dialog in the sample remote browser | Missing | `FtpPropertiesDialog` in `Views/property_dialogs.py` is a different dialog (name collision). |

## Main window, settings, rendering

Most of this area is ported (menus, Settings content, calibration, image adjust, overlays, Nautilus skin, window
state, VU, Help/About). The gaps, largest first:

| # | Gap | Status | Note |
|---|---|---|---|
| 1 | Macros JSON format | Incompatible | C#: `{trigger:"Ctrl+X", step_delay_ms (10..2000, default 50), steps:[{code,down}]}`. Python: Qt-int trigger, steps as daemon command strings, default delay 100. A shared settings file loses macros both ways. |
| 2 | Raw key map | Incompatible | C# keeps `raw_key_mappings.json` (WPF key names, keyed on key+shift); Python stores `raw_key_maps` inside settings.json (Qt ints, keyed on key+mods). |
| 3 | Settings key/enum names | Incompatible | `image_scaling_mode` vs `scaling_quality`; `merge_behavior` `TemporaryMemory`/`LocalStorage` vs `temporary_memory`/`local_storage`. The scaling setting is silently ignored in either direction. |
| 4 | Settings defaults | Differs | C# `PullMode=true`, `MergeBehavior=TemporaryMemory`; Python `pull_mode=False`, `local_storage`. |
| 5 | Child-window placement | **Missing behaviour** | `window_placements` is loaded and saved, but no window reads or writes it; every child window reopens at its default size. Largest visible gap. |
| 6 | Command-line flags | Partial | C# `--host`, `--mode pull`; Python positional host, `--pull`, `--data-dir`. `--host` and `--mode` missing. |
| 7 | Keybind strings | Partial, untested | WPF key names/`Win` vs Qt labels/`Meta`; OEM keys likely do not round-trip. Zoom In/Out default to `=`/`-` in Python, none in C#. |
| 8 | Status-bar wrap, menu-driven minimum width, live shortcut labels | Missing | Known (`CLAUDE.md`). |
| 9 | Help window text, command-palette entries | Open | Label differences ("Layout Preset: Full" vs "Layout: Full", "..." vs ellipsis); `NautilusModeF` entry not seen in Python. |
| 10 | Top-left change gate | Partial | C# only runs help detection when the 140x55 region changed; Python runs per frame (same outcome, more CPU). |
| 11 | Storage location | Differs, intentional | C# is portable (beside the exe); Python uses per-user app data with one-time migration. A C# settings.json must be copied by hand. Log file names differ. |

Retired in C# (not gaps; Python still carries dead code): Palette Editor and overlay (`draw_palette_editor`,
`palette_key`), Warp Mode, Input Tester, and the legacy `audio_*` workstation tree (`Core/audio_engine.py` stubs).

Cross-platform risks in Python (few real breaks; most Windows branches have fallbacks):
- Fonts hardcoded to Segoe UI / Consolas (`Utils/theme.py`, `main.py`, `Rendering/overlay_renderer.py`,
  `Views/help_window.py`); layout drifts on macOS/Linux.
- `Core/sample_playback.py`: WASAPI `auto_convert` only exists on Windows; off-rate playback on CoreAudio/ALSA is untested.
- `Rendering/vu_meter.py`: `_INPUT_TRIM` 7.5 dB is a WASAPI constant, so the meter is miscalibrated elsewhere.
- ICMP ping falls back to the `ping` binary, which must be installed.
- Checked-in `.venv/` is a macOS venv; unusable elsewhere.

Takeaway: if loading a C# settings file is a goal, items 1-4 are the cheapest parity win; otherwise document the
files as non-interchangeable.

## Converter port status

| Piece | C# | Python | State |
|---|---|---|---|
| Program (EXi, HD-1) | `ProgramConverter.cs` | `Conversion/program.py` | Ported; explicit `ProgramKind` instead of inferring HD-1 from length (a 4960-byte PCG slot is ambiguous). |
| Combination | `CombiConverter.cs` | `Conversion/combi.py` | Ported; Song Timbre Sets share the layout but have no entry point yet. |
| Drum Kit, Wave Sequence | `SampleZoneRomRemapper.cs` | `Conversion/media.py`, `zones.py` | Ported (remap only; layouts identical on both). |
| ROM / program-ref / GE maps | `RomWaveformMap.cs`, `ProgramRefMap.cs`, `KarmaGe*.cs` | `Conversion/data.py` | Ported; data assets copied to `Resources/Conversion/`. |
| KARMA strip, ARP assignment | `KarmaFieldStripper.cs`, `ArpConversion.cs` | `Conversion/karma.py` | Ported. |
| GE -> arp pattern transcriber | `ArpPatternTranscriber.cs` | - | Offline tooling; its output ships in `KronosKarmaGePatterns.bin`. Not ported. |
| Nautilus PCG reader/writer | `Core/Pcg/Nautilus/*` | `Conversion/nautilus_pcg.py` | Ported. PCG1 size rule verified on real Kronos and Nautilus files; DIV1 size and dwX differ from real Nautilus files and no file has been loaded by hardware. |
| Librarian UI entry | none in C# | `Views/librarian_shell_window.py`, `Conversion/push.py` | Done: "Convert to Nautilus..." on PCG and Local panes; live-verified (8 Programs to S000-S007). |

Tests: `python tests/test_conversion.py` (ported C# cases; mutation-checked). Corpus run over
`F:\K2_62gb\HD\NARF-SOUNDS\SGC V8 K2.PCG`: of 416 objects, 391 convert; the 25 refusals are EXs references or ROM
waveforms with no Nautilus mapping (the known limitations).
