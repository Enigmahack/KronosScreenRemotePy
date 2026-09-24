"""MIDI device detection and management."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, List
import subprocess
import re


@dataclass
class MidiDevice:
    """Represents a single MIDI input or output device."""
    name: str
    device_id: str
    is_input: bool
    is_virtual: bool = False
    online: bool = True
    description: str = ""


class MidiDeviceManager:
    """Enumerate and manage MIDI devices on the system."""

    def __init__(self):
        self.input_devices: List[MidiDevice] = []
        self.output_devices: List[MidiDevice] = []
        self._refresh()

    def _refresh(self):
        """Refresh the list of available MIDI devices."""
        self.input_devices = []
        self.output_devices = []

        # Try platform-specific enumeration
        if self._detect_alsa():
            return
        if self._detect_portaudio():
            return
        if self._detect_win32():
            return

        # Fallback: no devices found

    def _detect_alsa(self) -> bool:
        """Detect MIDI devices using ALSA (Linux)."""
        try:
            # Try aconnect if available
            result = subprocess.run(['aconnect', '-l'],
                                   capture_output=True, text=True, timeout=2)
            if result.returncode == 0:
                self._parse_alsa_output(result.stdout)
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass

        try:
            # Fallback: check /proc/asound
            import os
            if os.path.exists('/proc/asound/cards'):
                with open('/proc/asound/cards') as f:
                    self._parse_asound_cards(f.read())
                return True
        except (FileNotFoundError, PermissionError):
            pass

        return False

    def _parse_alsa_output(self, output: str):
        """Parse aconnect -l output for MIDI devices."""
        # Example output:
        # client 14: 'Midi Through' [kernel]
        #   0 'Midi Through Port-0'
        # client 16: 'MPK mini' [kernel]
        #   0 'MPK mini MIDI 1'

        client_pattern = re.compile(r"client (\d+): '([^']+)'")
        port_pattern = re.compile(r"\s+(\d+) '([^']+)'")

        current_client = None
        for line in output.split('\n'):
            client_match = client_pattern.match(line)
            if client_match:
                current_client = (client_match.group(1), client_match.group(2))
                continue

            port_match = port_pattern.match(line)
            if port_match and current_client:
                device_id = f"{current_client[0]}:{port_match.group(1)}"
                device_name = f"{current_client[1]} - {port_match.group(2)}"

                # Heuristic: if it contains "Through", it's probably a virtual device
                is_virtual = "Through" in device_name

                device = MidiDevice(
                    name=device_name,
                    device_id=device_id,
                    is_input=True,  # ALSA doesn't distinguish in this output
                    is_virtual=is_virtual,
                    description=f"ALSA device {device_id}"
                )
                self.input_devices.append(device)

    def _parse_asound_cards(self, content: str):
        """Parse /proc/asound/cards for MIDI devices."""
        # Look for MIDI card entries
        for line in content.split('\n'):
            if 'MIDI' in line.upper():
                # Extract device info
                device = MidiDevice(
                    name=line.strip(),
                    device_id=f"alsa_card_{len(self.input_devices)}",
                    is_input=True,
                    description="ALSA MIDI device"
                )
                self.input_devices.append(device)

    def _detect_portaudio(self) -> bool:
        """Detect MIDI devices using portaudio/portmidi."""
        try:
            import pygame
            pygame.init()
            pygame.midi.init()

            in_count = pygame.midi.get_count()
            for i in range(in_count):
                info = pygame.midi.get_device_info(i)
                device = MidiDevice(
                    name=info[1].decode() if isinstance(info[1], bytes) else info[1],
                    device_id=f"portmidi_{i}",
                    is_input=bool(info[2]),  # direction
                    description=f"PortMIDI device {i}"
                )
                if info[2]:  # is_input
                    self.input_devices.append(device)
                else:
                    self.output_devices.append(device)

            pygame.midi.quit()
            return len(self.input_devices) > 0 or len(self.output_devices) > 0
        except (ImportError, OSError):
            return False

    def _detect_win32(self) -> bool:
        """Detect MIDI devices on Windows."""
        try:
            import mido
            for device_name in mido.get_input_names():
                device = MidiDevice(
                    name=device_name,
                    device_id=device_name,
                    is_input=True,
                    description=f"Windows MIDI input device"
                )
                self.input_devices.append(device)

            for device_name in mido.get_output_names():
                device = MidiDevice(
                    name=device_name,
                    device_id=device_name,
                    is_input=False,
                    description=f"Windows MIDI output device"
                )
                self.output_devices.append(device)

            return len(self.input_devices) > 0 or len(self.output_devices) > 0
        except ImportError:
            return False

    def get_input_devices(self) -> List[MidiDevice]:
        """Get list of MIDI input devices."""
        return self.input_devices

    def get_output_devices(self) -> List[MidiDevice]:
        """Get list of MIDI output devices."""
        return self.output_devices

    def get_device_by_name(self, name: str, is_input: bool = True) -> Optional[MidiDevice]:
        """Find device by name."""
        devices = self.input_devices if is_input else self.output_devices
        for device in devices:
            if device.name == name:
                return device
        return None

    def get_device_by_id(self, device_id: str) -> Optional[MidiDevice]:
        """Find device by ID."""
        for device in self.input_devices + self.output_devices:
            if device.device_id == device_id:
                return device
        return None


# Global instance
_device_manager: Optional[MidiDeviceManager] = None


def get_midi_devices() -> MidiDeviceManager:
    """Get the global MIDI device manager."""
    global _device_manager
    if _device_manager is None:
        _device_manager = MidiDeviceManager()
    return _device_manager
