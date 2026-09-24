"""Dialog integration for MainWindow - adds testing and utility dialogs."""
from __future__ import annotations
from typing import Optional

from PySide6.QtWidgets import QDialog

from Views.dialogs import (
    ConnectionFailedDialog,
    LoginDialog,
    ObjectInfoDialog,
    FtpPropertiesDialog,
    InputTesterWindow,
    ButtonInjectorWindow,
    RemoteFilePickerDialog,
    MessageBox,
)
from Core.session_manager import get_session_manager


class MainWindowDialogMixin:
    """Mixin to add dialog functionality to MainWindow."""

    def __init__(self):
        super().__init__()
        self._input_tester = None
        self._button_injector = None
        self._file_picker = None
        self._session_mgr = get_session_manager()

    def show_connection_error(self, host: str, port: int, error: str) -> bool:
        """Show connection error dialog. Returns True if should retry."""
        dlg = ConnectionFailedDialog(host, port, error, self)
        if dlg.exec() == QDialog.Accepted:
            return dlg.should_retry()
        return False

    def show_login_dialog(self, saved_host: str = "") -> Optional[tuple[str, str, str, int]]:
        """Show login dialog. Returns (host, username, password, port) or None if canceled."""
        dlg = LoginDialog(saved_host, parent=self)
        if dlg.exec() == QDialog.Accepted:
            return dlg.get_credentials()
        return None

    def show_object_info(self, obj_type: str, bank: int, number: int,
                        name: str, properties: dict):
        """Show object information dialog."""
        dlg = ObjectInfoDialog(obj_type, bank, number, name, properties, self)
        dlg.exec()

    def show_ftp_properties(self) -> Optional[tuple[str, int, str]]:
        """Show FTP properties dialog. Returns (host, port, username) or None."""
        dlg = FtpPropertiesDialog(parent=self)
        if dlg.exec() == QDialog.Accepted:
            return dlg.get_config()
        return None

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

    def show_remote_file_picker(self) -> Optional[list[str]]:
        """Show remote file picker. Returns list of selected files or None."""
        dlg = RemoteFilePickerDialog(self)
        if dlg.exec() == QDialog.Accepted:
            return dlg.get_selected()
        return None

    # Menu action handlers for testing/debugging

    def on_show_input_tester(self):
        """Action handler for input tester menu item."""
        self.show_input_tester()

    def on_show_button_injector(self):
        """Action handler for button injector menu item."""
        self.show_button_injector()

    def on_ftp_properties(self):
        """Action handler for FTP properties menu item."""
        result = self.show_ftp_properties()
        if result:
            host, port, user = result
            MessageBox.info(self, "FTP Configuration",
                          f"Host: {host}\nPort: {port}\nUser: {user}")

    def on_show_device_info(self):
        """Show current device information."""
        session = self._session_mgr.get_session()
        if not session.connected:
            MessageBox.warning(self, "Not Connected",
                              "No Kronos is currently connected.")
            return

        properties = {
            "Host": session.host,
            "Port": session.port,
            "Family": session.device_family,
            "Model": session.device_model,
            "Firmware": session.firmware_version,
            "Screen": f"{session.screen_width}x{session.screen_height}",
            "Stream FPS": str(session.stream_fps),
            "Mode": "Pull" if session.pull_mode else "Change",
            "Uptime": session.uptime_str(),
        }

        self.show_object_info(
            "Device Info",
            0,
            0,
            f"{session.device_family} - {session.device_model}",
            properties
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
        """Show sample editor window.

        Returns:
            True if editor was opened
        """
        from Views.sample_editor_window import SampleEditorWindow

        editor = SampleEditorWindow(self)
        editor.show()
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
        tools_menu = main_window.menuBar().menus()[3]  # Tools is the 4th menu

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

        # FTP properties
        act_ftp_props = testing_menu.addAction("&FTP Configuration…")
        act_ftp_props.triggered.connect(main_window.on_ftp_properties)
        main_window._act_ftp_properties = act_ftp_props

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

        # Sample editor
        act_sample_editor = testing_menu.addAction("&Sample Editor…")
        act_sample_editor.triggered.connect(main_window.on_show_sample_editor)
        main_window._act_sample_editor = act_sample_editor

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
