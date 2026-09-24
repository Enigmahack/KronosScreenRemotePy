#!/usr/bin/env python3
"""DSP performance profiling and optimization tool.

Profiles the audio DSP algorithms to identify bottlenecks and
measure processing speed for real-time feasibility.
"""
import cProfile
import pstats
import io
import struct
import time
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from Core.audio_dsp import (
    ThreeBandEQ, SimpleCompressor, NoiseGate,
    DelayLine, SimpleDistortion, SimpleReverb
)
from Core.audio_effects import (
    VolumeEffect, SimpleEqualizerEffect, CompressorEffect,
    NoiseGateEffect, ReverbEffect, DelayEffect, DistortionEffect
)


def generate_test_audio(duration_sec: float, sample_rate: int = 44100) -> bytes:
    """Generate test sine wave audio.

    Args:
        duration_sec: Duration in seconds
        sample_rate: Sample rate in Hz

    Returns:
        16-bit PCM audio data
    """
    import math
    samples = int(duration_sec * sample_rate)
    output = bytearray()

    frequency = 440.0  # A4 note
    amplitude = 0.8

    for i in range(samples):
        sample = math.sin(2 * math.pi * frequency * i / sample_rate) * amplitude
        sample = int(sample * 32767)
        sample = max(-32768, min(32767, sample))
        output.extend(struct.pack('<h', sample))

    return bytes(output)


def benchmark_effect(effect_name: str, effect, audio_data: bytes,
                     sample_rate: int, channels: int, iterations: int = 3):
    """Benchmark a single effect.

    Args:
        effect_name: Name of effect for reporting
        effect: Effect instance
        audio_data: Test audio data
        sample_rate: Sample rate in Hz
        channels: Number of channels
        iterations: Number of times to process

    Returns:
        Tuple of (avg_time_ms, cpu_percent_for_realtime)
    """
    times = []

    for _ in range(iterations):
        start = time.perf_counter()
        for _ in range(5):  # Process 5 chunks
            effect.process(audio_data, sample_rate, channels)
        elapsed = time.perf_counter() - start
        times.append(elapsed * 1000)  # Convert to ms

    avg_time_ms = sum(times) / len(times)
    audio_duration_ms = len(audio_data) / (sample_rate * channels * 2) * 1000
    cpu_percent = (avg_time_ms / audio_duration_ms) * 100

    return avg_time_ms, cpu_percent


def profile_dsp():
    """Profile all DSP components."""
    print("=" * 70)
    print("KRONOS SCREENREMOTE - DSP PERFORMANCE PROFILING")
    print("=" * 70)

    # Generate test audio (1 second at 44.1kHz)
    test_audio = generate_test_audio(1.0, 44100)
    print(f"\nTest audio: 1 second, 44.1 kHz, {len(test_audio)} bytes\n")

    results = {}

    # Profile basic effects
    print("Basic Effects:")
    print("-" * 70)

    volume = VolumeEffect()
    volume.set_parameter("gain", 1.2)
    avg_time, cpu = benchmark_effect("Volume", volume, test_audio, 44100, 2)
    print(f"  Volume Effect:     {avg_time:6.2f}ms  ({cpu:5.1f}% CPU)")
    results["Volume"] = cpu

    eq = SimpleEqualizerEffect(44100)
    eq.set_parameter("low", 3.0)
    eq.set_parameter("mid", 0.0)
    eq.set_parameter("high", 3.0)
    avg_time, cpu = benchmark_effect("EQ", eq, test_audio, 44100, 2)
    print(f"  EQ (3-band):       {avg_time:6.2f}ms  ({cpu:5.1f}% CPU)")
    results["EQ"] = cpu

    comp = CompressorEffect(44100)
    comp.set_parameter("ratio", 4.0)
    comp.set_parameter("threshold", -20.0)
    avg_time, cpu = benchmark_effect("Compressor", comp, test_audio, 44100, 2)
    print(f"  Compressor:        {avg_time:6.2f}ms  ({cpu:5.1f}% CPU)")
    results["Compressor"] = cpu

    gate = NoiseGateEffect(44100)
    gate.set_parameter("threshold", -40.0)
    avg_time, cpu = benchmark_effect("Noise Gate", gate, test_audio, 44100, 2)
    print(f"  Noise Gate:        {avg_time:6.2f}ms  ({cpu:5.1f}% CPU)")
    results["NoiseGate"] = cpu

    # Profile advanced effects
    print("\nAdvanced Effects:")
    print("-" * 70)

    reverb = ReverbEffect(44100)
    reverb.set_parameter("room_size", 0.5)
    reverb.set_parameter("wet_level", 0.3)
    avg_time, cpu = benchmark_effect("Reverb", reverb, test_audio, 44100, 2)
    print(f"  Reverb (Schroeder):{avg_time:6.2f}ms  ({cpu:5.1f}% CPU)")
    results["Reverb"] = cpu

    delay = DelayEffect(44100)
    delay.set_parameter("delay_time", 250.0)
    delay.set_parameter("feedback", 0.5)
    delay.set_parameter("wet_level", 0.5)
    avg_time, cpu = benchmark_effect("Delay", delay, test_audio, 44100, 2)
    print(f"  Delay (250ms):     {avg_time:6.2f}ms  ({cpu:5.1f}% CPU)")
    results["Delay"] = cpu

    dist = DistortionEffect(44100)
    dist.set_parameter("drive", 2.0)
    avg_time, cpu = benchmark_effect("Distortion", dist, test_audio, 44100, 2)
    print(f"  Distortion:        {avg_time:6.2f}ms  ({cpu:5.1f}% CPU)")
    results["Distortion"] = cpu

    # Summary
    print("\n" + "=" * 70)
    print("ANALYSIS")
    print("=" * 70)

    total_cpu = sum(results.values())
    realtime_safe = total_cpu < 80  # Leave headroom for other operations

    print(f"\nTotal CPU (all effects active):  {total_cpu:.1f}%")
    print(f"Real-time safe (< 80% CPU):     {'✓ YES' if realtime_safe else '✗ NO'}")
    print(f"\nMax individual effect CPU:      {max(results.values()):.1f}%")
    print(f"Min individual effect CPU:      {min(results.values()):.1f}%")

    print("\nRecommendations:")
    if total_cpu > 80:
        print("  ⚠ CPU usage too high for real-time at 44.1 kHz")
        print("  - Consider using fewer effects simultaneously")
        print("  - Use lower sample rates for testing")
        print("  - Profile at higher sample rates (48kHz, 96kHz)")
    else:
        print("  ✓ Real-time performance is acceptable")
        print("  - Monitor CPU usage during actual playback")
        print("  - Profile at higher sample rates if needed")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    profile_dsp()
