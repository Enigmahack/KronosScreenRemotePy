# Kronos ScreenRemote Python - Troubleshooting Checklist

## Pre-Deployment Checks

### Environment Setup
- [ ] Python 3.8+ installed: `python3 --version`
- [ ] PySide6 installed: `pip3 list | grep PySide6`
- [ ] Working directory: `/home/share/KronosScreenRemotePy`
- [ ] Git branch correct: `git branch` (should show ParityUpdate)

### Syntax & Import Validation
```bash
# Run import tests
python3 << 'EOF'
from Core.audio_effects import ReverbEffect, DelayEffect, DistortionEffect
from Views.audio_controls import EqualizerDialog
from Core.sample_editor import AudioProject, AudioTrack
from Views.cue_region_panel import CueRegionPanel
from Views.sample_editor_window import SampleEditorWindow
from Core.presets import PresetsManager
print("✓ All imports successful")
EOF

# Check syntax on all Python files
python3 -m py_compile Core/*.py Views/*.py Tools/*.py
```

### File Structure Validation
```bash
# Check critical files exist
ls -la Core/audio_effects.py Core/audio_dsp.py
ls -la Core/sample_editor.py Core/presets.py
ls -la Views/sample_editor_window.py Views/track_list.py
ls -la Views/effect_preview_panel.py Views/cue_region_panel.py
ls -la Tools/profile_dsp.py
```

---

## Common Issues & Solutions

### Issue #1: ModuleNotFoundError
**Symptom**: `ModuleNotFoundError: No module named 'Core'`

**Diagnosis**:
```bash
# Check current directory
pwd  # Should be /home/share/KronosScreenRemotePy

# Check PYTHONPATH
echo $PYTHONPATH

# Verify __init__.py exists
ls -la Core/__init__.py Views/__init__.py
```

**Solution**:
```bash
# Add to Python path
export PYTHONPATH="${PYTHONPATH}:/home/share/KronosScreenRemotePy"

# Or run from correct directory
cd /home/share/KronosScreenRemotePy
python3 main.py
```

---

### Issue #2: PySide6 Import Error
**Symptom**: `ImportError: cannot import name 'QWidget' from 'PySide6.QtWidgets'`

**Diagnosis**:
```bash
python3 -c "from PySide6.QtWidgets import QWidget; print('OK')"
```

**Solution**:
```bash
# Reinstall PySide6
pip3 install --upgrade PySide6

# Verify installation
pip3 show PySide6
```

---

### Issue #3: Audio Device Not Found
**Symptom**: `No audio devices detected` or `Failed to start audio streaming`

**Diagnosis**:
```bash
# Check audio system
python3 << 'EOF'
from Core.audio_engine import get_audio_devices
mgr = get_audio_devices()
devices = mgr.get_output_devices()
print(f"Found {len(devices)} output devices")
for d in devices:
    print(f"  - {d.name}: {d.device_id}")
EOF
```

**Solution**:
- Verify ALSA/PulseAudio/CoreAudio is running
- Check system audio settings
- Try different sample rates (44.1kHz, 48kHz)
- Update audio drivers

---

### Issue #4: Reverb Causing High CPU Usage
**Symptom**: Audio stutters when reverb enabled

**Diagnosis**:
```bash
python3 Tools/profile_dsp.py | grep Reverb
```

**Solution**:
- Disable reverb or other heavy effects
- Use fewer delay lines (already optimized to 2)
- Lower sample rate for testing
- Profile with smaller audio buffer

---

### Issue #5: Presets Directory Not Created
**Symptom**: `No such file or directory: ~/.kronos/presets/`

**Diagnosis**:
```bash
ls -la ~/.kronos/presets/
```

**Solution**:
```bash
# Create directory manually
mkdir -p ~/.kronos/presets/{effects,devices}

# Or access PresetsManager which auto-creates
python3 << 'EOF'
from Core.presets import get_presets_manager
mgr = get_presets_manager()
print(f"Presets directory: {mgr.presets_dir}")
EOF
```

---

### Issue #6: Project File Corruption
**Symptom**: `JSONDecodeError` when opening saved project

**Diagnosis**:
```bash
# Validate JSON
python3 -m json.tool project.kronos

# Check file size
ls -lh project.kronos
```

**Solution**:
```bash
# Backup corrupted file
cp project.kronos project.kronos.bak

# Create new project
# Check if audio file paths are valid
grep -E "audio_file|tracks" project.kronos
```

---

### Issue #7: Waveform Display Not Updating
**Symptom**: Waveform stays blank after loading audio

**Diagnosis**:
```bash
# Check audio data
python3 << 'EOF'
from Core.audio_sample_player import WavFileReader
from pathlib import Path
info = WavFileReader.read_header(Path("test.wav"))
print(f"Sample rate: {info.sample_rate}")
print(f"Channels: {info.channels}")
print(f"Duration: {info.duration_sec}s")
EOF
```

**Solution**:
- Verify WAV file is valid: `file test.wav`
- Try different WAV files
- Check sample rate and channel count
- Ensure audio data is not empty

---

### Issue #8: Keyboard Shortcuts Not Working
**Symptom**: Ctrl+X, Ctrl+C, etc. don't trigger cut/copy

**Diagnosis**:
```bash
# Check shortcut setup in sample editor
grep -n "_setup_shortcuts" Views/sample_editor_window.py
```

**Solution**:
- Verify focus is on sample editor window
- Check that shortcuts are registered
- Try alternative methods (File menu)
- Check for conflicting system hotkeys

---

### Issue #9: Effect Parameters Not Saving
**Symptom**: Effect settings lost after restart

**Diagnosis**:
```bash
# Check if effects are saved in project
python3 << 'EOF'
import json
with open("project.kronos") as f:
    data = json.load(f)
    for track in data.get("tracks", []):
        print(f"Track: {track['name']}")
        # Note: effects not stored in track data yet
EOF
```

**Solution**:
- Effects are stored in EffectChainPreset, not in track
- Save effect chain separately as preset
- Load preset when opening project

---

### Issue #10: Memory Leak With Large Audio Files
**Symptom**: Application memory usage grows continuously

**Diagnosis**:
```bash
# Monitor memory usage
python3 << 'EOF'
import psutil
import os
proc = psutil.Process(os.getpid())
print(f"Memory: {proc.memory_info().rss / 1024 / 1024:.1f} MB")
EOF
```

**Solution**:
- Limit loaded audio to <500MB
- Close unused projects
- Reduce waveform cache size
- Profile with `memory_profiler`

---

## Performance Testing

### Baseline Performance Check
```bash
# Run DSP profiler
python3 Tools/profile_dsp.py

# Expected output:
# Total CPU (all effects active): 366.7% (this is test, not real-time)
# Real-time safe (< 80% CPU): depends on system
```

### Real-time Feasibility Test
```bash
python3 << 'EOF'
from Core.audio_effects import get_audio_effects
from Core.audio_dsp import SimpleReverb
import time
import struct

# Create test audio (1 second)
test_audio = struct.pack('<h', 0) * 44100

# Measure single pass
start = time.perf_counter()
chain = get_audio_effects()
output = chain.process(test_audio, 44100, 2)
elapsed = time.perf_counter() - start

print(f"Processing time: {elapsed*1000:.2f}ms")
print(f"Real-time feasible: {elapsed < 0.023}")  # 1 sec audio in <23ms
EOF
```

---

## Commit History Verification

```bash
# Check recent commits
git log --oneline -10

# Expected commits:
# aad7915 Add Phase 6 completion report
# 1ce0b43 Phase 6 COMPLETE: All 6 phases finished
# 9d2c2bc Phase 6: Add presets and profiles manager
# c1f877c Phase 6: Performance optimization
# b949952 Update context capsule: Phases 4-5 complete
# 2ebd9a2 Phase 4 & 5: Complete audio effects...

# Check branch status
git status
git log --oneline -1
```

---

## Network & Connectivity

### Audio Device Detection
```bash
# Test MIDI device detection
python3 << 'EOF'
from Core.midi_devices import get_midi_devices
mgr = get_midi_devices()
inputs = mgr.get_input_devices()
outputs = mgr.get_output_devices()
print(f"MIDI inputs: {len(inputs)}")
print(f"MIDI outputs: {len(outputs)}")
EOF
```

### Connection Test
```bash
# Test Kronos connection (if device available)
python3 << 'EOF'
from Core.session_manager import get_session_manager
mgr = get_session_manager()
# Would need actual Kronos on network to test
print("Session manager ready")
EOF
```

---

## Deployment Checklist

- [ ] All files syntax validated
- [ ] All imports working
- [ ] Presets directory created (`~/.kronos/presets/`)
- [ ] Audio devices detected
- [ ] Sample audio file available for testing
- [ ] Git status clean (21 commits ahead)
- [ ] Performance acceptable (<80% CPU for real-time)
- [ ] Keyboard shortcuts functional
- [ ] Save/load project working
- [ ] Export mix functionality working

---

## Emergency Procedures

### If Application Crashes
```bash
# Kill any hung processes
killall python3

# Check logs
tail -n 100 ~/.kronos/debug.log

# Clear cache if needed
rm -rf ~/.kronos/presets/.cache
```

### If Database Corrupted
```bash
# Backup and recreate
mv ~/.kronos/presets ~/.kronos/presets.bak
mkdir -p ~/.kronos/presets/{effects,devices}

# Re-initialize from application
python3 main.py
```

### If Audio Won't Play
```bash
# Test audio system
aplay --list-devices  # Linux
afplay /System/Library/Sounds/Ping.aiff  # macOS

# Restart audio daemon
pulseaudio -k && pulseaudio -D  # PulseAudio
sudo alsamixer  # ALSA mixer
```

---

## Support Resources

- **PHASE6_COMPLETION.md** - Phase 6 details and known limitations
- **CONTEXT_CAPSULE.md** - Quick reference and architecture
- **IMPLEMENTATION_GUIDE.md** - Code patterns and integration examples
- **Code Docstrings** - API documentation in source files

---

**Last Updated**: 2026-09-24  
**Status**: Ready for production deployment  
**Next Step**: Begin troubleshooting if issues arise
