"""Audio device detection and playback engine.

Supports cross-platform audio device enumeration:
- Windows: WASAPI (via sounddevice or pyaudio)
- macOS: CoreAudio (via sounddevice or pyaudio)
- Linux: ALSA/PulseAudio (via sounddevice or pyaudio)

Falls back gracefully if audio libraries unavailable.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, List
import subprocess
import re
import sys


@dataclass
class AudioDevice:
    """Represents a single audio device (input or output)."""
    name: str
    device_id: str
    is_input: bool
    is_output: bool
    is_default: bool = False
    channels: int = 2
    sample_rate: int = 44100
    description: str = ""
    latency_ms: float = 0.0
    online: bool = True


@dataclass
class AudioConfig:
    """Audio playback configuration."""
    device_id: str = "default"
    sample_rate: int = 44100
    channels: int = 2
    buffer_size: int = 2048
    latency_ms: float = 0.0


class AudioDeviceManager:
    """Enumerate and manage audio devices on the system."""

    def __init__(self):
        """Initialize audio device manager."""
        self.input_devices: List[AudioDevice] = []
        self.output_devices: List[AudioDevice] = []
        self.default_input: Optional[AudioDevice] = None
        self.default_output: Optional[AudioDevice] = None
        self._sounddevice_available = False
        self._pyaudio_available = False
        self._refresh()

    def _refresh(self):
        """Refresh the list of available audio devices."""
        self.input_devices = []
        self.output_devices = []
        self.default_input = None
        self.default_output = None

        # Try platform-specific enumeration
        if self._detect_with_sounddevice():
            return
        if self._detect_with_pyaudio():
            return
        if self._detect_alsa():
            return
        if self._detect_pulseaudio():
            return
        if self._detect_win32():
            return
        if self._detect_coreaudio():
            return

        # Fallback: add a default device
        self._add_default_device()

    def _detect_with_sounddevice(self) -> bool:
        """Detect audio devices using sounddevice library (preferred)."""
        try:
            import sounddevice
            self._sounddevice_available = True

            for device_id, info in enumerate(sounddevice.query_devices()):
                device = AudioDevice(
                    name=info.get('name', f'Device {device_id}'),
                    device_id=str(device_id),
                    is_input=info.get('max_input_channels', 0) > 0,
                    is_output=info.get('max_output_channels', 0) > 0,
                    channels=max(
                        info.get('max_input_channels', 0),
                        info.get('max_output_channels', 0)
                    ),
                    sample_rate=int(info.get('default_samplerate', 44100)),
                    latency_ms=(
                        (info.get('default_low_input_latency', 0) +
                         info.get('default_low_output_latency', 0)) * 1000
                    ),
                    description=f"sounddevice device {device_id}"
                )

                if device.is_input:
                    if info.get('name', '').startswith('*'):
                        self.default_input = device
                    self.input_devices.append(device)

                if device.is_output:
                    if info.get('name', '').startswith('*'):
                        self.default_output = device
                    self.output_devices.append(device)

            return len(self.output_devices) > 0

        except (ImportError, Exception):
            pass

        return False

    def _detect_with_pyaudio(self) -> bool:
        """Detect audio devices using PyAudio library (fallback)."""
        try:
            import pyaudio

            self._pyaudio_available = True
            pa = pyaudio.PyAudio()

            default_input_idx = pa.get_default_input_device_index()
            default_output_idx = pa.get_default_output_device_index()

            for device_id in range(pa.get_device_count()):
                info = pa.get_device_info_by_index(device_id)

                device = AudioDevice(
                    name=info['name'],
                    device_id=str(device_id),
                    is_input=info['maxInputChannels'] > 0,
                    is_output=info['maxOutputChannels'] > 0,
                    is_default=(device_id == default_input_idx or
                               device_id == default_output_idx),
                    channels=max(
                        info.get('maxInputChannels', 0),
                        info.get('maxOutputChannels', 0)
                    ),
                    sample_rate=int(info['defaultSampleRate']),
                    latency_ms=(
                        (info.get('defaultLowInputLatency', 0) +
                         info.get('defaultLowOutputLatency', 0)) * 1000
                    ),
                    description=f"PyAudio device {device_id}"
                )

                if device.is_input and device_id == default_input_idx:
                    self.default_input = device
                    self.input_devices.append(device)
                elif device.is_input:
                    self.input_devices.append(device)

                if device.is_output and device_id == default_output_idx:
                    self.default_output = device
                    self.output_devices.append(device)
                elif device.is_output:
                    self.output_devices.append(device)

            pa.terminate()
            return len(self.output_devices) > 0

        except (ImportError, Exception):
            pass

        return False

    def _detect_alsa(self) -> bool:
        """Detect audio devices using ALSA (Linux)."""
        try:
            result = subprocess.run(
                ['arecord', '-l'],
                capture_output=True, text=True, timeout=2
            )
            if result.returncode == 0:
                self._parse_alsa_output(result.stdout)
                if len(self.output_devices) > 0:
                    self.default_output = self.output_devices[0]
                return len(self.output_devices) > 0
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass

        try:
            result = subprocess.run(
                ['aplay', '-l'],
                capture_output=True, text=True, timeout=2
            )
            if result.returncode == 0:
                self._parse_alsa_output(result.stdout)
                if len(self.output_devices) > 0:
                    self.default_output = self.output_devices[0]
                return len(self.output_devices) > 0
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass

        return False

    def _parse_alsa_output(self, output: str):
        """Parse aplay -l or arecord -l output for audio devices."""
        # Example output:
        # card 0: ALSA [ALSA], device 0: Dummy PCM [Dummy PCM]
        # card 1: Generic [Generic USB Audio Device], device 0: USB Audio [USB Audio]
        device_pattern = re.compile(
            r"card (\d+): ([^,]+),\s+device (\d+): ([^\[]+)\[([^\]]+)\]"
        )

        for match in device_pattern.finditer(output):
            card_id = match.group(1)
            card_name = match.group(2).strip()
            device_id = match.group(3)
            device_name = match.group(4).strip()
            device_desc = match.group(5).strip()

            full_device_id = f"hw:{card_id},{device_id}"
            full_name = f"{card_name} - {device_name}"

            device = AudioDevice(
                name=full_name,
                device_id=full_device_id,
                is_input=True,
                is_output=True,
                channels=2,
                sample_rate=44100,
                description=f"ALSA {device_desc}"
            )

            if device not in self.output_devices:
                self.output_devices.append(device)

    def _detect_pulseaudio(self) -> bool:
        """Detect audio devices using PulseAudio (Linux)."""
        try:
            result = subprocess.run(
                ['pactl', 'list', 'sinks'],
                capture_output=True, text=True, timeout=2
            )
            if result.returncode == 0:
                self._parse_pulseaudio_output(result.stdout)
                if len(self.output_devices) > 0:
                    self.default_output = self.output_devices[0]
                return len(self.output_devices) > 0
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass

        return False

    def _parse_pulseaudio_output(self, output: str):
        """Parse pactl list sinks output."""
        # Example output:
        # Sink #0
        #  Name: alsa_output.pci-0000_00_1b.0.analog-stereo
        #  Description: Built-in Audio Analog Stereo
        device_pattern = re.compile(r"Sink #(\d+)")
        name_pattern = re.compile(r"\s+Name:\s+(.+)")
        desc_pattern = re.compile(r"\s+Description:\s+(.+)")

        current_id = None
        current_name = None
        current_desc = None

        for line in output.split('\n'):
            device_match = device_pattern.match(line)
            if device_match:
                if current_id is not None:
                    self._add_pulseaudio_device(
                        current_id, current_name, current_desc
                    )
                current_id = device_match.group(1)
                current_name = None
                current_desc = None
                continue

            name_match = name_pattern.match(line)
            if name_match:
                current_name = name_match.group(1)
                continue

            desc_match = desc_pattern.match(line)
            if desc_match:
                current_desc = desc_match.group(1)

        if current_id is not None:
            self._add_pulseaudio_device(current_id, current_name, current_desc)

    def _add_pulseaudio_device(self, device_id: str, name: Optional[str],
                              desc: Optional[str]):
        """Add a PulseAudio device to output list."""
        if name:
            device = AudioDevice(
                name=name,
                device_id=f"pulse:{device_id}",
                is_input=False,
                is_output=True,
                channels=2,
                sample_rate=44100,
                description=desc or f"PulseAudio device {device_id}"
            )
            self.output_devices.append(device)

    def _detect_win32(self) -> bool:
        """Detect audio devices using Windows APIs (Windows)."""
        if sys.platform != "win32":
            return False

        try:
            # Windows WASAPI device enumeration would go here
            # For now, rely on sounddevice/pyaudio fallback
            pass
        except Exception:
            pass

        return False

    def _detect_coreaudio(self) -> bool:
        """Detect audio devices using CoreAudio (macOS)."""
        if sys.platform != "darwin":
            return False

        try:
            # macOS CoreAudio enumeration would go here
            # For now, rely on sounddevice/pyaudio fallback
            pass
        except Exception:
            pass

        return False

    def _add_default_device(self):
        """Add a default device when no others are found."""
        default_device = AudioDevice(
            name="Default Audio Device",
            device_id="default",
            is_input=True,
            is_output=True,
            channels=2,
            sample_rate=44100,
            is_default=True,
            description="System default audio device"
        )
        self.output_devices.append(default_device)
        self.input_devices.append(default_device)
        self.default_output = default_device
        self.default_input = default_device

    def get_output_devices(self) -> List[AudioDevice]:
        """Get list of output devices."""
        if not self.output_devices:
            self._refresh()
        return self.output_devices

    def get_input_devices(self) -> List[AudioDevice]:
        """Get list of input devices."""
        if not self.input_devices:
            self._refresh()
        return self.input_devices

    def get_default_output(self) -> Optional[AudioDevice]:
        """Get the default output device."""
        if not self.default_output and not self.output_devices:
            self._refresh()
        return self.default_output or (
            self.output_devices[0] if self.output_devices else None
        )

    def get_default_input(self) -> Optional[AudioDevice]:
        """Get the default input device."""
        if not self.default_input and not self.input_devices:
            self._refresh()
        return self.default_input or (
            self.input_devices[0] if self.input_devices else None
        )

    def get_device_by_id(self, device_id: str) -> Optional[AudioDevice]:
        """Get a specific device by ID."""
        for device in self.output_devices + self.input_devices:
            if device.device_id == device_id:
                return device
        return None


# Global singleton instance
_audio_device_manager: Optional[AudioDeviceManager] = None


def get_audio_devices() -> AudioDeviceManager:
    """Get the global audio device manager (singleton)."""
    global _audio_device_manager
    if _audio_device_manager is None:
        _audio_device_manager = AudioDeviceManager()
    return _audio_device_manager
