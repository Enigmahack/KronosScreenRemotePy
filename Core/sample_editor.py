"""Sample editing operations and utilities.

Implements:
- Sample selection and manipulation
- Basic DSP operations (trim, normalize, reverse)
- Fade in/fade out curves
- Copy/paste operations
"""
from __future__ import annotations
from typing import Optional, Tuple, List
from dataclasses import dataclass
import struct
import math
import logging

log = logging.getLogger(__name__)


@dataclass
class AudioCue:
    """Marker point in audio."""
    name: str
    position_sec: float
    color: str = "#FF9999"  # RGB hex color

    def __lt__(self, other):
        return self.position_sec < other.position_sec


@dataclass
class AudioRegion:
    """Named region/range in audio."""
    name: str
    start_sec: float
    end_sec: float
    color: str = "#99CCFF"  # RGB hex color

    @property
    def duration_sec(self) -> float:
        """Get region duration."""
        return self.end_sec - self.start_sec

    def contains(self, position_sec: float) -> bool:
        """Check if position is within region."""
        return self.start_sec <= position_sec <= self.end_sec

    def overlaps(self, other: 'AudioRegion') -> bool:
        """Check if region overlaps with another."""
        return not (self.end_sec < other.start_sec or self.start_sec > other.end_sec)


class SampleSelection:
    """Represents a selection within audio data."""

    def __init__(self, start_sec: float, end_sec: float, sample_rate: int,
                 channels: int):
        """Initialize selection.

        Args:
            start_sec: Start position in seconds
            end_sec: End position in seconds
            sample_rate: Sample rate in Hz
            channels: Number of channels
        """
        self.start_sec = start_sec
        self.end_sec = end_sec
        self.sample_rate = sample_rate
        self.channels = channels

    @property
    def start_byte(self) -> int:
        """Get start position in bytes."""
        bytes_per_frame = self.channels * 2  # 16-bit samples
        frame = int(self.start_sec * self.sample_rate)
        return frame * bytes_per_frame

    @property
    def end_byte(self) -> int:
        """Get end position in bytes."""
        bytes_per_frame = self.channels * 2
        frame = int(self.end_sec * self.sample_rate)
        return frame * bytes_per_frame

    @property
    def duration_sec(self) -> float:
        """Get selection duration in seconds."""
        return self.end_sec - self.start_sec


class SampleEditor:
    """Performs editing operations on audio samples."""

    def __init__(self):
        """Initialize sample editor."""
        pass

    @staticmethod
    def trim(audio_data: bytes, selection: SampleSelection) -> bytes:
        """Trim audio to selection.

        Args:
            audio_data: Source audio data
            selection: Selection to trim to

        Returns:
            Trimmed audio data
        """
        try:
            start_byte = max(0, selection.start_byte)
            end_byte = min(len(audio_data), selection.end_byte)
            return audio_data[start_byte:end_byte]
        except Exception as e:
            log.error(f"Error trimming audio: {e}")
            return audio_data

    @staticmethod
    def copy(audio_data: bytes, selection: SampleSelection) -> bytes:
        """Copy selected region.

        Args:
            audio_data: Source audio data
            selection: Selection to copy

        Returns:
            Copied audio data
        """
        try:
            start_byte = max(0, selection.start_byte)
            end_byte = min(len(audio_data), selection.end_byte)
            return audio_data[start_byte:end_byte]
        except Exception as e:
            log.error(f"Error copying audio: {e}")
            return b''

    @staticmethod
    def paste(audio_data: bytes, clipboard: bytes, position_sec: float,
              sample_rate: int, channels: int) -> bytes:
        """Paste clipboard at position.

        Args:
            audio_data: Target audio data
            clipboard: Data to paste
            position_sec: Position to paste at
            sample_rate: Sample rate in Hz
            channels: Number of channels

        Returns:
            Modified audio data
        """
        try:
            bytes_per_frame = channels * 2
            insert_byte = int(position_sec * sample_rate * bytes_per_frame)
            insert_byte = max(0, min(insert_byte, len(audio_data)))

            return audio_data[:insert_byte] + clipboard + audio_data[insert_byte:]
        except Exception as e:
            log.error(f"Error pasting audio: {e}")
            return audio_data

    @staticmethod
    def normalize(audio_data: bytes, target_db: float = -3.0) -> bytes:
        """Normalize audio level.

        Args:
            audio_data: Audio to normalize
            target_db: Target peak level in dB

        Returns:
            Normalized audio data
        """
        try:
            target_linear = 10 ** (target_db / 20.0)

            # Find peak
            peak = 0.0
            for i in range(0, len(audio_data) - 1, 2):
                sample = struct.unpack('<h', audio_data[i:i+2])[0]
                peak = max(peak, abs(sample) / 32768.0)

            if peak <= 0.0:
                return audio_data

            # Calculate gain
            gain = target_linear / peak

            # Apply gain
            output = bytearray()
            for i in range(0, len(audio_data) - 1, 2):
                sample = struct.unpack('<h', audio_data[i:i+2])[0]
                sample = int(sample * gain)
                sample = max(-32768, min(32767, sample))
                output.extend(struct.pack('<h', sample))

            log.info(f"Normalized audio: peak={peak:.3f}, gain={gain:.3f}")
            return bytes(output)

        except Exception as e:
            log.error(f"Error normalizing audio: {e}")
            return audio_data

    @staticmethod
    def reverse(audio_data: bytes, sample_rate: int, channels: int) -> bytes:
        """Reverse audio.

        Args:
            audio_data: Audio to reverse
            sample_rate: Sample rate in Hz
            channels: Number of channels

        Returns:
            Reversed audio data
        """
        try:
            frame_size = channels * 2
            num_frames = len(audio_data) // frame_size

            output = bytearray()
            for frame_idx in range(num_frames - 1, -1, -1):
                start_byte = frame_idx * frame_size
                end_byte = start_byte + frame_size
                output.extend(audio_data[start_byte:end_byte])

            log.info(f"Reversed audio: {num_frames} frames")
            return bytes(output)

        except Exception as e:
            log.error(f"Error reversing audio: {e}")
            return audio_data

    @staticmethod
    def fade_in(audio_data: bytes, duration_sec: float, sample_rate: int,
                channels: int, curve: str = "linear") -> bytes:
        """Apply fade in effect.

        Args:
            audio_data: Audio to fade
            duration_sec: Fade duration in seconds
            sample_rate: Sample rate in Hz
            channels: Number of channels
            curve: Fade curve ("linear", "quadratic", "exponential")

        Returns:
            Audio with fade in applied
        """
        try:
            return SampleEditor._apply_fade(
                audio_data, duration_sec, sample_rate, channels,
                fade_in=True, curve=curve
            )
        except Exception as e:
            log.error(f"Error applying fade in: {e}")
            return audio_data

    @staticmethod
    def fade_out(audio_data: bytes, duration_sec: float, sample_rate: int,
                 channels: int, curve: str = "linear") -> bytes:
        """Apply fade out effect.

        Args:
            audio_data: Audio to fade
            duration_sec: Fade duration in seconds
            sample_rate: Sample rate in Hz
            channels: Number of channels
            curve: Fade curve ("linear", "quadratic", "exponential")

        Returns:
            Audio with fade out applied
        """
        try:
            return SampleEditor._apply_fade(
                audio_data, duration_sec, sample_rate, channels,
                fade_in=False, curve=curve
            )
        except Exception as e:
            log.error(f"Error applying fade out: {e}")
            return audio_data

    @staticmethod
    def _apply_fade(audio_data: bytes, duration_sec: float, sample_rate: int,
                    channels: int, fade_in: bool = True,
                    curve: str = "linear") -> bytes:
        """Apply fade effect with curve.

        Args:
            audio_data: Audio to fade
            duration_sec: Fade duration in seconds
            sample_rate: Sample rate in Hz
            channels: Number of channels
            fade_in: True for fade in, False for fade out
            curve: Fade curve type

        Returns:
            Audio with fade applied
        """
        fade_samples = int(duration_sec * sample_rate)
        output = bytearray(audio_data)

        for sample_idx in range(min(fade_samples, len(audio_data) // 2)):
            progress = sample_idx / fade_samples

            # Calculate gain based on curve
            if curve == "quadratic":
                gain = progress ** 2 if fade_in else 1.0 - (progress ** 2)
            elif curve == "exponential":
                gain = (math.exp(progress) - 1) / (math.e - 1) if fade_in else \
                       1.0 - ((math.exp(progress) - 1) / (math.e - 1))
            else:  # linear
                gain = progress if fade_in else 1.0 - progress

            # Apply to both channels
            for ch in range(channels):
                byte_idx = (sample_idx * channels + ch) * 2
                if byte_idx + 2 <= len(output):
                    sample = struct.unpack('<h', output[byte_idx:byte_idx+2])[0]
                    sample = int(sample * gain)
                    sample = max(-32768, min(32767, sample))
                    output[byte_idx:byte_idx+2] = struct.pack('<h', sample)

        return bytes(output)

    @staticmethod
    def apply_gain(audio_data: bytes, gain_db: float) -> bytes:
        """Apply gain/volume change.

        Args:
            audio_data: Audio to process
            gain_db: Gain in decibels

        Returns:
            Audio with gain applied
        """
        try:
            gain_linear = 10 ** (gain_db / 20.0)

            output = bytearray()
            for i in range(0, len(audio_data) - 1, 2):
                sample = struct.unpack('<h', audio_data[i:i+2])[0]
                sample = int(sample * gain_linear)
                sample = max(-32768, min(32767, sample))
                output.extend(struct.pack('<h', sample))

            return bytes(output)

        except Exception as e:
            log.error(f"Error applying gain: {e}")
            return audio_data

    @staticmethod
    def mix(audio_data1: bytes, audio_data2: bytes, mix_percent: float = 50.0,
            sample_rate: int = 44100, channels: int = 2) -> bytes:
        """Mix two audio samples.

        Args:
            audio_data1: First audio source
            audio_data2: Second audio source
            mix_percent: Mix percentage (0-100, 50=equal)
            sample_rate: Sample rate in Hz
            channels: Number of channels

        Returns:
            Mixed audio data
        """
        try:
            mix_gain1 = (100.0 - mix_percent) / 100.0
            mix_gain2 = mix_percent / 100.0

            output = bytearray()
            max_len = max(len(audio_data1), len(audio_data2))

            for i in range(0, max_len - 1, 2):
                sample1 = 0
                if i < len(audio_data1):
                    sample1 = struct.unpack('<h', audio_data1[i:i+2])[0]

                sample2 = 0
                if i < len(audio_data2):
                    sample2 = struct.unpack('<h', audio_data2[i:i+2])[0]

                mixed = int(sample1 * mix_gain1 + sample2 * mix_gain2)
                mixed = max(-32768, min(32767, mixed))
                output.extend(struct.pack('<h', mixed))

            return bytes(output)

        except Exception as e:
            log.error(f"Error mixing audio: {e}")
            return audio_data1


class AudioTrack:
    """Represents a single audio track with metadata and editing state."""

    def __init__(self, name: str, audio_data: bytes = b'', sample_rate: int = 44100,
                 channels: int = 2):
        """Initialize audio track.

        Args:
            name: Track name
            audio_data: Audio samples (16-bit PCM)
            sample_rate: Sample rate in Hz
            channels: Number of channels
        """
        self.name = name
        self.audio_data = audio_data
        self.sample_rate = sample_rate
        self.channels = channels
        self.enabled = True
        self.solo = False
        self.mute = False
        self.volume = 1.0  # Gain from 0.0 to 2.0
        self.pan = 0.5    # Pan from 0.0 (left) to 1.0 (right)

        # Cues and regions
        self.cues: List[AudioCue] = []
        self.regions: List[AudioRegion] = []

    @property
    def duration_sec(self) -> float:
        """Get track duration in seconds."""
        if not self.audio_data:
            return 0.0
        bytes_per_frame = self.channels * 2
        frames = len(self.audio_data) // bytes_per_frame
        return frames / self.sample_rate

    def set_volume(self, gain: float):
        """Set track volume."""
        self.volume = max(0.0, min(2.0, gain))

    def set_pan(self, pan: float):
        """Set track pan (-1.0=left, 0.0=center, 1.0=right)."""
        self.pan = max(-1.0, min(1.0, pan))

    def add_cue(self, cue: AudioCue) -> bool:
        """Add a cue marker."""
        if cue.position_sec < 0 or cue.position_sec > self.duration_sec:
            return False
        self.cues.append(cue)
        self.cues.sort()  # Keep sorted by position
        return True

    def remove_cue(self, index: int) -> bool:
        """Remove a cue marker."""
        if 0 <= index < len(self.cues):
            self.cues.pop(index)
            return True
        return False

    def get_cues(self) -> List[AudioCue]:
        """Get all cues."""
        return self.cues.copy()

    def add_region(self, region: AudioRegion) -> bool:
        """Add a region."""
        if region.start_sec < 0 or region.end_sec > self.duration_sec:
            return False
        if region.start_sec >= region.end_sec:
            return False
        self.regions.append(region)
        return True

    def remove_region(self, index: int) -> bool:
        """Remove a region."""
        if 0 <= index < len(self.regions):
            self.regions.pop(index)
            return True
        return False

    def get_regions(self) -> List[AudioRegion]:
        """Get all regions."""
        return self.regions.copy()

    def find_cue_at(self, position_sec: float, tolerance_sec: float = 0.05) -> Optional[AudioCue]:
        """Find cue near position."""
        for cue in self.cues:
            if abs(cue.position_sec - position_sec) < tolerance_sec:
                return cue
        return None

    def find_region_at(self, position_sec: float) -> Optional[AudioRegion]:
        """Find region containing position."""
        for region in self.regions:
            if region.contains(position_sec):
                return region
        return None

    def render(self) -> bytes:
        """Render track with volume and mute applied."""
        if self.mute or not self.enabled:
            return b''

        if self.volume == 1.0:
            return self.audio_data

        # Apply volume
        output = bytearray()
        for i in range(0, len(self.audio_data) - 1, 2):
            sample = struct.unpack('<h', self.audio_data[i:i+2])[0]
            sample = int(sample * self.volume)
            sample = max(-32768, min(32767, sample))
            output.extend(struct.pack('<h', sample))

        return bytes(output)


class AudioProject:
    """Multi-track audio project with editing support."""

    def __init__(self, name: str = "Untitled", sample_rate: int = 44100,
                 channels: int = 2):
        """Initialize audio project.

        Args:
            name: Project name
            sample_rate: Sample rate in Hz
            channels: Number of output channels
        """
        self.name = name
        self.sample_rate = sample_rate
        self.channels = channels
        self.tracks: list[AudioTrack] = []
        self.undo_stack: list[tuple[str, AudioProject]] = []
        self.redo_stack: list[tuple[str, AudioProject]] = []
        self.master_volume = 1.0

    def add_track(self, track: AudioTrack) -> int:
        """Add a track to the project.

        Returns:
            Track index
        """
        self.tracks.append(track)
        log.info(f"Added track: {track.name}")
        return len(self.tracks) - 1

    def remove_track(self, index: int) -> bool:
        """Remove a track from the project."""
        if 0 <= index < len(self.tracks):
            removed = self.tracks.pop(index)
            log.info(f"Removed track: {removed.name}")
            return True
        return False

    def get_track(self, index: int) -> Optional[AudioTrack]:
        """Get track by index."""
        if 0 <= index < len(self.tracks):
            return self.tracks[index]
        return None

    def move_track(self, from_idx: int, to_idx: int) -> bool:
        """Move track to different position."""
        if 0 <= from_idx < len(self.tracks) and 0 <= to_idx < len(self.tracks):
            track = self.tracks.pop(from_idx)
            self.tracks.insert(to_idx, track)
            return True
        return False

    @property
    def duration_sec(self) -> float:
        """Get project duration (length of longest track)."""
        if not self.tracks:
            return 0.0
        return max(track.duration_sec for track in self.tracks)

    def render(self) -> bytes:
        """Render all enabled tracks to stereo output.

        Returns:
            Mixed audio data (16-bit PCM, stereo)
        """
        if not self.tracks:
            return b''

        # Find solo tracks
        solo_tracks = [t for t in self.tracks if t.solo]
        playback_tracks = solo_tracks if solo_tracks else self.tracks

        # Render to float accumulator (to avoid clipping during mix)
        duration_sec = self.duration_sec
        total_frames = int(duration_sec * self.sample_rate)
        accum = [[0.0] * total_frames for _ in range(2)]

        # Mix each track
        for track in playback_tracks:
            if not track.enabled or track.mute:
                continue

            rendered = track.render()
            if not rendered:
                continue

            bytes_per_frame = track.channels * 2
            frames = len(rendered) // bytes_per_frame

            # Apply panning
            left_gain = math.sqrt(1.0 - track.pan)
            right_gain = math.sqrt(track.pan)

            for frame_idx in range(frames):
                byte_idx = frame_idx * bytes_per_frame

                # Get first channel (or mix if mono)
                sample = struct.unpack('<h', rendered[byte_idx:byte_idx+2])[0]
                normalized = sample / 32768.0

                # Mix to stereo with panning
                accum[0][frame_idx] += normalized * left_gain
                accum[1][frame_idx] += normalized * right_gain

        # Convert back to 16-bit with clipping
        output = bytearray()
        for frame_idx in range(total_frames):
            for ch in range(2):
                sample = accum[ch][frame_idx] * self.master_volume
                # Soft clipping
                sample = math.tanh(sample)
                sample = int(sample * 32767)
                sample = max(-32768, min(32767, sample))
                output.extend(struct.pack('<h', sample))

        return bytes(output)
