"""Offline smoke test for the application's Nautilus performance panel."""
import os
from pathlib import Path
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication
from Views.nautilus_perf_surface import NautilusPerfSurface

app = QApplication.instance() or QApplication([])
window = NautilusPerfSurface()
window.show()
app.processEvents()
window.close()
print("Performance surface constructed OK (offline)")
