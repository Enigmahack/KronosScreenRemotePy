"""Views.sysex_tool_window: Escape saves OUT CH and lets go of the bridge; a traffic burst is trimmed and counted."""
import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication

app = QApplication([])

import Views.sysex_tool_window as M

fails = []


def check(name, cond):
    if not cond:
        fails.append(name)


class FakeBridge(QObject):
    message_received = Signal(bytes)
    connection_changed = Signal(bool)
    is_connected = True

    def send_bytes(self, b):
        return True


bridge = FakeBridge()
saved = []
w = M.SysExToolWindow("h", bridge, None, initial_channel=5)
w.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
w.closing.connect(saved.append)
w.show()

# a burst bigger than the table: only the last _MAX_ENTRIES rows are built, every message is counted
for i in range(2500):
    bridge.message_received.emit(bytes([0x90, 60, 100]) if i % 2 else bytes([0xB0, 7, i & 0x7F]))
w._flush_incoming()
check("burst-trimmed", w._table.rowCount() == M._MAX_ENTRIES)
check("burst-counted", w._midi_count == 2500)
# the filter applies to rows added later as well as on a click
w._cycle_filter("CC"); w._cycle_filter("CC")            # On -> Solo -> Off: CC hidden
bridge.message_received.emit(bytes([0xB0, 1, 2]))
w._flush_incoming()
last = w._table.rowCount() - 1
check("new-row-filtered", w._table.isRowHidden(last))
check("old-row-filtered", all(w._table.isRowHidden(r) for r in range(w._table.rowCount())
                              if w._table.item(r, 2).data(Qt.ItemDataRole.UserRole) == "CC"))
check("trim-after-filter", w._table.rowCount() == M._MAX_ENTRIES)

w._chan_combo.setCurrentIndex(8)                          # CH 9
app.sendEvent(w, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))
app.processEvents()
check("esc-saves-channel", saved == [9])
check("esc-stops-timer", w._shut_down)
before = len(saved)
bridge.message_received.emit(bytes([0x90, 61, 1]))       # no longer connected: nothing queued
app.processEvents()
check("esc-disconnects", len(saved) == before)

if fails:
    print("FAIL:", fails)
    sys.exit(1)
print("ALL PASS")
