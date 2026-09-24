"""Digital signal processing utilities for audio effects.

Implements real DSP algorithms:
- IIR filter design
- Biquad filter coefficients
- Peak EQ, shelf filters
- Real-time filter state management
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import List
import math
import struct
import logging

log = logging.getLogger(__name__)


@dataclass
class FilterCoefficients:
    """Biquad filter coefficients."""
    b0: float = 1.0
    b1: float = 0.0
    b2: float = 0.0
    a1: float = 0.0
    a2: float = 0.0


class BiquadFilter:
    """Single biquad IIR filter stage."""

    def __init__(self):
        """Initialize biquad filter."""
        self.coeffs = FilterCoefficients()
        self.x1 = 0.0  # Previous input sample 1
        self.x2 = 0.0  # Previous input sample 2
        self.y1 = 0.0  # Previous output sample 1
        self.y2 = 0.0  # Previous output sample 2

    def set_coefficients(self, coeffs: FilterCoefficients):
        """Set filter coefficients."""
        self.coeffs = coeffs

    def process_sample(self, x: float) -> float:
        """Process a single audio sample.

        Args:
            x: Input sample

        Returns:
            Filtered output sample
        """
        # Direct Form II implementation
        w = x - self.coeffs.a1 * self.y1 - self.coeffs.a2 * self.y2

        y = (self.coeffs.b0 * w +
             self.coeffs.b1 * self.x1 +
             self.coeffs.b2 * self.x2)

        # Update state
        self.x2 = self.x1
        self.x1 = w
        self.y2 = self.y1
        self.y1 = y

        return y

    def reset(self):
        """Reset filter state."""
        self.x1 = 0.0
        self.x2 = 0.0
        self.y1 = 0.0
        self.y2 = 0.0


class PeakEQFilter:
    """Peak/shelf equalizer using biquad filters."""

    @staticmethod
    def design_peak(frequency: float, sample_rate: float, gain_db: float,
                    q: float = 1.0) -> FilterCoefficients:
        """Design a peak EQ filter.

        Args:
            frequency: Center frequency in Hz
            sample_rate: Sample rate in Hz
            gain_db: Gain in decibels
            q: Quality factor

        Returns:
            Filter coefficients
        """
        w0 = 2 * math.pi * frequency / sample_rate
        sin_w0 = math.sin(w0)
        cos_w0 = math.cos(w0)
        A = math.sqrt(10 ** (gain_db / 20))
        alpha = sin_w0 / (2 * q)

        b0 = 1 + alpha * A
        b1 = -2 * cos_w0
        b2 = 1 - alpha * A
        a0 = 1 + alpha / A
        a1 = -2 * cos_w0
        a2 = 1 - alpha / A

        # Normalize by a0
        return FilterCoefficients(
            b0=b0 / a0,
            b1=b1 / a0,
            b2=b2 / a0,
            a1=a1 / a0,
            a2=a2 / a0
        )

    @staticmethod
    def design_lowshelf(frequency: float, sample_rate: float, gain_db: float,
                        q: float = 0.707) -> FilterCoefficients:
        """Design a low-shelf filter.

        Args:
            frequency: Shelf frequency in Hz
            sample_rate: Sample rate in Hz
            gain_db: Gain in decibels
            q: Quality factor

        Returns:
            Filter coefficients
        """
        w0 = 2 * math.pi * frequency / sample_rate
        sin_w0 = math.sin(w0)
        cos_w0 = math.cos(w0)
        A = math.sqrt(10 ** (gain_db / 20))
        alpha = sin_w0 / (2 * q)

        # Low-shelf formulas
        two_sqrt_a_alpha = 2 * math.sqrt(A) * alpha

        b0 = A * ((A + 1) - (A - 1) * cos_w0 + two_sqrt_a_alpha)
        b1 = 2 * A * ((A - 1) - (A + 1) * cos_w0)
        b2 = A * ((A + 1) - (A - 1) * cos_w0 - two_sqrt_a_alpha)
        a0 = (A + 1) + (A - 1) * cos_w0 + two_sqrt_a_alpha
        a1 = -2 * ((A - 1) + (A + 1) * cos_w0)
        a2 = (A + 1) + (A - 1) * cos_w0 - two_sqrt_a_alpha

        # Normalize by a0
        return FilterCoefficients(
            b0=b0 / a0,
            b1=b1 / a0,
            b2=b2 / a0,
            a1=a1 / a0,
            a2=a2 / a0
        )

    @staticmethod
    def design_highshelf(frequency: float, sample_rate: float, gain_db: float,
                         q: float = 0.707) -> FilterCoefficients:
        """Design a high-shelf filter.

        Args:
            frequency: Shelf frequency in Hz
            sample_rate: Sample rate in Hz
            gain_db: Gain in decibels
            q: Quality factor

        Returns:
            Filter coefficients
        """
        w0 = 2 * math.pi * frequency / sample_rate
        sin_w0 = math.sin(w0)
        cos_w0 = math.cos(w0)
        A = math.sqrt(10 ** (gain_db / 20))
        alpha = sin_w0 / (2 * q)

        # High-shelf formulas
        two_sqrt_a_alpha = 2 * math.sqrt(A) * alpha

        b0 = A * ((A + 1) + (A - 1) * cos_w0 + two_sqrt_a_alpha)
        b1 = -2 * A * ((A - 1) + (A + 1) * cos_w0)
        b2 = A * ((A + 1) + (A - 1) * cos_w0 - two_sqrt_a_alpha)
        a0 = (A + 1) - (A - 1) * cos_w0 + two_sqrt_a_alpha
        a1 = 2 * ((A - 1) - (A + 1) * cos_w0)
        a2 = (A + 1) - (A - 1) * cos_w0 - two_sqrt_a_alpha

        # Normalize by a0
        return FilterCoefficients(
            b0=b0 / a0,
            b1=b1 / a0,
            b2=b2 / a0,
            a1=a1 / a0,
            a2=a2 / a0
        )


class ThreeBandEQ:
    """Real 3-band parametric EQ using biquad filters."""

    def __init__(self, sample_rate: int = 44100):
        """Initialize 3-band EQ.

        Args:
            sample_rate: Sample rate in Hz
        """
        self.sample_rate = sample_rate
        self.low_filter = BiquadFilter()
        self.mid_filter = BiquadFilter()
        self.high_filter = BiquadFilter()

        # Default settings
        self.low_freq = 100.0
        self.mid_freq = 1000.0
        self.high_freq = 10000.0
        self.q_factor = 0.707

        # Set default coefficients
        self._update_filters()

    def set_gains(self, low_db: float, mid_db: float, high_db: float):
        """Set EQ gains in decibels.

        Args:
            low_db: Low band gain (-12 to +12 dB)
            mid_db: Mid band gain (-12 to +12 dB)
            high_db: High band gain (-12 to +12 dB)
        """
        # Low shelf
        low_coeffs = PeakEQFilter.design_lowshelf(
            self.low_freq, self.sample_rate, low_db, self.q_factor
        )
        self.low_filter.set_coefficients(low_coeffs)

        # Mid peak
        mid_coeffs = PeakEQFilter.design_peak(
            self.mid_freq, self.sample_rate, mid_db, self.q_factor
        )
        self.mid_filter.set_coefficients(mid_coeffs)

        # High shelf
        high_coeffs = PeakEQFilter.design_highshelf(
            self.high_freq, self.sample_rate, high_db, self.q_factor
        )
        self.high_filter.set_coefficients(high_coeffs)

    def process(self, audio_data: bytes) -> bytes:
        """Process audio through EQ.

        Args:
            audio_data: 16-bit PCM audio samples

        Returns:
            Filtered audio data
        """
        try:
            output = bytearray()
            max_sample = 32768

            # Process each 16-bit sample
            for i in range(0, len(audio_data) - 1, 2):
                sample = struct.unpack('<h', audio_data[i:i+2])[0]
                normalized = sample / max_sample

                # Apply filters in series
                filtered = normalized
                filtered = self.low_filter.process_sample(filtered)
                filtered = self.mid_filter.process_sample(filtered)
                filtered = self.high_filter.process_sample(filtered)

                # Clamp and denormalize
                filtered = max(-1.0, min(1.0, filtered))
                output_sample = int(filtered * (max_sample - 1))
                output.extend(struct.pack('<h', output_sample))

            return bytes(output)

        except Exception as e:
            log.error(f"Error processing EQ: {e}")
            return audio_data

    def reset(self):
        """Reset filter states."""
        self.low_filter.reset()
        self.mid_filter.reset()
        self.high_filter.reset()

    def _update_filters(self):
        """Update filter coefficients (called on init)."""
        self.set_gains(0.0, 0.0, 0.0)


class SimpleCompressor:
    """Dynamic range compressor using peak detection."""

    def __init__(self, sample_rate: int = 44100):
        """Initialize compressor.

        Args:
            sample_rate: Sample rate in Hz
        """
        self.sample_rate = sample_rate
        self.threshold = -20.0  # dB
        self.ratio = 4.0        # 4:1 compression
        self.attack_ms = 10.0   # Attack time
        self.release_ms = 100.0 # Release time

        # Calculate coefficients
        self.attack_coeff = self._time_to_coeff(self.attack_ms)
        self.release_coeff = self._time_to_coeff(self.release_ms)
        self.envelope = 0.0

    def _time_to_coeff(self, time_ms: float) -> float:
        """Convert time constant to filter coefficient."""
        if time_ms <= 0:
            return 1.0
        samples = self.sample_rate * time_ms / 1000.0
        return 1.0 - math.exp(-2.0 / samples)

    def process(self, audio_data: bytes) -> bytes:
        """Process audio through compressor.

        Args:
            audio_data: 16-bit PCM audio samples

        Returns:
            Compressed audio data
        """
        try:
            output = bytearray()
            max_sample = 32768
            threshold_linear = 10 ** (self.threshold / 20.0)

            for i in range(0, len(audio_data) - 1, 2):
                sample = struct.unpack('<h', audio_data[i:i+2])[0]
                normalized = abs(sample) / max_sample

                # Envelope follower
                if normalized > self.envelope:
                    self.envelope += self.attack_coeff * (normalized - self.envelope)
                else:
                    self.envelope += self.release_coeff * (normalized - self.envelope)

                # Calculate gain reduction
                if self.envelope > threshold_linear:
                    gain_reduction = (threshold_linear +
                                    (self.envelope - threshold_linear) / self.ratio)
                else:
                    gain_reduction = self.envelope

                # Apply gain reduction
                if self.envelope > 0:
                    gain = gain_reduction / self.envelope
                else:
                    gain = 1.0

                output_sample = int(sample * gain)
                output_sample = max(-32768, min(32767, output_sample))
                output.extend(struct.pack('<h', output_sample))

            return bytes(output)

        except Exception as e:
            log.error(f"Error processing compression: {e}")
            return audio_data

    def reset(self):
        """Reset compressor state."""
        self.envelope = 0.0


class NoiseGate:
    """Simple noise gate with adjustable threshold."""

    def __init__(self, sample_rate: int = 44100):
        """Initialize noise gate.

        Args:
            sample_rate: Sample rate in Hz
        """
        self.sample_rate = sample_rate
        self.threshold = -40.0  # dB
        self.hold_ms = 10.0
        self.release_ms = 50.0
        self.envelope = 0.0
        self.hold_counter = 0
        self.hold_samples = int(sample_rate * self.hold_ms / 1000.0)
        self.release_coeff = self._time_to_coeff(self.release_ms)

    def _time_to_coeff(self, time_ms: float) -> float:
        """Convert time constant to filter coefficient."""
        if time_ms <= 0:
            return 1.0
        samples = self.sample_rate * time_ms / 1000.0
        return 1.0 - math.exp(-2.0 / samples)

    def process(self, audio_data: bytes) -> bytes:
        """Process audio through noise gate.

        Args:
            audio_data: 16-bit PCM audio samples

        Returns:
            Gated audio data
        """
        try:
            output = bytearray()
            max_sample = 32768
            threshold_linear = 10 ** (self.threshold / 20.0)

            for i in range(0, len(audio_data) - 1, 2):
                sample = struct.unpack('<h', audio_data[i:i+2])[0]
                normalized = abs(sample) / max_sample

                # Update envelope
                self.envelope = max(self.envelope * self.release_coeff, normalized)

                # Gate logic
                if self.envelope > threshold_linear:
                    self.hold_counter = self.hold_samples
                    gate_open = 1.0
                elif self.hold_counter > 0:
                    self.hold_counter -= 1
                    gate_open = 1.0
                else:
                    gate_open = 0.0

                # Apply gate
                output_sample = int(sample * gate_open)
                output.extend(struct.pack('<h', output_sample))

            return bytes(output)

        except Exception as e:
            log.error(f"Error processing noise gate: {e}")
            return audio_data
