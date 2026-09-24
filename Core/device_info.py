"""Device information and capabilities."""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Dict, Any


class DeviceFamily(Enum):
    """Device family identifier."""
    KRONOS = "KRONOS"
    NAUTILUS = "NAUTILUS"
    UNKNOWN = "UNKNOWN"


class DeviceModel(Enum):
    """Specific device model."""
    KRONOS_1 = "Kronos 1"
    KRONOS_X = "Kronos X"
    KRONOS_2 = "Kronos 2"
    KRONOS_3 = "Kronos 3"
    NAUTILUS = "Nautilus"
    UNKNOWN = "Unknown"


@dataclass
class DeviceCapabilities:
    """Device capabilities and features."""
    supports_audio_mirror: bool = True
    supports_sample_editing: bool = False
    supports_drum_kit: bool = True
    supports_combo_mode: bool = True
    max_programs: int = 128
    max_combis: int = 128
    max_drum_kits: int = 64
    max_samples: int = 1000
    screen_width: int = 800
    screen_height: int = 600
    supports_rgb565: bool = False  # Nautilus has native RGB565
    supports_palette: bool = True   # Kronos uses 8-bit palette
    supports_midi_bridge: bool = True


@dataclass
class DeviceInfo:
    """Complete device information."""
    family: DeviceFamily
    model: DeviceModel
    firmware_version: str = ""
    cpu: str = ""
    capabilities: DeviceCapabilities = None

    def __post_init__(self):
        if self.capabilities is None:
            self.capabilities = self._default_capabilities()

    def _default_capabilities(self) -> DeviceCapabilities:
        """Get default capabilities for this device."""
        if self.family == DeviceFamily.NAUTILUS:
            return DeviceCapabilities(
                supports_audio_mirror=False,
                supports_sample_editing=True,
                supports_rgb565=True,
                supports_palette=False,
                screen_height=480,
            )
        return DeviceCapabilities()

    def is_kronos(self) -> bool:
        """Check if device is a Kronos model."""
        return self.family == DeviceFamily.KRONOS

    def is_nautilus(self) -> bool:
        """Check if device is a Nautilus model."""
        return self.family == DeviceFamily.NAUTILUS


class DeviceDetector:
    """Detect and identify connected device."""

    @staticmethod
    def detect_from_discovery(discovery_response: str) -> DeviceInfo:
        """Parse UDP discovery response to identify device.

        Example: "KSCR SP=7373 CP=7374 MIDI=1 FAMILY=KRONOS PROTO=3 FMT=INDEX8 GEOM=800x600"
        """
        parts = discovery_response.strip().split()

        family_str = "KRONOS"
        pixel_fmt = "INDEX8"

        for part in parts:
            if part.startswith("FAMILY="):
                family_str = part.split("=")[1]
            elif part.startswith("FMT="):
                pixel_fmt = part.split("=")[1]

        # Detect family
        if "NAUTILUS" in family_str:
            family = DeviceFamily.NAUTILUS
            model = DeviceModel.NAUTILUS
        else:
            family = DeviceFamily.KRONOS
            # We can't determine exact Kronos model from discovery
            model = DeviceModel.UNKNOWN

        return DeviceInfo(
            family=family,
            model=model,
            capabilities=DeviceCapabilities(
                supports_rgb565=(pixel_fmt == "RGB565LE"),
                supports_palette=(pixel_fmt == "INDEX8"),
            )
        )

    @staticmethod
    def detect_from_sysinfo(sysinfo_response: str) -> Optional[DeviceInfo]:
        """Parse SYSINFO response to get detailed device information.

        (This would require parsing the actual SYSINFO response format)
        """
        # Placeholder for future SYSINFO parsing
        return None


# Device information registry
DEVICE_INFO_REGISTRY: Dict[DeviceFamily, DeviceCapabilities] = {
    DeviceFamily.KRONOS: DeviceCapabilities(
        supports_audio_mirror=True,
        supports_sample_editing=False,
        screen_width=800,
        screen_height=600,
        supports_palette=True,
        supports_rgb565=False,
    ),
    DeviceFamily.NAUTILUS: DeviceCapabilities(
        supports_audio_mirror=False,
        supports_sample_editing=True,
        screen_width=800,
        screen_height=480,
        supports_palette=False,
        supports_rgb565=True,
    ),
}


def get_device_capabilities(family: DeviceFamily) -> DeviceCapabilities:
    """Get capabilities for a device family."""
    return DEVICE_INFO_REGISTRY.get(family, DeviceCapabilities())
