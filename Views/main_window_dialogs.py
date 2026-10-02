"""Dialog integration for MainWindow - adds testing and utility dialogs."""
from __future__ import annotations
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog

from Views.dialogs import (
    ObjectInfoDialog,
    InputTesterWindow,
    ButtonInjectorWindow,
    MessageBox,
)


class MainWindowDialogMixin:
    """Mixin to add dialog functionality to MainWindow."""

    def __init__(self):
        super().__init__()
        self._input_tester = None
        self._button_injector = None
        self._sample_editor_win = None

    def show_object_info(self, obj_type: str, bank: int, number: int,
                        name: str, properties: dict):
        """Show object information dialog."""
        dlg = ObjectInfoDialog(obj_type, bank, number, name, properties, self)
        dlg.exec()

    def show_input_tester(self):
        """Show input testing window (non-modal)."""
        if self._input_tester is None:
            self._input_tester = InputTesterWindow(self)
        self._input_tester.show()
        self._input_tester.raise_()
        self._input_tester.activateWindow()

    def show_button_injector(self):
        """Show button injector window (non-modal)."""
        if self._button_injector is None:
            self._button_injector = ButtonInjectorWindow(self)
        self._button_injector.show()
        self._button_injector.raise_()
        self._button_injector.activateWindow()

    # Menu action handlers for testing/debugging

    def on_show_input_tester(self):
        """Action handler for input tester menu item."""
        self.show_input_tester()

    def on_show_button_injector(self):
        """Action handler for button injector menu item."""
        self.show_button_injector()

    def on_show_device_info(self):
        """Show current device information — real, live connection state plus
        a fresh MODEL query (docs/api.md §7 MODEL) rather than a cached/fake
        session object. Runs the query off the UI thread (matches Views/
        perf_window.py's SYSINFO-poll pattern) since it blocks on the network."""
        if not self._host or not self._receiver:
            MessageBox.warning(self, "Not Connected",
                              "No Kronos is currently connected.")
            return

        properties = {
            "Host": self._host,
            "Stream Port": str(self._stream_port),
            "Control Port": str(self._ctrl_port),
            "Stream Format": "RGB565LE (Nautilus)" if self._receiver.stream_fmt == 1
                              else "INDEX8 (Kronos)",
            "Screen": f"{self._receiver.width}x{self._receiver.height}",
            "Measured FPS": f"{self._measured_fps:.1f}",
            "Stream Mode": "Pull" if self._pull_mode else "Change",
        }
        self._fetch_model_for_device_info(properties)

    def _fetch_model_for_device_info(self, properties: dict):
        import threading
        import Core.ctrl_client as CC
        from PySide6.QtCore import QTimer
        host, port = self._host, self._ctrl_port

        def fetch():
            resp = CC.get().query(host, port, "MODEL", timeout_ms=1500)
            try:
                QTimer.singleShot(0, self, lambda: self._show_device_info(properties, resp))
            except RuntimeError:
                pass  # window closed before the query finished

        threading.Thread(target=fetch, daemon=True, name="ModelQuery").start()

    def _show_device_info(self, properties: dict, model_resp: Optional[str]):
        from Core.device_family import ModelInfo
        info = ModelInfo.parse(model_resp)
        family = info.family_raw or "unknown"
        model = info.model
        if model_resp and (info.family_raw or info.model):
            def n(v) -> str:
                return "?" if v is None else str(v)
            properties.update({
                "Family": family,
                "Model": model,
                "Board": info.board or "unknown",
                "Panel Hardware Version": n(info.panel_hwver),
                "Framebuffer BPP": n(info.fb_bpp),
                "CPUs / Cores / Threads": f"{n(info.cpus)} / {n(info.cores)} / {n(info.threads)}",
                "Daemon Stream": (f"{info.stream_fmt} {info.stream_geom[0]}x{info.stream_geom[1]}"
                                  if info.stream_fmt and info.stream_geom else "unknown"),
            })
        else:
            properties["Model Query"] = "No response from daemon (older than 3.0.2?)"

        self.show_object_info(
            "Device Info", 0, 0,
            f"{family} {model}".strip() or self._host,
            properties,
        )

    def show_audio_config(self) -> bool:
        """Show audio configuration dialog.

        Returns:
            True if audio config was confirmed, False if canceled
        """
        from Views.audio_controls import AudioDeviceDialog
        dlg = AudioDeviceDialog(self)
        if dlg.exec() == QDialog.Accepted:
            return True
        return False

    def on_show_audio_config(self):
        """Action handler for audio configuration menu item."""
        self.show_audio_config()

    def show_recording_dialog(self) -> bool:
        """Show audio recording dialog.

        Returns:
            True if recording dialog was shown
        """
        from Views.audio_controls import RecordingControlPanel
        from PySide6.QtWidgets import QDialog

        dialog = QDialog(self)
        dialog.setWindowTitle("Audio Recording")
        dialog.setMinimumWidth(500)
        dialog.setMinimumHeight(400)

        layout = QVBoxLayout(dialog)
        recording_panel = RecordingControlPanel(dialog)
        layout.addWidget(recording_panel)

        dialog.exec()
        return True

    def on_show_recording(self):
        """Action handler for recording menu item."""
        self.show_recording_dialog()

    def show_sample_editor(self) -> bool:
        """Show the Sample Editor window (real FTP-backed .KSC/.KMP/.KSF
        browsing/editing — see Views/sample_editor_window.py). Same
        singleton-window + credential pattern as _open_file_manager."""
        if self._sample_editor_win is not None:
            self._sample_editor_win.raise_()
            self._sample_editor_win.activateWindow()
            return True
        if not self._ensure_ftp_credentials():
            return False
        host = self._host or self._settings.kronos_host
        if not host:
            MessageBox.warning(self, "Sample Editor",
                              "No Kronos host configured. Set it in Settings first.")
            return False
        from Views.sample_editor_window import SampleEditorWindow
        self._sample_editor_win = SampleEditorWindow(
            host, self._settings.ftp_port,
            self._settings.ftp_username, self._settings.ftp_password, self)
        self._sample_editor_win.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._sample_editor_win.destroyed.connect(lambda: setattr(self, '_sample_editor_win', None))
        self._sample_editor_win.show()
        return True

    def on_show_sample_editor(self):
        """Action handler for sample editor menu item."""
        self.show_sample_editor()

    def show_equalizer(self) -> bool:
        """Show equalizer control dialog.

        Returns:
            True if dialog was shown
        """
        from Views.audio_controls import EqualizerDialog

        dlg = EqualizerDialog(self)
        dlg.exec()
        return True

    def on_show_equalizer(self):
        """Action handler for equalizer menu item."""
        self.show_equalizer()


def add_testing_menu_items(main_window):
    """Add testing/debugging menu items to Tools menu."""
    if not hasattr(main_window, '_tools_menu_separator_added'):
        # Add separator and testing submenu if not already done
        tools_menu = main_window._tools_menu

        tools_menu.addSeparator()

        # Testing submenu
        testing_menu = tools_menu.addMenu("&Testing && &Diagnostics")

        # Input tester
        act_input_tester = testing_menu.addAction("&Input Tester…")
        act_input_tester.triggered.connect(main_window.on_show_input_tester)
        main_window._act_input_tester = act_input_tester

        # Button injector
        act_button_injector = testing_menu.addAction("&Button Injector…")
        act_button_injector.triggered.connect(main_window.on_show_button_injector)
        main_window._act_button_injector = act_button_injector

        testing_menu.addSeparator()

        # Audio configuration
        act_audio_config = testing_menu.addAction("&Audio Configuration…")
        act_audio_config.triggered.connect(main_window.on_show_audio_config)
        main_window._act_audio_config = act_audio_config

        # Audio recording
        act_recording = testing_menu.addAction("&Audio Recording…")
        act_recording.triggered.connect(main_window.on_show_recording)
        main_window._act_recording = act_recording

        # Equalizer
        act_eq = testing_menu.addAction("3-Band &Equalizer…")
        act_eq.triggered.connect(main_window.on_show_equalizer)
        main_window._act_eq = act_eq

        testing_menu.addSeparator()

        # Device info
        act_device_info = testing_menu.addAction("&Device Information…")
        act_device_info.triggered.connect(main_window.on_show_device_info)
        main_window._act_device_info = act_device_info

        main_window._tools_menu_separator_added = True


def integrate_dialogs_into_main_window(main_window_class):
    """Decorator to add dialog mixin to MainWindow class."""
    # This would be used if we refactor MainWindow as a proper class
    class EnhancedMainWindow(MainWindowDialogMixin, main_window_class):
        def __init__(self, *args, **kwargs):
            MainWindowDialogMixin.__init__(self)
            main_window_class.__init__(self, *args, **kwargs)

    return EnhancedMainWindow
