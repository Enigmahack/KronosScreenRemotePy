"""Headless checks for the manual control-surface tester."""
import os
import sys
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication

from manual_control_surface import ControlSurfaceTestWindow


app = QApplication.instance() or QApplication([])
for family in ("kronos", "nautilus"):
    window = ControlSurfaceTestWindow(family)
    assert window.surface._active_skin == family
    assert window.mode_group.isEnabled() == (family == "kronos")
    assert window.naut_group.isEnabled() == (family == "nautilus")
    steps = []
    window.surface.wheel_step.connect(steps.append)
    for reverse in (False, True):
        window.chk_reverse.setChecked(reverse)
        event = QWheelEvent(QPointF(), QPointF(), QPoint(), QPoint(0, 120),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase, False)
        window.surface.wheelEvent(event)
    assert steps == [1, -1]
    window.cmb_mode.setCurrentText("Program")
    assert window.surface._btn_map["Program"].active
    window._manual_toggle("EXIT", True)
    assert window.surface._pressed_btn.name == ("NAUT_EXIT" if family == "nautilus" else "EXIT")
    window._manual_toggle("EXIT", False)
    assert window.surface._pressed_btn is None
    window.cmb_family.setCurrentText("Nautilus" if family == "kronos" else "Kronos")
    assert window.surface._active_skin != family
    window.close()
print("Manual control surface OK")
