"""Debug controls send exact daemon commands without sending on assignment."""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialog
from Views.main_window_dialogs import MainWindowDialogMixin
from Views.testing_dialogs import ButtonInjectorWindow

app = QApplication.instance() or QApplication([])
sent = []
mw = SimpleNamespace(_host="192.168.100.27", _receiver=object(),
                     _daemon_booting=False, _ctrl_send=sent.append)
w = ButtonInjectorWindow(mw)
assert not w.isModal()
assert sent == []
w._assign_button(23)
assert w.button_code.value() == 23 and sent == []
w.assigned_button.click()
assert sent == ["BTN 23"]
w.knob_assignment.setValue(6)
assert sent == ["BTN 23"]
w.knob.setValue(127)
assert sent[-1] == "KNOB 6 127" and w.knob_value.value() == 127
count = len(sent)
w.knob_value.setValue(0)
assert sent[count:] == ["KNOB 6 0"] and w.knob.value() == 0
w.knob_send.click()
assert sent[-1] == "KNOB 6 0"
w.button_code.setValue(127)
assert w.button_assignment.currentText() == "Custom code"
w.assigned_button.click()
assert sent[-1] == "BTN 127"
w.button_input.setText("enter")
w._inject_custom()
assert sent[-1] == "BTN 23"
w.button_input.setText("0")
w._inject_custom()
assert sent[-1] == "BTN 0"
count = len(sent)
for token in ("128", "-1", "NOPE", "BTN 23\nKNOB 1 127"):
    w.button_input.setText(token)
    w._inject_custom()
assert len(sent) == count
mw._receiver = None
w.assigned_button.click()
w.knob.setValue(32)
assert len(sent) == count and "Not connected" in w.status_label.text()
mw._receiver = object()
mw._daemon_booting = True
w.knob_send.click()
assert len(sent) == count and "booting" in w.status_label.text()
assert not w.knob_value.keyboardTracking()
w.close()

settings = QDialog()
modal_checks = []


def check_child():
    injector = QApplication.activeModalWidget()
    modal_checks.append(isinstance(injector, ButtonInjectorWindow)
                        and injector.parentWidget() is settings)
    injector.accept()


def open_injector():
    QTimer.singleShot(0, check_child)
    MainWindowDialogMixin.show_button_injector(mw)
    settings.accept()


QTimer.singleShot(0, open_injector)
settings.exec()
assert modal_checks == [True]
print("Control injector OK")
