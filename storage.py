"""
JSON persistence — settings, palette overrides, palette locks, calibration data.

Data directory: same folder as the running script on first launch, then
falls back to ~/.config/KronosScreenRemote/ if the script dir is not writable
(typical install on Linux/Mac).
"""
from __future__ import annotations
import json
import os
import pathlib
from typing import Dict, List, Set, Tuple

from models import PaletteEntry, CalMesh, CalBiasDot
from app_settings import AppSettings, MacroDef, RawKeyMap


def _data_dir() -> pathlib.Path:
    script_dir = pathlib.Path(__file__).resolve().parent
    if os.access(script_dir, os.W_OK):
        return script_dir
    cfg = pathlib.Path.home() / ".config" / "KronosScreenRemote"
    cfg.mkdir(parents=True, exist_ok=True)
    return cfg


def _path(name: str) -> pathlib.Path:
    return _data_dir() / name


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
        s.pull_mode              = root.get("pull_mode",              s.pull_mode)
        s.max_fps                = root.get("max_fps",                s.max_fps)
        s.prompt_before_quitting = root.get("prompt_before_quitting", s.prompt_before_quitting)
        s.hide_controls          = root.get("hide_controls",          s.hide_controls)
        s.screenshot_dir         = root.get("screenshot_dir",         s.screenshot_dir)
        s.vga_mirror_enabled     = root.get("vga_mirror_enabled",     s.vga_mirror_enabled)
        s.screensaver_timeout    = root.get("screensaver_timeout",    s.screensaver_timeout)
        s.layout_preset          = root.get("layout_preset",          s.layout_preset)
        s.zoom_default_level     = float(root.get("zoom_default_level", s.zoom_default_level))
        s.zoom_window_size       = float(root.get("zoom_window_size",   s.zoom_window_size))
        s.debug_logging          = root.get("debug_logging",          s.debug_logging)
        s.always_on_top          = root.get("always_on_top",          s.always_on_top)
        s.recent_hosts           = list(root.get("recent_hosts",      []))
        s.keybinds               = root.get("keybinds",               {})

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
        print(f"[settings] load failed: {e}")
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
            "pull_mode":              s.pull_mode,
            "max_fps":                s.max_fps,
            "prompt_before_quitting": s.prompt_before_quitting,
            "hide_controls":          s.hide_controls,
            "screenshot_dir":         s.screenshot_dir,
            "vga_mirror_enabled":     s.vga_mirror_enabled,
            "screensaver_timeout":    s.screensaver_timeout,
            "layout_preset":          s.layout_preset,
            "zoom_default_level":     s.zoom_default_level,
            "zoom_window_size":       s.zoom_window_size,
            "debug_logging":          s.debug_logging,
            "always_on_top":          s.always_on_top,
            "recent_hosts":           s.recent_hosts,
            "keybinds":               s.keybinds,
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
        _path("settings.json").write_text(
            json.dumps(root, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[settings] save failed: {e}")


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
    for name in ("settings.json", "palette_override.json", "palette_lock.json", "cal_data.json"):
        p = _path(name)
        try:
            if p.exists():
                p.unlink()
        except Exception as e:
            print(f"[reset] could not delete {name}: {e}")


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
    _path("palette_override.json").write_text(
        json.dumps(root, indent=2), encoding="utf-8")
    print(f"[palette] {len(overrides)} override(s) saved")


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
    _path("palette_lock.json").write_text(
        json.dumps(sorted(locked)), encoding="utf-8")
    print(f"[lock] {len(locked)} locked entry/entries saved")


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
        _path("cal_data.json").write_text(
            json.dumps(root, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[cal] save failed: {e}")
