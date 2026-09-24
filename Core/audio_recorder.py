"""Audio recording engine with file output.

Handles recording from audio input devices:
- Input device selection
- Format configuration
- File writing (WAV format)
- Real-time level monitoring
- Recording statistics
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Callable
import struct
import threading
import logging
import time

from Core.audio_engine import get_audio_devices, AudioConfig

log = logging.getLogger(__name__)


@dataclass
class RecordingStats:
    """Statistics about an active recording."""
    duration_sec: float = 0.0
    bytes_recorded: int = 0
    peak_level: float = 0.0
    input_underruns: int = 0
    file_writes: int = 0


class WavFileWriter:
    """Writer for WAV format audio files."""

    def __init__(self, filepath: Path, sample_rate: int, channels: int,
                 bit_depth: int = 16):
        """Initialize WAV file writer.

        Args:
            filepath: Path to output WAV file
            sample_rate: Sample rate in Hz
            channels: Number of audio channels
            bit_depth: Bits per sample (16, 24, or 32)
        """
        self.filepath = filepath
        self.sample_rate = sample_rate
        self.channels = channels
        self.bit_depth = bit_depth
        self.bytes_written = 0
        self.file = None

    def open(self) -> bool:
        """Open WAV file for writing.

        Returns:
            True if opened successfully, False otherwise
        """
        try:
            self.file = open(self.filepath, 'wb')

            # Write RIFF header (placeholder sizes, will update on close)
            self.file.write(b'RIFF')
            self.file.write(struct.pack('<I', 0))  # Placeholder file size
            self.file.write(b'WAVE')

            # Write fmt chunk
            fmt_chunk_size = 16
            audio_format = 1  # PCM
            byte_rate = self.sample_rate * self.channels * self.bit_depth // 8
            block_align = self.channels * self.bit_depth // 8

            self.file.write(b'fmt ')
            self.file.write(struct.pack('<I', fmt_chunk_size))
            self.file.write(struct.pack('<H', audio_format))
            self.file.write(struct.pack('<H', self.channels))
            self.file.write(struct.pack('<I', self.sample_rate))
            self.file.write(struct.pack('<I', byte_rate))
            self.file.write(struct.pack('<H', block_align))
            self.file.write(struct.pack('<H', self.bit_depth))

            # Write data chunk header (placeholder size)
            self.file.write(b'data')
            self.file.write(struct.pack('<I', 0))  # Placeholder data size

            self.bytes_written = self.file.tell()
            log.info(f"Opened WAV file for recording: {self.filepath}")
            return True

        except Exception as e:
            log.error(f"Error opening WAV file: {e}")
            if self.file:
                self.file.close()
            return False

    def write(self, data: bytes) -> int:
        """Write audio data to file.

        Args:
            data: Audio sample data

        Returns:
            Number of bytes written
        """
        if not self.file:
            return 0

        try:
            self.file.write(data)
            self.bytes_written += len(data)
            return len(data)
        except Exception as e:
            log.error(f"Error writing audio data: {e}")
            return 0

    def close(self):
        """Close WAV file and update headers.

        Must be called to finalize the file.
        """
        if not self.file:
            return

        try:
            # Calculate data chunk size
            data_size = self.bytes_written - 36  # Subtract header size

            # Update RIFF file size
            self.file.seek(4)
            self.file.write(struct.pack('<I', 36 + data_size))

            # Update data chunk size
            self.file.seek(40)
            self.file.write(struct.pack('<I', data_size))

            self.file.close()
            self.file = None

            duration_sec = data_size / (
                self.sample_rate * self.channels * self.bit_depth // 8
            )
            log.info(f"Closed WAV file: {self.filepath.name} "
                    f"({duration_sec:.2f}s, {data_size} bytes)")

        except Exception as e:
            log.error(f"Error closing WAV file: {e}")
            if self.file:
                self.file.close()
                self.file = None

    def __del__(self):
        """Ensure file is closed on deletion."""
        if self.file:
            try:
                self.close()
            except Exception:
                pass


class AudioRecorder:
    """Records audio from input devices to files."""

    def __init__(self):
        """Initialize audio recorder."""
        self.is_recording = False
        self.config: Optional[AudioConfig] = None
        self.output_file: Optional[WavFileWriter] = None
        self.audio_mgr = get_audio_devices()

        # Statistics
        self.stats = RecordingStats()
        self.stats_lock = threading.Lock()

        # Recording thread
        self.record_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()

        # Callbacks
        self.level_callback: Optional[Callable[[float, float], None]] = None
        self.stats_callback: Optional[Callable[[RecordingStats], None]] = None

        # Audio backends
        self._sounddevice_available = self._try_import_sounddevice()
        self._pyaudio_available = self._try_import_pyaudio()

    def _try_import_sounddevice(self) -> bool:
        """Try to import sounddevice library."""
        try:
            import sounddevice
            self.sd = sounddevice
            return True
        except (ImportError, OSError):
            return False

    def _try_import_pyaudio(self) -> bool:
        """Try to import PyAudio library."""
        try:
            import pyaudio
            self.pa = pyaudio.PyAudio()
            return True
        except (ImportError, Exception):
            return False

    def start_recording(self, output_path: Path, config: AudioConfig) -> bool:
        """Start recording audio to file.

        Args:
            output_path: Path to output WAV file
            config: Audio configuration (device, sample rate, channels, etc.)

        Returns:
            True if recording started, False otherwise
        """
        if self.is_recording:
            log.warning("Recording already active")
            return False

        # Create output file
        wav_writer = WavFileWriter(
            output_path,
            sample_rate=config.sample_rate,
            channels=config.channels,
            bit_depth=16
        )

        if not wav_writer.open():
            return False

        self.config = config
        self.output_file = wav_writer
        self.stats = RecordingStats()

        # Start recording thread
        self.is_recording = True
        self.stop_event.clear()
        self.record_thread = threading.Thread(
            target=self._recording_loop,
            daemon=True,
            name="AudioRecording"
        )
        self.record_thread.start()

        log.info(f"Started recording to {output_path.name}: "
                f"{config.sample_rate}Hz, {config.channels}ch")
        return True

    def stop_recording(self) -> RecordingStats:
        """Stop recording and finalize file.

        Returns:
            Recording statistics
        """
        if not self.is_recording:
            return self.stats

        self.is_recording = False
        self.stop_event.set()

        # Wait for thread
        if self.record_thread and self.record_thread.is_alive():
            self.record_thread.join(timeout=2.0)

        # Close file
        if self.output_file:
            self.output_file.close()
            self.output_file = None

        log.info(f"Recording stopped: {self.stats.duration_sec:.2f}s recorded")
        return self.stats

    def _recording_loop(self):
        """Main recording loop - reads from input device."""
        if not self.config:
            return

        # Attempt to create input stream
        stream = None
        try:
            if self._sounddevice_available:
                stream = self._create_sounddevice_stream()
            elif self._pyaudio_available:
                stream = self._create_pyaudio_stream()

            if not stream:
                log.error("Failed to create input stream")
                self.is_recording = False
                return

            # Recording loop
            chunk_frames = self.config.buffer_size
            bytes_per_frame = self.config.channels * 2  # 16-bit

            start_time = time.time()

            while self.is_recording and not self.stop_event.is_set():
                try:
                    # Read from input
                    if hasattr(stream, 'read'):
                        audio_data = stream.read(chunk_frames, exception_on_overflow=False)

                        # Convert to bytes if needed
                        if hasattr(audio_data, 'tobytes'):
                            audio_bytes = audio_data.tobytes()
                        else:
                            audio_bytes = audio_data

                        # Write to file
                        if self.output_file:
                            bytes_written = self.output_file.write(audio_bytes)

                            # Update statistics
                            with self.stats_lock:
                                self.stats.bytes_recorded += bytes_written
                                self.stats.duration_sec = time.time() - start_time
                                self.stats.file_writes += 1
                                self._update_peak_level(audio_bytes)

                            # Emit callbacks
                            if self.stats_callback:
                                with self.stats_lock:
                                    self.stats_callback(self.stats)

                except Exception as e:
                    log.error(f"Error reading audio: {e}")
                    self.is_recording = False
                    break

        finally:
            # Cleanup
            if stream:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass

    def _create_sounddevice_stream(self):
        """Create a sounddevice input stream."""
        try:
            device_id = None
            if self.config.device_id != "default":
                try:
                    device_id = int(self.config.device_id)
                except (ValueError, TypeError):
                    device_id = None

            return self.sd.InputStream(
                device=device_id,
                samplerate=self.config.sample_rate,
                channels=self.config.channels,
                blocksize=self.config.buffer_size,
            )
        except Exception as e:
            log.error(f"Failed to create sounddevice stream: {e}")
            return None

    def _create_pyaudio_stream(self):
        """Create a PyAudio input stream."""
        try:
            device_id = None
            if self.config.device_id != "default":
                try:
                    device_id = int(self.config.device_id)
                except (ValueError, TypeError):
                    device_id = None

            return self.pa.open(
                format=self.pa.get_format_from_width(2),  # 16-bit
                channels=self.config.channels,
                rate=self.config.sample_rate,
                input=True,
                input_device_index=device_id,
                frames_per_buffer=self.config.buffer_size,
            )
        except Exception as e:
            log.error(f"Failed to create PyAudio stream: {e}")
            return None

    def _update_peak_level(self, audio_data: bytes):
        """Update peak level from audio data."""
        try:
            import struct

            max_sample = 32768  # Max for 16-bit
            peak = 0.0

            for i in range(0, len(audio_data) - 1, 2):
                value = struct.unpack('<h', audio_data[i:i+2])[0]
                peak = max(peak, abs(value) / max_sample)

            self.stats.peak_level = max(self.stats.peak_level, peak)

        except Exception:
            pass

    def get_stats(self) -> RecordingStats:
        """Get current recording statistics."""
        with self.stats_lock:
            return RecordingStats(
                duration_sec=self.stats.duration_sec,
                bytes_recorded=self.stats.bytes_recorded,
                peak_level=self.stats.peak_level,
                input_underruns=self.stats.input_underruns,
                file_writes=self.stats.file_writes,
            )

    def set_level_callback(self, callback: Optional[Callable[[float, float], None]]):
        """Set callback for input level updates."""
        self.level_callback = callback

    def set_stats_callback(self, callback: Optional[Callable[[RecordingStats], None]]):
        """Set callback for recording statistics."""
        self.stats_callback = callback

    def __del__(self):
        """Cleanup on destruction."""
        try:
            if self.is_recording:
                self.stop_recording()
        except Exception:
            pass

        try:
            if self._pyaudio_available and hasattr(self, 'pa'):
                self.pa.terminate()
        except Exception:
            pass


# Global singleton instance
_audio_recorder: Optional[AudioRecorder] = None


def get_audio_recorder() -> AudioRecorder:
    """Get the global audio recorder (singleton)."""
    global _audio_recorder
    if _audio_recorder is None:
        _audio_recorder = AudioRecorder()
    return _audio_recorder
