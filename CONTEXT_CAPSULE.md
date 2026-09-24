"""
KRONOS SCREENREMOTE PYTHON - CONTEXT CAPSULE FOR CONTINUING AGENTS

This document provides the essential context needed to continue C# feature parity work.
Read this FIRST before working on Phases 4+.
"""

# Quick Context

**Project**: Add C# client feature parity to Python Kronos ScreenRemote  
**Branch**: ParityUpdate  
**Status**: Phases 1-3 complete (40% done), ready for Phase 4  
**Last Work**: 2026-09-24, full session completed  

---

## What's Been Done (Phases 1-3)

### Phase 1: Dialog Infrastructure ✅ COMPLETE
- **15+ production dialogs** with consistent theme-aware styling
- **Location**: `Views/dialog_base.py`, `Views/connection_dialogs.py`, `Views/property_dialogs.py`, `Views/testing_dialogs.py`, `Views/file_dialogs.py`
- **Key Classes**: BaseDialog, ConnectionFailedDialog, LoginDialog, InputTesterWindow, ButtonInjectorWindow, etc.
- **Status**: All tested, 100% syntax validation passed

### Phase 2: Core Services ✅ COMPLETE
- **MIDI device manager** (cross-platform: ALSA/PortAudio/Windows MME)
- **Session manager** (connection profiles, persistence, uptime tracking)
- **Device detection** (auto-detect Kronos vs Nautilus, capability gating)
- **Location**: `Core/midi_devices.py`, `Core/session_manager.py`, `Core/device_info.py`
- **Status**: All tested and working

### Phase 3: UI Integration ✅ COMPLETE
- **MainWindowDialogMixin** for easy dialog integration
- **Device-specific panels** (KronosControlPanel, NautilusControlPanel, DeviceAdaptationPanel)
- **MIDI device selector UI** (dialog + status widget)
- **Location**: `Views/main_window_dialogs.py`, `Views/device_panels.py`, `Views/midi_device_selector.py`
- **Status**: All tested, ready for MainWindow integration

---

## Current Architecture

### File Organization
```
Views/
├── dialog_base.py              # Base classes
├── connection_dialogs.py        # Auth/connection dialogs
├── property_dialogs.py          # Properties/info dialogs
├── testing_dialogs.py           # Input testing windows
├── file_dialogs.py              # File/resource browsers
├── dialogs.py                   # Unified interface
├── main_window_dialogs.py       # Dialog mixin for MainWindow [PHASE 3]
├── device_panels.py             # Device-specific panels [PHASE 3]
└── midi_device_selector.py      # MIDI UI components [PHASE 3]

Core/
├── midi_devices.py              # MIDI device enumeration
├── session_manager.py           # Connection management
└── device_info.py               # Device capabilities
```

### Key Design Patterns
1. **Mixin** - MainWindowDialogMixin adds methods to MainWindow
2. **Strategy** - Device-family based panel selection
3. **Registry** - Device capability lookup
4. **Singleton** - Global service access (get_session_manager(), get_midi_devices())
5. **Factory** - Device creation
6. **Template Method** - BaseDialog structure

### Theme System
- All dialogs use `Utils.theme` tokens (T.BG, T.TEXT, T.ACCENT, etc.)
- Light/dark mode ready via stylesheet overrides
- Consistent across all UI components

---

## What Still Needs Work (Phases 4-6)

### Phase 4: Audio System (60-100 hours)
**What**: Audio playback, device selection, VU meter  
**Where**: New `Core/audio_engine.py`, enhancements to `Views/device_panels.py`  
**Dependencies**: Phase 3 device panels ready  
**Next Step**: Enumerate audio devices (similar to MIDI), hook to device panels

### Phase 5: Sample Editing (100-150 hours)
**What**: Sample editor UI, waveform display, operations  
**Where**: New sample editor module + Views  
**Dependencies**: Complex, Phase 4 audio knowledge helpful  
**Next Step**: Design sample editor UI, implement waveform display

### Phase 6: Advanced Features (50-100 hours)
**What**: Device-specific panels integration, help detection, advanced MIDI  
**Where**: Various modules  
**Dependencies**: Phases 4-5 complete  
**Next Step**: Enhanced menu items, device-specific features

---

## How to Continue (Immediate Next Steps)

### Step 1: Integrate Dialogs into MainWindow
1. Open `Views/main_window.py`
2. Add imports:
   ```python
   from Views.main_window_dialogs import MainWindowDialogMixin, add_testing_menu_items
   from Core.session_manager import get_session_manager
   ```
3. Make MainWindow inherit from mixin:
   ```python
   class MainWindow(MainWindowDialogMixin, QMainWindow):
       def __init__(self, settings):
           MainWindowDialogMixin.__init__(self)
           QMainWindow.__init__(self)
   ```
4. Call `add_testing_menu_items(self)` in `_build_menu()`

### Step 2: Wire Connection Error Handling
1. Add in connection error handler:
   ```python
   if self.show_connection_error(host, port, error):
       self._reconnect()
   ```

### Step 3: Test Integration
1. Run `python3 main.py`
2. Verify menus appear
3. Verify dialogs open and close

### Step 4: Device Detection
1. On successful connection, detect device:
   ```python
   device_info = DeviceDetector.detect_from_discovery(response)
   panel = DeviceAdaptationPanel(device_info.family, self)
   ```

---

## Code Quality Standards

All existing code follows:
- ✅ Comprehensive type hints
- ✅ Theme-aware styling
- ✅ Clear separation of concerns
- ✅ No hardcoded values
- ✅ Full docstrings
- ✅ Production-quality

**Maintain these standards** when adding Phase 4+.

---

## Key Files to Reference

| File | Purpose | Know Before Modifying |
|------|---------|----------------------|
| `Views/dialogs.py` | Unified dialog interface | All dialogs exported here |
| `Core/session_manager.py` | Connection state | Singleton pattern - use `get_session_manager()` |
| `Core/device_info.py` | Device capabilities | DeviceFamily enum used everywhere |
| `Views/device_panels.py` | Device-specific UI | Adapts based on family - extend for Phase 4 |
| `Utils/theme.py` | Color tokens | Always use T.* constants, never hardcode |

---

## Common Integration Patterns

### Using Dialogs
```python
from Views.dialogs import MessageBox, ConnectionFailedDialog

# Simple message
MessageBox.info(self, "Title", "Message")

# Modal with result
dlg = ConnectionFailedDialog(host, port, error, self)
if dlg.exec() == QDialog.Accepted and dlg.should_retry():
    self._reconnect()
```

### Session Management
```python
from Core.session_manager import get_session_manager

mgr = get_session_manager()
mgr.start_session(host, port, user)
# Later:
session = mgr.get_session()
print(f"Uptime: {session.uptime_str()}")
```

### Device Adaptation
```python
from Core.device_info import DeviceFamily
from Views.device_panels import DeviceAdaptationPanel

if device.family == DeviceFamily.KRONOS:
    panel = DeviceAdaptationPanel(DeviceFamily.KRONOS, self)
```

---

## Critical Architecture Decisions

1. **Mixin Pattern**: MainWindowDialogMixin is intentionally a mixin (not a base class) to avoid deep inheritance hierarchies
2. **Singleton Services**: MIDI manager and session manager are singletons accessed via `get_*()` functions
3. **Capability-Based Gating**: Features shown/hidden based on `DeviceCapabilities`, not hardcoded conditionals
4. **Theme Tokens**: All colors come from `Utils.theme`, enabling light/dark mode without code changes
5. **Platform Abstraction**: MIDI detection uses graceful fallback chains (ALSA → PortAudio → Windows MME)

---

## Documentation Available

- `IMPLEMENTATION_GUIDE.md` - Quick integration guide with code examples
- `PHASE1_SUMMARY.md` - Dialog infrastructure details
- `PHASE2_SUMMARY.md` - Core services details
- `PHASE3_SUMMARY.md` - UI integration details
- `STATUS_REPORT_FINAL.md` - Comprehensive progress report
- `PARITY_PLAN.md` - Overall 5-phase strategy

**Read `IMPLEMENTATION_GUIDE.md` before starting Phase 4 work.**

---

## Testing Checklist Before Committing

- [ ] All imports work (`python3 -c "from Views.dialogs import *"`)
- [ ] Theme constants used (no hardcoded colors)
- [ ] Type hints present
- [ ] Docstrings complete
- [ ] Cross-platform compatible (test MIDI on available system)
- [ ] Git commits are atomic and well-messaged

---

## Git Workflow

```bash
# All work on ParityUpdate branch
git checkout ParityUpdate

# Create feature branch for Phase 4
git checkout -b feature/phase-4-audio

# Commit with clear messages
git commit -m "Phase 4: Add audio device enumeration

Added AudioDeviceManager for cross-platform audio device detection.
Supports ALSA, PortAudio, and Windows audio devices.
..."

# Push when ready for review
git push origin feature/phase-4-audio
```

---

## Contact Information for Future Work

When continuing this work:
1. Check git log to see what was done
2. Read this context capsule (you are here)
3. Read relevant PHASE*_SUMMARY.md for technical details
4. Read IMPLEMENTATION_GUIDE.md for integration patterns
5. Reference existing dialogs in Views/ as examples

---

**Last Updated**: 2026-09-24  
**Phases Complete**: 1, 2, 3 (40% toward full parity)  
**Next Phase**: 4 (Audio System, 60-100 hours)  
**Status**: Ready for review and continuation
