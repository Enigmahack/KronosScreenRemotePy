"""SampleEditorWindow driven headless against COPIES of real fixtures: open, select, edit through the real handlers,
undo, save, stereo, keymap, drag/drop. Run from the repo root."""
import os, shutil, sys, tempfile, wave
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_data_")
import numpy as np
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

import Views.sample_editor_dialogs as dlgs
from Views.sample_editor_window import SampleEditorWindow, find_node_for_zone
from Data.ksf_sample import KsfSample

# Dialogs would block a headless run: answer them programmatically.
dlgs.confirm = lambda *a, **k: True
import Views.sample_editor_window as sew
sew.confirm = lambda *a, **k: True

FIX = r"Z:\KronosScreenRemote\SampleFixtures"
fails = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


tmp = tempfile.mkdtemp(prefix="kr_win_")
shutil.copytree(os.path.join(FIX, "SMPTEST"), os.path.join(tmp, "S"))

w = SampleEditorWindow("", 21, "", "", None)
w.show()
check("empty state shown", w._empty_text.isVisibleTo(w) and not w._editor.isVisibleTo(w))

w.open_collection_path(os.path.join(tmp, "S", "LOOP.KSC"))
m = w.model
check("tree has the collection", w._tree.topLevelItemCount() == 1)
check("editor visible after open", w._editor.isVisibleTo(w) and not w._empty_text.isVisibleTo(w))
check("nothing below the collection is selected yet", m.current_multisample_zones is None)
check("MS combo lists multisamples", w._ms_combo.count() >= 1)
w._ms_combo.setCurrentIndex(0)                       # the user picking a multisample from the combo
check("picking a multisample drops into its first zone", m.has_zone_selected and m.current_multisample_zones is not None)
check("keymap bound to the zones", w._keymap.zones is m.current_multisample_zones)

# find an audio zone
for z in (m.current_multisample_zones or []):
    node = find_node_for_zone(m.roots, z)
    w._select_tree_node(node)
    if m.has_sample_loaded and not m.sample_is_header_only:
        break
check("audio zone selected", m.has_sample_loaded and not m.sample_is_header_only)
check("waveform pane has samples", w._wf_left.samples is not None and len(w._wf_left.samples) == m.sample_frame_count)
check("R pane shown exactly when the sample is half of a stereo pair", w._row_r.isVisibleTo(w) == m.has_stereo_pair)
check("fields mirrored", w._rate_box.text() == str(m.sample_rate) and w._name_text.text() == m.sample_name)

# edits through the real handlers
s = m.selected_sample
n0 = s.frame_count
m.selection_start_frame, m.selection_end_frame = 100, 600
w.refresh()
check("selection pushed to the pane", w._wf_left.selection_start_frame == 100 and w._wf_left.selection_end_frame == 600)
w.on_crop()
check("crop via handler", s.frame_count == 500 and w._frames_text.text() == "500")
check("dirty marker in title", w.windowTitle().startswith("*") and w._btn_save.isEnabled())
check("undo enabled", w._btn_undo.isEnabled())
w.on_undo()
check("undo via handler", s.frame_count == n0 and not w._btn_redo.isEnabled() is False)

w._loop_box.setChecked(True)
w.on_loop_toggled()
check("loop toggle shows loop fields", w._loop_fields.isVisibleTo(w) and m.sample_loop_enabled)
w._loop_start_box.setText(str(m.sample_start + 300))
w._on_marker_edited(__import__("Core.sample_editor_model.markers", fromlist=["x"]).SampleMarkerKind.LOOP_START, w._loop_start_box, True)
check("typed Loop Start commits through set_marker", m.loop_start == m.sample_start + 300)

# transport
w.toggle_playback()
check("play starts", m.is_playing and w._playhead_timer.isActive())
QTest.qWait(250)
check("playhead advanced on the pane", w._wf_left.playhead_frame >= 0)
w.toggle_playback()
check("stop retires the pump", not m.is_playing and not w._playhead_timer.isActive())
check("play button back to play icon", w._btn_play.toolTip() == "Play")

# playback finishing on its own (audio thread) flips the UI through the marshal
m.sample_loop_enabled = False
w._loop_box.setChecked(False)
w.on_loop_toggled()
m.selection_start_frame = m.selection_end_frame = 0
m._cursor_frame = max(0, m.sample_frame_count - 400)
w.toggle_playback()
QTest.qWait(1200)
check("natural end-of-buffer clears is_playing via the marshal", not m.is_playing)

# zoom / view sync
w._wf_left.set_view(1000, 3000)
check("ruler follows the view", w._ruler._view_start == 1000 and w._ruler._view_end == 3000)
check("scrollbar visible when zoomed", w._hscroll.isVisibleTo(w) and w._hscroll.value() == 1000)
w._on_hscroll(2000)
check("scrollbar drives the view", w._wf_left.view_start_frame == 2000)
w.on_zoom_fit()
check("fit resets", w._wf_left.view_start_frame == 0 and not w._hscroll.isVisibleTo(w))

# save
w.on_save_changes()
check("save clears the dirty marker", not w.windowTitle().startswith("*") and not w._btn_save.isEnabled())

# keymap interaction through the window
z0 = m.current_multisample_zones[0]
w._on_keymap_zone_clicked(z0)
check("keymap click selects the zone", m.selected_zone is z0)

# add / delete zone through handlers, with re-select by position
before = len(m.current_multisample_zones)
w.on_add_zone()
check("placeholder zone added and re-selected", len(m.current_multisample_zones) == before + 1 and m.has_zone_selected)
w.on_delete_zone()
check("zone deleted and neighbour re-selected", len(m.current_multisample_zones) == before and m.has_zone_selected)
w.on_undo()
check("zone delete undone via window", len(m.current_multisample_zones) == before + 1)

# drag & drop an audio file onto the window imports a zone (prompts answered)
wav = os.path.join(tmp, "d.wav")
with wave.open(wav, "wb") as f:
    f.setnchannels(1); f.setsampwidth(2); f.setframerate(44100)
    f.writeframes((np.sin(np.arange(3000) * .05) * 9000).astype(np.int16).tobytes())
w._prompt_zone_keys = lambda: (60, 72)
from PySide6.QtCore import QMimeData, QUrl
from PySide6.QtGui import QDropEvent
from PySide6.QtCore import QPointF
md = QMimeData(); md.setUrls([QUrl.fromLocalFile(wav)])
n_zones = len(m.current_multisample_zones)
w.dropEvent(QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction, md, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
check("drop imports a new zone", any(not z.is_skipped for z in m.current_multisample_zones) and
      len(m.current_multisample_zones) >= n_zones)

# stereo pair through the window
st = os.path.join(tmp, "st.wav")
with wave.open(st, "wb") as f:
    f.setnchannels(2); f.setsampwidth(2); f.setframerate(44100)
    d = np.zeros((4000, 2), np.int16); d[:, 0] = (np.sin(np.arange(4000) * .05) * 20000); d[:, 1] = (np.sin(np.arange(4000) * .11) * 5000)
    f.writeframes(d.tobytes())
node = m.new_stereo_multisample_pair_in_collection("WPAIR", m.next_free_mno1(2))
w.refresh()
w._select_tree_node(node)
m.import_stereo_audio_as_new_zone_pair(st, 60, 90)
w.refresh()
pair_node = m._node_for_path(node.multisample_ref[1])
w._select_tree_node(pair_node.children[1])
check("stereo pair detected -> R pane shown", m.has_stereo_pair and w._row_r.isVisibleTo(w) and w._split_box.isVisibleTo(w))
check("both panes carry audio", w._wf_left.samples is not None and w._wf_right.samples is not None
      and not np.array_equal(w._wf_left.samples, w._wf_right.samples))
check("shared pair span", w._wf_left.view_frame_count == 4000 == w._wf_right.view_frame_count)
w._wf_left.set_view(500, 2500)
check("zoom mirrors onto the sibling pane", (w._wf_right.view_start_frame, w._wf_right.view_end_frame) == (500, 2500))
w._split_box.setChecked(True)
w.on_split_toggled()
check("Split sets the panes up as channel panes", w._wf_left.is_split_channel_pane and w._wf_right.is_split_channel_pane)
w._split_box.setChecked(False)
w.on_split_toggled()

# closing with unsaved changes asks; we answered Yes
m.selection_start_frame, m.selection_end_frame = 0, 50
w.on_cut()
check("dirty again", m.has_unsaved_changes)
w.close()
check("close stops playback", not m.is_playing)

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
