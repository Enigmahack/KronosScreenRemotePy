"""Librarian 'Convert to Nautilus...' UX and protection, with synthetic objects (no PCG fixture needed): menu
enablement + tooltip reasons, a confirm dialog whose default is Cancel and that shows exact slot ranges and the arp
range, the window's busy state and hardware-transaction protection while a conversion runs (and that a second click
is explained), the result box (warning icon, failed names), and the remembered next arp slot. Run from the repo root."""
import os
import sys
import tempfile
import threading
import logging
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_convdlg_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtWidgets import QApplication, QDialog, QMenu, QMessageBox, QPushButton
app = QApplication([])
logging.disable(logging.CRITICAL)   # the crash case logs a traceback on purpose

import Data.nautilus_sysex as nsx
from Conversion.data import (ARP_ENCODING_NORMAL, ARP_PATTERN_SIZE, GePatternEntry, KarmaGeCatalog,
                             KarmaGePatternLibrary, ProgramRefMap, RomWaveformMap)
from Conversion.push import ConvertItem, ConvertTables, PushReports
from Data.librarian_model import ObjLoc
from Data.librarian_sysex import OBJ_COMBI, OBJ_PROGRAM
from Models.app_settings import AppSettings
from Tools.exs_option_index import ExsOptionIndex
import Views.librarian_shell_window as L

fails = []


def check(name, cond):
    if not cond:
        print("FAIL " + name)
        fails.append(name)


def wait(cond, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


class Fake:
    can_dump = True
    is_nautilus = True

    def __init__(self):
        self.slots, self.calls = {}, []
        self.release = None          # Event: write_object blocks on it when set
        self.refuse_index = None
        self.explode = False

    def bank_digest(self, obj, bank):
        return b"d" * 20

    def dump_object_parsed(self, obj, bank, index, **kw):
        if self.explode:
            raise RuntimeError("link exploded")
        n = {OBJ_PROGRAM: 3700, OBJ_COMBI: 5640}.get(obj, 556)
        return SimpleNamespace(version=6, body=self.slots.get((obj, bank, index)) or b"I" * n)

    def backup_objects(self, ops, path):
        self.calls.append("backup")
        with open(path, "wb") as f:
            for op in ops:
                f.write(nsx.object_dump_write(op.obj, op.bank, op.index, op.version, op.body))

    def write_object(self, op):
        if self.release is not None:
            self.release.wait(20)
        self.calls.append("write")
        if op.index == self.refuse_index:
            return 5
        self.slots[(op.obj, op.bank, op.index)] = op.body
        return 0

    def store_bank(self, obj, bank):
        self.calls.append("store")
        return 0

    def cached_bank_names(self, *a, **k): return {}
    def sync_names(self, **k): return None
    def __getattr__(self, name): return MagicMock()


fake = Fake()
w = L.LibrarianShellWindow("1.2.3.4", fake, parent=None, settings=AppSettings())
w._selected_pcg_object_locs = lambda: [ObjLoc(OBJ_PROGRAM, 0, i) for i in range(3)]
w._pcg_resolve_content = lambda t, b, n: bytes(3706)
w._pcg_by_addr = {}
logs, boxes = [], []
w._log = lambda m: logs.append(m)
w._set_status = lambda m: logs.append("status: " + m)
QDialog.exec = lambda self: QDialog.DialogCode.Accepted
QMessageBox.exec = lambda self: boxes.append((self.icon(), self.text(), self.detailedText())) or 0


_menus = []


def action(source="pcg"):
    m = QMenu()
    _menus.append(m)   # the QAction dies with its menu
    w._add_convert_action(m, source)
    return m.actions()[0]


# --- menu enablement and tooltip reasons
a = action()
check("menu: enabled when a Nautilus is connected and Programs are selected", a.isEnabled() and "backed up first" in a.toolTip())
fake.is_nautilus = False
a = action()
check("menu: disabled without a Nautilus, tooltip says why and mentions detection",
      not a.isEnabled() and "Connect to a Nautilus first" in a.toolTip() and "detected" in a.toolTip())
fake.is_nautilus = True
w._selected_pcg_object_locs = lambda: []
check("menu: disabled with nothing convertible selected", not action().isEnabled() and "Select Programs" in action().toolTip())
w._selected_pcg_object_locs = lambda: [ObjLoc(OBJ_PROGRAM, 0, i) for i in range(3)]
w._busy = True
check("menu: disabled while busy", not action().isEnabled() and "Another Librarian operation" in action().toolTip())
w._busy = False

# --- clicking while not connected explains instead of silently returning, and sends nothing
fake.is_nautilus = False
w._convert_to_nautilus("pcg")
check("click without a Nautilus: logged and shown in the status line, nothing sent",
      any("Connect to a Nautilus first" in m for m in logs) and any(m.startswith("status:") for m in logs) and not fake.calls)
fake.is_nautilus = True

# --- confirm dialog
tables = ConvertTables(RomWaveformMap(), ExsOptionIndex.from_catalog("{}"), KarmaGeCatalog(), ProgramRefMap())
progs = [ConvertItem(OBJ_PROGRAM, str(i), bytes(3706)) for i in range(3)]
d = L._ConvertConfirmDialog(None, progs, {OBJ_PROGRAM: (0x4C, 0)}, tables, 43, "C:/b/x.syx")
btn = {b.text(): b for b in d.findChildren(QPushButton)}
check("confirm: Cancel is the default button, Convert is not", btn["Cancel"].isDefault() and not btn["Convert"].isDefault())
check("confirm: shows the exact range, not 'onward'", "S000-S002" in d._summary.text() and "onward" not in d._summary.text())
check("confirm: names the backup file", "C:/b/x.syx" in " ".join(l.text() for l in d.findChildren(L.QLabel)))
check("confirm: arp off by default", d.arp_start() is None)
d = L._ConvertConfirmDialog(None, progs, {OBJ_PROGRAM: (0x4C, 126)}, tables, 43, "x")
check("confirm: flags a range past the end of the bank", "past the end of the bank" in d._summary.text() and "S126-S127" in d._summary.text())

pat = bytes([47]) * ARP_PATTERN_SIZE
lib = KarmaGePatternLibrary.parse(KarmaGePatternLibrary.pack([GePatternEntry(47, ARP_ENCODING_NORMAL, 4, 0.95, pat)]))
arp_tables = ConvertTables(RomWaveformMap(), ExsOptionIndex.from_catalog("{}"), KarmaGeCatalog(), ProgramRefMap(), lib)


def prog_ge(ge):
    b = bytearray(3706)
    b[1814], b[1815] = ge & 0xFF, ge >> 8
    return ConvertItem(OBJ_PROGRAM, f"GE{ge}", bytes(b))


d = L._ConvertConfirmDialog(None, [prog_ge(47)], {OBJ_PROGRAM: (0x4C, 0)}, arp_tables, 60, "x")
d._arp_chk.setChecked(True)
check("confirm: arp on -> start slot and the range it overwrites", d.arp_start() == 60 and "B060" in d._arp_line.text())
d._arp_spin.setValue(70)
check("confirm: arp range follows the spin", d.arp_start() == 70 and "B070" in d._arp_line.text())
d2 = L._ConvertConfirmDialog(None, [ConvertItem(OBJ_PROGRAM, "x", bytes(3706))], {OBJ_PROGRAM: (0x4C, 0)}, tables, 43, "x")
d2._arp_chk.setChecked(True)
check("confirm: arp on but no patch needs one says so", "No patch here needs" in d2._arp_line.text())

# --- a running conversion: busy, protected, a second click explained, result box
fake.release = threading.Event()
logs.clear()
w._convert_to_nautilus("pcg")
check("running: hardware transaction flag set", wait(lambda: w._hardware_transaction_active, 10))
check("running: window is busy", w._busy and w._converting)
check("running: MainWindow's request_shutdown() refuses (disconnect/close/reconnect blocked)", not w.request_shutdown())
n_calls = len(fake.calls)
w._convert_to_nautilus("pcg")
check("running: a second click is explained, not silently ignored", any("Another Librarian operation" in m for m in logs))
check("running: the second click sent nothing", len(fake.calls) == n_calls)
fake.release.set()
check("finished", wait(lambda: not w._converting and boxes, 30))
check("finished: busy and protection cleared", not w._busy and not w.hardware_transaction_active and w.request_shutdown())
icon, text, detail = boxes[-1]
check("result: information icon when everything was written", icon == QMessageBox.Icon.Information and "3 of 3" in text)

# --- a partial failure: warning icon, the failed object named, one recovery line
w._sync_cancel.clear()
fake.release, fake.refuse_index, fake.slots = None, 1, {}
boxes.clear()
w._convert_to_nautilus("pcg")
check("partial: finished", wait(lambda: not w._converting and boxes, 30))
icon, text, detail = boxes[-1]
check("partial: warning icon, 2 of 3, names what was not written",
      icon == QMessageBox.Icon.Warning and "2 of 3" in text and "Not written:" in text and "(failed)" in text)
check("partial: details hold the per-object messages", "[REFUSED]" in detail or "[FAILED]" in detail)

# --- the worker blowing up
w._sync_cancel.clear()
fake.refuse_index, fake.explode = None, True
boxes.clear()
w._convert_to_nautilus("pcg")
check("crash: finished and protection cleared", wait(lambda: not w._converting and boxes, 30) and not w.hardware_transaction_active and not w._busy)
check("crash: says it stopped with an error", boxes and "stopped with an error" in boxes[-1][1] and boxes[-1][0] == QMessageBox.Icon.Warning)
fake.explode = False

# --- remembered arp slot
rep = PushReports()
rep.arp_used = [60, 61]
boxes.clear()
w._on_convert_done(rep, "x.syx")
check("arp: next default is one past the last written slot", w._settings.convert_arp_next_slot == 62)

w._closed = True
print(f"{len(fails)} violations")
sys.exit(1 if fails else 0)
