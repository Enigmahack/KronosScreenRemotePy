"""Audio effects and processing framework.

Provides a foundation for audio effects:
- Effect chain management
- Common DSP effects
- Real-time parameter adjustment
- Extensible architecture
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, List, Dict
import logging
import struct

log = logging.getLogger(__name__)


@dataclass
class EffectParameter:
    """An adjustable parameter for an audio effect."""
    name: str
    min_value: float = 0.0
    max_value: float = 1.0
    default_value: float = 0.5
    current_value: float = None

    def __post_init__(self):
        if self.current_value is None:
            self.current_value = self.default_value

    def set_value(self, value: float):
        """Set parameter value with bounds checking."""
        self.current_value = max(self.min_value, min(self.max_value, value))

    def get_normalized(self) -> float:
        """Get value normalized to 0-1 range."""
        if self.max_value == self.min_value:
            return 0.0
        return (self.current_value - self.min_value) / (self.max_value - self.min_value)


class AudioEffect(ABC):
    """Base class for audio effects."""

    def __init__(self, name: str):
        """Initialize audio effect.

        Args:
            name: Effect name
        """
        self.name = name
        self.enabled = True
        self.parameters: Dict[str, EffectParameter] = {}

    @abstractmethod
    def process(self, audio_data: bytes, sample_rate: int,
                channels: int) -> bytes:
        """Process audio data through the effect.

        Args:
            audio_data: Input audio samples (16-bit PCM)
            sample_rate: Sample rate in Hz
            channels: Number of channels

        Returns:
            Processed audio data
        """
        pass

    def add_parameter(self, param: EffectParameter):
        """Add an adjustable parameter."""
        self.parameters[param.name] = param

    def set_parameter(self, name: str, value: float):
        """Set parameter value."""
        if name in self.parameters:
            self.parameters[name].set_value(value)

    def get_parameter(self, name: str) -> Optional[float]:
        """Get parameter value."""
        if name in self.parameters:
            return self.parameters[name].current_value
        return None


class VolumeEffect(AudioEffect):
    """Simple volume/gain effect."""

    def __init__(self):
        super().__init__("Volume")
        self.add_parameter(EffectParameter(
            name="gain",
            min_value=0.0,
            max_value=2.0,
            default_value=1.0
        ))

    def process(self, audio_data: bytes, sample_rate: int,
                channels: int) -> bytes:
        """Apply volume gain to audio."""
        if not self.enabled:
            return audio_data

        gain = self.get_parameter("gain")
        if gain is None or gain == 1.0:
            return audio_data

        try:
            # Process 16-bit samples
            output = bytearray()
            for i in range(0, len(audio_data) - 1, 2):
                sample = struct.unpack('<h', audio_data[i:i+2])[0]
                sample = int(sample * gain)
                # Clamp to 16-bit range
                sample = max(-32768, min(32767, sample))
                output.extend(struct.pack('<h', sample))

            return bytes(output)

        except Exception as e:
            log.error(f"Error processing volume: {e}")
            return audio_data


class SimpleEqualizerEffect(AudioEffect):
    """Real 3-band parametric equalizer using IIR filters."""

    def __init__(self, sample_rate: int = 44100):
        super().__init__("Equalizer")
        self.sample_rate = sample_rate
        self._eq = None
        self._init_eq()

        self.add_parameter(EffectParameter(
            name="low",
            min_value=-12.0,
            max_value=12.0,
            default_value=0.0
        ))
        self.add_parameter(EffectParameter(
            name="mid",
            min_value=-12.0,
            max_value=12.0,
            default_value=0.0
        ))
        self.add_parameter(EffectParameter(
            name="high",
            min_value=-12.0,
            max_value=12.0,
            default_value=0.0
        ))

    def _init_eq(self):
        """Initialize the EQ engine."""
        try:
            from Core.audio_dsp import ThreeBandEQ
            self._eq = ThreeBandEQ(self.sample_rate)
        except ImportError:
            self._eq = None
            log.warning("audio_dsp module not available, EQ will be disabled")

    def process(self, audio_data: bytes, sample_rate: int,
                channels: int) -> bytes:
        """Apply real IIR EQ to audio."""
        if not self.enabled or not self._eq:
            return audio_data

        try:
            low_db = self.get_parameter("low") or 0.0
            mid_db = self.get_parameter("mid") or 0.0
            high_db = self.get_parameter("high") or 0.0

            # Update EQ settings
            self._eq.set_gains(low_db, mid_db, high_db)

            # Process audio
            return self._eq.process(audio_data)

        except Exception as e:
            log.error(f"Error processing EQ: {e}")
            return audio_data


class CompressorEffect(AudioEffect):
    """Real dynamic range compressor with envelope following."""

    def __init__(self, sample_rate: int = 44100):
        super().__init__("Compressor")
        self.sample_rate = sample_rate
        self._compressor = None
        self._init_compressor()

        self.add_parameter(EffectParameter(
            name="ratio",
            min_value=1.0,
            max_value=16.0,
            default_value=4.0
        ))
        self.add_parameter(EffectParameter(
            name="threshold",
            min_value=-60.0,
            max_value=0.0,
            default_value=-20.0
        ))

    def _init_compressor(self):
        """Initialize the compressor engine."""
        try:
            from Core.audio_dsp import SimpleCompressor
            self._compressor = SimpleCompressor(self.sample_rate)
        except ImportError:
            self._compressor = None
            log.warning("audio_dsp module not available, compressor will be disabled")

    def process(self, audio_data: bytes, sample_rate: int,
                channels: int) -> bytes:
        """Apply real compression to audio."""
        if not self.enabled or not self._compressor:
            return audio_data

        try:
            ratio = self.get_parameter("ratio") or 4.0
            threshold = self.get_parameter("threshold") or -20.0

            # Update compressor settings
            self._compressor.ratio = ratio
            self._compressor.threshold = threshold

            # Process audio
            return self._compressor.process(audio_data)

        except Exception as e:
            log.error(f"Error processing compression: {e}")
            return audio_data


class AudioEffectChain:
    """Chain of audio effects applied in sequence."""

    def __init__(self):
        """Initialize effect chain."""
        self.effects: List[AudioEffect] = []
        self.enabled = True

    def add_effect(self, effect: AudioEffect):
        """Add an effect to the chain."""
        self.effects.append(effect)
        log.info(f"Added effect: {effect.name}")

    def remove_effect(self, effect: AudioEffect):
        """Remove an effect from the chain."""
        if effect in self.effects:
            self.effects.remove(effect)
            log.info(f"Removed effect: {effect.name}")

    def clear_effects(self):
        """Clear all effects from the chain."""
        self.effects.clear()
        log.info("Cleared all effects")

    def process(self, audio_data: bytes, sample_rate: int,
                channels: int) -> bytes:
        """Process audio through all effects in the chain.

        Args:
            audio_data: Input audio samples
            sample_rate: Sample rate in Hz
            channels: Number of channels

        Returns:
            Processed audio data
        """
        if not self.enabled:
            return audio_data

        output = audio_data
        for effect in self.effects:
            if effect.enabled:
                try:
                    output = effect.process(output, sample_rate, channels)
                except Exception as e:
                    log.error(f"Error processing {effect.name}: {e}")

        return output

    def get_effect(self, name: str) -> Optional[AudioEffect]:
        """Get an effect by name."""
        for effect in self.effects:
            if effect.name == name:
                return effect
        return None


class NoiseGateEffect(AudioEffect):
    """Noise gate for removing low-level noise."""

    def __init__(self, sample_rate: int = 44100):
        super().__init__("Noise Gate")
        self.sample_rate = sample_rate
        self._gate = None
        self._init_gate()

        self.add_parameter(EffectParameter(
            name="threshold",
            min_value=-80.0,
            max_value=0.0,
            default_value=-40.0
        ))
        self.add_parameter(EffectParameter(
            name="hold",
            min_value=1.0,
            max_value=100.0,
            default_value=10.0
        ))

    def _init_gate(self):
        """Initialize the noise gate engine."""
        try:
            from Core.audio_dsp import NoiseGate
            self._gate = NoiseGate(self.sample_rate)
        except ImportError:
            self._gate = None
            log.warning("audio_dsp module not available, noise gate will be disabled")

    def process(self, audio_data: bytes, sample_rate: int,
                channels: int) -> bytes:
        """Apply noise gate to audio."""
        if not self.enabled or not self._gate:
            return audio_data

        try:
            threshold = self.get_parameter("threshold") or -40.0
            hold_ms = self.get_parameter("hold") or 10.0

            # Update gate settings
            self._gate.threshold = threshold
            self._gate.hold_ms = hold_ms
            self._gate.hold_samples = int(self.sample_rate * hold_ms / 1000.0)

            # Process audio
            return self._gate.process(audio_data)

        except Exception as e:
            log.error(f"Error processing noise gate: {e}")
            return audio_data


# Global effect chain
_audio_effect_chain: Optional[AudioEffectChain] = None


def get_audio_effects() -> AudioEffectChain:
    """Get the global audio effect chain (singleton)."""
    global _audio_effect_chain
    if _audio_effect_chain is None:
        _audio_effect_chain = AudioEffectChain()
        # Add default effects
        _audio_effect_chain.add_effect(VolumeEffect())
        _audio_effect_chain.add_effect(SimpleEqualizerEffect())
        _audio_effect_chain.add_effect(CompressorEffect())
        _audio_effect_chain.add_effect(NoiseGateEffect())
    return _audio_effect_chain
