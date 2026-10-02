"""Panel for managing audio cues and regions."""
from __future__ import annotations
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTabWidget,
    QListWidget, QListWidgetItem, QPushButton, QLabel,
    QLineEdit, QSpinBox, QDoubleSpinBox, QDialog, QFormLayout,
    QColorDialog
)
from PySide6.QtGui import QColor

import Utils.theme as T
from Core.sample_editor import AudioTrack, AudioCue, AudioRegion


class CueDialog(QDialog):
    """Dialog for creating/editing cues."""

    def __init__(self, cue: Optional[AudioCue] = None, parent=None):
        super().__init__(parent)
        self.cue = cue or AudioCue("Cue", 0.0)
        self.setWindowTitle("Add/Edit Cue")
        self.setMinimumWidth(300)
        self._setup_ui()

    def _setup_ui(self):
        """Setup dialog UI."""
        layout = QFormLayout(self)

        # Name
        self.name_input = QLineEdit(self.cue.name)
        layout.addRow("Name:", self.name_input)

        # Position
        self.pos_spin = QDoubleSpinBox()
        self.pos_spin.setMinimum(0.0)
        self.pos_spin.setMaximum(3600.0)
        self.pos_spin.setValue(self.cue.position_sec)
        self.pos_spin.setSuffix(" sec")
        layout.addRow("Position:", self.pos_spin)

        # Color
        color_layout = QHBoxLayout()
        self.color_label = QLabel()
        self.color_label.setMinimumHeight(30)
        self.color_label.setStyleSheet(f"background-color: {self.cue.color};")
        color_layout.addWidget(self.color_label)

        self.color_btn = QPushButton("Choose Color…")
        self.color_btn.clicked.connect(self._on_choose_color)
        color_layout.addWidget(self.color_btn)
        layout.addRow("Color:", color_layout)

        # Buttons
        button_layout = QHBoxLayout()
        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        button_layout.addStretch()
        button_layout.addWidget(ok_btn)
        button_layout.addWidget(cancel_btn)
        layout.addRow(button_layout)

    def _on_choose_color(self):
        """Choose cue color."""
        color = QColorDialog.getColor(QColor(self.cue.color), self)
        if color.isValid():
            self.cue.color = color.name()
            self.color_label.setStyleSheet(f"background-color: {self.cue.color};")

    def get_cue(self) -> AudioCue:
        """Get the cue from dialog."""
        self.cue.name = self.name_input.text()
        self.cue.position_sec = self.pos_spin.value()
        return self.cue


class RegionDialog(QDialog):
    """Dialog for creating/editing regions."""

    def __init__(self, region: Optional[AudioRegion] = None, parent=None):
        super().__init__(parent)
        self.region = region or AudioRegion("Region", 0.0, 1.0)
        self.setWindowTitle("Add/Edit Region")
        self.setMinimumWidth(300)
        self._setup_ui()

    def _setup_ui(self):
        """Setup dialog UI."""
        layout = QFormLayout(self)

        # Name
        self.name_input = QLineEdit(self.region.name)
        layout.addRow("Name:", self.name_input)

        # Start position
        self.start_spin = QDoubleSpinBox()
        self.start_spin.setMinimum(0.0)
        self.start_spin.setMaximum(3600.0)
        self.start_spin.setValue(self.region.start_sec)
        self.start_spin.setSuffix(" sec")
        layout.addRow("Start:", self.start_spin)

        # End position
        self.end_spin = QDoubleSpinBox()
        self.end_spin.setMinimum(0.0)
        self.end_spin.setMaximum(3600.0)
        self.end_spin.setValue(self.region.end_sec)
        self.end_spin.setSuffix(" sec")
        layout.addRow("End:", self.end_spin)

        # Color
        color_layout = QHBoxLayout()
        self.color_label = QLabel()
        self.color_label.setMinimumHeight(30)
        self.color_label.setStyleSheet(f"background-color: {self.region.color};")
        color_layout.addWidget(self.color_label)

        self.color_btn = QPushButton("Choose Color…")
        self.color_btn.clicked.connect(self._on_choose_color)
        color_layout.addWidget(self.color_btn)
        layout.addRow("Color:", color_layout)

        # Buttons
        button_layout = QHBoxLayout()
        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        button_layout.addStretch()
        button_layout.addWidget(ok_btn)
        button_layout.addWidget(cancel_btn)
        layout.addRow(button_layout)

    def _on_choose_color(self):
        """Choose region color."""
        color = QColorDialog.getColor(QColor(self.region.color), self)
        if color.isValid():
            self.region.color = color.name()
            self.color_label.setStyleSheet(f"background-color: {self.region.color};")

    def get_region(self) -> AudioRegion:
        """Get the region from dialog."""
        self.region.name = self.name_input.text()
        self.region.start_sec = self.start_spin.value()
        self.region.end_sec = self.end_spin.value()
        return self.region


class CueRegionPanel(QWidget):
    """Panel for managing cues and regions."""

    cue_added = Signal(AudioCue)
    region_added = Signal(AudioRegion)
    selection_changed = Signal(float, float)  # start_sec, end_sec

    def __init__(self, track: Optional[AudioTrack] = None, parent=None):
        super().__init__(parent)
        self.track = track
        self._setup_ui()
        if track:
            self._refresh_lists()

    def _setup_ui(self):
        """Setup panel UI."""
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # Tab widget for cues and regions
        self.tabs = QTabWidget()

        # Cues tab
        cues_widget = QWidget()
        cues_layout = QVBoxLayout(cues_widget)

        self.cue_list = QListWidget()
        cues_layout.addWidget(QLabel("Cues:"))
        cues_layout.addWidget(self.cue_list)

        cue_buttons_layout = QHBoxLayout()
        add_cue_btn = QPushButton("+ Add Cue")
        add_cue_btn.clicked.connect(self._on_add_cue)
        cue_buttons_layout.addWidget(add_cue_btn)

        edit_cue_btn = QPushButton("✎ Edit")
        edit_cue_btn.clicked.connect(self._on_edit_cue)
        cue_buttons_layout.addWidget(edit_cue_btn)

        remove_cue_btn = QPushButton("- Remove")
        remove_cue_btn.clicked.connect(self._on_remove_cue)
        cue_buttons_layout.addWidget(remove_cue_btn)

        cues_layout.addLayout(cue_buttons_layout)
        self.tabs.addTab(cues_widget, "Cues")

        # Regions tab
        regions_widget = QWidget()
        regions_layout = QVBoxLayout(regions_widget)

        self.region_list = QListWidget()
        regions_layout.addWidget(QLabel("Regions:"))
        regions_layout.addWidget(self.region_list)

        region_buttons_layout = QHBoxLayout()
        add_region_btn = QPushButton("+ Add Region")
        add_region_btn.clicked.connect(self._on_add_region)
        region_buttons_layout.addWidget(add_region_btn)

        edit_region_btn = QPushButton("✎ Edit")
        edit_region_btn.clicked.connect(self._on_edit_region)
        region_buttons_layout.addWidget(edit_region_btn)

        remove_region_btn = QPushButton("- Remove")
        remove_region_btn.clicked.connect(self._on_remove_region)
        region_buttons_layout.addWidget(remove_region_btn)

        regions_layout.addLayout(region_buttons_layout)
        self.tabs.addTab(regions_widget, "Regions")

        layout.addWidget(self.tabs)

    def set_track(self, track: AudioTrack):
        """Set the track to manage cues/regions for."""
        self.track = track
        self._refresh_lists()

    def _refresh_lists(self):
        """Refresh cue and region lists from track."""
        if not self.track:
            return

        # Refresh cues
        self.cue_list.clear()
        for cue in self.track.cues:
            item_text = f"{cue.name} @ {cue.position_sec:.3f}s"
            item = QListWidgetItem(item_text)
            item.setData(Qt.ItemDataRole.UserRole, cue)
            self.cue_list.addItem(item)

        # Refresh regions
        self.region_list.clear()
        for region in self.track.regions:
            item_text = f"{region.name} [{region.start_sec:.3f}s - {region.end_sec:.3f}s]"
            item = QListWidgetItem(item_text)
            item.setData(Qt.ItemDataRole.UserRole, region)
            self.region_list.addItem(item)

    def _on_add_cue(self):
        """Add a new cue."""
        if not self.track:
            return

        dlg = CueDialog(parent=self)
        if dlg.exec():
            cue = dlg.get_cue()
            if self.track.add_cue(cue):
                self.cue_added.emit(cue)
                self._refresh_lists()

    def _on_edit_cue(self):
        """Edit selected cue."""
        if not self.track:
            return

        current_item = self.cue_list.currentItem()
        if current_item:
            cue = current_item.data(Qt.ItemDataRole.UserRole)
            dlg = CueDialog(cue, self)
            if dlg.exec():
                self._refresh_lists()

    def _on_remove_cue(self):
        """Remove selected cue."""
        if not self.track:
            return

        row = self.cue_list.currentRow()
        if row >= 0:
            self.track.remove_cue(row)
            self._refresh_lists()

    def _on_add_region(self):
        """Add a new region."""
        if not self.track:
            return

        dlg = RegionDialog(parent=self)
        if dlg.exec():
            region = dlg.get_region()
            if self.track.add_region(region):
                self.region_added.emit(region)
                self._refresh_lists()
                self.selection_changed.emit(region.start_sec, region.end_sec)

    def _on_edit_region(self):
        """Edit selected region."""
        if not self.track:
            return

        current_item = self.region_list.currentItem()
        if current_item:
            region = current_item.data(Qt.ItemDataRole.UserRole)
            dlg = RegionDialog(region, self)
            if dlg.exec():
                self._refresh_lists()
                self.selection_changed.emit(region.start_sec, region.end_sec)

    def _on_remove_region(self):
        """Remove selected region."""
        if not self.track:
            return

        row = self.region_list.currentRow()
        if row >= 0:
            self.track.remove_region(row)
            self._refresh_lists()
