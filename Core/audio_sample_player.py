"""Sample file playback engine.

Handles loading and playback of audio sample files:
- WAV file support (primary)
- Raw PCM support (fallback)
- Format detection and parsing
- Playback controls (play, pause, stop, seek)
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import struct
import logging
import threading
import time

from Core.audio_streaming import get_audio_streaming, AudioStreamBuffer
from Core.audio_engine import AudioConfig

log = logging.getLogger(__name__)


@dataclass
class SampleInfo:
    """Information about an audio sample file."""
    filename: str
    duration_sec: float
    sample_rate: int
    channels: int
    bit_depth: int
    format: str  # 'WAV', 'RAW', etc.
    data_size: int  # bytes


@dataclass
class PlaybackState:
    """Current playback state."""
    is_playing: bool = False
    is_paused: bool = False
    position_sec: float = 0.0
    duration_sec: float = 0.0


class WavFileReader:
    """Reader for WAV format audio files."""

    @staticmethod
    def read_header(filepath: Path) -> Optional[SampleInfo]:
        """Read WAV file header and extract sample info.

        Args:
            filepath: Path to WAV file

        Returns:
            SampleInfo if valid WAV, None otherwise
        """
        try:
            with open(filepath, 'rb') as f:
                # Read RIFF header
                riff = f.read(4)
                if riff != b'RIFF':
                    return None

                file_size = struct.unpack('<I', f.read(4))[0]
                wave = f.read(4)
                if wave != b'WAVE':
                    return None

                # Find fmt chunk
                fmt_data = None
                data_pos = 0
                data_size = 0

                while True:
                    chunk_id = f.read(4)
                    if not chunk_id:
                        break

                    chunk_size = struct.unpack('<I', f.read(4))[0]

                    if chunk_id == b'fmt ':
                        fmt_data = f.read(chunk_size)
                    elif chunk_id == b'data':
                        data_pos = f.tell()
                        data_size = chunk_size
                        break
                    else:
                        f.seek(chunk_size, 1)  # Skip unknown chunk

                if not fmt_data or data_size == 0:
                    return None

                # Parse fmt chunk
                if len(fmt_data) < 16:
                    return None

                audio_format, channels, sample_rate, byte_rate, block_align, bits_per_sample = \
                    struct.unpack('<HHIIHH', fmt_data[:16])

                if audio_format != 1:  # PCM only
                    return None

                duration_sec = data_size / (sample_rate * channels * bits_per_sample // 8)

                return SampleInfo(
                    filename=filepath.name,
                    duration_sec=duration_sec,
                    sample_rate=sample_rate,
                    channels=channels,
                    bit_depth=bits_per_sample,
                    format='WAV',
                    data_size=data_size
                )

        except Exception as e:
            log.error(f"Error reading WAV file: {e}")
            return None

    @staticmethod
    def read_samples(filepath: Path, start_byte: int, num_bytes: int) -> Optional[bytes]:
        """Read audio samples from WAV file.

        Args:
            filepath: Path to WAV file
            start_byte: Starting byte offset in data chunk
            num_bytes: Number of bytes to read

        Returns:
            Audio data or None on error
        """
        try:
            with open(filepath, 'rb') as f:
                # Find data chunk
                while True:
                    chunk_id = f.read(4)
                    if not chunk_id:
                        return None

                    chunk_size = struct.unpack('<I', f.read(4))[0]

                    if chunk_id == b'data':
                        # Found data chunk
                        f.seek(start_byte, 1)  # Relative to after size field
                        return f.read(num_bytes)
                    else:
                        f.seek(chunk_size, 1)

        except Exception as e:
            log.error(f"Error reading WAV samples: {e}")
            return None


class SamplePlayer:
    """Manages playback of audio sample files."""

    def __init__(self):
        """Initialize sample player."""
        self.current_file: Optional[Path] = None
        self.sample_info: Optional[SampleInfo] = None
        self.playback_state = PlaybackState()
        self.streaming_service = get_audio_streaming()

        # Playback thread
        self.playback_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()

        # Playback configuration
        self.loop_mode = "none"  # 'none', 'repeat', 'bounce'
        self.playback_speed = 1.0  # Pitch-neutral speed

        # Callbacks
        self.position_callback: Optional[callable] = None
        self.end_callback: Optional[callable] = None

    def load_sample(self, filepath: Path) -> bool:
        """Load an audio sample file.

        Args:
            filepath: Path to audio file

        Returns:
            True if loaded successfully, False otherwise
        """
        # Stop current playback
        self.stop()

        # Try WAV format first
        info = WavFileReader.read_header(filepath)

        if not info:
            log.error(f"Unsupported audio format: {filepath}")
            return False

        self.current_file = filepath
        self.sample_info = info
        self.playback_state.duration_sec = info.duration_sec
        self.playback_state.position_sec = 0.0

        log.info(f"Loaded sample: {info.filename} "
                f"({info.sample_rate}Hz, {info.channels}ch, {info.duration_sec:.2f}s)")
        return True

    def play(self) -> bool:
        """Start or resume playback.

        Returns:
            True if playback started, False otherwise
        """
        if not self.current_file or not self.sample_info:
            log.warning("No sample loaded")
            return False

        if self.playback_state.is_playing:
            return True  # Already playing

        if self.playback_state.is_paused:
            # Resume from pause
            self.playback_state.is_paused = False
            self.playback_state.is_playing = True
            self._resume_playback_thread()
            return True

        # Start new playback
        config = AudioConfig(
            sample_rate=self.sample_info.sample_rate,
            channels=self.sample_info.channels,
            buffer_size=2048,
        )

        if not self.streaming_service.start_streaming(config):
            log.error("Failed to start audio streaming")
            return False

        self.playback_state.is_playing = True
        self.playback_state.position_sec = 0.0
        self.stop_event.clear()

        # Start playback thread
        self.playback_thread = threading.Thread(
            target=self._playback_loop,
            daemon=True,
            name="SamplePlayback"
        )
        self.playback_thread.start()

        log.info(f"Started playback: {self.sample_info.filename}")
        return True

    def pause(self):
        """Pause playback."""
        if self.playback_state.is_playing:
            self.playback_state.is_playing = False
            self.playback_state.is_paused = True
            log.info("Playback paused")

    def stop(self):
        """Stop playback."""
        self.playback_state.is_playing = False
        self.playback_state.is_paused = False
        self.stop_event.set()

        if self.playback_thread and self.playback_thread.is_alive():
            self.playback_thread.join(timeout=2.0)

        self.streaming_service.stop_streaming()
        self.playback_state.position_sec = 0.0

        log.info("Playback stopped")

    def seek(self, position_sec: float) -> bool:
        """Seek to a position in the file.

        Args:
            position_sec: Position in seconds

        Returns:
            True if seek successful, False otherwise
        """
        if not self.sample_info:
            return False

        position_sec = max(0.0, min(position_sec, self.sample_info.duration_sec))
        self.playback_state.position_sec = position_sec

        # Calculate byte offset
        bytes_per_frame = self.sample_info.channels * self.sample_info.bit_depth // 8
        byte_offset = int(position_sec * self.sample_info.sample_rate * bytes_per_frame)

        log.info(f"Seek to {position_sec:.2f}s (byte offset: {byte_offset})")
        return True

    def _resume_playback_thread(self):
        """Resume existing playback thread (for pause/resume)."""
        self.stop_event.clear()
        if not self.playback_thread or not self.playback_thread.is_alive():
            self.playback_thread = threading.Thread(
                target=self._playback_loop,
                daemon=True,
                name="SamplePlayback"
            )
            self.playback_thread.start()

    def _playback_loop(self):
        """Main playback loop."""
        if not self.current_file or not self.sample_info:
            return

        chunk_size = 4096  # bytes
        bytes_per_frame = self.sample_info.channels * self.sample_info.bit_depth // 8
        byte_offset = int(self.playback_state.position_sec *
                         self.sample_info.sample_rate * bytes_per_frame)

        while self.playback_state.is_playing and not self.stop_event.is_set():
            # Check for pause
            if self.playback_state.is_paused:
                time.sleep(0.01)
                continue

            # Read chunk from file
            audio_data = WavFileReader.read_samples(
                self.current_file, byte_offset, chunk_size
            )

            if not audio_data:
                # End of file reached
                self._handle_end_of_file()
                break

            # Write to streaming buffer
            self.streaming_service.write_samples(audio_data)

            # Update position
            byte_offset += len(audio_data)
            self.playback_state.position_sec = byte_offset / (
                self.sample_info.sample_rate * bytes_per_frame
            )

            # Emit callback
            if self.position_callback:
                self.position_callback(self.playback_state.position_sec)

            # Sleep to avoid hammering
            time.sleep(0.001)

    def _handle_end_of_file(self):
        """Handle end of file reached."""
        self.playback_state.is_playing = False

        # Check loop mode
        if self.loop_mode == "repeat":
            # Loop from start
            self.playback_state.position_sec = 0.0
            self.playback_state.is_playing = True
            # Playback loop will continue
            return
        elif self.loop_mode == "bounce":
            # Bounce back to start
            self.playback_state.position_sec = 0.0
            self.playback_state.is_playing = True
            return

        # End of playback
        log.info("End of file reached")
        if self.end_callback:
            self.end_callback()

    def get_state(self) -> PlaybackState:
        """Get current playback state."""
        return PlaybackState(
            is_playing=self.playback_state.is_playing,
            is_paused=self.playback_state.is_paused,
            position_sec=self.playback_state.position_sec,
            duration_sec=self.playback_state.duration_sec
        )

    def set_loop_mode(self, mode: str):
        """Set loop mode ('none', 'repeat', 'bounce')."""
        if mode in ('none', 'repeat', 'bounce'):
            self.loop_mode = mode
            log.info(f"Loop mode: {mode}")


# Global singleton instance
_sample_player: Optional[SamplePlayer] = None


def get_sample_player() -> SamplePlayer:
    """Get the global sample player (singleton)."""
    global _sample_player
    if _sample_player is None:
        _sample_player = SamplePlayer()
    return _sample_player
