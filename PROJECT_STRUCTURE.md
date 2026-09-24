# Project Structure

This project has been reorganized into logical modules for better maintainability.

## Directory Organization

### `/Core`
Core communication and device services:
- `ctrl_client.py` - Device control/keyboard input
- `sysex_service.py` - MIDI SysEx orchestration
- `stream_receiver.py` - Live stream reception
- `midi_bridge.py` - MIDI bridge transport
- `kronos_sysex.py` - Kronos-specific SysEx handling

### `/Models`
Data models and configuration:
- `app_settings.py` - Application settings management
- `storage.py` - Storage and data persistence
- `models.py` - Data models and structures

### `/Utils`
Utility functions and helpers:
- `theme.py` - UI design tokens and styling
- `char_map.py` - Character mapping utilities
- `key_map.py` - Keyboard mapping utilities
- `image_adjust.py` - Image processing utilities
- `setlist_colors.py` - Set list color definitions

### `/Views`
UI windows and dialogs:
- `main_window.py` - Main application window
- `librarian_shell_window.py` - Librarian/file manager window
- `help_window.py` - Help dialog
- `settings_window.py` - Settings dialog
- `perf_window.py` - Performance monitoring window
- `sysex_tool_window.py` - SysEx tool dialog
- `about_dialog.py` - About dialog

### `/Data`
File and data management:
- `local_library_store.py` - Local library persistence
- `librarian_model.py` - Librarian data model
- `librarian_sysex.py` - Librarian SysEx operations
- `librarian_undo.py` - Undo/redo system
- `changeset_sync.py` - Changeset synchronization
- `merge_cache.py` - Merge cache management
- `library_pull_pipeline.py` - Library pull pipeline
- `pcg_file.py` - PCG file handling
- `blank_template_store.py` - Blank template storage

### `/Objects`
Object body handlers:
- `object_body.py` - Generic object body handling
- `global_body.py` - Global object handling
- `erase_body.py` - Erase operation handling

### `/Rendering`
Display and rendering:
- `overlay_renderer.py` - Screen overlay rendering
- `vu_meter.py` - VU meter widget
- `control_surface.py` - Control surface rendering

### `/Commands`
Command and clipboard handling:
- `command_palette.py` - Command palette
- `batch_clipboard.py` - Batch clipboard operations
- `session_dependency_clipboard.py` - Session dependency clipboard

### `/Tools`
Analysis and processing tools:
- `sysex_dump_collector.py` - SysEx dump collection
- `dependency_scanner.py` - Dependency analysis
- `mode_detector.py` - Mode detection
- `boot_phase_detector.py` - Boot phase detection
- `file_manager.py` - File management utilities
- `setlist_data.py` - Set list data utilities

## Entry Point

- `main.py` - Application entry point (remains in project root)

## Resources

- `/Resources` - Static resources (icons, images, documentation)

## Import Pattern

All imports within the project use the new package structure:

```python
# Instead of: import ctrl_client
# Use: import Core.ctrl_client

# Instead of: from app_settings import AppSettings
# Use: from Models.app_settings import AppSettings

# For aliases:
import Utils.theme as T
import Core.ctrl_client as CtrlClient
```

## Notes

- Each package directory contains an `__init__.py` file to make it a proper Python package
- The project maintains all original functionality - this is purely a reorganization
- All imports have been updated throughout the codebase to reference the new locations
- Resources directory remains in the project root for icon/image asset discovery
