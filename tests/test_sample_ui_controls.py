"""Sample Editor custom controls, driven with synthesized Qt input (offscreen). Run from the repo root."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import numpy as np
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from Core.sample_editor_model.markers import SampleMarkerKind
from Views.sample_waveform_control import SampleWaveformControl, nice_interval

fails = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


check("nice_interval 1/2/5", nice_interval(10, 90) == 1000 and nice_interval(0.001, 90) == 1)

# ---- waveform
wf = SampleWaveformControl()
wf.resize(800, 170)
wf.show()
n = 80000
wf.samples = (np.sin(np.arange(n) * 0.02) * 20000).astype(np.int16)
check("view reset to full on load", (wf.view_start_frame, wf.view_end_frame) == (0, n))
img = wf.grab().toImage()
dark = sum(1 for x in range(0, 800, 40) for y in range(0, 170, 10) if img.pixelColor(x, y).lightness() > 150)
check("trace drew bright pixels", dark > 10)

events = {"sel": 0, "prev": 0, "markers": [], "scrub": [], "view": 0, "loop": [], "moved": [], "changing": 0}
wf.selection_changed.connect(lambda: events.__setitem__("sel", events["sel"] + 1))
wf.selection_preview_changed.connect(lambda: events.__setitem__("prev", events["prev"] + 1))
wf.marker_dragged.connect(lambda k, f: events["markers"].append((k, f)))
wf.scrub_requested.connect(lambda f: events["scrub"].append(f))
wf.view_changed.connect(lambda: events.__setitem__("view", events["view"] + 1))
wf.loop_region_changed.connect(lambda a, b: events["loop"].append((a, b)))
wf.waveform_moved.connect(lambda d: events["moved"].append(d))
wf.markers_changing.connect(lambda: events.__setitem__("changing", events["changing"] + 1))


def px(frame):
    return int(frame / n * 800)


# plain click -> scrub, no selection
QTest.mouseClick(wf, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(px(20000), 80))
check("click -> scrub_requested", events["scrub"] and abs(events["scrub"][-1] - 20000) < 200)
check("click with no prior selection fires no selection_changed", events["sel"] == 0)

# drag -> selection commit on release only
committed_before = (wf.selection_start_frame, wf.selection_end_frame)
QTest.mousePress(wf, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(px(10000), 80))
QTest.mouseMove(wf, QPoint(px(30000), 80))
check("drag shows preview, nothing committed", (wf.selection_start_frame, wf.selection_end_frame) == committed_before
      and wf.effective_selection_end > 25000)
QTest.mouseRelease(wf, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(px(30000), 80))
check("release commits selection", events["sel"] == 1 and wf.selection_start_frame < wf.selection_end_frame)
check("committed range ~ dragged range", abs(wf.selection_start_frame - 10000) < 200 and abs(wf.selection_end_frame - 30000) < 200)

# click again clears the selection (and tells the model)
QTest.mouseClick(wf, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(px(60000), 80))
check("plain click clears selection", wf.selection_end_frame == wf.selection_start_frame and events["sel"] == 2)

# marker drag
wf.loop_enabled = True
wf.sample_start_frame = 5000
wf.loop_start_frame, wf.loop_end_frame = 20000, 40000
QTest.mousePress(wf, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(px(5000), 80))
QTest.mouseMove(wf, QPoint(px(8000), 80))
check("marker drag previews only", wf.sample_start_frame == 5000 and wf.effective_sample_start > 5000 and events["changing"] > 0)
QTest.mouseRelease(wf, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(px(8000), 80))
check("marker_dragged reported on release", events["markers"] and events["markers"][-1][0] is SampleMarkerKind.SAMPLE_START
      and abs(events["markers"][-1][1] - 8000) < 200)

# abandon a drag (grab lost) -> nothing committed
before = events["markers"][:]
wf.sample_start_frame = 5000
QTest.mousePress(wf, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(px(5000), 80))
QTest.mouseMove(wf, QPoint(px(9000), 80))
QApplication.sendEvent(wf, QEvent(QEvent.Type.UngrabMouse))
check("abandoned drag drops preview", wf.effective_sample_start == 5000)
QTest.mouseRelease(wf, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(px(9000), 80))
check("abandoned drag commits nothing", events["markers"] == before and wf.sample_start_frame == 5000)

# loop whole-region drag needs Move tool
wf.move_tool_active = False
QTest.mousePress(wf, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(px(30000), 80))
QTest.mouseMove(wf, QPoint(px(35000), 80))
QTest.mouseRelease(wf, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(px(35000), 80))
check("Select mode never moves the loop", not events["loop"] and wf.loop_start_frame == 20000)
wf.selection_start_frame = wf.selection_end_frame = 0
wf.move_tool_active = True
QTest.mousePress(wf, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(px(30000), 80))
QTest.mouseMove(wf, QPoint(px(35000), 80))
QTest.mouseRelease(wf, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(px(35000), 80))
check("Move mode drags the loop, length preserved", events["loop"]
      and events["loop"][-1][1] - events["loop"][-1][0] == 20000 and events["loop"][-1][0] > 20000)

# Move tool whole-waveform drag only with can_move_waveform
wf.loop_enabled = False
wf.move_tool_active = True
wf.can_move_waveform = True
QTest.mousePress(wf, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(300, 80))
QTest.mouseMove(wf, QPoint(400, 80))
QTest.mouseRelease(wf, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(400, 80))
check("waveform move reports a +frame delta", events["moved"] and abs(events["moved"][-1] - 10000) < 300)
wf.can_move_waveform = False
wf.move_tool_active = False

# wheel zoom toward cursor, then double-click resets
v0 = events["view"]
for _ in range(3):
    wf.wheelEvent(QWheelEvent(QPointF(400, 80), QPointF(400, 80), QPoint(0, 0), QPoint(0, 120),
                              Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False))
check("wheel zooms in", wf.view_end_frame - wf.view_start_frame < n and events["view"] > v0)
mid = (wf.view_start_frame + wf.view_end_frame) / 2
check("zoom stays centred on cursor", abs(mid - n / 2) < n * 0.05)
QTest.mouseDClick(wf, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(400, 80))
check("double-click resets zoom", (wf.view_start_frame, wf.view_end_frame) == (0, n))

wf.scroll_to_zoom = False
e = QWheelEvent(QPointF(400, 80), QPointF(400, 80), QPoint(0, 0), QPoint(0, 120), Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
e.accept()
wf.wheelEvent(e)
check("Scroll-to-Zoom off ignores the wheel", not e.isAccepted() and wf.view_end_frame == n)

# set_view clamps; keeps view when same-length buffer is re-assigned
wf.set_view(1000, 2000)
v = (wf.view_start_frame, wf.view_end_frame)
wf.samples = wf.samples.copy()
check("re-decode of same length keeps the zoomed view", (wf.view_start_frame, wf.view_end_frame) == v)
wf.samples = wf.samples[:50000]
check("length change resets the view", (wf.view_start_frame, wf.view_end_frame) == (0, 50000))

# pair override extends the span
wf.view_frame_count = 70000
wf.reset_view()
check("view span honours pair override", wf.view_end_frame == 70000)
wf.view_frame_count = 0

# keyboard nudge needs loop lock
wf.loop_enabled = True; wf.loop_start_frame, wf.loop_end_frame = 100, 200; wf.loop_lock_enabled = True
events["loop"].clear()
QTest.keyClick(wf, Qt.Key.Key_Right)
check("arrow nudges locked loop by one frame", events["loop"] == [(101, 201)])


# ---- keymap
from Data.kmp_multisample import KmpZone
from Views.sample_keymap_control import (SampleKeymapControl, build_layout, compute_ranges, is_black_key,
                                          pixel_to_boundary_key, zone_at)

wl, left, right = build_layout(1000)
check("layout monotone on white keys", all(right[k] <= left[k + 1] + 1e-9 for k in range(127)
                                           if not is_black_key(k) and not is_black_key(k + 1)))
check("black key is 60% of white", abs((right[1] - left[1]) - wl * 0.6) < 1e-6)
zs = [KmpZone(filename="A.KSF", original_key=30, top_key=47), KmpZone(filename="B.KSF", original_key=60, top_key=71),
      KmpZone(filename="SKIPPEDSAMPLE", original_key=90, top_key=127)]
rg = compute_ranges(zs)
check("ranges tile 0..127", [(lo, hi) for _z, lo, hi in rg] == [(0, 47), (48, 71), (72, 127)])
check("zone_at", zone_at(50, rg) is zs[1] and zone_at(100, rg) is zs[2])
check("boundary scan reaches the far right", pixel_to_boundary_key(999, right, 0, 127) == 127)

km = SampleKeymapControl()
km.resize(1000, 104)
km.show()
km.zones = zs
km.selected_zone = zs[1]
img = km.grab().toImage()
check("keymap painted piano keys", any(img.pixelColor(x, 70).lightness() > 200 for x in range(0, 1000, 7)))
ev = {"zone": [], "key": [], "rel": 0, "bound": [], "reorder": [], "ctrl": []}
km.zone_clicked.connect(lambda z: ev["zone"].append(z))
km.piano_key_clicked.connect(lambda z, k: ev["key"].append((z, k)))
km.piano_key_released.connect(lambda: ev.__setitem__("rel", ev["rel"] + 1))
km.boundary_moved.connect(lambda z, k: ev["bound"].append((z, k)))
km.zone_reordered.connect(lambda a, b: ev["reorder"].append((a, b)))
km.piano_key_ctrl_clicked.connect(lambda k: ev["ctrl"].append(k))
LB = Qt.MouseButton.LeftButton
NM = Qt.KeyboardModifier.NoModifier

kx = lambda k: int((left[k] + right[k]) / 2)
QTest.mousePress(km, LB, NM, QPoint(kx(60), 80))
check("piano press auditions the key's zone", ev["key"] and ev["key"][-1][0] is zs[1] and ev["key"][-1][1] == 60)
check("piano press does NOT select", not ev["zone"])
QTest.mouseRelease(km, LB, NM, QPoint(kx(60), 80))
check("release ends the hold", ev["rel"] == 1)

QTest.mouseClick(km, LB, NM, QPoint(kx(60), 6))
check("raised-label click selects, no audition", ev["zone"] and ev["zone"][-1] is zs[1] and len(ev["key"]) == 1)

bx = int(right[47])
QTest.mousePress(km, LB, NM, QPoint(bx, 22))
QTest.mouseMove(km, QPoint(bx + 40, 22))
QTest.mouseRelease(km, LB, NM, QPoint(bx + 40, 22))
check("boundary drag reports (zone, newTopKey)", ev["bound"] and ev["bound"][-1][0] is zs[0] and ev["bound"][-1][1] > 47)
n_b = len(ev["bound"])
QTest.mousePress(km, LB, NM, QPoint(bx, 22))
QTest.mouseMove(km, QPoint(bx + 2, 22))
QTest.mouseRelease(km, LB, NM, QPoint(bx + 2, 22))
check("jitter inside the hit tolerance is not a drag", len(ev["bound"]) == n_b)

QTest.mousePress(km, LB, NM, QPoint(kx(20), 22))
QTest.mouseMove(km, QPoint(kx(60), 22))
QTest.mouseRelease(km, LB, NM, QPoint(kx(60), 22))
check("zone-bar drag onto another zone reorders", ev["reorder"] and ev["reorder"][-1] == (zs[0], zs[1]))
nz = len(ev["zone"])
QTest.mouseClick(km, LB, NM, QPoint(kx(20), 22))
check("zone-bar click without moving selects", len(ev["zone"]) == nz + 1 and ev["zone"][-1] is zs[0])

QTest.mouseClick(km, LB, Qt.KeyboardModifier.ControlModifier, QPoint(kx(64), 80))
check("Ctrl+Click on the piano reports the raw key only", ev["ctrl"] == [64] and len(ev["key"]) == 1)

QTest.mousePress(km, LB, NM, QPoint(kx(60), 80))
QApplication.sendEvent(km, QEvent(QEvent.Type.UngrabMouse))
check("lost grab mid-hold releases the key", ev["rel"] == 2)
QTest.mouseRelease(km, LB, NM, QPoint(kx(60), 80))
check("no double release", ev["rel"] == 2)

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
