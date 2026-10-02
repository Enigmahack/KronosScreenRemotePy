"""Real-time audio effect preview panel for sample editor."""
from __future__ import annotations
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
    QPushButton, QLabel, QSlider, QCheckBox, QListWidget,
    QListWidgetItem
)

import Utils.theme as T
from Core.audio_effects import get_audio_effects, AudioEffect, AudioEffectChain


class EffectControlWidget(QWidget):
    """Control widget for a single effect."""

    effect_changed = Signal()

    def __init__(self, effect: AudioEffect, parent=None):
        super().__init__(parent)
        self.effect = effect
        self._setup_ui()

    def _setup_ui(self):
        """Setup effect control UI."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        # Effect name and enable toggle
        header_layout = QHBoxLayout()
        self.enable_check = QCheckBox(self.effect.name)
        self.enable_check.setChecked(self.effect.enabled)
        self.enable_check.toggled.connect(self._on_enabled_toggled)
        header_layout.addWidget(self.enable_check)
        header_layout.addStretch()
        layout.addLayout(header_layout)

        # Parameter controls
        for param_name, param in self.effect.parameters.items():
            param_layout = QHBoxLayout()
            param_layout.addWidget(QLabel(param_name + ":"))

            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setMinimum(int(param.min_value * 100))
            slider.setMaximum(int(param.max_value * 100))
            slider.setValue(int(param.current_value * 100))

            # Store parameter reference in slider for callback
            slider.param = param
            slider.param_name = param_name
            slider.valueChanged.connect(self._on_param_changed)
            param_layout.addWidget(slider)

            value_label = QLabel(f"{param.current_value:.1f}")
            value_label.setMaximumWidth(60)
            value_label.param_name = param_name
            param_layout.addWidget(value_label)

            layout.addLayout(param_layout)
            self._param_labels = getattr(self, '_param_labels', {})
            self._param_labels[param_name] = value_label

        layout.addStretch()

    def _on_enabled_toggled(self, checked: bool):
        """Handle effect enable/disable."""
        self.effect.enabled = checked
        self.effect_changed.emit()

    def _on_param_changed(self, value: int):
        """Handle parameter slider change."""
        sender = self.sender()
        if hasattr(sender, 'param'):
            sender.param.set_value(value / 100.0)

            # Update label
            if hasattr(sender, 'param_name') and hasattr(self, '_param_labels'):
                label = self._param_labels.get(sender.param_name)
                if label:
                    label.setText(f"{sender.param.current_value:.2f}")

            self.effect_changed.emit()


class EffectPreviewPanel(QWidget):
    """Panel for real-time effect preview and control."""

    effects_changed = Signal()
    preview_enabled_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.effect_chain = get_audio_effects()
        self.effect_widgets = {}
        self._setup_ui()

    def _setup_ui(self):
        """Setup effect preview panel."""
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # Preview enable/disable toggle
        preview_layout = QHBoxLayout()
        self.preview_check = QCheckBox("Enable Effect Preview")
        self.preview_check.setChecked(False)
        self.preview_check.toggled.connect(self._on_preview_toggled)
        preview_layout.addWidget(self.preview_check)
        preview_layout.addStretch()
        layout.addLayout(preview_layout)

        layout.addWidget(QLabel("Effects:"))

        # Effect list
        self.effect_scroll = QVBoxLayout()
        effect_group = QGroupBox("Active Effects")
        effect_group.setLayout(self.effect_scroll)
        layout.addWidget(effect_group)

        # Populate with current effects
        for effect in self.effect_chain.effects:
            widget = EffectControlWidget(effect, self)
            widget.effect_changed.connect(self.effects_changed.emit)
            self.effect_scroll.addWidget(widget)
            self.effect_widgets[effect.name] = widget

        self.effect_scroll.addStretch()

        # Reset button
        reset_btn = QPushButton("Reset All Effects")
        reset_btn.clicked.connect(self._on_reset_effects)
        layout.addWidget(reset_btn)

        layout.addStretch()

    def _on_preview_toggled(self, checked: bool):
        """Handle preview enable/disable."""
        self.preview_enabled_changed.emit(checked)

    def _on_reset_effects(self):
        """Reset all effects to default."""
        for effect in self.effect_chain.effects:
            for param in effect.parameters.values():
                param.set_value(param.default_value)
                effect.enabled = True

        # Update UI
        for widget in self.effect_widgets.values():
            widget._setup_ui()

        self.effects_changed.emit()

    def is_preview_enabled(self) -> bool:
        """Check if effect preview is enabled."""
        return self.preview_check.isChecked()

    def get_effect_chain(self) -> AudioEffectChain:
        """Get the effect chain."""
        return self.effect_chain
