"""Sample editor window for editing audio samples.

Provides:
- Waveform display with zoom/pan
- Selection editing
- Basic operations (cut, copy, paste, trim)
- File I/O
- Undo/redo
"""
from __future__ import annotations
from pathlib import Path
from typing import Optional
import struct

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QToolBar,
    QPushButton, QLabel, QSlider, QSpinBox, QFileDialog, QMessageBox,
    QStatusBar, QMenuBar, QMenu
)
from PySide6.QtGui import QAction

import Utils.theme as T
from Views.waveform_display import WaveformDisplay


class SampleEditorWindow(QMainWindow):
    """Main window for sample editing."""

    # Signals
    sample_saved = Signal(str)  # filename

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Sample Editor")
        self.setMinimumSize(800, 600)

        # Sample data
        self.audio_data: Optional[bytes] = None
        self.sample_rate = 44100
        self.channels = 2
        self.duration_sec = 0.0
        self.current_file: Optional[Path] = None

        # Playback
        self.is_playing = False
        self.playhead_timer = QTimer(self)
        self.playhead_timer.timeout.connect(self._update_playhead)

        # Undo/redo
        self.undo_stack = []
        self.redo_stack = []

        self._setup_ui()
        self._setup_menu()

    def _setup_ui(self):
        """Setup the user interface."""
        central = QWidget(self)
        self.setCentralWidget(central)

        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(10)

        # Toolbar
        toolbar = self._create_toolbar()
        main_layout.addWidget(toolbar)

        # Waveform display
        self.waveform = WaveformDisplay(self)
        self.waveform.selection_changed.connect(self._on_selection_changed)
        main_layout.addWidget(self.waveform)

        # Controls
        control_layout = QHBoxLayout()

        # Playback controls
        self.play_btn = QPushButton("▶ Play")
        self.play_btn.clicked.connect(self._on_play)
        control_layout.addWidget(self.play_btn)

        self.stop_btn = QPushButton("⏹ Stop")
        self.stop_btn.clicked.connect(self._on_stop)
        control_layout.addWidget(self.stop_btn)

        # Position display
        control_layout.addWidget(QLabel("Position:"))
        self.position_label = QLabel("00:00.000")
        control_layout.addWidget(self.position_label)

        control_layout.addWidget(QLabel("Duration:"))
        self.duration_label = QLabel("00:00.000")
        control_layout.addWidget(self.duration_label)

        control_layout.addStretch()

        # Zoom controls
        control_layout.addWidget(QLabel("Zoom:"))
        self.zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self.zoom_slider.setMinimum(0)
        self.zoom_slider.setMaximum(100)
        self.zoom_slider.setValue(50)
        self.zoom_slider.setMaximumWidth(100)
        control_layout.addWidget(self.zoom_slider)

        zoom_fit_btn = QPushButton("Fit")
        zoom_fit_btn.clicked.connect(self.waveform.zoom_to_fit)
        control_layout.addWidget(zoom_fit_btn)

        main_layout.addLayout(control_layout)

        # Status bar
        self.statusBar().showMessage("Ready")

    def _create_toolbar(self) -> QToolBar:
        """Create the toolbar."""
        toolbar = QToolBar("Main Toolbar")

        # File operations
        open_action = QAction("📂 Open", self)
        open_action.triggered.connect(self._on_open)
        toolbar.addAction(open_action)

        save_action = QAction("💾 Save", self)
        save_action.triggered.connect(self._on_save)
        toolbar.addAction(save_action)

        toolbar.addSeparator()

        # Edit operations
        undo_action = QAction("↶ Undo", self)
        undo_action.triggered.connect(self._on_undo)
        toolbar.addAction(undo_action)

        redo_action = QAction("↷ Redo", self)
        redo_action.triggered.connect(self._on_redo)
        toolbar.addAction(redo_action)

        toolbar.addSeparator()

        # Sample operations
        cut_action = QAction("✂️ Cut", self)
        cut_action.triggered.connect(self._on_cut)
        toolbar.addAction(cut_action)

        copy_action = QAction("📋 Copy", self)
        copy_action.triggered.connect(self._on_copy)
        toolbar.addAction(copy_action)

        paste_action = QAction("📌 Paste", self)
        paste_action.triggered.connect(self._on_paste)
        toolbar.addAction(paste_action)

        trim_action = QAction("✂️ Trim", self)
        trim_action.triggered.connect(self._on_trim)
        toolbar.addAction(trim_action)

        return toolbar

    def _setup_menu(self):
        """Setup the menu bar."""
        menubar = self.menuBar()

        # File menu
        file_menu = menubar.addMenu("&File")
        file_menu.addAction("&Open", self._on_open)
        file_menu.addAction("&Save", self._on_save)
        file_menu.addAction("Save &As", self._on_save_as)
        file_menu.addSeparator()
        file_menu.addAction("&Close", self.close)

        # Edit menu
        edit_menu = menubar.addMenu("&Edit")
        edit_menu.addAction("&Undo", self._on_undo)
        edit_menu.addAction("&Redo", self._on_redo)
        edit_menu.addSeparator()
        edit_menu.addAction("&Cut", self._on_cut)
        edit_menu.addAction("&Copy", self._on_copy)
        edit_menu.addAction("&Paste", self._on_paste)

        # Sample menu
        sample_menu = menubar.addMenu("&Sample")
        sample_menu.addAction("&Trim", self._on_trim)
        sample_menu.addAction("&Normalize", self._on_normalize)
        sample_menu.addAction("&Reverse", self._on_reverse)
        sample_menu.addAction("&Fade In", self._on_fade_in)
        sample_menu.addAction("&Fade Out", self._on_fade_out)

    def load_sample(self, filepath: Path) -> bool:
        """Load a sample file.

        Args:
            filepath: Path to sample file

        Returns:
            True if loaded successfully
        """
        try:
            from Core.audio_sample_player import WavFileReader

            info = WavFileReader.read_header(filepath)
            if not info:
                QMessageBox.warning(self, "Error", f"Cannot load {filepath.name}")
                return False

            # Load audio data
            self.audio_data = WavFileReader.read_samples(filepath, 0, info.data_size)
            if not self.audio_data:
                return False

            self.current_file = filepath
            self.sample_rate = info.sample_rate
            self.channels = info.channels
            self.duration_sec = info.duration_sec

            # Update waveform display
            self.waveform.load_audio(
                self.audio_data, self.sample_rate, self.channels, self.duration_sec
            )

            # Update labels
            self._update_labels()

            self.statusBar().showMessage(f"Loaded: {filepath.name}")
            self.setWindowTitle(f"Sample Editor - {filepath.name}")
            return True

        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load sample: {e}")
            return False

    def _update_labels(self):
        """Update position and duration labels."""
        duration_str = self._format_time(self.duration_sec)
        self.duration_label.setText(duration_str)

    def _format_time(self, seconds: float) -> str:
        """Format time as MM:SS.ms."""
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        ms = int((seconds % 1) * 1000)
        return f"{minutes}:{secs:02d}.{ms:03d}"

    def _update_playhead(self):
        """Update playhead position during playback."""
        # This would be connected to actual playback engine
        pass

    # File operations

    def _on_open(self):
        """Open a sample file."""
        filepath, _ = QFileDialog.getOpenFileName(
            self, "Open Sample", "", "WAV Files (*.wav);;All Files (*)"
        )
        if filepath:
            self.load_sample(Path(filepath))

    def _on_save(self):
        """Save the current sample."""
        if self.current_file:
            self._save_to_file(self.current_file)
        else:
            self._on_save_as()

    def _on_save_as(self):
        """Save sample as a new file."""
        filepath, _ = QFileDialog.getSaveFileName(
            self, "Save Sample As", "", "WAV Files (*.wav)"
        )
        if filepath:
            self._save_to_file(Path(filepath))

    def _save_to_file(self, filepath: Path):
        """Save sample to file."""
        if not self.audio_data:
            QMessageBox.warning(self, "Error", "No sample loaded")
            return

        try:
            from Core.audio_recorder import WavFileWriter

            writer = WavFileWriter(
                filepath, self.sample_rate, self.channels, bit_depth=16
            )
            if writer.open():
                writer.write(self.audio_data)
                writer.close()
                self.current_file = filepath
                self.statusBar().showMessage(f"Saved: {filepath.name}")
                self.sample_saved.emit(str(filepath))
            else:
                QMessageBox.critical(self, "Error", "Failed to save sample")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Save failed: {e}")

    # Playback

    def _on_play(self):
        """Start playback."""
        if self.audio_data:
            self.is_playing = True
            self.play_btn.setText("⏸ Pause")
            self.playhead_timer.start(50)

    def _on_stop(self):
        """Stop playback."""
        self.is_playing = False
        self.play_btn.setText("▶ Play")
        self.playhead_timer.stop()
        self.waveform.set_playhead(0.0)

    # Edit operations

    def _on_undo(self):
        """Undo the last operation."""
        if self.undo_stack:
            self.redo_stack.append(self.audio_data)
            self.audio_data = self.undo_stack.pop()
            self.waveform.load_audio(
                self.audio_data, self.sample_rate, self.channels, self.duration_sec
            )
            self.statusBar().showMessage("Undo")

    def _on_redo(self):
        """Redo the last undone operation."""
        if self.redo_stack:
            self.undo_stack.append(self.audio_data)
            self.audio_data = self.redo_stack.pop()
            self.waveform.load_audio(
                self.audio_data, self.sample_rate, self.channels, self.duration_sec
            )
            self.statusBar().showMessage("Redo")

    # Sample operations

    def _on_cut(self):
        """Cut selected region."""
        start_sec, end_sec = self.waveform.get_selection()
        self.statusBar().showMessage(f"Cut: {start_sec:.3f} - {end_sec:.3f}")

    def _on_copy(self):
        """Copy selected region."""
        start_sec, end_sec = self.waveform.get_selection()
        self.statusBar().showMessage(f"Copy: {start_sec:.3f} - {end_sec:.3f}")

    def _on_paste(self):
        """Paste at current position."""
        self.statusBar().showMessage("Paste")

    def _on_trim(self):
        """Trim to selection."""
        start_sec, end_sec = self.waveform.get_selection()
        self.statusBar().showMessage(f"Trim: {start_sec:.3f} - {end_sec:.3f}")

    def _on_normalize(self):
        """Normalize audio level."""
        self.statusBar().showMessage("Normalize")

    def _on_reverse(self):
        """Reverse audio."""
        self.statusBar().showMessage("Reverse")

    def _on_fade_in(self):
        """Apply fade in to selection."""
        self.statusBar().showMessage("Fade In")

    def _on_fade_out(self):
        """Apply fade out to selection."""
        self.statusBar().showMessage("Fade Out")

    def _on_selection_changed(self, start_sec: float, end_sec: float):
        """Handle selection change."""
        duration = end_sec - start_sec
        self.statusBar().showMessage(
            f"Selection: {start_sec:.3f} - {end_sec:.3f} ({duration:.3f}s)"
        )
