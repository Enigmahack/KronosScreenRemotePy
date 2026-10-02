"""Audio streaming and continuous playback management.

Handles audio routing from Kronos device to local playback with:
- Stream buffering
- Sample rate conversion
- Channel routing
- Real-time monitoring
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Callable, Deque
from collections import deque
import threading
import logging
import time

from Core.audio_engine import AudioConfig
from Core.audio_playback import get_audio_playback

log = logging.getLogger(__name__)


@dataclass
class StreamStats:
    """Statistics about audio streaming."""
    bytes_received: int = 0
    bytes_played: int = 0
    frames_dropped: int = 0
    buffer_fill_percent: float = 0.0
    underruns: int = 0
    overruns: int = 0


class AudioStreamBuffer:
    """Thread-safe circular buffer for audio samples."""

    def __init__(self, max_frames: int, channels: int):
        """Initialize audio stream buffer.

        Args:
            max_frames: Maximum frames to buffer
            channels: Number of audio channels
        """
        self.max_frames = max_frames
        self.channels = channels
        self.max_bytes = max_frames * channels * 2  # 16-bit samples
        self.buffer: Deque[bytes] = deque()
        self.lock = threading.Lock()

    def write(self, data: bytes) -> int:
        """Write audio data to buffer.

        Args:
            data: Audio data to write

        Returns:
            Number of bytes written
        """
        with self.lock:
            current_size = sum(len(frame) for frame in self.buffer)

            if current_size + len(data) > self.max_bytes:
                # Buffer full - drop oldest frames
                bytes_to_drop = (current_size + len(data)) - self.max_bytes
                while bytes_to_drop > 0 and self.buffer:
                    frame = self.buffer.popleft()
                    bytes_to_drop -= len(frame)

            self.buffer.append(data)
            return len(data)

    def read(self, num_bytes: int) -> Optional[bytes]:
        """Read audio data from buffer.

        Args:
            num_bytes: Number of bytes to read

        Returns:
            Audio data or None if buffer empty
        """
        with self.lock:
            if not self.buffer:
                return None

            output = bytearray()
            bytes_needed = num_bytes

            while bytes_needed > 0 and self.buffer:
                frame = self.buffer[0]
                if len(frame) <= bytes_needed:
                    # Use entire frame
                    output.extend(frame)
                    self.buffer.popleft()
                    bytes_needed -= len(frame)
                else:
                    # Use part of frame
                    output.extend(frame[:bytes_needed])
                    # Update frame in place
                    self.buffer[0] = frame[bytes_needed:]
                    bytes_needed = 0

            return bytes(output) if output else None

    def available_bytes(self) -> int:
        """Get number of bytes available in buffer."""
        with self.lock:
            return sum(len(frame) for frame in self.buffer)

    def fill_percent(self) -> float:
        """Get buffer fill percentage (0-100)."""
        available = self.available_bytes()
        return (available / self.max_bytes * 100) if self.max_bytes > 0 else 0.0

    def clear(self):
        """Clear all buffered data."""
        with self.lock:
            self.buffer.clear()


class AudioStreamingService:
    """Manages continuous audio streaming from device to playback."""

    def __init__(self):
        """Initialize audio streaming service."""
        self.is_streaming = False
        self.config: Optional[AudioConfig] = None
        self.buffer: Optional[AudioStreamBuffer] = None
        self.playback_engine = get_audio_playback()

        # Statistics
        self.stats = StreamStats()
        self.stats_lock = threading.Lock()

        # Streaming thread
        self.stream_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()

        # Callbacks
        self.level_callback: Optional[Callable[[float, float], None]] = None
        self.stats_callback: Optional[Callable[[StreamStats], None]] = None

    def start_streaming(self, config: AudioConfig) -> bool:
        """Start audio streaming.

        Args:
            config: Audio configuration

        Returns:
            True if streaming started successfully
        """
        if self.is_streaming:
            log.warning("Streaming already active")
            return False

        self.config = config
        self.buffer = AudioStreamBuffer(
            max_frames=config.buffer_size * 4,  # 4x buffer size
            channels=config.channels
        )

        # Start playback engine
        if not self.playback_engine.start_playback(config):
            log.error("Failed to start playback engine")
            return False

        # Start streaming thread
        self.is_streaming = True
        self.stop_event.clear()
        self.stream_thread = threading.Thread(
            target=self._streaming_loop,
            daemon=True,
            name="AudioStreaming"
        )
        self.stream_thread.start()

        log.info(f"Audio streaming started: {config.sample_rate}Hz, {config.channels}ch")
        return True

    def stop_streaming(self):
        """Stop audio streaming."""
        if not self.is_streaming:
            return

        self.is_streaming = False
        self.stop_event.set()

        # Wait for thread to finish
        if self.stream_thread and self.stream_thread.is_alive():
            self.stream_thread.join(timeout=2.0)

        # Stop playback
        self.playback_engine.stop_playback()

        if self.buffer:
            self.buffer.clear()

        log.info("Audio streaming stopped")

    def write_samples(self, data: bytes):
        """Write audio samples from device to streaming buffer.

        Args:
            data: Audio sample data to stream
        """
        if not self.is_streaming or not self.buffer:
            return

        bytes_written = self.buffer.write(data)

        with self.stats_lock:
            self.stats.bytes_received += bytes_written

    def _streaming_loop(self):
        """Main streaming loop - reads from buffer and plays audio."""
        if not self.config or not self.buffer:
            return

        # Frame size in bytes (sample_rate / frames_per_second)
        frame_size = self.config.channels * 2  # 16-bit samples
        target_frames = max(1, self.config.buffer_size // 4)
        chunk_size = frame_size * target_frames

        underrun_threshold = 0.1  # 10% fill triggers underrun warning

        while self.is_streaming:
            try:
                # Read from buffer
                audio_data = self.buffer.read(chunk_size)

                if audio_data is None or len(audio_data) == 0:
                    # Buffer underrun - sleep briefly
                    with self.stats_lock:
                        self.stats.underruns += 1
                    time.sleep(0.001)
                    continue

                # Write to playback engine
                if not self.playback_engine.write_frame(
                    type('Frame', (), {
                        'data': audio_data,
                        'num_frames': len(audio_data) // frame_size,
                        'sample_rate': self.config.sample_rate,
                        'channels': self.config.channels,
                    })()
                ):
                    self.is_streaming = False
                    break

                # Update statistics
                with self.stats_lock:
                    self.stats.bytes_played += len(audio_data)
                    self.stats.buffer_fill_percent = self.buffer.fill_percent()

                # Check for overruns
                if self.stats.buffer_fill_percent > 90:
                    with self.stats_lock:
                        self.stats.overruns += 1

                # Emit callbacks
                if self.level_callback:
                    levels = self.playback_engine.get_levels()
                    self.level_callback(levels[0], levels[1])

                if self.stats_callback:
                    with self.stats_lock:
                        self.stats_callback(self.stats)

            except Exception as e:
                log.error(f"Error in streaming loop: {e}")
                self.is_streaming = False
                break

    def get_stats(self) -> StreamStats:
        """Get current streaming statistics."""
        with self.stats_lock:
            return StreamStats(
                bytes_received=self.stats.bytes_received,
                bytes_played=self.stats.bytes_played,
                frames_dropped=self.stats.frames_dropped,
                buffer_fill_percent=self.stats.buffer_fill_percent,
                underruns=self.stats.underruns,
                overruns=self.stats.overruns,
            )

    def set_level_callback(self, callback: Optional[Callable[[float, float], None]]):
        """Set callback for audio level updates."""
        self.level_callback = callback

    def set_stats_callback(self, callback: Optional[Callable[[StreamStats], None]]):
        """Set callback for streaming statistics updates."""
        self.stats_callback = callback


# Global singleton instance
_audio_streaming_service: Optional[AudioStreamingService] = None


def get_audio_streaming() -> AudioStreamingService:
    """Get the global audio streaming service (singleton)."""
    global _audio_streaming_service
    if _audio_streaming_service is None:
        _audio_streaming_service = AudioStreamingService()
    return _audio_streaming_service
