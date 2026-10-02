import os, sys, importlib, pkgutil
os.environ["QT_QPA_PLATFORM"] = "offscreen"
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)
from PySide6.QtWidgets import QApplication
app = QApplication([])
bad = []
n = 0
for pkg in ("Core", "Models", "Views", "Data", "Objects", "Rendering", "Commands", "Tools", "Utils"):
    for m in pkgutil.iter_modules([os.path.join(root, pkg)]):
        name = f"{pkg}.{m.name}"
        try:
            importlib.import_module(name); n += 1
        except Exception as e:
            bad.append((name, repr(e)))
print("imported", n, "modules; failures:", bad)
from Models.app_settings import AppSettings
from Views.main_window import MainWindow
w = MainWindow(AppSettings())
print("MainWindow constructed OK")
