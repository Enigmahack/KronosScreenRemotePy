import os, sys, importlib, pkgutil, logging, tempfile
os.environ["QT_QPA_PLATFORM"] = "offscreen"
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)
from PySide6.QtWidgets import QApplication


def main():
    app = QApplication([])
    w = None
    with tempfile.TemporaryDirectory(prefix="kr_regress_") as data_dir:
        os.environ["KRONOS_DATA_DIR"] = data_dir
        try:
            bad = []
            n = 0
            for pkg in ("Core", "Models", "Views", "Data", "Objects", "Rendering", "Commands", "Tools", "Utils"):
                for m in pkgutil.walk_packages(
                        [os.path.join(root, pkg)], prefix=pkg + ".",
                        onerror=lambda name: bad.append((name, "package discovery failed"))):
                    try:
                        importlib.import_module(m.name)
                        n += 1
                    except Exception as e:
                        bad.append((m.name, repr(e)))
            print("imported", n, "modules; failures:", bad)
            if bad:
                return 1
            from Models.app_settings import AppSettings
            from Views.main_window import MainWindow
            settings = AppSettings()
            settings.prompt_before_quitting = False
            w = MainWindow(settings)
            print("MainWindow constructed OK")
            return 0
        finally:
            if w is not None:
                w.close()
                app.processEvents()
            logging.shutdown()


if __name__ == "__main__":
    sys.exit(main())
