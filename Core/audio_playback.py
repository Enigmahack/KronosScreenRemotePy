"""Audio playback engine with streaming support.

Provides real-time audio output with:
- Device selection
- Stream management
- Automatic fallback
- Level monitoring
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Callable
import threading
import logging

from Core.audio_engine import AudioConfig, get_audio_devices

log = logging.getLogger(__name__)


@dataclass
class AudioFrame:
    """A single audio frame for playback."""
    data: bytes
    num_frames: int
    sample_rate: int
    channels: int


class AudioPlaybackEngine:
    """Manages audio playback with device selection and streaming."""

    def __init__(self):
        """Initialize audio playback engine."""
        self.is_playing = False
        self.current_config: Optional[AudioConfig] = None
        self.audio_mgr = get_audio_devices()
        self.stream = None
        self._stream_lock = threading.Lock()

        # Audio processing
        self.level_callback: Optional[Callable[[float, float], None]] = None
        self._level_left = 0.0
        self._level_right = 0.0

        # Attempt to initialize available audio libraries
        self._sounddevice_available = self._try_import_sounddevice()
        self._pyaudio_available = self._try_import_pyaudio()

        if not self._sounddevice_available and not self._pyaudio_available:
            log.warning("No audio playback libraries available (install sounddevice or pyaudio)")

    def _try_import_sounddevice(self) -> bool:
        """Try to import sounddevice library."""
        try:
            import sounddevice
            self.sd = sounddevice
            log.info("sounddevice library available for audio playback")
            return True
        except (ImportError, OSError) as e:
            log.debug(f"sounddevice not available: {e}")
            return False

    def _try_import_pyaudio(self) -> bool:
        """Try to import PyAudio library."""
        try:
            import pyaudio
            try:
                self.pa = pyaudio.PyAudio()
                log.info("PyAudio library available for audio playback")
                return True
            except Exception as e:
                log.debug(f"PyAudio initialization failed: {e}")
                return False
        except ImportError:
            return False

    def start_playback(self, config: AudioConfig) -> bool:
        """Start audio playback with the given configuration.

        Args:
            config: Audio configuration (device, sample rate, channels, etc.)

        Returns:
            True if playback started, False if failed
        """
        with self._stream_lock:
            if self.is_playing:
                log.warning("Playback already active")
                return False

            self.current_config = config

            # Try sounddevice first (preferred)
            if self._sounddevice_available:
                if self._start_sounddevice_stream(config):
                    self.is_playing = True
                    return True

            # Fallback to PyAudio
            if self._pyaudio_available:
                if self._start_pyaudio_stream(config):
                    self.is_playing = True
                    return True

            log.error("Failed to start audio playback - no available backend")
            return False

    def stop_playback(self):
        """Stop audio playback."""
        with self._stream_lock:
            if self.stream:
                try:
                    self.stream.stop_stream()
                    self.stream.close()
                except Exception as e:
                    log.error(f"Error closing audio stream: {e}")

                self.stream = None

            self.is_playing = False

    def _start_sounddevice_stream(self, config: AudioConfig) -> bool:
        """Start a sounddevice audio stream."""
        try:
            device_id = None
            if config.device_id != "default":
                try:
                    device_id = int(config.device_id)
                except (ValueError, TypeError):
                    device_id = None

            self.stream = self.sd.OutputStream(
                device=device_id,
                samplerate=config.sample_rate,
                channels=config.channels,
                blocksize=config.buffer_size,
            )
            self.stream.start()
            log.info(f"Started sounddevice stream: {config.sample_rate}Hz, "
                    f"{config.channels}ch, buffer={config.buffer_size}")
            return True

        except Exception as e:
            log.error(f"Failed to start sounddevice stream: {e}")
            return False

    def _start_pyaudio_stream(self, config: AudioConfig) -> bool:
        """Start a PyAudio stream."""
        try:
            device_id = None
            if config.device_id != "default":
                try:
                    device_id = int(config.device_id)
                except (ValueError, TypeError):
                    device_id = None

            self.stream = self.pa.open(
                format=self.pa.get_format_from_width(2),  # 16-bit audio
                channels=config.channels,
                rate=config.sample_rate,
                output=True,
                output_device_index=device_id,
                frames_per_buffer=config.buffer_size,
            )
            log.info(f"Started PyAudio stream: {config.sample_rate}Hz, "
                    f"{config.channels}ch, buffer={config.buffer_size}")
            return True

        except Exception as e:
            log.error(f"Failed to start PyAudio stream: {e}")
            return False

    def write_frame(self, frame: AudioFrame) -> bool:
        """Write audio data to the playback stream.

        Args:
            frame: Audio frame data to play

        Returns:
            True if written successfully, False otherwise
        """
        if not self.is_playing or not self.stream:
            return False

        try:
            with self._stream_lock:
                if self._sounddevice_available and hasattr(self.stream, 'write'):
                    # sounddevice
                    import numpy as np
                    audio_data = np.frombuffer(frame.data, dtype=np.int16)
                    audio_data = audio_data.reshape(-1, frame.channels)
                    self.stream.write(audio_data)
                elif self._pyaudio_available and hasattr(self.stream, 'write'):
                    # PyAudio
                    self.stream.write(frame.data)

            # Update levels
            self._update_levels(frame)
            return True

        except Exception as e:
            log.error(f"Error writing audio frame: {e}")
            self.is_playing = False
            return False

    def _update_levels(self, frame: AudioFrame):
        """Update audio level indicators from frame data."""
        try:
            import struct
            import math

            # Parse 16-bit signed samples
            sample_size = 2  # 16-bit = 2 bytes
            num_samples = len(frame.data) // sample_size

            if num_samples == 0:
                return

            max_sample = 32768  # Max value for 16-bit signed
            levels = [0.0] * frame.channels

            for i in range(num_samples):
                sample_idx = i * sample_size
                if sample_idx + sample_size <= len(frame.data):
                    value = struct.unpack('<h', frame.data[sample_idx:sample_idx + 2])[0]
                    channel = i % frame.channels
                    levels[channel] = max(levels[channel], abs(value) / max_sample)

            # Update stored levels (with smoothing)
            alpha = 0.3  # Smoothing factor
            if frame.channels >= 1:
                self._level_left = alpha * levels[0] + (1 - alpha) * self._level_left
            if frame.channels >= 2:
                self._level_right = alpha * levels[1] + (1 - alpha) * self._level_right

            # Emit callback
            if self.level_callback:
                self.level_callback(self._level_left, self._level_right)

        except Exception as e:
            log.debug(f"Error updating audio levels: {e}")

    def get_levels(self) -> tuple[float, float]:
        """Get current audio levels (left, right)."""
        return self._level_left, self._level_right

    def set_level_callback(self, callback: Optional[Callable[[float, float], None]]):
        """Set callback for audio level updates."""
        self.level_callback = callback

    def __del__(self):
        """Cleanup on destruction."""
        try:
            self.stop_playback()
        except Exception:
            pass

        try:
            if hasattr(self, '_pyaudio_available') and self._pyaudio_available and hasattr(self, 'pa'):
                self.pa.terminate()
        except Exception:
            pass


# Global singleton instance
_audio_playback_engine: Optional[AudioPlaybackEngine] = None


def get_audio_playback() -> AudioPlaybackEngine:
    """Get the global audio playback engine (singleton)."""
    global _audio_playback_engine
    if _audio_playback_engine is None:
        _audio_playback_engine = AudioPlaybackEngine()
    return _audio_playback_engine
