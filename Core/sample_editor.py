"""Sample editing operations and utilities.

Implements:
- Sample selection and manipulation
- Basic DSP operations (trim, normalize, reverse)
- Fade in/fade out curves
- Copy/paste operations
"""
from __future__ import annotations
from typing import Optional, Tuple
import struct
import math
import logging

log = logging.getLogger(__name__)


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
