"""MainWindow button actions emit BTN / BTN_DOWN / BTN_UP only (never BUTTON/CHORD), per device family."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_data_")
from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])
from Models.app_settings import AppSettings
from Views.main_window import MainWindow

fails = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


class StubCtrl:
    def __init__(self):
        self.sent, self.chords = [], []

    def send(self, host, port, cmd):
        self.sent.append(cmd)

    def send_chord(self, host, port, names, hold_ms=0):
        self.chords.append((list(names), hold_ms))


w = MainWindow(AppSettings())
w._host, w._ctrl_port = "127.0.0.1", 7374
stub = w._ctrl = StubCtrl()

w._run_action("Mode Combi")
check("Mode Combi -> BTN 1", stub.sent[-1] == "BTN 1")
w._run_action("Mode Setlist")
check("Mode Setlist -> BTN 7", stub.sent[-1] == "BTN 7")
w._run_action("Bank I-C")
check("Bank I-C -> BTN 26", stub.sent[-1] == "BTN 26")
w._run_action("Bank U-C")
check("Bank U-C -> BTN 33", stub.sent[-1] == "BTN 33")
w._run_action("Bank U-CC")
check("Bank U-CC is a chord of U-C then I-C", stub.chords[-1] == (["BANK_UC", "BANK_IC"], 0))
w._is_nautilus = True
n_before, c_before = len(stub.sent), len(stub.chords)
w._run_action("Bank U-CC")
w._run_action("Bank I-C")
check("bank buttons stay disabled on Nautilus", len(stub.sent) == n_before and len(stub.chords) == c_before)
w._run_action("Seq Locate")
check("Nautilus Seq Locate -> MS1 = BTN 66", stub.sent[-1] == "BTN 66")
w._is_nautilus = False
w._run_action("Seq Locate")
check("Kronos Seq Locate -> SEQ_LOCATE = BTN 41", stub.sent[-1] == "BTN 41")
check("nothing sent as BUTTON/CHORD", not any(c.startswith(("BUTTON", "CHORD")) for c in stub.sent))

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
