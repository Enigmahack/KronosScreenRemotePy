import os, sys, time
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtCore import QEvent
app = QApplication([])
from Models.app_settings import AppSettings
from Models.models import CalMesh, CalBiasDot
import Models.cal_text as cal_text
from Views.main_window import MainWindow

w = MainWindow(AppSettings())
fw = w._frame_w

def pump(ms=300):
    end = time.time() + ms / 1000
    while time.time() < end:
        app.processEvents(); time.sleep(0.01)

class FakeCtrl:
    def __init__(self): self.sent = []; self.reply = "OK"
    def query(self, host, port, cmd, timeout_ms=2000):
        self.sent.append(cmd); return self.reply
w._ctrl = FakeCtrl(); w._host = "1.2.3.4"; w._ctrl_port = 1; w._receiver = object()

# apply unit calibration: parse
mesh = CalMesh(4, 4); mesh.set_offset(1, 1, 5, -3)
txt = cal_text.serialize(mesh, [CalBiasDot(100, 100)])
w._apply_unit_calibration("CAL " + txt)
assert fw._cal_mesh.cols == 4 and len(fw._cal_bias_dots) == 1, "load failed"
assert w._act_grid[4].isChecked() and not w._act_grid[5].isChecked()
w._apply_unit_calibration("CAL NONE"); assert fw._cal_mesh.is_identity() and fw._cal_mesh.cols == 5
w._apply_unit_calibration(None); assert fw._cal_mesh.is_identity()
print("apply ok")

# save: success
fw._cal_mesh.set_offset(1, 1, 7, 7); fw._cal_dirty = False
w._save_calibration(); pump()
assert w._ctrl.sent and w._ctrl.sent[-1].startswith("CAL_SET "), w._ctrl.sent
assert not fw._cal_dirty
print("save ok:", w._ctrl.sent[-1][:40])

# failures
for reply, frag in (("ERR", "too old"), (None, "did not answer"), ("ERR INVALID", 'replied "ERR INVALID"')):
    w._ctrl.reply = reply; n = w._notify_count
    w._save_calibration(); pump()
    assert fw._cal_dirty and w._notify_count == n + 1 and frag in w._notify_msgs[-1], (reply, w._notify_msgs[-1:])
    fw._cal_dirty = False
w._receiver = None; n = w._notify_count
w._save_calibration(); pump()
assert "connect to it first" in w._notify_msgs[-1]; fw._cal_dirty = False
w._receiver = object(); w._ctrl.reply = "OK"
print("failure messages ok")

# blocking
assert w._save_calibration_blocking() is None
w._ctrl.reply = "ERR"; assert "too old" in w._save_calibration_blocking(); w._ctrl.reply = "OK"
print("blocking ok")

# grid change: confirm No keeps, Yes clears+saves
fw._cal_mesh = CalMesh(5, 5); fw._cal_mesh.set_offset(2, 2, 4, 4)
QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.No)
w._set_cal_grid(3); assert fw._cal_mesh.cols == 5 and w._act_grid[5].isChecked()
QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.Yes)
sent = len(w._ctrl.sent); w._set_cal_grid(3); pump()
assert fw._cal_mesh.cols == 3 and fw._cal_mesh.is_identity() and len(w._ctrl.sent) == sent + 1
print("grid ok")

# keys
def key(k, mods=Qt.NoModifier):
    return QKeyEvent(QEvent.Type.KeyPress, k, mods)
fw._cal_mesh.set_offset(1, 1, 3, 3)
assert w._handle_cal_key(key(Qt.Key_R)) and fw._cal_mesh.is_identity() and fw._cal_dirty
assert w._handle_cal_key(key(Qt.Key_S)) and not fw._cal_dirty
assert not w._handle_cal_key(key(Qt.Key_Escape))   # falls through to EXIT
assert not w._handle_cal_key(key(Qt.Key_Q))
print("keys ok")

# coordinate round-trip: dot placed at a click maps back to same native coordinate
from PySide6.QtCore import QRectF
w.resize(900, 700); w.show(); pump(200)
fw._fw, fw._fh = 800, 600
fw._frame_rect = QRectF(10, 10, 800, 600)
fw._cal_mode = True
fp = fw._widget_to_frame(QPointF(809.99, 609.99)); assert (fp.x(), fp.y()) == (799, 599), fp
sp = fw._kron_to_screen(799, 599); assert abs(sp.x() - 810) < 1e-6 and abs(sp.y() - 610) < 1e-6
fw._cal_mesh = CalMesh(5, 5)
fw._cal_right_click(QPointF(410, 310)); assert len(fw._cal_bias_dots) == 1
kx, ky = fw._cal_mesh.apply(fw._cal_bias_dots[0].nx, fw._cal_bias_dots[0].ny, 800, 600)
s2 = fw._kron_to_screen(kx, ky); assert abs(s2.x() - 410) < 1.5 and abs(s2.y() - 310) < 1.5, (s2,)
fw._cal_right_click(QPointF(410, 310)); assert len(fw._cal_bias_dots) == 0
assert fw._cal_hit_node(fw._kron_to_screen(*fw._cal_mesh.node_dst(2, 2, 800, 600))) == (2, 2)
fw._cal_mode = False
# paint overlay without error on both geometries
from PySide6.QtGui import QPixmap, QPainter
for (kw, kh) in ((800, 600), (800, 480)):
    pm = QPixmap(900, 700); p = QPainter(pm)
    fw._renderer.draw_cal_overlay(p, QRectF(10, 10, 800, kh * 800 / kw), fw._cal_mesh, [CalBiasDot(5, 5)], (1, 1), None, True, kw, kh, 900, 700)
    p.end()
print("overlay ok")
print("ALL PASS")
