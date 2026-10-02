"""Track list widget for multi-track audio editing."""
from __future__ import annotations
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QSlider, QLabel, QSpinBox, QDoubleSpinBox, QCheckBox
)
from PySide6.QtGui import QIcon

import Utils.theme as T
from Core.sample_editor import AudioTrack, AudioProject


class TrackWidget(QWidget):
    """Single track control widget."""

    track_changed = Signal()

    def __init__(self, track: AudioTrack, parent=None):
        super().__init__(parent)
        self.track = track
        self._setup_ui()

    def _setup_ui(self):
        """Setup track control UI."""
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(8)

        # Track name label
        self.name_label = QLabel(self.track.name)
        self.name_label.setMinimumWidth(100)
        layout.addWidget(self.name_label)

        # Solo button
        self.solo_btn = QPushButton("S")
        self.solo_btn.setCheckable(True)
        self.solo_btn.setMaximumWidth(30)
        self.solo_btn.toggled.connect(self._on_solo_toggled)
        layout.addWidget(self.solo_btn)

        # Mute button
        self.mute_btn = QPushButton("M")
        self.mute_btn.setCheckable(True)
        self.mute_btn.setMaximumWidth(30)
        self.mute_btn.toggled.connect(self._on_mute_toggled)
        layout.addWidget(self.mute_btn)

        # Volume slider
        layout.addWidget(QLabel("Vol:"))
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setMinimum(0)
        self.volume_slider.setMaximum(200)
        self.volume_slider.setValue(100)
        self.volume_slider.setMaximumWidth(100)
        self.volume_slider.valueChanged.connect(self._on_volume_changed)
        layout.addWidget(self.volume_slider)

        self.volume_label = QLabel("1.0")
        self.volume_label.setMaximumWidth(40)
        layout.addWidget(self.volume_label)

        # Pan slider
        layout.addWidget(QLabel("Pan:"))
        self.pan_slider = QSlider(Qt.Orientation.Horizontal)
        self.pan_slider.setMinimum(-100)
        self.pan_slider.setMaximum(100)
        self.pan_slider.setValue(0)
        self.pan_slider.setMaximumWidth(80)
        self.pan_slider.valueChanged.connect(self._on_pan_changed)
        layout.addWidget(self.pan_slider)

        self.pan_label = QLabel("C")
        self.pan_label.setMaximumWidth(20)
        layout.addWidget(self.pan_label)

        layout.addStretch()

        # Duration label
        self.duration_label = QLabel(f"{self.track.duration_sec:.2f}s")
        self.duration_label.setMaximumWidth(60)
        layout.addWidget(self.duration_label)

    def _on_solo_toggled(self, checked: bool):
        """Handle solo toggle."""
        self.track.solo = checked
        self.track_changed.emit()

    def _on_mute_toggled(self, checked: bool):
        """Handle mute toggle."""
        self.track.mute = checked
        self.track_changed.emit()

    def _on_volume_changed(self, value: int):
        """Handle volume slider change."""
        gain = value / 100.0
        self.track.set_volume(gain)
        self.volume_label.setText(f"{gain:.1f}")
        self.track_changed.emit()

    def _on_pan_changed(self, value: int):
        """Handle pan slider change."""
        pan = value / 100.0
        self.track.set_pan(pan)

        # Update label
        if value < -5:
            pan_text = "L"
        elif value > 5:
            pan_text = "R"
        else:
            pan_text = "C"
        self.pan_label.setText(pan_text)
        self.track_changed.emit()


class TrackListWidget(QWidget):
    """List of audio tracks with controls."""

    track_selected = Signal(int)  # track index
    track_changed = Signal()
    add_track_requested = Signal()
    remove_track_requested = Signal(int)  # track index

    def __init__(self, project: AudioProject, parent=None):
        super().__init__(parent)
        self.project = project
        self.track_widgets = {}
        self._setup_ui()
        self._refresh_tracks()

    def _setup_ui(self):
        """Setup track list UI."""
        layout = QVBoxLayout(self)
        layout.setSpacing(4)

        # Master volume control
        master_layout = QHBoxLayout()
        master_layout.addWidget(QLabel("Master Volume:"))
        self.master_slider = QSlider(Qt.Orientation.Horizontal)
        self.master_slider.setMinimum(0)
        self.master_slider.setMaximum(200)
        self.master_slider.setValue(100)
        self.master_slider.valueChanged.connect(self._on_master_volume_changed)
        master_layout.addWidget(self.master_slider)
        self.master_label = QLabel("1.0")
        self.master_label.setMaximumWidth(40)
        master_layout.addWidget(self.master_label)
        layout.addLayout(master_layout)

        # Track list
        self.track_list = QListWidget()
        self.track_list.itemSelectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self.track_list)

        # Add/remove buttons
        button_layout = QHBoxLayout()
        add_btn = QPushButton("+ Add Track")
        add_btn.clicked.connect(self.add_track_requested.emit)
        button_layout.addWidget(add_btn)

        remove_btn = QPushButton("- Remove Track")
        remove_btn.clicked.connect(self._on_remove_track)
        button_layout.addWidget(remove_btn)

        layout.addLayout(button_layout)

    def _refresh_tracks(self):
        """Refresh track list display."""
        self.track_list.clear()
        self.track_widgets.clear()

        for idx, track in enumerate(self.project.tracks):
            item = QListWidgetItem()
            widget = TrackWidget(track, self)
            widget.track_changed.connect(self.track_changed.emit)
            item.setSizeHint(widget.sizeHint())

            self.track_list.addItem(item)
            self.track_list.setItemWidget(item, widget)
            self.track_widgets[idx] = widget

    def _on_selection_changed(self):
        """Handle track selection change."""
        current_row = self.track_list.currentRow()
        if current_row >= 0:
            self.track_selected.emit(current_row)

    def _on_master_volume_changed(self, value: int):
        """Handle master volume change."""
        gain = value / 100.0
        self.project.master_volume = gain
        self.master_label.setText(f"{gain:.1f}")
        self.track_changed.emit()

    def _on_remove_track(self):
        """Remove selected track."""
        current_row = self.track_list.currentRow()
        if current_row >= 0:
            self.remove_track_requested.emit(current_row)
            self._refresh_tracks()

    def add_track(self, track: AudioTrack):
        """Add a track to the project and refresh display."""
        self.project.add_track(track)
        self._refresh_tracks()

    def remove_track(self, index: int):
        """Remove track from project and refresh display."""
        self.project.remove_track(index)
        self._refresh_tracks()

    def get_selected_track_index(self) -> int:
        """Get index of selected track."""
        return self.track_list.currentRow()

    def get_selected_track(self) -> Optional[AudioTrack]:
        """Get selected track."""
        idx = self.get_selected_track_index()
        if idx >= 0:
            return self.project.get_track(idx)
        return None
