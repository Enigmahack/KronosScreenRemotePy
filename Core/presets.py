"""Audio effect and device presets manager.

Handles saving, loading, and organizing effect chains and device configurations.
"""
from __future__ import annotations
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict
from pathlib import Path
import json
import logging

log = logging.getLogger(__name__)


@dataclass
class EffectPreset:
    """Preset for an audio effect with parameters."""
    name: str
    effect_name: str  # e.g., "Reverb", "EQ", "Delay"
    parameters: Dict[str, float]
    enabled: bool = True

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> EffectPreset:
        """Create from dictionary."""
        return EffectPreset(**data)


@dataclass
class EffectChainPreset:
    """Preset for a complete effect chain."""
    name: str
    description: str = ""
    effects: List[EffectPreset] = None
    master_volume: float = 1.0

    def __post_init__(self):
        if self.effects is None:
            self.effects = []

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "name": self.name,
            "description": self.description,
            "effects": [e.to_dict() for e in self.effects],
            "master_volume": self.master_volume
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> EffectChainPreset:
        """Create from dictionary."""
        preset = EffectChainPreset(
            name=data["name"],
            description=data.get("description", ""),
            master_volume=data.get("master_volume", 1.0)
        )
        preset.effects = [EffectPreset.from_dict(e) for e in data.get("effects", [])]
        return preset


@dataclass
class AudioDeviceProfile:
    """Profile for audio device configuration."""
    name: str
    device_id: str = "default"
    sample_rate: int = 44100
    channels: int = 2
    buffer_size: int = 2048
    latency_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> AudioDeviceProfile:
        """Create from dictionary."""
        return AudioDeviceProfile(**data)


class PresetsManager:
    """Manages effect and device presets."""

    def __init__(self, presets_dir: Optional[Path] = None):
        """Initialize presets manager.

        Args:
            presets_dir: Directory to store presets (default: ~/.kronos/presets)
        """
        if presets_dir is None:
            presets_dir = Path.home() / ".kronos" / "presets"
        self.presets_dir = Path(presets_dir)
        self.presets_dir.mkdir(parents=True, exist_ok=True)

        self.effect_presets: Dict[str, EffectChainPreset] = {}
        self.device_profiles: Dict[str, AudioDeviceProfile] = {}
        self._load_all_presets()

    def _load_all_presets(self):
        """Load all presets from disk."""
        # Load effect chain presets
        effects_dir = self.presets_dir / "effects"
        if effects_dir.exists():
            for preset_file in effects_dir.glob("*.json"):
                try:
                    with open(preset_file, 'r') as f:
                        data = json.load(f)
                    preset = EffectChainPreset.from_dict(data)
                    self.effect_presets[preset.name] = preset
                except Exception as e:
                    log.warning(f"Failed to load preset {preset_file}: {e}")

        # Load device profiles
        devices_dir = self.presets_dir / "devices"
        if devices_dir.exists():
            for profile_file in devices_dir.glob("*.json"):
                try:
                    with open(profile_file, 'r') as f:
                        data = json.load(f)
                    profile = AudioDeviceProfile.from_dict(data)
                    self.device_profiles[profile.name] = profile
                except Exception as e:
                    log.warning(f"Failed to load device profile {profile_file}: {e}")

        log.info(f"Loaded {len(self.effect_presets)} effect presets, {len(self.device_profiles)} device profiles")

    def save_effect_chain(self, preset: EffectChainPreset) -> bool:
        """Save an effect chain preset.

        Args:
            preset: EffectChainPreset to save

        Returns:
            True if saved successfully
        """
        try:
            effects_dir = self.presets_dir / "effects"
            effects_dir.mkdir(parents=True, exist_ok=True)

            filepath = effects_dir / f"{preset.name}.json"
            with open(filepath, 'w') as f:
                json.dump(preset.to_dict(), f, indent=2)

            self.effect_presets[preset.name] = preset
            log.info(f"Saved effect chain preset: {preset.name}")
            return True

        except Exception as e:
            log.error(f"Failed to save effect chain preset: {e}")
            return False

    def load_effect_chain(self, name: str) -> Optional[EffectChainPreset]:
        """Load an effect chain preset.

        Args:
            name: Name of preset to load

        Returns:
            EffectChainPreset if found, None otherwise
        """
        return self.effect_presets.get(name)

    def delete_effect_chain(self, name: str) -> bool:
        """Delete an effect chain preset.

        Args:
            name: Name of preset to delete

        Returns:
            True if deleted successfully
        """
        try:
            filepath = self.presets_dir / "effects" / f"{name}.json"
            if filepath.exists():
                filepath.unlink()
            if name in self.effect_presets:
                del self.effect_presets[name]
            return True
        except Exception as e:
            log.error(f"Failed to delete effect chain preset: {e}")
            return False

    def save_device_profile(self, profile: AudioDeviceProfile) -> bool:
        """Save a device profile.

        Args:
            profile: AudioDeviceProfile to save

        Returns:
            True if saved successfully
        """
        try:
            devices_dir = self.presets_dir / "devices"
            devices_dir.mkdir(parents=True, exist_ok=True)

            filepath = devices_dir / f"{profile.name}.json"
            with open(filepath, 'w') as f:
                json.dump(profile.to_dict(), f, indent=2)

            self.device_profiles[profile.name] = profile
            log.info(f"Saved device profile: {profile.name}")
            return True

        except Exception as e:
            log.error(f"Failed to save device profile: {e}")
            return False

    def load_device_profile(self, name: str) -> Optional[AudioDeviceProfile]:
        """Load a device profile.

        Args:
            name: Name of profile to load

        Returns:
            AudioDeviceProfile if found, None otherwise
        """
        return self.device_profiles.get(name)

    def get_effect_presets(self) -> List[str]:
        """Get list of available effect presets."""
        return list(self.effect_presets.keys())

    def get_device_profiles(self) -> List[str]:
        """Get list of available device profiles."""
        return list(self.device_profiles.keys())

    def create_builtin_presets(self):
        """Create built-in presets for common scenarios."""
        # Reverb-heavy preset
        reverb_preset = EffectChainPreset(
            name="Reverb Hall",
            description="Spacious hall reverb for drums and vocals",
            effects=[
                EffectPreset("Reverb", "Reverb", {"room_size": 0.8, "wet_level": 0.4})
            ]
        )
        self.save_effect_chain(reverb_preset)

        # Bright EQ preset
        bright_preset = EffectChainPreset(
            name="Bright",
            description="Boosted high frequencies for clarity",
            effects=[
                EffectPreset("EQ", "EQ", {"low": 0.0, "mid": 2.0, "high": 6.0})
            ]
        )
        self.save_effect_chain(bright_preset)

        # Warm preset
        warm_preset = EffectChainPreset(
            name="Warm",
            description="Warm tone with bass and mid boost",
            effects=[
                EffectPreset("EQ", "EQ", {"low": 4.0, "mid": 2.0, "high": -3.0})
            ]
        )
        self.save_effect_chain(warm_preset)

        # Distortion preset
        dist_preset = EffectChainPreset(
            name="Gritty",
            description="Distorted sound with edge",
            effects=[
                EffectPreset("Distortion", "Distortion", {"drive": 3.0}),
                EffectPreset("EQ", "EQ", {"low": 2.0, "mid": 0.0, "high": 4.0})
            ]
        )
        self.save_effect_chain(dist_preset)


# Global presets manager
_presets_manager: Optional[PresetsManager] = None


def get_presets_manager() -> PresetsManager:
    """Get the global presets manager (singleton)."""
    global _presets_manager
    if _presets_manager is None:
        _presets_manager = PresetsManager()
        _presets_manager.create_builtin_presets()
    return _presets_manager
