"""
JSON persistence — settings, palette overrides, palette locks, calibration data.

Data directory (see _data_dir): an explicit --data-dir/KRONOS_DATA_DIR override,
else the per-user application-data directory for the platform, else the script's
own folder for a legacy install that already has data there and can still write
it. The program may live anywhere — a network share, a read-only image, a USB
stick — without that dictating where its state goes.
"""
from __future__ import annotations
import logging
import json
import os
import pathlib
import shutil
import sys
import threading
from typing import Dict, List, Optional, Set, Tuple

from Models.models import PaletteEntry, CalMesh, CalBiasDot
from Models.app_settings import AppSettings, MacroDef, RawKeyMap
from Core.kronos_sysex import CachedName
from Tools.setlist_data import SetListData, SetListSlot

log = logging.getLogger(__name__)


# ── Atomic writes ──────────────────────────────────────────────────────────────
# Every persisted file here is a FULL REWRITE of its previous contents, and every
# loader below treats a parse failure as "no data" and silently returns defaults.
# A plain write_text() that is interrupted (crash, power loss, disk full) leaves a
# truncated file, which those loaders then read as an empty cache — i.e. silent
# total loss of the name cache, the set-list cache, or the local library index.
#
# The temp file MUST be created in the target's own directory: os.replace is only
# atomic within a single filesystem, so a /tmp staging file would degrade to a
# copy+unlink and reintroduce exactly the torn-write window this closes.

def _atomic_write(path: pathlib.Path, data: bytes, mode: Optional[int] = None):
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


# ── Write-failure reporting ────────────────────────────────────────────────────
# Every saver in this module logs its failure and carries on, which is the right
# behaviour (a failed cache write must not take down a sync) but on its own it is
# invisible: the app quietly stops persisting anything and the user finds out when
# their work isn't there. These let the UI say so once.

_write_failure_lock = threading.Lock()
_write_failures_seen: Set[str] = set()
#: Number of save failures this session, and the most recent message.
write_failure_count = 0
last_write_failure: Optional[str] = None
#: Set by the UI: called (from whatever thread failed) the FIRST time a given
#: kind of save fails, never repeatedly for the same kind.
on_write_failure = None


def _note_write_failure(what: str, exc: BaseException) -> None:
    global write_failure_count, last_write_failure
    msg = f"{what} could not be saved: {exc}"
    log.error("%s", msg)
    with _write_failure_lock:
        write_failure_count += 1
        last_write_failure = msg
        first = what not in _write_failures_seen
        _write_failures_seen.add(what)
        cb = on_write_failure
    if first and cb is not None:
        try:
            cb(msg)
        except Exception:                     # noqa: BLE001 - reporting must not throw
            log.exception("write-failure callback raised")


def atomic_write_text(path: pathlib.Path, text: str, encoding: str = "utf-8",
                      mode: Optional[int] = None):
    """Replace `path`'s contents with `text`, or leave the old file untouched."""
    _atomic_write(path, text.encode(encoding), mode)


def atomic_write_bytes(path: pathlib.Path, data: bytes, mode: Optional[int] = None):
    """Replace `path`'s contents with `data`, or leave the old file untouched."""
    _atomic_write(path, data, mode)


_data_dir_cache: Optional[pathlib.Path] = None
_data_dir_lock = threading.Lock()
_data_dir_override: Optional[pathlib.Path] = None
#: Set when _data_dir() could not use the legacy script-directory location. The UI
#: reads it at startup: silently using a DIFFERENT data directory means the user's
#: settings, caches and whole local library appear to be empty, which needs saying
#: out loud — and offering to migrate (see legacy_data_dir / migrate_data_dir).
data_dir_fallback_reason: Optional[str] = None

#: The names _data_dir() looks for to decide whether a directory is already
#: serving as somebody's data directory. Only these; a directory that merely
#: contains the source tree does not qualify.
_DATA_MARKERS = ("settings.json", "local_library", "name_cache.json", "cal_data.json")

# Environment variable form of --data-dir, for launchers/shortcuts that can't
# easily pass arguments.
_DATA_DIR_ENV = "KRONOS_DATA_DIR"


def _write_probe(d: pathlib.Path) -> bool:
    """Can this directory actually take the writes this module performs?

    os.access(W_OK) is a permission-BIT check, not a can-I-write check, and even
    a successful create is not enough: EVERY save here is _atomic_write, whose
    last step is os.replace() over an ALREADY-EXISTING file. On an SMB/CIFS share
    those are separate permissions — creating a new file can succeed while
    replacing an existing one fails with "Access is denied" (observed 2026-08-12
    on a Windows client writing to \\\\host\\share, which is what made the app
    log 'name cache save failed' on a loop). So the probe performs the whole
    sequence: create, replace-over-existing, delete."""
    probe = d / f".kronos_write_probe.{os.getpid()}"
    tmp = d / f".kronos_write_probe.{os.getpid()}.tmp"
    try:
        with open(probe, "wb") as f:
            f.write(b"0")
        with open(tmp, "wb") as f:
            f.write(b"1")
        os.replace(tmp, probe)       # the operation every save actually depends on
        return True
    except OSError:
        return False
    finally:
        for p in (tmp, probe):
            try:
                p.unlink()
            except OSError:
                pass


# Kept as the historical name used elsewhere in the app.
_really_writable = _write_probe


def _user_data_dir() -> pathlib.Path:
    """The platform's per-user application-data directory.

    This is the default, and it is deliberately independent of where the program
    was launched from: the app must run from a network share, a read-only image
    or a USB stick without any of those becoming a requirement."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = pathlib.Path(base) if base else pathlib.Path.home() / "AppData" / "Local"
        return root / "KronosScreenRemote"
    if sys.platform == "darwin":
        return pathlib.Path.home() / "Library" / "Application Support" / "KronosScreenRemote"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    root = pathlib.Path(xdg) if xdg else pathlib.Path.home() / ".config"
    return root / "KronosScreenRemote"


def _has_data(d: pathlib.Path) -> bool:
    try:
        return any((d / name).exists() for name in _DATA_MARKERS)
    except OSError:
        return False


def set_data_dir_override(path) -> None:
    """Point every persisted file at `path` (the --data-dir argument).

    Must be called before anything reads or writes; _data_dir() caches its
    answer for the process."""
    global _data_dir_override, _data_dir_cache
    with _data_dir_lock:
        _data_dir_override = pathlib.Path(path).expanduser()
        _data_dir_cache = None


def data_dir_notice() -> Optional[str]:
    """data_dir_fallback_reason, but guaranteed resolved.

    The module-level variable is only populated once _data_dir() has actually
    run, so reading it directly before any storage access always yields None.
    Callers that want the answer should ask for it through here."""
    _data_dir()
    return data_dir_fallback_reason


def legacy_data_dir() -> Optional[pathlib.Path]:
    """The script-directory location, if it holds data that the currently
    resolved data directory doesn't. None when there is nothing to migrate —
    which is the normal case, and which is also what makes the migration offer
    a ONE-time event: once the active directory has data of its own, this stops
    reporting anything, so the prompt doesn't come back every launch."""
    active = _data_dir()
    script_dir = pathlib.Path(__file__).resolve().parent
    if script_dir == active or not _has_data(script_dir) or _has_data(active):
        return None
    return script_dir


def _resolve_data_dir() -> Tuple[pathlib.Path, Optional[str]]:
    """(directory, reason-to-tell-the-user-or-None). Order:

      1. --data-dir / KRONOS_DATA_DIR — an explicit choice always wins.
      2. The per-user application-data directory, once it exists or once there
         is nothing to inherit from the script directory.
      3. The script directory, but ONLY for a legacy install that already keeps
         its data there AND can still be written to. That case is preserved so
         an existing setup (including a deliberately shared library on a file
         server) keeps working untouched; it is never created fresh.

    A directory that fails the write probe is never returned, whichever rule
    selected it."""
    script_dir = pathlib.Path(__file__).resolve().parent
    user_dir = _user_data_dir()

    chosen = _data_dir_override
    if chosen is None:
        env = os.environ.get(_DATA_DIR_ENV, "").strip()
        if env:
            chosen = pathlib.Path(env).expanduser()
    if chosen is not None:
        try:
            chosen.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise SystemExit(f"data directory {chosen} cannot be created: {e}")
        if not _write_probe(chosen):
            raise SystemExit(f"data directory {chosen} is not writable")
        return chosen, None

    legacy_in_use = _has_data(script_dir) and not _has_data(user_dir)
    if legacy_in_use:
        if _write_probe(script_dir):
            return script_dir, None
        reason = (f"{script_dir} holds this app's data but can no longer be written to "
                  f"(read-only or permission-denied share?). Now using {user_dir}.")
    else:
        reason = None

    try:
        user_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.error("per-user data dir %s is unusable: %s", user_dir, e)
    if _write_probe(user_dir):
        return user_dir, reason

    # Last resort: somewhere writable beats refusing to start. Whatever this
    # process manages to save is better than losing every setting it changes.
    # Resolved WITHOUT scratch_dir(), which calls back into _data_dir() and would
    # deadlock on _data_dir_lock from here.
    import tempfile
    for base in (pathlib.Path(tempfile.gettempdir()), pathlib.Path.home()):
        fallback = base / "KronosScreenRemote"
        try:
            fallback.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        if _write_probe(fallback):
            return fallback, (f"Neither {script_dir} nor {user_dir} is writable — using "
                              f"{fallback} for this session.")
    raise SystemExit(
        "No writable location for application data. Tried "
        f"{script_dir}, {user_dir}, the system temp directory and {pathlib.Path.home()}. "
        f"Pass --data-dir <path> (or set {_DATA_DIR_ENV}) to choose one explicitly.")


def _data_dir() -> pathlib.Path:
    """Resolved once per process — the probe costs a few file operations, and
    every persisted file in the app routes through here."""
    global _data_dir_cache, data_dir_fallback_reason
    if _data_dir_cache is not None:
        return _data_dir_cache
    with _data_dir_lock:
        if _data_dir_cache is not None:
            return _data_dir_cache
        resolved, reason = _resolve_data_dir()
        if reason:
            data_dir_fallback_reason = reason
            log.warning("%s", reason)
        _data_dir_cache = resolved
        log.info("data directory: %s", resolved)
        return _data_dir_cache


#: What migrate_data_dir() copies — this app's persisted state, nothing else.
#: Every name here is a real target of _path()/local_library_dir()/backup_dir();
#: keep it in step with them when a new persisted file is added.
_MIGRATE_NAMES = (
    "settings.json", "name_cache.json", "cal_data.json", "dumped_banks.json",
    "setlist_cache.json", "category_names_cache.json", "palette_override.json",
    "palette_lock.json", "local_library", "librarian_backups",
)


def migrate_data_dir(src: pathlib.Path, progress=None) -> Tuple[int, List[str]]:
    """Copy an existing data directory's contents into the active one.

    Copies only this app's own files (see _MIGRATE_NAMES) — the script directory
    doubles as the source tree in a legacy install, and nobody wants .py files
    copied into their application-data folder. Never deletes from `src`: the
    original stays exactly as it was, so a migration that turns out to be
    unwanted costs nothing. Returns (files copied, per-item error strings)."""
    dst = _data_dir()
    copied = 0
    errors: List[str] = []
    for name in _MIGRATE_NAMES:
        s = src / name
        if not s.exists():
            continue
        d = dst / name
        try:
            if progress is not None:
                progress(name)
            if s.is_dir():
                shutil.copytree(s, d, dirs_exist_ok=True)
                copied += sum(1 for _ in s.rglob("*") if _.is_file())
            else:
                shutil.copy2(s, d)
                copied += 1
        except OSError as e:
            errors.append(f"{name}: {e}")
    return copied, errors


def scratch_dir() -> pathlib.Path:
    """A directory this process can definitely write scratch/temp files into.

    Tries the system temp dir first, then the app data dir, then the user's home
    — the first one that survives a real write probe. Callers that can work
    entirely in memory should do that instead; this exists for the ones that
    genuinely need a path, so "temp is not writable" degrades to "somewhere else"
    rather than to an exception out of a UI handler."""
    import tempfile
    candidates = [pathlib.Path(tempfile.gettempdir()), _data_dir(), pathlib.Path.home()]
    for d in candidates:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        if _really_writable(d):
            return d
    raise OSError("no writable directory available for temporary files "
                  f"(tried: {', '.join(str(c) for c in candidates)})")


def _path(name: str) -> pathlib.Path:
    return _data_dir() / name


def data_dir() -> pathlib.Path:
    """Public accessor for the same data directory _path() resolves against,
    for callers that persist their own file there without adding a dedicated
    load_/save_ pair to this module."""
    return _data_dir()


def backup_dir() -> pathlib.Path:
    """Directory for pre-write hardware object backups (.syx files) — mirrors
    the C# client's Storage.BackupDir(). Already gitignored (librarian_backups/).

    Falls back to a scratch location if the data directory can't take it. These
    are pre-image snapshots taken immediately before overwriting a slot on the
    instrument — the one recovery path for a bad push — so "somewhere else" beats
    "skip the backup and write anyway", which is what the caller does when this
    raises."""
    d = _data_dir() / "librarian_backups"
    try:
        d.mkdir(parents=True, exist_ok=True)
        if _really_writable(d):
            return d
    except OSError as e:
        log.warning("backup dir %s unusable (%s)", d, e)
    alt = scratch_dir() / "KronosScreenRemote_backups"
    alt.mkdir(parents=True, exist_ok=True)
    log.warning("using %s for hardware pre-write backups", alt)
    return alt


# ── Settings ───────────────────────────────────────────────────────────────────

def load_settings() -> AppSettings:
    p = _path("settings.json")
    s = AppSettings()
    if not p.exists():
        return s
    try:
        root = json.loads(p.read_text(encoding="utf-8"))
        s.kronos_host            = root.get("kronos_host",            s.kronos_host)
        s.stream_port            = root.get("stream_port",            s.stream_port)
        s.ctrl_port              = root.get("ctrl_port",              s.ctrl_port)
        s.ftp_username           = root.get("ftp_username",           s.ftp_username)
        s.ftp_password           = root.get("ftp_password",           s.ftp_password)
        s.ftp_port               = root.get("ftp_port",               s.ftp_port)
        s.midi_monitor_enabled   = root.get("midi_monitor_enabled",   s.midi_monitor_enabled)
        s.pull_mode              = root.get("pull_mode",              s.pull_mode)
        s.max_fps                = root.get("max_fps",                s.max_fps)
        s.prompt_before_quitting = root.get("prompt_before_quitting", s.prompt_before_quitting)
        s.hide_data_input        = root.get("hide_data_input",        root.get("hide_controls", s.hide_data_input))
        s.hide_value_input       = root.get("hide_value_input",       s.hide_value_input)
        s.reverse_scrolling      = root.get("reverse_scrolling",      s.reverse_scrolling)
        s.screenshot_dir         = root.get("screenshot_dir",         s.screenshot_dir)
        s.vga_mirror_enabled     = root.get("vga_mirror_enabled",     s.vga_mirror_enabled)
        s.screensaver_timeout    = root.get("screensaver_timeout",    s.screensaver_timeout)
        s.layout_preset          = root.get("layout_preset",          s.layout_preset)
        s.focused_data_expanded  = root.get("focused_data_expanded",  s.focused_data_expanded)
        s.focused_value_expanded = root.get("focused_value_expanded", s.focused_value_expanded)
        s.boot_screen_threshold  = root.get("boot_screen_threshold",  s.boot_screen_threshold)
        s.disable_boot_screen    = root.get("disable_boot_screen",    s.disable_boot_screen)
        s.zoom_default_level     = float(root.get("zoom_default_level", s.zoom_default_level))
        s.zoom_window_size       = float(root.get("zoom_window_size",   s.zoom_window_size))
        s.scaling_quality        = root.get("scaling_quality",         s.scaling_quality)
        s.image_brightness       = int(root.get("image_brightness",    s.image_brightness))
        s.image_contrast         = int(root.get("image_contrast",      s.image_contrast))
        s.image_gamma            = float(root.get("image_gamma",       s.image_gamma))
        s.image_saturation       = int(root.get("image_saturation",    s.image_saturation))
        s.image_sharpen          = int(root.get("image_sharpen",       s.image_sharpen))
        s.debug_logging          = root.get("debug_logging",          s.debug_logging)
        s.always_on_top          = root.get("always_on_top",          s.always_on_top)
        s.merge_preserve_duplicate_programs = root.get("merge_preserve_duplicate_programs",
                                                        s.merge_preserve_duplicate_programs)
        s.merge_preserve_duplicate_combis   = root.get("merge_preserve_duplicate_combis",
                                                        s.merge_preserve_duplicate_combis)
        s.merge_behavior        = root.get("merge_behavior",         s.merge_behavior)
        s.recent_hosts           = list(root.get("recent_hosts",      []))
        s.keybinds               = root.get("keybinds",               {})
        s.blank_template_source_slots = {
            k: list(v) for k, v in root.get("blank_template_source_slots", s.blank_template_source_slots).items()
        }

        for m in root.get("macros", []):
            try:
                s.macros.append(MacroDef(
                    description   = m.get("description", ""),
                    trigger_key   = int(m.get("trigger_key",   0)),
                    trigger_mods  = int(m.get("trigger_mods",  0)),
                    step_delay_ms = int(m.get("step_delay_ms", 100)),
                    steps         = list(m.get("steps",        [])),
                ))
            except Exception:
                pass

        for r in root.get("raw_key_maps", []):
            try:
                s.raw_key_maps.append(RawKeyMap(
                    label      = r.get("label",      ""),
                    host_key   = int(r.get("host_key",   0)),
                    host_mods  = int(r.get("host_mods",  0)),
                    raw_code   = int(r.get("raw_code",   0)),
                    send_shift = bool(r.get("send_shift", False)),
                ))
            except Exception:
                pass

    except Exception as e:
        log.warning("settings load failed: %s", e)
    return s


def save_settings(s: AppSettings):
    try:
        root = {
            "kronos_host":            s.kronos_host,
            "stream_port":            s.stream_port,
            "ctrl_port":              s.ctrl_port,
            "ftp_username":           s.ftp_username,
            "ftp_password":           s.ftp_password,
            "ftp_port":               s.ftp_port,
            "midi_monitor_enabled":   s.midi_monitor_enabled,
            "pull_mode":              s.pull_mode,
            "max_fps":                s.max_fps,
            "prompt_before_quitting": s.prompt_before_quitting,
            "hide_data_input":        s.hide_data_input,
            "hide_value_input":       s.hide_value_input,
            "reverse_scrolling":      s.reverse_scrolling,
            "screenshot_dir":         s.screenshot_dir,
            "vga_mirror_enabled":     s.vga_mirror_enabled,
            "screensaver_timeout":    s.screensaver_timeout,
            "layout_preset":          s.layout_preset,
            "focused_data_expanded":  s.focused_data_expanded,
            "focused_value_expanded": s.focused_value_expanded,
            "boot_screen_threshold":  s.boot_screen_threshold,
            "disable_boot_screen":    s.disable_boot_screen,
            "zoom_default_level":     s.zoom_default_level,
            "zoom_window_size":       s.zoom_window_size,
            "scaling_quality":        s.scaling_quality,
            "image_brightness":       s.image_brightness,
            "image_contrast":         s.image_contrast,
            "image_gamma":            s.image_gamma,
            "image_saturation":       s.image_saturation,
            "image_sharpen":          s.image_sharpen,
            "debug_logging":          s.debug_logging,
            "always_on_top":          s.always_on_top,
            "merge_preserve_duplicate_programs": s.merge_preserve_duplicate_programs,
            "merge_preserve_duplicate_combis":   s.merge_preserve_duplicate_combis,
            "merge_behavior":         s.merge_behavior,
            "recent_hosts":           s.recent_hosts,
            "keybinds":               s.keybinds,
            "blank_template_source_slots": s.blank_template_source_slots,
            "macros": [
                {
                    "description":   m.description,
                    "trigger_key":   m.trigger_key,
                    "trigger_mods":  m.trigger_mods,
                    "step_delay_ms": m.step_delay_ms,
                    "steps":         m.steps,
                }
                for m in s.macros
            ],
            "raw_key_maps": [
                {
                    "label":      r.label,
                    "host_key":   r.host_key,
                    "host_mods":  r.host_mods,
                    "raw_code":   r.raw_code,
                    "send_shift": r.send_shift,
                }
                for r in s.raw_key_maps
            ],
        }
        # 0o600: this file stores the FTP password in cleartext (the daemon's
        # stream handshake requires it), so keep it unreadable to other users.
        atomic_write_text(_path("settings.json"), json.dumps(root, indent=2), mode=0o600)
    except Exception as e:
        _note_write_failure("Settings", e)


def export_settings(s: AppSettings, path: str):
    """Export all settings to a JSON file."""
    save_settings(s)
    import shutil
    shutil.copy2(str(_path("settings.json")), path)


def import_settings(path: str) -> AppSettings:
    """Import settings from a JSON file, replace current settings."""
    import shutil
    shutil.copy2(path, str(_path("settings.json")))
    return load_settings()


def reset_all():
    """Delete all persisted data files and return a fresh AppSettings."""
    for name in ("settings.json", "palette_override.json", "palette_lock.json", "cal_data.json",
                 "name_cache.json", "dumped_banks.json", "setlist_cache.json"):
        p = _path(name)
        try:
            if p.exists():
                p.unlink()
        except Exception as e:
            log.warning("could not delete %s: %s", name, e)


# ── Palette overrides ──────────────────────────────────────────────────────────

def load_overrides() -> Dict[int, PaletteEntry]:
    p = _path("palette_override.json")
    if not p.exists():
        return {}
    try:
        root = json.loads(p.read_text(encoding="utf-8"))
        d: dict[int, PaletteEntry] = {}
        for k, v in root.items():
            try:
                idx = int(k)
                d[idx] = PaletteEntry(int(v[0]), int(v[1]), int(v[2]))
            except Exception:
                pass
        return d
    except Exception:
        return {}


def save_overrides(overrides: Dict[int, PaletteEntry]):
    root = {str(k): [v.r, v.g, v.b] for k, v in sorted(overrides.items())}
    atomic_write_text(_path("palette_override.json"), json.dumps(root, indent=2))
    log.debug("%d palette override(s) saved", len(overrides))


# ── Palette locks ──────────────────────────────────────────────────────────────

def load_locks() -> Set[int]:
    p = _path("palette_lock.json")
    if not p.exists():
        return set()
    try:
        arr = json.loads(p.read_text(encoding="utf-8"))
        return {int(x) for x in arr if isinstance(x, int)}
    except Exception:
        return set()


def save_locks(locked: Set[int]):
    atomic_write_text(_path("palette_lock.json"), json.dumps(sorted(locked)))
    log.debug("%d locked palette entry/entries saved", len(locked))


# ── Calibration ────────────────────────────────────────────────────────────────

def load_cal() -> Tuple[CalMesh, List[CalBiasDot]]:
    dots: list[CalBiasDot] = []
    p = _path("cal_data.json")

    # Fall back to embedded default bundled with the Python project
    embedded = pathlib.Path(__file__).parent / "Resources" / "cal_data.json"
    if not p.exists() and embedded.exists():
        p = embedded

    if not p.exists():
        return CalMesh(), dots

    try:
        root = json.loads(p.read_text(encoding="utf-8"))
        size = root.get("grid_size", 5)
        if size not in (3, 4, 5):
            size = 5
        mesh = CalMesh(size, size)

        for entry in root.get("mesh", []):
            if len(entry) >= 4:
                mesh.set_offset(entry[0], entry[1], entry[2], entry[3])

        for d in root.get("bias_dots", []):
            if len(d) >= 2:
                dots.append(CalBiasDot(d[0], d[1]))

        return mesh, dots
    except Exception:
        return CalMesh(), dots


def save_cal(mesh: CalMesh, dots: List[CalBiasDot]):
    try:
        mesh_arr = []
        for c in range(mesh.cols):
            for r in range(mesh.rows):
                ox, oy = mesh.get_offset(c, r)
                if ox != 0 or oy != 0:
                    mesh_arr.append([c, r, ox, oy])
        root = {
            "grid_size":  mesh.cols,
            "mesh":       mesh_arr,
            "bias_dots":  [[d.nx, d.ny] for d in dots],
        }
        atomic_write_text(_path("cal_data.json"), json.dumps(root, indent=2))
    except Exception as e:
        _note_write_failure("Calibration data", e)


# ── Program/Combi name cache ────────────────────────────────────────────────
# name_cache.json: { host: [ {type, bank, number, name}, ... ] }
# Keyed by "host" (the SysExService cache key — the TCP host, matching
# Core/Storage.cs LoadNames/SaveNames).

_names_lock = threading.Lock()
_dumped_lock = threading.Lock()
_setlists_lock = threading.Lock()


def load_names(cache_key: str) -> List[CachedName]:
    p = _path("name_cache.json")
    if not p.exists():
        return []
    try:
        with _names_lock:
            root = json.loads(p.read_text(encoding="utf-8"))
        out: List[CachedName] = []
        for e in root.get(cache_key, []):
            try:
                out.append(CachedName(int(e["type"]), int(e["bank"]), int(e["number"]), str(e["name"])))
            except Exception:
                pass
        return out
    except Exception as e:
        log.warning("name cache load failed: %s", e)
        return []


def save_names(cache_key: str, names: List[CachedName]):
    p = _path("name_cache.json")
    try:
        with _names_lock:
            root = {}
            if p.exists():
                try:
                    root = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    root = {}
            root[cache_key] = [
                {"type": n.type, "bank": n.bank, "number": n.number, "name": n.name}
                for n in names
            ]
            atomic_write_text(p, json.dumps(root, indent=2))
    except Exception as e:
        _note_write_failure("Program/Combi name cache", e)


# ── Dumped-bank ledger ───────────────────────────────────────────────────────
# dumped_banks.json: { host: ["type:bankHex", ...] }
# Tracks which (type, objBank) name sweeps have completed, separately from the
# name cache since an empty bank legitimately caches zero names.

def load_dumped_banks(cache_key: str) -> Set[Tuple[int, int]]:
    p = _path("dumped_banks.json")
    if not p.exists():
        return set()
    try:
        with _dumped_lock:
            root = json.loads(p.read_text(encoding="utf-8"))
        out: Set[Tuple[int, int]] = set()
        for tok in root.get(cache_key, []):
            try:
                t, b = tok.split(":")
                out.add((int(t), int(b, 16)))
            except Exception:
                pass
        return out
    except Exception as e:
        log.warning("dumped-bank ledger load failed: %s", e)
        return set()


def save_dumped_banks(cache_key: str, banks: Set[Tuple[int, int]]):
    p = _path("dumped_banks.json")
    try:
        with _dumped_lock:
            root = {}
            if p.exists():
                try:
                    root = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    root = {}
            root[cache_key] = sorted(f"{t}:{b:02x}" for t, b in banks)
            atomic_write_text(p, json.dumps(root, indent=2))
    except Exception as e:
        _note_write_failure("Dumped-bank ledger", e)


# ── Category names cache (GlobalBody.ReadCategoryNames) ────────────────────
# category_names_cache.json: { host: {program, program_sub, combi, combi_sub} }
# Mirrors Core/Storage.cs's CategoryNamesDto persistence, host-keyed exactly
# like the name/dumped-banks caches. Load returns None when this host was never
# synced — the caller falls back to CategoryNames.numeric() (plain "Category 05"
# labels), never to an error. Shape-validated on load so a truncated/hand-edited
# file degrades to None rather than crashing the Properties dialog.

_category_names_lock = threading.Lock()


def load_category_names(cache_key: str) -> Optional[dict]:
    p = _path("category_names_cache.json")
    if not p.exists():
        return None
    try:
        with _category_names_lock:
            root = json.loads(p.read_text(encoding="utf-8"))
        d = root.get(cache_key)
        if not isinstance(d, dict):
            return None
        return {
            "program": d.get("program"),
            "program_sub": d.get("program_sub"),
            "combi": d.get("combi"),
            "combi_sub": d.get("combi_sub"),
        }
    except Exception as e:
        log.warning("category-name cache load failed: %s", e)
        return None


def save_category_names(cache_key: str, names: dict):
    p = _path("category_names_cache.json")
    try:
        with _category_names_lock:
            root = {}
            if p.exists():
                try:
                    root = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    root = {}
            root[cache_key] = names
            atomic_write_text(p, json.dumps(root, indent=2))
    except Exception as e:
        _note_write_failure("Category-name cache", e)


# ── Set List cache ───────────────────────────────────────────────────────────
# setlist_cache.json: { host: { "<number>": {name, slots:[...]}, ... } }

def load_setlists(cache_key: str) -> Dict[int, SetListData]:
    p = _path("setlist_cache.json")
    if not p.exists():
        return {}
    try:
        with _setlists_lock:
            root = json.loads(p.read_text(encoding="utf-8"))
        out: Dict[int, SetListData] = {}
        for num_str, sl in root.get(cache_key, {}).items():
            try:
                num = int(num_str)
                slots = [
                    SetListSlot(
                        number=s["number"], name=s["name"], type=s["type"], bank=s["bank"],
                        index=s["index"], color=s["color"], hold_time=s["hold_time"],
                        volume=s["volume"], comments=s["comments"])
                    for s in sl.get("slots", [])
                ]
                out[num] = SetListData(num, sl.get("name", ""), slots)
            except Exception:
                pass
        return out
    except Exception as e:
        log.warning("set-list cache load failed: %s", e)
        return {}


def save_setlists(cache_key: str, setlists: Dict[int, SetListData]):
    p = _path("setlist_cache.json")
    try:
        with _setlists_lock:
            root = {}
            if p.exists():
                try:
                    root = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    root = {}
            root[cache_key] = {
                str(num): {
                    "name": data.name,
                    "slots": [
                        {"number": s.number, "name": s.name, "type": s.type, "bank": s.bank,
                         "index": s.index, "color": s.color, "hold_time": s.hold_time,
                         "volume": s.volume, "comments": s.comments}
                        for s in data.slots
                    ],
                }
                for num, data in setlists.items()
            }
            atomic_write_text(p, json.dumps(root, indent=2))
    except Exception as e:
        _note_write_failure("Set-list cache", e)
