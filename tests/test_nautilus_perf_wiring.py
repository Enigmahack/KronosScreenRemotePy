"""Measured Nautilus control assignments, layout and SHIFT release safety."""
import os
from pathlib import Path
import sys
from types import SimpleNamespace

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from Views.nautilus_perf_surface import NautilusPerfSurface
app = QApplication.instance() or QApplication([])
sent = []
client = SimpleNamespace(send=lambda host, port, command: sent.append(command))
mw = SimpleNamespace(_ctrl=client, _host="192.168.100.27", _ctrl_port=7374,
                     _receiver=object(), _is_nautilus=True,
                     _daemon_booting=False, _ctrl_send=sent.append)
w = NautilusPerfSurface(mw)
w.show()
app.processEvents()
assert sent == []
for index, knob in enumerate(w.knobs, 1):
    knob.set_value(127)
    assert sent[-1] == f"KNOB {index} 127"
    count = len(sent)
    knob.set_value(200)
    assert len(sent) == count
    knob.set_value(-1)
    assert sent[-1] == f"KNOB {index} 0"

for button, code in ((w.dynamics_btn, 13), (w.cycle_btn, 14),
                     (w.plus_btn, 24)):
    button.click()
    assert sent[-1] == f"BTN {code}"
count = len(sent)
initial_state = w.three_way._state
for step in range(1, 4):
    QTest.mouseClick(w.three_way, Qt.LeftButton)
    assert w.three_way._state == (initial_state + step) % 3
    assert len(sent) == count
QTest.mouseClick(w.three_way, Qt.RightButton)
assert w.three_way._state == initial_state and len(sent) == count
QTest.mouseClick(w.cycle_btn, Qt.LeftButton)
assert w.three_way._state == (initial_state + 1) % 3
assert sent[count:] == ["BTN 14"]
for label, code in (("LATCH", 15), ("ARP", 16), ("DRUM", 17),
                    ("AUDIO IN", 18), ("TAP", 20), ("MFX", 21),
                    ("TFX", 22), ("-", 23)):
    w.buttons[label].click()
    assert sent[-1] == f"BTN {code}"

parent = w.centralWidget()
def center(widget):
    return widget.mapTo(parent, widget.rect().center())

for upper, lower in (("LATCH", "TAP"), ("ARP", "MFX"), ("DRUM", "TFX"),
                     ("AUDIO IN", "-")):
    assert center(w.buttons[upper]).y() == center(w.shift_btn).y()
    assert center(w.buttons[lower]).y() == center(w.plus_btn).y()
    assert center(w.buttons[upper]).x() == center(w.buttons[lower]).x()
assert center(w.knobs[1]).x() < center(w.buttons["LATCH"]).x() < center(w.knobs[2]).x()
assert center(w.knobs[5]).x() < center(w.buttons["AUDIO IN"]).x() < center(w.shift_btn).x()
assert (center(w.buttons["ARP"]).x() - center(w.buttons["LATCH"]).x()
        == center(w.buttons["DRUM"]).x() - center(w.buttons["ARP"]).x()
        == round((center(w.shift_btn).x() - center(w.buttons["AUDIO IN"]).x()) * 0.9))
assert all(button.size() == w.dynamics_btn.size()
           for button in (w.shift_btn, w.cycle_btn, w.plus_btn, *w.buttons.values()))
assert w.buttons["AUDIO IN"].geometry().left() - w.buttons["DRUM"].geometry().right() > 0
for a, b in (("LATCH", "ARP"), ("ARP", "DRUM"), ("DRUM", "AUDIO IN")):
    assert not w.buttons[a].geometry().intersects(w.buttons[b].geometry())

QTest.mousePress(w.shift_btn, Qt.LeftButton)
assert sent[-1] == "BTN_DOWN 19"
w.buttons["ARP"].click()
QTest.mouseRelease(w.shift_btn, Qt.LeftButton)
assert sent[-3:] == ["BTN_DOWN 19", "BTN 16", "BTN_UP 19"]
w._shift_down()
app.sendEvent(w, QEvent(QEvent.WindowDeactivate))
assert sent[-2:] == ["BTN_DOWN 19", "BTN_UP 19"]
count = len(sent)
w._shift_up()
assert len(sent) == count
w._shift_down()
w.hide()
assert sent[-1] == "BTN_UP 19"
w._shift_down()
mw._receiver = object()
count = len(sent)
w._shift_up()
assert len(sent) == count
mw._receiver = None
w.buttons["LATCH"].click()
w.knobs[0].set_value(64)
assert len(sent) == count
mw._receiver = object()
mw._is_nautilus = False
w.plus_btn.click()
w._shift_down()
assert len(sent) == count
mw._is_nautilus = True
mw._daemon_booting = True
w.plus_btn.click()
w._shift_down()
assert len(sent) == count
w.close()
print("Nautilus performance wiring OK")
