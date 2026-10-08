"""Sample Editor window — port of Views/SampleEditorWindow.xaml(.cs).

All state and behaviour lives in `Core.sample_editor_model.SampleEditorModel`; this module is layout plus the
glue that turns widget gestures into model calls and model state back into widgets. Most handlers follow one
shape — mutate the model, `refresh()`, show the status line — which `_after()` keeps in one place so a handler
can't land with a step missing.

The model's zone-adding calls rebuild the tree and drop the selection, so those handlers re-select by POSITION
afterwards (`_reselect`): the rebuild replaces every KmpZone instance, only an index survives it.
"""
from __future__ import annotations

import logging
import os
from typing import Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCursor
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMenu, QPushButton, QScrollArea, QScrollBar, QSizePolicy, QSplitter, QToolButton, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget)

import Models.storage as storage
import Utils.theme as T
from Core.sample_editor_model import SampleEditorModel
from Core.sample_editor_model.markers import SampleMarkerKind
from Core.sample_editor_model.tree import SampleTreeNode, enumerate_nodes
from Core.sample_support import SampleClipboard
from Data.ksf_sample import KsfSample
from Utils.midi_note_name import to_name, try_parse
from Views.sample_editor_dialogs import (
    KRONOS_NAME_MAX_LENGTH, CreateMultisampleDialog, InsertSilenceDialog, SampleReportWindow, confirm, prompt_text)
from Views.sample_editor_widgets import (
    FieldBox, ICON_GREEN, ICON_GREY, path_icon, play_stop_icon, undo_icon, zoom_icon)
from Views.sample_keymap_control import SampleKeymapControl
from Views.sample_remote_source import KronosRemoteSampleSource
from Views.sample_small_controls import (
    SamplePanControl, SampleVolumeControl, SampleVuMeter, SampleWaveformRuler)
from Views.sample_waveform_control import SampleWaveformControl

log = logging.getLogger(__name__)

AUDIO_FILTER = "Audio Files (*.wav *.mp3 *.mp4 *.m4a *.wma);;WAV Files (*.wav);;All Files (*)"
DROPPABLE_AUDIO = (".wav", ".mp3", ".mp4", ".m4a", ".wma")
MUTED = f"color: {T.TEXT_DIM};"


class _Marshal(QObject):
    """GUI-thread trampoline. The playback end-of-buffer callback fires on the audio thread, which has no Qt event
    loop — a Signal emitted from it is queued onto this object's (GUI) thread."""
    call = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.call.connect(lambda fn: fn())


_CHECK_ICON: Optional[str] = None


def _check_icon_url() -> str:
    """Checkmark glyph for the styled checkbox indicator (QSS image: needs a file). Painted once into the temp dir."""
    global _CHECK_ICON
    if _CHECK_ICON is None:
        import tempfile
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QColor, QImage, QPainter, QPen
        img = QImage(14, 14, QImage.Format.Format_ARGB32)
        img.fill(0)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(T.TEXT), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.drawPolyline([QPointF(3, 7.5), QPointF(6, 10.5), QPointF(11, 3.5)])
        p.end()
        path = os.path.join(tempfile.gettempdir(), "ksr_checkbox_check.png")
        img.save(path)
        _CHECK_ICON = path.replace("\\", "/")
    return _CHECK_ICON


def window_stylesheet() -> str:
    """Explicit dark styling, like the app's dialogs (BaseDialog): the editor must not depend on the OS colour scheme."""
    return f"""
    QMainWindow, QScrollArea, QScrollArea > QWidget > QWidget {{ background: {T.BG}; color: {T.TEXT}; }}
    QLabel, QCheckBox {{ color: {T.TEXT}; background: transparent; }}
    QCheckBox::indicator {{ width: 14px; height: 14px; border: 1px solid #8A8A8A; border-radius: 2px;
                            background: {T.PANEL_ALT}; }}
    QCheckBox::indicator:hover {{ border-color: {T.ACCENT}; }}
    QCheckBox::indicator:checked {{ image: url({_check_icon_url()}); }}
    QCheckBox::indicator:disabled {{ border-color: {T.BORDER_STRONG}; }}
    QMenuBar {{ background: {T.BG}; color: {T.TEXT}; }}
    QMenuBar::item:selected, QMenu::item:selected {{ background: {T.ACCENT_DEEP}; }}
    QMenu {{ background: {T.PANEL_ALT}; color: {T.TEXT}; border: 1px solid {T.BORDER_STRONG}; }}
    QMenu::item {{ padding: 4px 28px 4px 22px; }}
    QMenu::item:disabled {{ color: {T.TEXT_FAINT}; }}
    QMenu::separator {{ height: 1px; background: {T.BORDER_STRONG}; margin: 3px 6px; }}
    QLineEdit, QComboBox {{ background: {T.PANEL_ALT}; color: {T.TEXT}; border: 1px solid {T.BORDER_STRONG};
                            border-radius: 3px; padding: 2px 4px; }}
    QLineEdit:focus {{ border-color: {T.ACCENT}; }}
    QComboBox QAbstractItemView {{ background: {T.PANEL_ALT}; color: {T.TEXT}; selection-background-color: {T.ACCENT_DEEP}; }}
    QTreeWidget {{ background: {T.PANEL}; color: {T.TEXT}; border: 1px solid {T.BORDER_STRONG}; }}
    QTreeWidget::item:selected {{ background: {T.ACCENT_DEEP}; color: {T.TEXT}; }}
    QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: 3px; }}
    QToolButton:hover {{ background: #333333; }}
    QToolButton:checked {{ background: #454545; border-color: #6E6E6E; }}
    QToolButton:disabled {{ opacity: 0.35; }}
    QSplitter::handle {{ background: {T.BORDER_STRONG}; }}
    QScrollBar:horizontal {{ background: {T.PANEL}; height: 12px; }}
    QScrollBar::handle:horizontal {{ background: #555555; border-radius: 4px; min-width: 24px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    """


def _panel(title: str, tint: Optional[str] = None) -> Tuple[QFrame, QVBoxLayout]:
    f = QFrame()
    if tint == "kronos":
        f.setStyleSheet("QFrame#panel{background:rgba(46,127,214,0.12);border:1px solid rgba(46,127,214,0.30);border-radius:3px;}")
    elif tint == "local":
        f.setStyleSheet("QFrame#panel{background:rgba(255,140,0,0.12);border:1px solid rgba(255,140,0,0.30);border-radius:3px;}")
    else:
        f.setStyleSheet(f"QFrame#panel{{border:1px solid {T.BORDER_STRONG};border-radius:3px;}}")
    f.setObjectName("panel")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(10, 8, 10, 8)
    if title:
        lbl = QLabel(title)
        color = {"kronos": "#3E7FC0", "local": "#CC7000"}.get(tint or "", T.TEXT_DIM)
        lbl.setStyleSheet(f"color:{color};font-weight:600;font-size:{'9' if tint else '11'}px;")
        lay.addWidget(lbl)
    return f, lay


def _flow(*widgets) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setSpacing(6)
    for w in widgets:
        row.addWidget(w)
    row.addStretch()
    return row


def _label(text: str) -> QLabel:
    lb = QLabel(text)
    lb.setStyleSheet(MUTED)
    return lb


def _tool_button(icon=None, tip: str = "", size=(34, 30), checkable: bool = False) -> QToolButton:
    b = QToolButton()
    if icon is not None:
        b.setIcon(icon)
    b.setToolTip(tip)
    b.setFixedSize(*size)
    b.setCheckable(checkable)
    b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    b.setAutoRaise(True)
    return b


def find_node_for_zone(nodes, zone) -> Optional[SampleTreeNode]:
    for n in enumerate_nodes(list(nodes)):
        if n.zone_ref is not None and n.zone_ref[0] is zone:
            return n
    return None


def find_multisample_node(nodes, kmp_path: str) -> Optional[SampleTreeNode]:
    key = os.path.normcase(kmp_path)
    for n in enumerate_nodes(list(nodes)):
        if n.multisample_ref is not None and os.path.normcase(n.multisample_ref[1]) == key:
            return n
    return None


class SampleEditorWindow(QMainWindow):
    def __init__(self, host: str = "", ftp_port: int = 21, user: str = "", password: str = "", parent=None,
                 model: Optional[SampleEditorModel] = None):
        super().__init__(parent)
        self._host, self._ftp_port, self._user, self._pass = host, ftp_port, user, password
        settings = getattr(parent, "_settings", None) or storage.load_settings()
        self._model = model or SampleEditorModel(settings, storage.save_settings)
        self._marshal = _Marshal(self)
        self._model.dispatch = self._marshal.call.emit

        self._syncing_views = False
        self._suppress_combo = False
        self._split_drag_crossed = False
        self._repo_cache: Dict[str, Tuple[str, str]] = {}
        self._sample_combo_paths: List[str] = []
        self._ms_nodes: List[SampleTreeNode] = []

        self.setStyleSheet(window_stylesheet())
        self.resize(1200, 780)
        self.setMinimumSize(900, 560)
        self.setAcceptDrops(True)
        self._build_menus()
        self._build_ui()

        self._vu_timer = QTimer(self)
        self._vu_timer.setInterval(40)
        self._vu_timer.timeout.connect(self._update_vu)
        self._vu_timer.start()
        self._playhead_timer = QTimer(self)
        self._playhead_timer.setInterval(16)
        self._playhead_timer.timeout.connect(self._update_playhead)

        self._model.add_listener(self._on_model_changed)
        self._model.cursor_moved_listeners.append(self._on_cursor_moved)
        QApplication.instance().installEventFilter(self)
        self.refresh()
        self._update_status()

    # ── public entry points ────────────────────────────────────────────────────────────────

    @property
    def model(self) -> SampleEditorModel:
        return self._model

    def open_collection_path(self, path: str) -> None:
        self._model.open_collection(path)
        self._select_first_root()
        self._update_status()

    def open_kmp_path(self, path: str) -> None:
        self._model.open_multisample_direct(path)
        self._select_first_root()
        self._update_status()

    # ── construction ───────────────────────────────────────────────────────────────────────

    def _act(self, menu: QMenu, text: str, slot: Callable, shortcut: str = "", tip: str = "") -> QAction:
        a = menu.addAction(text)
        a.triggered.connect(lambda _checked=False: slot())
        if shortcut:
            # Advertised only: the shortcut itself is handled by the window's key filter so it still works while
            # a field has focus.
            a.setText(f"{text}\t{shortcut}")
        if tip:
            a.setToolTip(tip)
            a.setStatusTip(tip)
        return a

    def _build_menus(self) -> None:
        mb = self.menuBar()
        f = mb.addMenu("&File")
        self._m_file = f
        self._a_new_coll = self._act(f, "&New Sample Collection (.KSC)...", self.on_new_collection)
        self._a_new_ms = self._act(f, "New M&ultisample in Collection...", self.on_new_multisample)
        self._a_new_pair = self._act(f, "New &Stereo Multisample Pair...", self.on_new_stereo_pair)
        f.addSeparator()
        self._act(f, "&Open Sample Collection (.KSC)...", self.on_open_collection, "Ctrl+O")
        self._act(f, "Open &Multisample (.KMP)...", self.on_open_kmp)
        self._m_recent = f.addMenu("&Recent...")
        self._m_recent.aboutToShow.connect(self._fill_recent)
        self._a_unload = self._act(f, "Close Sample Collection (.KSC)", self.on_unload_active,
                                   tip="Closes the active collection from the tree - files on disk are untouched.")
        f.addSeparator()
        self._a_save_changes = self._act(f, "Save &Changes", self.on_save_changes, "Ctrl+S",
                                         "Writes everything edited this session, across every open collection.")
        self._a_save_ms = self._act(f, "Save &Multisample", self.on_save_multisample)
        self._a_save_sample = self._act(f, "Save &Sample", self.on_save_sample)
        f.addSeparator()
        self._act(f, "Pull Collection from &Instrument (.KSC)...", self.on_pull_collection)
        self._act(f, "Pull Multisample from Instrument (.KMP)...", self.on_pull_multisample)
        self._a_push_sample = self._act(f, "Push Sample to Instrument", self.on_push_sample)
        self._a_push_ms = self._act(f, "Push Multisample to Instrument", self.on_push_multisample)
        f.addSeparator()
        self._a_import = self._act(f, "&Import Audio (WAV/MP3/MP4)...", self.on_import_audio)
        self._a_import_stereo = self._act(f, "Import Audio as Stereo &Pair...", self.on_import_stereo_audio)
        self._a_new_zone = self._act(f, "New &Zone from Existing Sample (.KSF)...", self.on_new_zone_from_ksf)
        self._a_export_sample = self._act(f, "Export Sample to WAV...", self.on_export_sample)
        self._a_export_ms = self._act(f, "Export Multisample to Folder...", self.on_export_multisample)
        self._a_export_coll = self._act(f, "Export Collection to Folder...", self.on_export_collection)
        f.addSeparator()
        self._a_report = self._act(f, "Sample Report...", self.on_report)
        f.addSeparator()
        self._act(f, "&Close", self.close, "Alt+F4", "Closes the editor window. Unsaved edits are kept in memory only until then.")
        f.aboutToShow.connect(self._on_file_menu_opened)

        e = mb.addMenu("&Edit")
        self._m_edit = e
        self._a_undo = self._act(e, "&Undo", self.on_undo, "Ctrl+Z")
        self._a_redo = self._act(e, "&Redo", self.on_redo, "Ctrl+Y")
        e.addSeparator()
        self._a_cut = self._act(e, "Cu&t", self.on_cut, "Ctrl+X")
        self._a_copy = self._act(e, "&Copy", self.on_copy, "Ctrl+C")
        self._a_paste = self._act(e, "&Paste", self.on_paste, "Ctrl+V")
        self._a_select_all = self._act(e, "Select &All", self.on_select_all, "Ctrl+A")
        e.addSeparator()
        self._a_reverse = self._act(e, "Re&verse", self.on_reverse,
                                    tip="Reverses the selection, or the whole sample when nothing is highlighted")
        self._a_silence_sel = self._act(e, "&Silence Selection", self.on_silence_selection)
        self._a_insert_silence = self._act(e, "&Insert Silence...", self.on_insert_silence)
        self._a_dc = self._act(e, "Remove D&C Offset", self.on_remove_dc)
        self._a_gain = self._act(e, "&Gain...", self.on_gain_dialog,
                                 tip="Apply an arbitrary dB change - the toolbar presets only offer +/-1, 3 and 6 dB")
        e.addSeparator()
        self._m_zoom = e.addMenu("&Zoom")
        self._act(self._m_zoom, "Zoom &In", self.on_zoom_in, "Ctrl++")
        self._act(self._m_zoom, "Zoom &Out", self.on_zoom_out, "Ctrl+-")
        self._act(self._m_zoom, "Zoom to &Selection", self.on_zoom_selection)
        self._act(self._m_zoom, "&Fit Whole Sample", self.on_zoom_fit, "Ctrl+0")
        e.addSeparator()
        self._a_delete_zone = self._act(e, "&Delete Zone", self.on_delete_zone, "Del")
        e.addSeparator()
        self._a_rename_ms = self._act(e, "Rename &Multisample...", self.on_rename_multisample,
                                      tip="Renames the multisample (mirrored to its stereo partner, if any) - not written until Save.")
        self._a_rename_sample = self._act(e, "Rename &Sample...", self.on_rename_sample,
                                          tip="Renames the loaded sample (mirrored to its stereo partner, if any) - not written until Save.")
        e.addSeparator()
        self._a_revert_ksc = self._act(e, "Revert &KSC Changes", self.on_revert_ksc,
                                       tip="Re-reads the active collection from disk, discarding unsaved edits in it.")
        self._a_revert_all = self._act(e, "Revert &ALL Changes", self.on_revert_all,
                                       tip="Closes every open collection/multisample and starts fresh.")
        e.aboutToShow.connect(self._on_edit_menu_opened)

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Toolbar — the only "what do I do first" affordance on an empty load.
        bar = QFrame()
        bar.setStyleSheet(f"QFrame{{background:{T.PANEL};border-bottom:1px solid {T.BORDER};}}")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(8, 6, 8, 6)
        b_new = QPushButton("New Collection...")
        b_new.setToolTip("Creates a brand-new, empty .KSC collection")
        b_new.clicked.connect(self.on_new_collection)
        bl.addWidget(b_new)
        bl.addSpacing(16)
        bl.addWidget(_label("Open Collection:"))
        b_file = QPushButton("From File...")
        b_file.clicked.connect(self.on_open_collection)
        b_kron = QPushButton("From Instrument...")
        b_kron.clicked.connect(self.on_pull_collection)
        bl.addWidget(b_file)
        bl.addWidget(b_kron)
        bl.addStretch()
        outer.addWidget(bar)

        split = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(split, 1)

        # Left: the tree only picks which LOADED LIBRARY (.KSC) is active — multisample/zone selection lives in the
        # right pane (MS and Sample dropdowns, Index field, keymap).
        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.setMinimumWidth(240)
        self._tree.itemSelectionChanged.connect(self._on_tree_selection)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        split.addWidget(self._tree)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        self._scroll.setWidget(content)
        split.addWidget(self._scroll)
        split.setStretchFactor(1, 1)
        split.setSizes([340, 860])

        cl = QVBoxLayout(content)
        cl.setContentsMargins(16, 16, 16, 16)
        self._empty_text = _label("Import a .KSC file or open a collection to begin.")
        cl.addWidget(self._empty_text)
        self._editor = QWidget()
        el = QVBoxLayout(self._editor)
        el.setContentsMargins(0, 0, 0, 0)
        cl.addWidget(self._editor)
        cl.addStretch()

        hdr = QLabel("Multisample Editor")
        hdr.setStyleSheet(f"font-size:14px;font-weight:600;color:{T.TEXT};")
        el.addWidget(hdr)
        el.addWidget(self._build_ms_section())
        self._zone_panel = self._build_zone_section()
        el.addWidget(self._zone_panel)
        self._editing_frame = self._build_editing_frame()
        el.addWidget(self._editing_frame)
        self._no_selection = _label("Select a zone in the tree to view/edit its sample.")
        el.addWidget(self._no_selection)

        # Status bar + the one Save Changes button.
        self._status = QLabel("")
        self._status.setStyleSheet(MUTED)
        self.statusBar().addWidget(self._status, 1)
        self._btn_save = QPushButton("Save Changes")
        self._btn_save.clicked.connect(self.on_save_changes)
        self.statusBar().addPermanentWidget(self._btn_save)

    def _build_ms_section(self) -> QFrame:
        f, lay = _panel("MULTISAMPLE (MS)")
        self._ms_section = f
        self._ms_combo = QComboBox()
        self._ms_combo.setMinimumWidth(320)
        self._ms_combo.currentIndexChanged.connect(self._on_ms_combo_changed)
        self._btn_create_ms = QPushButton("Create")
        self._btn_create_ms.setToolTip("Creates a new multisample in the next free slot - asks mono or stereo")
        self._btn_create_ms.clicked.connect(self.on_create_multisample)
        self._btn_rename_ms = QPushButton("Rename")
        self._btn_rename_ms.clicked.connect(self.on_rename_multisample)
        self._btn_delete_ms = QPushButton("Delete")
        self._btn_delete_ms.setToolTip("Permanently deletes the selected multisample and its samples from disk")
        self._btn_delete_ms.clicked.connect(self.on_delete_multisample)
        lay.addLayout(_flow(self._ms_combo, self._btn_create_ms, self._btn_rename_ms, self._btn_delete_ms))
        self._keymap = SampleKeymapControl()
        self._keymap.zone_clicked.connect(self._on_keymap_zone_clicked)
        self._keymap.boundary_moved.connect(lambda z, k: self._after(lambda: self._model.move_zone_boundary(z, k)))
        self._keymap.zone_reordered.connect(lambda a, b: self._after(lambda: self._model.reorder_zone(a, b)))
        self._keymap.piano_key_clicked.connect(self._on_piano_key_clicked)
        self._keymap.piano_key_released.connect(self._model.release_piano_key)
        self._keymap.piano_key_ctrl_clicked.connect(self._on_piano_ctrl_clicked)
        lay.addWidget(self._keymap)
        return f

    def _build_zone_section(self) -> QFrame:
        f, lay = _panel("SAMPLE")
        self._idx_box = FieldBox("", 34)
        self._idx_box.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._idx_box.step_with_arrows = True
        self._idx_box.wheel_enabled = True
        self._idx_box.setToolTip("Position of this zone within the multisample - Up/Down or the wheel steps")
        self._idx_box.editingFinished.connect(self._on_zone_index_finished)
        self._idx_box.committed.connect(self._on_zone_index_finished)
        self._idx_box.key_step.connect(lambda d: self._commit_zone_index(self._current_zone_index() + d))
        self._idx_box.wheel_step.connect(lambda d: self._commit_zone_index(self._current_zone_index() + d))
        self._idx_total = QLabel("")
        self._sample_combo = QComboBox()
        self._sample_combo.setMinimumWidth(220)
        self._sample_combo.setToolTip("Assigns a sample to this zone - lists everything already imported into the "
                                      "collection's repository; the first entry is what's currently assigned")
        self._sample_combo.activated.connect(self._on_zone_sample_activated)
        self._link_box = QCheckBox("Sample Shortcut")
        self._link_box.setToolTip("Enabled: create a shortcut to a sample without duplicating. "
                                  "Disabled: duplicate with separate settings and controls. See help for details.")
        self._orig_box = FieldBox("", 55)
        self._orig_box.setToolTip("Note name, e.g. C4")
        self._top_box = FieldBox("", 55)
        self._top_box.setToolTip("Note name, e.g. G5")
        for box, which in ((self._orig_box, "orig"), (self._top_box, "top")):
            box.wheel_enabled = True
            box.textEdited.connect(lambda _t, w=which: self._on_zone_key_edited(w))
            box.editingFinished.connect(lambda w=which: self._on_zone_key_edited(w))
            box.committed.connect(lambda w=which: self._on_zone_key_edited(w))
            box.wheel_step.connect(lambda d, w=which: self._on_zone_key_wheel(w, d))
        self._range_text = _label("")
        lay.addLayout(_flow(_label("Index:"), self._idx_box, self._idx_total, _label("Sample:"), self._sample_combo,
                            self._link_box, _label("Orig.Key:"), self._orig_box, _label("Top Key:"), self._top_box,
                            _label("Range:"), self._range_text))
        self._btn_add_zone = QPushButton("Create")
        self._btn_add_zone.setToolTip("Adds an empty zone - right-click it and choose Import Sample... to attach audio")
        self._btn_add_zone.clicked.connect(self.on_add_zone)
        self._btn_rename_sample = QPushButton("Rename")
        self._btn_rename_sample.clicked.connect(self.on_rename_sample)
        self._btn_delete_zone = QPushButton("Delete Zone")
        self._btn_delete_zone.setToolTip("Deletes the zone, but not the sample from the current session")
        self._btn_delete_zone.clicked.connect(self.on_delete_zone)
        self._btn_import_zone = QPushButton("Import Sample...")
        self._btn_import_zone.setToolTip("Decodes audio files into the collection's repository and assigns the first to this zone")
        self._btn_import_zone.clicked.connect(self.on_import_sample_into_zone)
        self._btn_remove_sample = QPushButton("Remove Sample")
        self._btn_remove_sample.setToolTip("Removes the selected sample from the session; a stereo sample loses both channels")
        self._btn_remove_sample.clicked.connect(self.on_remove_sample)
        lay.addLayout(_flow(self._btn_add_zone, self._btn_rename_sample, self._btn_delete_zone,
                            self._btn_import_zone, self._btn_remove_sample))
        return f

    def _build_editing_frame(self) -> QFrame:
        f, lay = _panel("SAMPLE/LOOP EDITOR")

        self._tempo_box = FieldBox("1.0", 48)
        self._tempo_box.setToolTip("Playback-speed multiplier, 0.25 to 4. Out-of-range values are clamped.")
        self._pitch_box = FieldBox("0", 48)
        self._pitch_box.setToolTip("Transpose by up to 24 semitones either way. Out-of-range values are clamped.")
        self._btn_tempo = QPushButton("Apply Tempo/Pitch")
        self._btn_tempo.clicked.connect(self.on_tempo_pitch)
        lay.addLayout(_flow(_label("Tempo Multiplier:"), self._tempo_box, _label("Pitch (semitones):"),
                            self._pitch_box, self._btn_tempo))

        self._name_text = QLabel("")
        self._frames_text = QLabel("")
        self._rate_box = FieldBox("", 90)
        self._rate_box.setReadOnly(True)
        self._rate_box.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._rate_box.setStyleSheet(f"border:1px solid transparent;background:transparent;{MUTED}")
        self._rate_box.setToolTip("Informational - resample via Tempo/Pitch above to actually change this")
        self._warning = QLabel("")
        self._warning.setStyleSheet(f"color:{T.ERROR_TEXT};")
        lay.addLayout(_flow(_label("Name:"), self._name_text, _label("Frames:"), self._frames_text,
                            _label("Sample Rate:"), self._rate_box, self._warning))

        # KRONOS panel — fields written into the .KSF and reflected on the hardware once pushed.
        kp, kl = _panel("INSTRUMENT", "kronos")
        self._reverse_box = QCheckBox("Reverse")
        self._reverse_box.setToolTip("The instrument Reverse flag - reverses playback direction, doesn't touch the audio data")
        self._boost_box = QCheckBox("+12dB Boost")
        self._boost_box.setToolTip("Sample-level +12dB gain boost")
        self._loop_box = QCheckBox("Loop Enabled")
        for cb, slot in ((self._reverse_box, self.on_reverse_toggled), (self._boost_box, self.on_boost_toggled),
                         (self._loop_box, self.on_loop_toggled)):
            cb.clicked.connect(lambda _c=False, s=slot: s())
        kl.addLayout(_flow(self._reverse_box, self._boost_box, self._loop_box))

        self._start_box = self._marker_box("Sample Start", "#FF0000", SampleMarkerKind.SAMPLE_START)
        self._loop_start_box = self._marker_box("Loop Start", "#00FF00", SampleMarkerKind.LOOP_START)
        self._loop_end_box = self._marker_box("Loop End", "#0000FF", SampleMarkerKind.LOOP_END)
        self._tune_box = FieldBox("", 90)
        self._tune_box.setToolTip("-99..+99. Changes tuning of the loop.")
        self._tune_box.textEdited.connect(lambda _t: self._on_tune_edited())
        self._tune_box.editingFinished.connect(self._on_tune_edited)
        self._tune_box.committed.connect(self._on_tune_edited)
        self._btn_loop_sel = QPushButton("Loop Selected")
        self._btn_loop_sel.setToolTip("Sets Loop Start/End to the current waveform selection - highlight a range first")
        self._btn_loop_sel.clicked.connect(lambda: self._after(self._model.set_loop_from_selection))
        self._loop_fields = QWidget()
        lf = QHBoxLayout(self._loop_fields)
        lf.setContentsMargins(0, 0, 0, 0)
        for lbl, box in (("Sample Start:", self._start_box), ("Loop Start:", self._loop_start_box),
                         ("Loop End:", self._loop_end_box), ("Loop Tune:", self._tune_box)):
            lf.addWidget(_label(lbl))
            lf.addWidget(box)
        lf.addStretch()
        kl.addWidget(self._loop_fields)
        kl.addLayout(_flow(self._btn_loop_sel))
        lay.addWidget(kp)

        # LOCAL EDITS panel — destructive DSP that only touches this app's own buffer and undo stack.
        lp, ll = _panel("LOCAL EDITS", "local")
        self._btn_select_tool = _tool_button(path_icon("select", 20), "Select tool - click/drag to select a range (default)",
                                             (36, 32), True)
        self._btn_move_tool = _tool_button(path_icon("move", 20),
                                           "Move tool - drag to relocate a selection or the loop region; with Split L/R on, "
                                           "drag the bare waveform to offset that channel", (36, 32), True)
        self._btn_select_tool.clicked.connect(lambda: self.set_tool(False))
        self._btn_move_tool.clicked.connect(lambda: self.set_tool(True))
        self._btn_select_tool.setChecked(True)
        ll.addLayout(_flow(self._btn_select_tool, self._btn_move_tool))
        self._zero_box = QCheckBox("Use Zero")
        self._zero_box.setToolTip("Snaps Sample Start/Loop Start/Loop End to the nearest zero-crossing (either channel of a pair)")
        self._lock_box = QCheckBox("Loop Lock")
        self._lock_box.setToolTip("Links Loop Start and End: editing one adjusts the other to keep the loop length")
        self._split_box = QCheckBox("Split L/R")
        self._split_box.setToolTip("Combined: edits apply to both channels. Split: edits apply only to the pane you click/drag on.")
        self._zero_box.clicked.connect(lambda: setattr(self._model, "use_zero_crossing", self._zero_box.isChecked()))
        self._lock_box.clicked.connect(self.on_loop_lock_toggled)
        self._split_box.clicked.connect(self.on_split_toggled)
        ll.addLayout(_flow(self._zero_box, self._lock_box, self._split_box))

        self._btn_norm = QPushButton("Normalize")
        self._btn_norm.setToolTip("Normalizes the selection, or the whole sample when nothing is highlighted")
        self._btn_norm.clicked.connect(self.on_normalize)
        self._btn_amp = QPushButton("Amplify ▾")
        self._btn_amp.setToolTip("Boosts the selection (or whole sample) by a fixed dB amount")
        self._btn_amp.clicked.connect(lambda: self._gain_menu(self._btn_amp, (1, 3, 6)))
        self._btn_soft = QPushButton("Soften ▾")
        self._btn_soft.setToolTip("Cuts the selection (or whole sample) by a fixed dB amount")
        self._btn_soft.clicked.connect(lambda: self._gain_menu(self._btn_soft, (-1, -3, -6)))
        self._btn_trim = QPushButton("Trim Silence")
        self._btn_trim.clicked.connect(lambda: self._after(self._model.apply_silence_trim))
        self._btn_rev_edit = QPushButton("Reverse")
        self._btn_rev_edit.setToolTip("Reverses the selection, or the whole sample when nothing is highlighted")
        self._btn_rev_edit.clicked.connect(self.on_reverse)
        self._btn_dc = QPushButton("Remove DC Offset")
        self._btn_dc.setToolTip("Recentres the waveform on zero. A DC offset costs headroom on Normalize and stops Use Zero finding crossings.")
        self._btn_dc.clicked.connect(self.on_remove_dc)
        self._btn_ins_sil = QPushButton("Insert Silence...")
        self._btn_ins_sil.setToolTip("Inserts silence at the selection start (or the scrub cursor)")
        self._btn_ins_sil.clicked.connect(self.on_insert_silence)
        ll.addLayout(_flow(self._btn_norm, self._btn_amp, self._btn_soft, self._btn_trim, self._btn_rev_edit,
                           self._btn_dc, self._btn_ins_sil))
        lay.addWidget(lp)

        lay.addWidget(self._build_waveform_frame())
        self._sel_info = _label("")
        self._sel_info.setStyleSheet(MUTED + "font-size:11px;")
        lay.addWidget(self._sel_info)
        return f

    def _marker_box(self, name: str, color: str, kind: SampleMarkerKind) -> FieldBox:
        box = FieldBox("", 90)
        box.setStyleSheet(f"QLineEdit{{border:1px solid {color};}}")
        box.setToolTip("Mouse-wheel steps by ~1% of the sample length - border color matches the marker line")
        box.wheel_enabled = True
        box.textEdited.connect(lambda _t, k=kind, b=box: self._on_marker_edited(k, b, False))
        box.editingFinished.connect(lambda k=kind, b=box: self._on_marker_edited(k, b, True))
        box.committed.connect(lambda k=kind, b=box: self._on_marker_edited(k, b, True))
        box.wheel_step.connect(lambda d, k=kind, b=box: self._on_marker_wheel(k, b, d))
        return box

    def _build_waveform_frame(self) -> QFrame:
        f, lay = _panel("")
        row = QHBoxLayout()
        row.setSpacing(6)   # same gap as the LOCAL EDITS rows (_flow); 1 px ran the buttons together
        self._btn_start = _tool_button(path_icon("locate_start", 14), "Rewind to start (Home)")
        self._btn_rew = _tool_button(path_icon("rewind", 14), "Rewind")
        self._btn_play = _tool_button(play_stop_icon(False), "Play / Stop (Space)")
        self._btn_pause = _tool_button(path_icon("pause", 14), "Pause / Resume")
        self._btn_ff = _tool_button(path_icon("forward", 14), "Fast-forward")
        self._btn_end = _tool_button(path_icon("locate_end", 14), "Go to end (End)")
        self._btn_start.clicked.connect(lambda: self._after(self._model.transport_locate_start))
        self._btn_rew.clicked.connect(lambda: self._after(lambda: self._model.transport_seek_relative(-1)))
        self._btn_play.clicked.connect(self.toggle_playback)
        self._btn_pause.clicked.connect(lambda: self._after(self._model.transport_toggle_pause))
        self._btn_ff.clicked.connect(lambda: self._after(lambda: self._model.transport_seek_relative(1)))
        self._btn_end.clicked.connect(lambda: self._after(self._model.transport_locate_end))
        for b in (self._btn_start, self._btn_rew, self._btn_play, self._btn_pause, self._btn_ff, self._btn_end):
            row.addWidget(b)
        row.addSpacing(10)
        self._btn_zin = _tool_button(zoom_icon(True), "Zoom in (Ctrl+Plus, or scroll over the waveform)")
        self._btn_zout = _tool_button(zoom_icon(False), "Zoom out (Ctrl+Minus)")
        self._btn_zin.clicked.connect(self.on_zoom_in)
        self._btn_zout.clicked.connect(self.on_zoom_out)
        self._btn_zsel = QPushButton("Zoom to Selection")
        self._btn_zsel.setToolTip("Fit the highlighted range to the full width - highlight a range first")
        self._btn_zsel.clicked.connect(self.on_zoom_selection)
        self._btn_fit = QPushButton("Fit")
        self._btn_fit.setToolTip("Show the whole sample (Ctrl+0, or double-click the waveform)")
        self._btn_fit.clicked.connect(self.on_zoom_fit)
        self._scroll_zoom = QCheckBox("Scroll to Zoom")
        self._scroll_zoom.setChecked(True)
        self._scroll_zoom.setToolTip("Checked: the wheel over the waveform zooms. Unchecked: it just scrolls this pane.")
        self._scroll_zoom.clicked.connect(self._on_scroll_zoom_toggled)
        for w in (self._btn_zin, self._btn_zout, self._btn_zsel, self._btn_fit, self._scroll_zoom):
            row.addWidget(w)
        row.addSpacing(10)
        self._btn_undo = _tool_button(undo_icon(False), "Undo")
        self._btn_redo = _tool_button(undo_icon(True), "Redo")
        self._btn_undo.clicked.connect(self.on_undo)
        self._btn_redo.clicked.connect(self.on_redo)
        row.addWidget(self._btn_undo)
        row.addWidget(self._btn_redo)
        row.addStretch()
        lay.addLayout(row)

        # Waveform stack (L, optional R) + ruler + scrollbar on the left; pan / L-R VU / volume on the right.
        grid = QHBoxLayout()
        left = QVBoxLayout()
        left.setSpacing(0)
        self._wf_left = self._make_pane(True)
        self._wf_right = self._make_pane(False)
        self._lbl_l = QLabel("L")
        self._lbl_r = QLabel("R")
        for lb in (self._lbl_l, self._lbl_r):
            lb.setFixedWidth(16)
            lb.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lb.setStyleSheet(MUTED + "font-weight:bold;")
        self._row_l = QWidget()
        rl = QHBoxLayout(self._row_l)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)
        rl.addWidget(self._lbl_l)
        rl.addWidget(self._wf_left)
        self._divider = QFrame()
        self._divider.setStyleSheet(f"background:{T.BORDER_STRONG};")
        self._row_r = QWidget()
        rr = QHBoxLayout(self._row_r)
        rr.setContentsMargins(0, 0, 0, 0)
        rr.setSpacing(0)
        rr.addWidget(self._lbl_r)
        rr.addWidget(self._wf_right)
        left.addWidget(self._row_l)
        left.addWidget(self._divider)
        left.addWidget(self._row_r)
        self._ruler = SampleWaveformRuler()
        left.addWidget(self._ruler)
        self._hscroll = QScrollBar(Qt.Orientation.Horizontal)
        self._hscroll.valueChanged.connect(self._on_hscroll)
        left.addWidget(self._hscroll)
        grid.addLayout(left, 1)

        right = QFrame()
        right.setFixedWidth(104)
        right.setStyleSheet(f"QFrame#vu{{border:1px solid {T.BORDER_STRONG};border-radius:2px;}}")
        right.setObjectName("vu")
        rg = QGridLayout(right)
        rg.setContentsMargins(8, 6, 8, 6)
        rg.setSpacing(3)
        self._pan = SamplePanControl()
        self._pan.setFixedHeight(22)
        self._pan.pan = self._model.pan
        self._pan.pan_changed.connect(lambda v: setattr(self._model, "pan", v))
        self._vu_l = SampleVuMeter(show_labels=False)
        self._vu_r = SampleVuMeter(show_labels=False)
        self._volume = SampleVolumeControl()
        self._volume.volume = self._model.volume
        self._volume.volume_changed.connect(lambda v: setattr(self._model, "volume", float(v)))
        pan_cap, vol_cap = QLabel("Pan"), QLabel("Vol")
        for c in (pan_cap, vol_cap):
            c.setAlignment(Qt.AlignmentFlag.AlignCenter)
            c.setStyleSheet(MUTED + "font-size:10px;")
        rg.addWidget(pan_cap, 0, 0, 1, 3)
        rg.addWidget(self._pan, 1, 0, 1, 3)
        rg.addWidget(vol_cap, 2, 2)
        rg.addWidget(self._vu_l, 3, 0)
        rg.addWidget(self._vu_r, 3, 1)
        rg.addWidget(self._volume, 3, 2, 2, 1)
        cap_l, cap_r = QLabel("L"), QLabel("R")
        for c in (cap_l, cap_r):
            c.setAlignment(Qt.AlignmentFlag.AlignCenter)
            c.setStyleSheet(MUTED + "font-size:9px;")
        rg.addWidget(cap_l, 4, 0)
        rg.addWidget(cap_r, 4, 1)
        rg.setColumnMinimumWidth(2, 34)
        rg.setRowStretch(3, 1)
        grid.addWidget(right)
        self._vu_left_widgets = (self._vu_l, cap_l)
        lay.addLayout(grid)
        return f

    def _make_pane(self, is_left: bool) -> SampleWaveformControl:
        wf = SampleWaveformControl()
        wf.setFixedHeight(170)
        wf.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        wf.customContextMenuRequested.connect(lambda pos, w=wf: self._on_wave_context_menu(w, pos))
        wf.selection_changed.connect(lambda w=wf: self._on_pane_selection_changed(w))
        wf.selection_preview_changed.connect(lambda w=wf: self._mirror_selection_preview(w))
        wf.view_changed.connect(lambda w=wf: self._sync_views(w))
        wf.loop_region_changed.connect(lambda a, b: self._after(lambda: self._model.move_loop_region(a, b)))
        wf.marker_dragged.connect(lambda k, fr, w=wf: self._on_pane_marker_dragged(w, k, fr))
        wf.markers_changing.connect(lambda w=wf: self._mirror_markers_preview(w))
        wf.scrub_requested.connect(lambda fr, w=wf: self._on_pane_scrub(w, fr))
        wf.waveform_moved.connect(lambda d, w=wf: self._on_pane_moved(w, d))
        wf.channel_double_clicked.connect(self._on_pane_channel_double_clicked)
        wf.drag_started.connect(lambda: setattr(self, "_split_drag_crossed", False))
        return wf

    # ── refresh (port of RefreshDetailPanels) ──────────────────────────────────────────────

    def _after(self, mutate: Callable[[], None]) -> None:
        mutate()
        self.refresh()
        self._update_status()

    def _after_keep_multisample(self, mutate: Callable[[], None]) -> None:
        """For mutations whose model call rebuilds the tree (imports that add zones): the rebuild drops the selection
        and collapses the zone/sample panels, so re-enter the multisample the action ran in."""
        ctx = self._model._resolve_context_multisample()[1]
        mutate()
        self.refresh()
        self._update_status()
        if ctx is not None and self._model.current_multisample_zones is None:
            node = find_multisample_node(self._model.roots, ctx)
            if node is not None:
                self._select_tree_node(node)

    def _update_status(self) -> None:
        self._status.setText(self._model.status_text)

    def _update_title(self) -> None:
        m = self._model
        name = os.path.basename(m.active_collection_path) if m.active_collection_path else None
        self.setWindowTitle(("*" if m.has_unsaved_changes else "")
                            + ("Sample Editor - Instrument" if name is None else f"{name} - Sample Editor - Instrument"))
        self._btn_save.setEnabled(m.has_unsaved_changes)
        self._a_save_changes.setEnabled(m.has_unsaved_changes)

    def _set_text(self, edit: QLineEdit, text: str) -> None:
        """Only when different — a programmatic setText with identical text still moves the caret."""
        if edit.text() != text:
            edit.setText(text)

    def refresh(self) -> None:
        m = self._model
        anything = len(m.roots) > 0
        self._empty_text.setVisible(not anything)
        self._editor.setVisible(anything)
        self._update_title()
        self._rebuild_tree_items()
        if not anything:
            return

        zones = m.current_multisample_zones
        has_ms = zones is not None
        self._zone_panel.setVisible(has_ms)
        self._keymap.setVisible(has_ms)
        has_sample = m.has_sample_loaded
        self._editing_frame.setVisible(has_sample)
        self._no_selection.setVisible(not m.has_zone_selected)
        self._a_unload.setEnabled(m.has_active_collection)
        self._a_revert_ksc.setEnabled(m.has_active_collection)
        self._a_revert_all.setEnabled(len(m.roots) > 0)
        self._a_rename_ms.setEnabled(m.current_multisample_name is not None)
        self._btn_rename_ms.setEnabled(m.current_multisample_name is not None)
        self._a_rename_sample.setEnabled(has_sample)

        if m.has_zone_selected:
            self._set_text(self._orig_box, to_name(m.zone_original_key))
            self._set_text(self._top_box, to_name(m.zone_top_key))
        else:
            self._set_text(self._orig_box, "")
            self._set_text(self._top_box, "")

        all_ms = m.all_multisample_nodes()
        current_ms = next((n for n in all_ms if n.multisample_ref[0].zones is zones), None) if zones is not None else None
        self._refresh_index_and_sample_combo(current_ms.multisample_ref[1] if current_ms else None)
        self._refresh_ms_combo(current_ms, all_ms)

        self._keymap.zones = zones
        self._keymap.selected_zone = m.selected_zone
        self._keymap.update()

        self._split_box.setVisible(m.has_stereo_pair)
        self._split_box.setChecked(m.split_lr)
        self._vu_l.setVisible(m.has_stereo_pair)

        if has_sample:
            self._refresh_sample_panels()
        else:
            self._wf_left.samples = None
            self._wf_right.samples = None
            self._set_stereo_rows(False, False)

        self._btn_undo.setEnabled(m.can_undo)
        self._btn_redo.setEnabled(m.can_redo)
        self._a_undo.setEnabled(m.can_undo)
        self._a_redo.setEnabled(m.can_redo)
        self._btn_play.setIcon(play_stop_icon(m.is_playing))
        self._btn_play.setToolTip("Stop" if m.is_playing else "Play")
        usable = has_sample and (not m.sample_is_header_only or m.sample_is_linked_stub or m.is_playing)
        for b in (self._btn_play, self._btn_start, self._btn_rew, self._btn_ff, self._btn_end):
            b.setEnabled(usable)
        self._btn_pause.setEnabled(usable and (m.is_playing or m.is_paused))
        self._btn_pause.setIcon(path_icon("pause", 14, ICON_GREEN if m.is_paused else ICON_GREY))
        can_delete = m.has_zone_selected and (len(zones) if zones else 0) > 1
        self._btn_delete_zone.setEnabled(can_delete)
        self._a_delete_zone.setEnabled(can_delete)
        self._btn_rename_sample.setEnabled(has_sample)
        self._btn_remove_sample.setEnabled(m.has_zone_selected and not m.zone_is_skipped)
        self._btn_import_zone.setEnabled(m.has_zone_selected)
        has_sel = m.selection_end_frame > m.selection_start_frame
        self._btn_zsel.setEnabled(has_sel)
        if has_sel:
            frames = m.selection_end_frame - m.selection_start_frame
            dur = f" = {frames / m.sample_rate:.3f} s" if m.sample_rate > 0 else ""
            self._sel_info.setText(f"Selection: [{m.selection_start_frame}, {m.selection_end_frame})  ({frames} frames{dur})"
                                   "  -  drag on the waveform to change, scroll to zoom, double-click to reset zoom")
        else:
            self._sel_info.setText("No selection - drag on the waveform to select a range. Scroll to zoom, double-click to reset.")

    def _refresh_sample_panels(self) -> None:
        m = self._model
        self._name_text.setText(m.sample_name)
        self._frames_text.setText(str(m.sample_frame_count))
        # Three states doc §3.2/§3.3 draw apart: own audio, a resolved link (borrowed, shown so it reads as
        # "linked" not "broken"), and an unresolved link / no SMF1 at all (genuinely no audio to recover).
        if m.sample_is_linked_stub:
            warn = f"Linked sample - plays '{m.sample_link_target_file}', this zone's own loop points"
        elif not m.sample_is_header_only:
            warn = ""
        elif m.sample_link_target_file:
            warn = f"Linked sample - target '{m.sample_link_target_file}' not found in this collection"
        else:
            warn = "No audio data (header-only save)"
        self._warning.setText(warn)
        self._rate_box.setText(str(m.sample_rate))
        self._loop_box.setChecked(m.sample_loop_enabled)
        self._loop_fields.setVisible(m.sample_loop_enabled)
        self._btn_loop_sel.setVisible(m.sample_loop_enabled)
        self._set_text(self._start_box, str(m.sample_start))
        self._set_text(self._loop_start_box, str(m.loop_start))
        self._set_text(self._loop_end_box, str(m.loop_end))
        self._zero_box.setChecked(m.use_zero_crossing)
        self._lock_box.setChecked(m.loop_lock_enabled)
        self._reverse_box.setChecked(m.sample_reverse_enabled)
        self._boost_box.setChecked(m.sample_12db_boost_enabled)
        self._set_text(self._tune_box, str(m.sample_loop_tune))
        self._btn_select_tool.setChecked(not m.is_move_tool_active)
        self._btn_move_tool.setChecked(m.is_move_tool_active)

        L, R = self._wf_left, self._wf_right
        if m.has_stereo_pair:
            self._set_stereo_rows(True, m.split_lr)
            # view_frame_count MUST be set on both panes BEFORE the samples: assigning samples resets/reclamps the
            # view from it, and Split's Move tool can leave the two channels different lengths — without the shared
            # span in place first, the pane that updates first clamps the pair back and erases the offset.
            lw, rw = m.left_sample_waveform, m.right_sample_waveform
            span = max(len(lw) if lw is not None else 0, len(rw) if rw is not None else 0)
            L.view_frame_count = R.view_frame_count = span
            L.samples, R.samples = lw, rw
            for pane in (L, R):
                pane.move_tool_active = m.is_move_tool_active
                pane.can_move_waveform = m.split_lr
            if not m.split_lr:
                for pane in (L, R):
                    pane.selection_start_frame, pane.selection_end_frame = m.selection_start_frame, m.selection_end_frame
                    pane.sample_start_frame, pane.loop_start_frame, pane.loop_end_frame = m.sample_start, m.loop_start, m.loop_end
                    pane.loop_enabled, pane.loop_lock_enabled = m.sample_loop_enabled, m.loop_lock_enabled
                    pane.is_split_channel_pane = False
                    pane.is_active_channel = False
            else:
                left_active, both = m.is_primary_left_channel, m.split_both_active
                L.selection_start_frame = m.selection_start_frame if (left_active or both) else 0
                L.selection_end_frame = m.selection_end_frame if (left_active or both) else 0
                R.selection_start_frame = m.selection_start_frame if (not left_active or both) else 0
                R.selection_end_frame = m.selection_end_frame if (not left_active or both) else 0
                L.sample_start_frame, R.sample_start_frame = m.left_sample_start_frame, m.right_sample_start_frame
                L.loop_start_frame, R.loop_start_frame = m.left_loop_start_frame, m.right_loop_start_frame
                L.loop_end_frame, R.loop_end_frame = m.left_loop_end_frame, m.right_loop_end_frame
                L.loop_enabled, R.loop_enabled = m.left_loop_enabled, m.right_loop_enabled
                L.loop_lock_enabled = R.loop_lock_enabled = m.loop_lock_enabled
                L.is_split_channel_pane = R.is_split_channel_pane = True
                L.is_active_channel = left_active or both
                R.is_active_channel = (not left_active) or both
            L.update()
            R.update()
        else:
            self._set_stereo_rows(False, False)
            L.view_frame_count = 0
            L.samples = m.sample_waveform
            L.selection_start_frame, L.selection_end_frame = m.selection_start_frame, m.selection_end_frame
            L.sample_start_frame, L.loop_start_frame, L.loop_end_frame = m.sample_start, m.loop_start, m.loop_end
            L.loop_enabled, L.loop_lock_enabled = m.sample_loop_enabled, m.loop_lock_enabled
            L.move_tool_active = m.is_move_tool_active
            L.can_move_waveform = False
            L.is_split_channel_pane = L.is_active_channel = False

    def _set_stereo_rows(self, visible: bool, wide_gap: bool) -> None:
        """A hidden child must also give back its fixed height, or a mono sample keeps the R pane's dead space.
        Split gets a visibly wider seam (14 vs 2 px) so two independent channels read as independent."""
        self._row_r.setVisible(visible)
        self._divider.setVisible(visible)
        self._divider.setFixedHeight(14 if (visible and wide_gap) else 2)
        self._lbl_l.setVisible(visible)

    def _rebuild_tree_items(self) -> None:
        roots = self._model.roots
        cur = [self._tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole) for i in range(self._tree.topLevelItemCount())]
        if len(cur) != len(roots) or any(a is not b for a, b in zip(cur, roots)) or any(
                self._tree.topLevelItem(i).text(0) != roots[i].label for i in range(len(roots))):
            self._tree.blockSignals(True)
            self._tree.clear()
            for r in roots:
                it = QTreeWidgetItem([r.label])
                it.setData(0, Qt.ItemDataRole.UserRole, r)
                self._tree.addTopLevelItem(it)
            self._tree.blockSignals(False)
        self._highlight_owning_root(self._model._selected_node)

    def _highlight_owning_root(self, node: Optional[SampleTreeNode]) -> None:
        if node is None:
            return
        for i, r in enumerate(self._model.roots):
            if r is node or any(n is node for n in enumerate_nodes(r.children)):
                item = self._tree.topLevelItem(i)
                if item is not None and not item.isSelected():
                    self._tree.blockSignals(True)
                    self._tree.setCurrentItem(item)
                    self._tree.blockSignals(False)
                return

    # ── index / sample / MS combos ─────────────────────────────────────────────────────────

    def _repo_info(self, ksf_path: str) -> Optional[Tuple[str, str]]:
        """Name/Suffix of a .KSF without re-parsing its PCM on every refresh. A pending edit wins and is never
        cached (disk hasn't changed until Save)."""
        pending = self._model.try_get_pending_sample_info(ksf_path)
        if pending is not None:
            return pending
        hit = self._repo_cache.get(ksf_path.lower())
        if hit is not None:
            return hit
        try:
            if not os.path.isfile(ksf_path):
                return None
            with open(ksf_path, "rb") as fh:
                s = KsfSample.open(fh.read())
        except Exception:
            return None
        if s is None:
            return None
        self._repo_cache[ksf_path.lower()] = (s.name, s.suffix)
        return s.name, s.suffix

    @staticmethod
    def _stereo_tag(suffix: Optional[str]) -> str:
        return "(S)" if suffix in ("-L", "-R") else "(M)"

    def _refresh_index_and_sample_combo(self, kmp_path: Optional[str]) -> None:
        m = self._model
        zones = m.current_multisample_zones
        selected = m.selected_zone
        idx = next((i for i, z in enumerate(zones) if z is selected), -1) if zones is not None and selected is not None else -1

        self._suppress_combo = True
        self._sample_combo.clear()
        self._sample_combo_paths: List[str] = []
        if kmp_path is not None and selected is not None:
            own_path = None if selected.is_skipped else selected.ksf_path(kmp_path)
            own_info = self._repo_info(own_path) if own_path else None
            if own_path:
                own_name = own_info[0] if own_info else selected.filename
                self._sample_combo.addItem(f"{own_name} {self._stereo_tag(own_info[1] if own_info else None)}")
                self._sample_combo_paths.append(own_path)
            repo = [(p, self._repo_info(p)) for p in m.bare_sample_entries()]
            repo = [(p, i) for p, i in repo if i is not None]
            grouped = set()
            for path, info in repo:
                if path in grouped:
                    continue
                # Excluded by IDENTITY (Name, and Suffix for mono; Name alone for a stereo pair), NOT by path:
                # assigning copies the audio into the zone's own file, so the zone's file and the repository entry
                # it came from are two paths with identical content, and a path check listed the same sample twice.
                if own_info is not None and info[0] == own_info[0] and (
                        info[1] == own_info[1] or (own_info[1] in ("-L", "-R") and info[1] in ("-L", "-R"))):
                    continue
                self._sample_combo.addItem(f"{info[0]} {self._stereo_tag(info[1])}")
                self._sample_combo_paths.append(path)
                if info[1] in ("-L", "-R"):
                    want = "-R" if info[1] == "-L" else "-L"
                    partner = next((p for p, i in repo if i[0] == info[0] and i[1] == want), None)
                    if partner:
                        grouped.add(partner)
            self._sample_combo.setCurrentIndex(0 if own_path and self._sample_combo.count() else -1)
        self._suppress_combo = False

        self._set_text(self._idx_box, str(idx + 1) if idx >= 0 else "")
        self._idx_total.setText(f"/ {len(zones)}" if zones is not None else "")
        if idx >= 0 and zones is not None:
            low = 0 if idx == 0 else zones[idx - 1].top_key + 1
            self._range_text.setText(f"({to_name(low)} - {to_name(selected.top_key)})")
        else:
            self._range_text.setText("")

    def _refresh_ms_combo(self, current: Optional[SampleTreeNode], all_nodes: List[SampleTreeNode]) -> None:
        self._suppress_combo = True
        self._ms_combo.clear()
        self._ms_nodes = list(all_nodes)
        for n in all_nodes:
            self._ms_combo.addItem(n.label)
        if current is not None:
            self._ms_combo.setCurrentIndex(next(i for i, n in enumerate(all_nodes) if n is current))
        else:
            self._ms_combo.setCurrentIndex(-1)
        self._suppress_combo = False

    # ── selection plumbing ─────────────────────────────────────────────────────────────────

    def _select_first_root(self) -> None:
        if self._model.roots:
            self._select_tree_node(self._model.roots[0])

    def _select_tree_node(self, target: SampleTreeNode) -> None:
        """Drives the model + panels directly; the tree only highlights whichever library owns the target."""
        self._model.select_node(target)
        self.refresh()
        self._update_status()
        # Picking a multisample drops straight into its first zone (the Kronos shows zone 1 too).
        if target.multisample_ref is not None and target.children:
            self._select_tree_node(target.children[0])

    def _reselect(self, kmp_path: Optional[str], index: int) -> None:
        if kmp_path is None:
            return
        node = find_multisample_node(self._model.roots, kmp_path)
        if node is not None and 0 <= index < len(node.children):
            self._select_tree_node(node.children[index])

    def _on_tree_selection(self) -> None:
        items = self._tree.selectedItems()
        node = items[0].data(0, Qt.ItemDataRole.UserRole) if items else None
        self._model.select_node(node)
        self.refresh()
        self._update_status()

    def _on_ms_combo_changed(self, i: int) -> None:
        if self._suppress_combo or i < 0 or i >= len(self._ms_nodes):
            return
        self._select_tree_node(self._ms_nodes[i])

    def _on_keymap_zone_clicked(self, zone) -> None:
        node = find_node_for_zone(self._model.roots, zone)
        if node is not None:
            self._select_tree_node(node)

    def _on_zone_sample_activated(self, i: int) -> None:
        """The Sample dropdown is the ASSIGNMENT control (zone navigation is the Index box, the keymap and the MS
        list)."""
        if self._suppress_combo or i < 0 or i >= len(self._sample_combo_paths):
            return
        zone = self._model.selected_zone
        if zone is None:
            return
        path = self._sample_combo_paths[i]
        if self._link_box.isChecked():
            kmp = self._model.link_existing_ksf_to_zone(zone, path)
        else:
            kmp = self._model.assign_existing_ksf_to_zone(zone, path)
        index = self._model.last_imported_zone_index
        self._repo_cache.clear()
        self.refresh()
        self._update_status()
        self._reselect(kmp, index)

    # ── zone fields ────────────────────────────────────────────────────────────────────────

    def _current_zone_index(self) -> int:
        zones, sel = self._model.current_multisample_zones, self._model.selected_zone
        if zones is not None and sel is not None:
            for i, z in enumerate(zones):
                if z is sel:
                    return i + 1
        return 1

    def _commit_zone_index(self, idx: int) -> None:
        zones = self._model.current_multisample_zones
        if zones:
            idx = max(1, min(idx, len(zones)))
            node = find_node_for_zone(self._model.roots, zones[idx - 1])
            if node is not None:
                self._select_tree_node(node)
        self.refresh()
        self._update_status()

    def _on_zone_index_finished(self) -> None:
        try:
            self._commit_zone_index(int(self._idx_box.text()))
        except ValueError:
            self.refresh()
            self._update_status()

    def _zone_keys_from_boxes(self) -> Tuple[Optional[int], Optional[int]]:
        return try_parse(self._orig_box.text()), try_parse(self._top_box.text())

    def _on_zone_key_edited(self, which: str) -> None:
        """Each box guards on its OWN text parsing: refresh() rewrites every field from the model, so committing a
        half-typed value ('C' before the octave digit) would stomp the user's keystrokes."""
        orig, top = self._zone_keys_from_boxes()
        m = self._model
        mine = orig if which == "orig" else top
        if mine is None or not m.has_zone_selected:
            return
        self._after(lambda: m.apply_zone_edits(orig if orig is not None else m.zone_original_key,
                                               top if top is not None else m.zone_top_key))

    def _on_zone_key_wheel(self, which: str, direction: int) -> None:
        # One notch = one semitone, deliberately not the percent-of-length step the frame fields use: a key number
        # has a small fixed range where "one notch, one step" is what's expected.
        orig, top = self._zone_keys_from_boxes()
        m = self._model
        if (orig if which == "orig" else top) is None or not m.has_zone_selected:
            return
        o = (orig if orig is not None else m.zone_original_key) + (direction if which == "orig" else 0)
        t = (top if top is not None else m.zone_top_key) + (direction if which == "top" else 0)
        self._after(lambda: m.apply_zone_edits(o, t))

    def _on_piano_key_clicked(self, zone, key: int) -> None:
        self._model.play_zone_at_key(zone, key)
        self.refresh()

    def _on_piano_ctrl_clicked(self, key: int) -> None:
        """Ctrl+Click writes the key into whichever of Orig./Top Key still has focus — the keymap fires this
        before it takes focus itself, so the field the user armed is still the live focus widget."""
        if self._orig_box.hasFocus():
            self._orig_box.setText(to_name(key))
            self._on_zone_key_edited("orig")
        elif self._top_box.hasFocus():
            self._top_box.setText(to_name(key))
            self._on_zone_key_edited("top")

    # ── marker / loop fields ───────────────────────────────────────────────────────────────

    def _wheel_step(self) -> int:
        return max(1, self._model.sample_frame_count // 100)

    def _on_marker_edited(self, kind: SampleMarkerKind, box: FieldBox, finished: bool) -> None:
        try:
            v = int(box.text())
        except ValueError:
            return
        committed = self._model.set_marker(kind, v)
        self.refresh()
        self._update_status()
        # Only on a real commit, and only on LostFocus/Enter — panning on every digit typed would be worse than
        # helpful, and tabbing through with nothing typed must not yank a manually zoomed view.
        if committed and finished and kind is not SampleMarkerKind.SAMPLE_START:
            self._ensure_loop_visible()

    def _on_marker_wheel(self, kind: SampleMarkerKind, box: FieldBox, direction: int) -> None:
        try:
            v = int(box.text())
        except ValueError:
            return
        committed = self._model.set_marker(kind, v + direction * self._wheel_step())
        self.refresh()
        self._update_status()
        if committed and kind is not SampleMarkerKind.SAMPLE_START:
            self._ensure_loop_visible()

    def _ensure_loop_visible(self) -> None:
        """Pan/zoom so the WHOLE loop region is visible when it isn't — only called right after an action that
        changes whether Loop is on or where its points are, so it never fights an unrelated manual zoom."""
        m = self._model
        if not m.sample_loop_enabled or m.loop_end <= m.loop_start:
            return
        w = self._wf_left
        if m.loop_start >= w.view_start_frame and m.loop_end <= w.view_end_frame:
            return
        w.set_view(m.loop_start, m.loop_end)

    def _on_tune_edited(self) -> None:
        try:
            v = int(self._tune_box.text())
        except ValueError:
            return
        self._after(lambda: self._model.set_loop_tune(v))

    def on_loop_toggled(self) -> None:
        committed = self._model.set_loop_enabled(self._loop_box.isChecked())
        self.refresh()
        self._update_status()
        if committed:
            self._ensure_loop_visible()

    def on_reverse_toggled(self) -> None:
        self._after(lambda: self._model.set_reversed(self._reverse_box.isChecked()))

    def on_boost_toggled(self) -> None:
        self._after(lambda: self._model.set_12db_boost_enabled(self._boost_box.isChecked()))

    def on_loop_lock_toggled(self) -> None:
        # Drives the panes directly (whole-region drag gate + fill colour), which only refresh() pushes down.
        self._after(lambda: setattr(self._model, "loop_lock_enabled", self._lock_box.isChecked()))

    def set_tool(self, move: bool) -> None:
        """Exactly one tool is always active, whatever the button toggled itself to."""
        self._model.is_move_tool_active = move
        self._btn_select_tool.setChecked(not move)
        self._btn_move_tool.setChecked(move)
        self.refresh()

    def on_split_toggled(self) -> None:
        def go() -> None:
            self._model.split_lr = self._split_box.isChecked()
            self._model.split_both_active = False       # re-entering Split starts single-active; Combine has no such state
        self._after(go)

    def _on_scroll_zoom_toggled(self) -> None:
        on = self._scroll_zoom.isChecked()
        self._wf_left.scroll_to_zoom = self._wf_right.scroll_to_zoom = on

    # ── waveform panes ─────────────────────────────────────────────────────────────────────

    def _other(self, pane: SampleWaveformControl) -> SampleWaveformControl:
        return self._wf_right if pane is self._wf_left else self._wf_left

    def _activate_split_channel(self, want_left: bool) -> None:
        """In Split, clicking/dragging a pane makes ITS channel the active one — routed through real node selection
        (which resets undo scope; a known cost, not a bug). A plain click means 'just this one', so Both is dropped
        even when the side already matches."""
        m = self._model
        if not m.has_stereo_pair or not m.split_lr:
            return
        m.split_both_active = False
        if want_left == m.is_primary_left_channel:
            return
        partner = m.partner_zone_ref
        if partner is None:
            return
        node = find_node_for_zone(m.roots, partner[0])
        if node is not None:
            self._select_tree_node(node)

    def _on_pane_selection_changed(self, pane: SampleWaveformControl) -> None:
        # Read the pane's just-committed range BEFORE activating: switching channel goes through select_node, which
        # zeroes the selection and refreshes both panes, so reading afterwards would see the cleared 0/0.
        start, end = pane.selection_start_frame, pane.selection_end_frame
        m = self._model
        if m.split_lr and m.has_stereo_pair and self._split_drag_crossed:
            m.split_both_active = True
        else:
            self._activate_split_channel(pane is self._wf_left)
        m.selection_start_frame, m.selection_end_frame = start, end
        self.refresh()
        # The sibling's mirrored preview bypasses the committed selection, so committing never clears it; with one
        # channel active the inactive pane's real selection is 0/0 (a no-op write), which would leave it stuck.
        self._other(pane).clear_preview_selection()

    def _mirror_selection_preview(self, source: SampleWaveformControl) -> None:
        m = self._model
        if not m.has_stereo_pair:
            return
        other = self._other(source)
        # Latched for the rest of the gesture once the pointer enters the sibling pane: in Split a drag highlights
        # only its own pane UNTIL it crosses over, and a crossing drag activates both channels.
        if not self._split_drag_crossed and 0 <= other.mapFromGlobal(QCursor.pos()).y() <= other.height():
            self._split_drag_crossed = True
        if m.split_lr and not self._split_drag_crossed:
            other.clear_preview_selection()
            return
        other.set_preview_selection(source.effective_selection_start, source.effective_selection_end)

    def _mirror_markers_preview(self, source: SampleWaveformControl) -> None:
        m = self._model
        if not m.has_stereo_pair or m.split_lr:
            return
        other = self._other(source)
        if not source.has_marker_preview:
            other.clear_preview_markers()
            return
        other.set_preview_markers(source.effective_sample_start, source.effective_loop_start, source.effective_loop_end)

    def _on_pane_marker_dragged(self, pane: SampleWaveformControl, kind, frame: int) -> None:
        # The DRAGGED pane's channel must be active before the commit, or in Split the line snaps back and the
        # OTHER channel moves.
        self._activate_split_channel(pane is self._wf_left)
        self._model.set_marker(kind, frame)
        self.refresh()
        self._update_status()

    def _on_pane_scrub(self, pane: SampleWaveformControl, frame: int) -> None:
        self._activate_split_channel(pane is self._wf_left)
        self._wf_left.scrub_frame = frame
        if self._model.has_stereo_pair:
            self._wf_right.scrub_frame = frame
        self._model.set_cursor_frame(frame)
        self._update_status()

    def _on_pane_moved(self, pane: SampleWaveformControl, delta: int) -> None:
        """Move-tool whole-waveform drag. Deliberately does NOT activate the dragged pane first (a move-drag must
        not reset undo history as a side effect). The scrub line placed before the drag survives it: the channel's
        frame count changes, which the pane treats as 'a different sample loaded' and clears the scrub."""
        m = self._model
        pane_is_active = (pane is self._wf_left) == m.is_primary_left_channel
        ls, rs = self._wf_left.scrub_frame, self._wf_right.scrub_frame
        m.apply_channel_move(not pane_is_active, delta)
        self.refresh()
        self._wf_left.scrub_frame, self._wf_right.scrub_frame = ls, rs
        self._update_status()

    def _on_pane_channel_double_clicked(self) -> None:
        m = self._model
        if m.has_stereo_pair and m.split_lr:
            m.split_both_active = True
        self.refresh()
        self._update_status()

    def _on_cursor_moved(self, frame: int) -> None:
        self._wf_left.scrub_frame = frame
        if self._model.has_stereo_pair:
            self._wf_right.scrub_frame = frame

    # ── view sync (zoom / pan) ─────────────────────────────────────────────────────────────

    def _sync_views(self, source: SampleWaveformControl) -> None:
        """Keeps the ruler, the scrollbar AND the other stereo pane on the same time window as whichever pane just
        zoomed/panned; the guard stops the mirrored set_view from calling back in."""
        if self._syncing_views:
            return
        self._syncing_views = True
        try:
            if self._model.has_stereo_pair:
                self._other(source).set_view(source.view_start_frame, source.view_end_frame)
            self._ruler.set_view(source.view_start_frame, source.view_end_frame, self._model.sample_rate)
            frame_count = source.view_span_frame_count
            view_len = max(1, source.view_end_frame - source.view_start_frame)
            sb = self._hscroll
            sb.setMinimum(0)
            sb.setMaximum(max(0, frame_count - view_len))
            sb.setPageStep(view_len)
            sb.setValue(source.view_start_frame)
            sb.setVisible(frame_count > view_len)
        finally:
            self._syncing_views = False

    def _on_hscroll(self, value: int) -> None:
        if self._syncing_views:
            return
        w = self._wf_left
        view_len = max(1, w.view_end_frame - w.view_start_frame)
        w.set_view(value, value + view_len)

    def on_zoom_in(self) -> None:
        self._zoom_by(0.5)

    def on_zoom_out(self) -> None:
        self._zoom_by(2.0)

    def _zoom_by(self, factor: float) -> None:
        w = self._wf_left
        total = w.frame_count
        if total == 0:
            return
        view_len = max(1, w.view_end_frame - w.view_start_frame)
        centre = w.view_start_frame + view_len // 2
        new_len = max(1, min(int(view_len * factor), total))
        w.set_view(centre - new_len // 2, centre - new_len // 2 + new_len)

    def on_zoom_selection(self) -> None:
        m = self._model
        if m.selection_end_frame <= m.selection_start_frame:
            self._status.setText("Select a range in the waveform first.")
            return
        self._wf_left.set_view(m.selection_start_frame, m.selection_end_frame)

    def on_zoom_fit(self) -> None:
        self._wf_left.set_view(0, max(1, self._wf_left.frame_count or 1))

    # ── playback ───────────────────────────────────────────────────────────────────────────

    def toggle_playback(self) -> None:
        def go() -> None:
            if self._model.is_playing:
                self._model.stop_playback()
            else:
                self._model.play_selected_sample()
        self._after(go)

    def _on_model_changed(self, name: str) -> None:
        if name in ("is_playing", "is_paused"):
            self.refresh()
            self._update_status()
            self._sync_playhead_pump()

    def _sync_playhead_pump(self) -> None:
        should = self._model.is_playing
        if should and not self._playhead_timer.isActive():
            self._playhead_timer.start()
        elif not should and self._playhead_timer.isActive():
            self._playhead_timer.stop()
            self._update_playhead()                      # one last tick to retire the line

    def _update_playhead(self) -> None:
        m = self._model
        frame = m.get_playback_frame() if (m.is_playing and m.playback_matches_selection) else -1
        self._wf_left.playhead_frame = frame
        self._wf_right.playhead_frame = frame
        if frame >= 0:
            self._follow_playhead(frame)

    def _follow_playhead(self, frame: int) -> None:
        """Page by a whole view-width when the playhead leaves a zoomed view — continuous recentring scrolls the
        trace under a fixed line (hard to read) and invalidates the cached trace every frame."""
        w = self._wf_left
        vs, ve = w.view_start_frame, w.view_end_frame
        vl = ve - vs
        if vl <= 0 or vl >= w.frame_count or vs <= frame < ve:
            return
        new_start = max(0, frame - vl // 8)
        w.set_view(new_start, new_start + vl)

    def _update_vu(self) -> None:
        self._vu_l.level = self._model.get_playback_level_left()
        self._vu_r.level = self._model.get_playback_level_right()

    # ── menus ──────────────────────────────────────────────────────────────────────────────

    def _on_file_menu_opened(self) -> None:
        # Recomputed at display time rather than pushed from every state change: that would mean auditing every
        # mutation site and silently missing one.
        m = self._model
        coll, ms, sample = m.has_active_collection, m.current_multisample_name is not None, m.has_sample_loaded
        for a, v in ((self._a_new_ms, coll), (self._a_new_pair, coll), (self._a_unload, coll),
                     (self._a_save_changes, m.has_unsaved_changes), (self._a_save_ms, ms), (self._a_save_sample, sample),
                     (self._a_push_sample, m.can_push_selected_sample), (self._a_push_ms, m.can_push_selected_multisample),
                     (self._a_import, ms), (self._a_import_stereo, ms), (self._a_new_zone, ms),
                     (self._a_export_sample, sample and (not m.sample_is_header_only or m.sample_is_linked_stub)),
                     (self._a_export_ms, ms), (self._a_export_coll, coll), (self._a_report, len(m.roots) > 0)):
            a.setEnabled(bool(v))

    def _on_edit_menu_opened(self) -> None:
        m = self._model
        audio = m.has_sample_loaded and not m.sample_is_header_only
        sel = audio and m.selection_end_frame > m.selection_start_frame
        for a, v in ((self._a_undo, m.can_undo), (self._a_redo, m.can_redo), (self._a_cut, sel), (self._a_copy, sel),
                     (self._a_paste, audio and SampleClipboard.has_content()), (self._a_select_all, audio),
                     (self._a_reverse, audio), (self._a_silence_sel, sel), (self._a_insert_silence, audio),
                     (self._a_dc, audio), (self._a_gain, audio), (self._m_zoom.menuAction(), audio),
                     (self._a_delete_zone, m.has_zone_selected and (len(m.current_multisample_zones or [])) > 1),
                     (self._a_rename_ms, m.current_multisample_name is not None), (self._a_rename_sample, m.has_sample_loaded),
                     (self._a_revert_ksc, m.has_active_collection), (self._a_revert_all, len(m.roots) > 0)):
            a.setEnabled(bool(v))

    def _fill_recent(self) -> None:
        self._m_recent.clear()
        recent = self._model.get_recent_files()
        if not recent:
            self._m_recent.addAction("(none)").setEnabled(False)
            return
        for p in recent:
            a = self._m_recent.addAction(p)
            a.triggered.connect(lambda _c=False, path=p: self._open_recent(path))
        self._m_recent.addSeparator()
        self._m_recent.addAction("C&lear All").triggered.connect(self._model.clear_recent_files)

    def _open_recent(self, p: str) -> None:
        if p.upper().endswith(".KSC"):
            self.open_collection_path(p)
        elif p.upper().endswith(".KMP"):
            self.open_kmp_path(p)

    def _on_tree_context_menu(self, pos: QPoint) -> None:
        item = self._tree.itemAt(pos)
        if item is None:
            return
        self._tree.setCurrentItem(item)
        node = item.data(0, Qt.ItemDataRole.UserRole)
        owning = self._model.find_owning_collection_path(node)
        if owning is None:
            return
        menu = QMenu(self)
        menu.addAction("New Collection (.KSC)...").triggered.connect(self.on_new_collection)
        menu.addAction("Open Collection (.KSC)...").triggered.connect(self.on_open_collection)
        menu.addSeparator()
        a = menu.addAction("Save Changes")
        a.setEnabled(self._model.has_unsaved_changes)
        a.triggered.connect(self.on_save_changes)
        menu.addAction("Save as...").triggered.connect(lambda: self.on_save_collection_as(owning))
        menu.addAction("Push to Instrument...").triggered.connect(self.on_push_collection)
        menu.addSeparator()
        menu.addAction("Close Collection").triggered.connect(lambda: self._unload_with_confirm(owning))
        menu.addAction("Close Editor").triggered.connect(self.close)
        menu.exec(self._tree.viewport().mapToGlobal(pos))

    def _on_wave_context_menu(self, pane: SampleWaveformControl, pos: QPoint) -> None:
        """Always opens (right-click must still reach Paste with no selection); selection-only items are disabled
        until a range exists."""
        m = self._model
        has_sel = m.selection_end_frame > m.selection_start_frame
        menu = QMenu(self)

        def add(text: str, slot: Callable, enabled: bool = True, parent: QMenu = menu) -> None:
            a = parent.addAction(text)
            a.setEnabled(enabled)
            a.triggered.connect(lambda _c=False: slot())

        add("Cu&t", self.on_cut, has_sel)
        add("&Copy", self.on_copy, has_sel)
        add("&Paste", self.on_paste, SampleClipboard.has_content())
        menu.addSeparator()
        add("&Undo", self.on_undo, m.can_undo)
        add("&Redo", self.on_redo, m.can_redo)
        menu.addSeparator()
        add("Cro&p to Selection", self.on_crop, has_sel)
        add("Fade &In", lambda: self._after(m.apply_fade_in_selection), has_sel)
        add("Fade &Out", lambda: self._after(m.apply_fade_out_selection), has_sel)
        add("&Silence", self.on_silence_selection, has_sel)
        add("&Normalize", self.on_normalize, has_sel)
        for title, vals in (("Amplify", (1, 3, 6)), ("Soften", (-1, -3, -6))):
            sub = menu.addMenu(title)
            sub.setEnabled(has_sel)
            for v in vals:
                add(f"{v:+d} dB", lambda d=v: self._after(lambda: m.apply_gain_adjust(float(d))), True, sub)
        menu.addSeparator()
        add("&Loop Selected Area", lambda: self._after(m.set_loop_from_selection), has_sel)
        menu.exec(pane.mapToGlobal(pos))

    def _gain_menu(self, button: QPushButton, presets) -> None:
        menu = QMenu(self)
        for v in presets:
            menu.addAction(f"{v:+d} dB").triggered.connect(
                lambda _c=False, d=v: self._after(lambda: self._model.apply_gain_adjust(float(d))))
        menu.exec(button.mapToGlobal(QPoint(0, button.height())))

    # ── file / collection handlers ─────────────────────────────────────────────────────────

    def on_open_collection(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open Collection", "", "Korg KSC Files (*.KSC *.ksc);;All Files (*)")
        if path:
            self.open_collection_path(path)

    def on_open_kmp(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open Multisample", "", "Korg KMP Files (*.KMP *.kmp);;All Files (*)")
        if path:
            self.open_kmp_path(path)

    def on_new_collection(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "New Collection", "", "Korg KSC Files (*.KSC);;All Files (*)")
        if path:
            self._model.new_collection(path)
            self._select_first_root()
            self._update_status()

    def on_save_collection_as(self, owning_path: str) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save Collection As", os.path.basename(owning_path),
                                              "Korg KSC Files (*.KSC);;All Files (*)")
        if not path:
            return
        self._model.save_collection_as(path)
        new_root = next((r for r in self._model.roots if r.collection_ref is not None
                         and os.path.normcase(r.collection_ref[1]) == os.path.normcase(path)), None)
        if new_root is not None:
            self._select_tree_node(new_root)
        self.refresh()
        self._update_status()

    def on_new_multisample(self) -> None:
        name = prompt_text(self, "New Multisample", f"Multisample name ({KRONOS_NAME_MAX_LENGTH} char max):",
                           "NewMS", KRONOS_NAME_MAX_LENGTH)
        if not name or not name.strip():
            return
        id_text = prompt_text(self, "New Multisample", "Multisample ID # (0-3999):", "0")
        try:
            mno1 = min(int(id_text), 3999) if id_text is not None else 0
        except ValueError:
            mno1 = 0
        node = self._model.new_multisample_in_collection(name, max(0, mno1))
        self.refresh()
        self._update_status()
        if node is not None:
            self._select_tree_node(node)

    def on_new_stereo_pair(self) -> None:
        name = prompt_text(self, "New Stereo Pair", f"Stereo pair base name (no -L/-R, {KRONOS_NAME_MAX_LENGTH} char max):",
                           "NewStereoMS", KRONOS_NAME_MAX_LENGTH)
        if not name or not name.strip():
            return
        id_text = prompt_text(self, "New Stereo Pair", "Left multisample ID # (0-3998; Right uses ID+1):", "0")
        try:
            mno1 = min(int(id_text), 3998) if id_text is not None else 0
        except ValueError:
            mno1 = 0
        node = self._model.new_stereo_multisample_pair_in_collection(name, max(0, mno1))
        self.refresh()
        self._update_status()
        if node is not None:
            self._select_tree_node(node)

    def on_create_multisample(self) -> None:
        m = self._model
        if not m.has_active_collection:
            self._status.setText("Open or create a collection first.")
            return
        preview = m.next_free_mno1()
        dlg = CreateMultisampleDialog(preview, self)
        if dlg.exec() != CreateMultisampleDialog.DialogCode.Accepted:
            return
        # A stereo pair needs TWO contiguous free slots; the previewed (mono) slot can collide with a still-taken
        # neighbour, so it is recomputed with the real count. The title may differ — accepted, not worth a second
        # round-trip through the dialog.
        slot = m.next_free_mno1(2) if dlg.stereo else preview
        base = f"NEWMS{slot:03d}"
        node = (m.new_stereo_multisample_pair_in_collection(base, slot) if dlg.stereo
                else m.new_multisample_in_collection(base, slot))
        self.refresh()
        self._update_status()
        if node is not None:
            self._select_tree_node(node)

    def on_delete_multisample(self) -> None:
        label = self._model.selected_multisample_label
        if label is None:
            self._status.setText("Select a multisample first.")
            return
        if not confirm(self, "Delete Multisample", f"Permanently delete '{label}' and all of its samples from disk?\n"
                                                    "This cannot be undone."):
            return
        self._after(self._model.delete_selected_multisample)

    def on_save_multisample(self) -> None:
        self._after(self._model.save_selected_multisample)

    def on_save_sample(self) -> None:
        self._after(self._model.save_selected_sample)

    def on_save_changes(self) -> None:
        # A value typed but not yet committed would be missed: moving focus off the field first commits it.
        if isinstance(QApplication.focusWidget(), QLineEdit):
            self._btn_save.setFocus()
        self._after(self._model.save_all_changes)

    def on_unload_active(self) -> None:
        path = self._model.active_collection_path
        if path is None:
            self._status.setText("No collection is open to unload.")
            return
        self._unload_with_confirm(path)

    def _unload_with_confirm(self, ksc_path: str) -> None:
        if self._model.has_unsaved_changes and not confirm(
                self, "Unload Collection",
                f"There may be unsaved changes this session. Unload '{os.path.basename(ksc_path)}' anyway?"):
            return
        self._after(lambda: self._model.unload_collection(ksc_path))

    def on_revert_ksc(self) -> None:
        path = self._model.active_collection_path
        if path is None:
            self._status.setText("No collection is open to revert.")
            return
        if confirm(self, "Revert KSC Changes",
                   f"Discard all unsaved changes in '{os.path.basename(path)}' and reload it from disk?"):
            self._after(self._model.revert_active_collection_changes)

    def on_revert_all(self) -> None:
        if confirm(self, "Revert ALL Changes", "Close every open collection/multisample and start fresh, discarding "
                                                "any unsaved changes? Nothing already saved to disk is affected."):
            self._after(self._model.revert_all_changes)

    # ── remote ─────────────────────────────────────────────────────────────────────────────

    def _remote_source(self) -> Optional[KronosRemoteSampleSource]:
        if not self._host:
            self._status.setText("No instrument host configured - set one from the main window first.")
            return None
        return KronosRemoteSampleSource(self, self._host, self._ftp_port, self._user, self._pass)

    def on_pull_collection(self) -> None:
        src = self._remote_source()
        if src is None:
            return
        self._model.pull_collection_from_kronos(src)
        self._select_first_root()
        self.refresh()
        self._update_status()

    def on_pull_multisample(self) -> None:
        src = self._remote_source()
        if src is None:
            return
        self._model.pull_multisample_from_kronos(src)
        self._select_first_root()
        self.refresh()
        self._update_status()

    def on_push_sample(self) -> None:
        src = self._remote_source()
        if src is not None:
            self._model.push_selected_sample(src)
            self._update_status()

    def on_push_multisample(self) -> None:
        src = self._remote_source()
        if src is not None:
            self._model.push_selected_multisample(src)
            self._update_status()

    def on_push_collection(self) -> None:
        src = self._remote_source()
        if src is not None:
            self._model.push_collection_to_kronos(src)
            self._update_status()

    # ── import / export ────────────────────────────────────────────────────────────────────

    def _prompt_zone_keys(self) -> Optional[Tuple[int, int]]:
        o = prompt_text(self, "Import", "Original key (note name, e.g. C4):", "C4")
        if o is None:
            return None
        orig = try_parse(o)
        orig = 60 if orig is None else orig
        t = prompt_text(self, "Import", "Top key (note name, e.g. C4):", to_name(orig))
        if t is None:
            return None
        top = try_parse(t)
        return orig, (orig if top is None else top)

    def on_import_audio(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import Audio", "", AUDIO_FILTER)
        if not path:
            return
        keys = self._prompt_zone_keys()
        if keys is None:
            return
        self._after_keep_multisample(lambda: self._model.import_audio_as_new_zone(path, *keys))

    def on_import_stereo_audio(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import Audio as Stereo Pair", "", AUDIO_FILTER)
        if not path:
            return
        keys = self._prompt_zone_keys()
        if keys is None:
            return
        self._after_keep_multisample(lambda: self._model.import_stereo_audio_as_new_zone_pair(path, *keys))

    def on_new_zone_from_ksf(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "New Zone from Existing Sample", "", "Korg KSF Files (*.KSF *.ksf);;All Files (*)")
        if not path:
            return
        keys = self._prompt_zone_keys()
        if keys is None:
            return
        self._after_keep_multisample(lambda: self._model.add_zone_from_existing_ksf(path, *keys))

    def on_add_zone(self) -> None:
        kmp = self._model.add_placeholder_zone()
        index = self._model.last_added_zone_index
        self.refresh()
        self._update_status()
        self._reselect(kmp, index)

    def on_import_sample_into_zone(self) -> None:
        zone = self._model.selected_zone
        if zone is None:
            self._update_status()
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "Import Sample(s)", "", AUDIO_FILTER)
        if not paths:
            return
        kmp = self._model.import_sample_into_zone(zone, paths)
        index = self._model.last_imported_zone_index
        self._repo_cache.clear()
        self.refresh()
        self._update_status()
        self._reselect(kmp, index)

    def on_delete_zone(self) -> None:
        zone = self._model.selected_zone
        if zone is None:
            self._update_status()
            return
        label = "(skipped)" if zone.is_skipped else zone.filename
        if not confirm(self, "Delete Zone", f"Delete zone '{label}' from the keymap?\n"
                                            "The underlying sample is not deleted - only the zone entry itself."):
            return
        kmp = self._model.delete_zone_completely()
        index = self._model.last_deleted_zone_index
        self.refresh()
        self._update_status()
        self._reselect(kmp, index)

    def on_remove_sample(self) -> None:
        if not self._model.has_sample_loaded:
            self._update_status()
            return
        label = self._model.sample_name or "this sample"
        if not confirm(self, "Remove Sample", f"Permanently remove '{label}' from the session?\nThe underlying .KSF "
                       "file(s) - including any matching repository copy - will be deleted from disk. This cannot be undone."):
            return
        self._after(self._model.remove_selected_sample)

    def on_rename_multisample(self) -> None:
        current = self._model.current_multisample_bare_name
        if current is None:
            return
        name = prompt_text(self, "Rename Multisample", f"New multisample name ({KRONOS_NAME_MAX_LENGTH} char max):",
                           current, KRONOS_NAME_MAX_LENGTH)
        if not name or not name.strip():
            return
        self._model.rename_selected_multisample(name)
        self._update_status()
        # A rename inside a collection rebuilds the tree, throwing away the selected node; re-select its replacement
        # through this window's own path so the panels (and the first zone) come back instead of going blank.
        renamed = self._model.last_renamed_multisample_path
        node = find_multisample_node(self._model.roots, renamed) if renamed else None
        if node is not None:
            self._select_tree_node(node)
        else:
            self.refresh()

    def on_rename_sample(self) -> None:
        if not self._model.has_sample_loaded:
            return
        name = prompt_text(self, "Rename Sample", f"New sample name ({KRONOS_NAME_MAX_LENGTH} char max):",
                           self._model.current_sample_bare_name or "", KRONOS_NAME_MAX_LENGTH)
        if not name or not name.strip():
            return
        self._model.rename_selected_sample(name)
        self._repo_cache.clear()
        self.refresh()
        self._update_status()

    def on_export_sample(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export Sample to WAV", self._model.sample_name, "WAV Files (*.wav)")
        if path:
            self._model.export_selected_sample_to_wav(path)
            self._update_status()

    def on_export_multisample(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Export Multisample to Folder")
        if d:
            self._model.export_selected_multisample_to_folder(d)
            self._update_status()

    def on_export_collection(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Export Collection to Folder")
        if d:
            self._model.export_collection_to_folder(d)
            self._update_status()

    def on_report(self) -> None:
        self._report = SampleReportWindow(self._model.build_normalization_report(), self)
        self._report.show()

    # ── edit handlers ──────────────────────────────────────────────────────────────────────

    def _reset_tempo_pitch(self) -> None:
        # Tempo/Pitch are a one-shot "apply this next" value, not persisted state: undo reverts the PCM but nothing
        # tells these boxes, so they'd keep claiming a change that is no longer there.
        self._tempo_box.setText("1.0")
        self._pitch_box.setText("0")

    def on_undo(self) -> None:
        self._after(lambda: (self._model.undo(), self._reset_tempo_pitch()))

    def on_redo(self) -> None:
        self._after(lambda: (self._model.redo(), self._reset_tempo_pitch()))

    def on_cut(self) -> None:
        self._after(self._model.cut_selection)

    def on_copy(self) -> None:
        self._model.copy_selection()
        self._update_status()

    def on_paste(self) -> None:
        self._after(self._model.paste_at_selection)

    def on_select_all(self) -> None:
        if not self._model.has_sample_loaded:
            return

        def go() -> None:
            self._model.selection_start_frame = 0
            self._model.selection_end_frame = self._model.sample_frame_count
        self._after(go)

    def on_reverse(self) -> None:
        self._after(self._model.apply_reverse)

    def on_silence_selection(self) -> None:
        self._after(self._model.apply_silence_selection)

    def on_remove_dc(self) -> None:
        self._after(self._model.apply_dc_offset_removal)

    def on_crop(self) -> None:
        self._after(self._model.apply_crop)

    def on_normalize(self) -> None:
        self._after(lambda: self._model.apply_normalize())

    def on_insert_silence(self) -> None:
        m = self._model
        if not m.has_sample_loaded:
            self._status.setText("No sample loaded.")
            return
        dlg = InsertSilenceDialog(m.sample_rate, max(1, m.sample_rate // 4), m.has_stereo_pair, self)
        if dlg.exec() != InsertSilenceDialog.DialogCode.Accepted:
            return
        self._after(lambda: m.apply_insert_silence(dlg.frames, dlg.apply_to_left, dlg.apply_to_right))

    def on_gain_dialog(self) -> None:
        if not self._model.has_sample_loaded:
            self._status.setText("No sample loaded.")
            return
        text = prompt_text(self, "Gain", "Gain change in dB (negative to attenuate):", "0")
        if text is None:
            return
        try:
            db = float(text)
        except ValueError:
            self._status.setText("That isn't a number of decibels.")
            return
        if db == 0:
            self._status.setText("0 dB - nothing to apply.")
            return
        self._after(lambda: self._model.apply_gain_adjust(db))

    def on_tempo_pitch(self) -> None:
        msg = None
        try:
            tempo = float(self._tempo_box.text())
            if tempo <= 0:
                raise ValueError
        except ValueError:
            msg = f"'{self._tempo_box.text()}' isn't a usable tempo multiplier - using 1.0."
            tempo = 1.0
        try:
            pitch = float(self._pitch_box.text())
        except ValueError:
            msg = f"'{self._pitch_box.text()}' isn't a number of semitones - using 0."
            pitch = 0.0
        if msg:
            self._status.setText(msg)
        self._model.apply_tempo_pitch(tempo, pitch)
        self._reset_tempo_pitch()
        self.refresh()
        self._update_status()

    # ── keys, drag & drop, close ───────────────────────────────────────────────────────────

    def eventFilter(self, obj, ev) -> bool:
        from PySide6.QtCore import QEvent
        if ev.type() != QEvent.Type.KeyPress or not self.isActiveWindow():
            return False
        mods = ev.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        key = ev.key()
        if key == Qt.Key.Key_Plus and (mods & Qt.KeyboardModifier.ControlModifier):
            mods, key = Qt.KeyboardModifier.ControlModifier, Qt.Key.Key_Equal     # '+' is Shift+= on most layouts
        # Ctrl chords stay live even in a text box: Ctrl+Z must mean the app's Undo everywhere, and Ctrl+S must work
        # while a field still has focus — that is exactly when a user reaches for it.
        if mods == Qt.KeyboardModifier.ControlModifier:
            table = {Qt.Key.Key_Z: self.on_undo, Qt.Key.Key_Y: self.on_redo, Qt.Key.Key_S: self.on_save_changes,
                     Qt.Key.Key_O: self.on_open_collection, Qt.Key.Key_Equal: self.on_zoom_in,
                     Qt.Key.Key_Minus: self.on_zoom_out, Qt.Key.Key_0: self.on_zoom_fit}
            if key in table:
                table[key]()
                return True
        # Space / Delete / clipboard chords / Home / End only act when focus is NOT in a text box.
        if isinstance(QApplication.focusWidget(), QLineEdit):
            return False
        if mods == Qt.KeyboardModifier.ControlModifier:
            table = {Qt.Key.Key_A: self.on_select_all, Qt.Key.Key_X: self.on_cut, Qt.Key.Key_C: self.on_copy,
                     Qt.Key.Key_V: self.on_paste}
            if key in table:
                table[key]()
                return True
        if mods == Qt.KeyboardModifier.NoModifier:
            if key == Qt.Key.Key_Home:
                self._after(self._model.transport_locate_start)
                return True
            if key == Qt.Key.Key_End:
                self._after(self._model.transport_locate_end)
                return True
            if key == Qt.Key.Key_Space:
                self.toggle_playback()
                return True
            if key == Qt.Key.Key_Delete:
                # "Remove the highlighted range" whenever one exists — routing this to Delete Zone regardless of a
                # selection silently discarded the WHOLE sample.
                m = self._model
                if m.has_sample_loaded and m.selection_end_frame > m.selection_start_frame:
                    self.on_cut()
                else:
                    self.on_delete_zone()
                return True
        return False

    def dragEnterEvent(self, e) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e) -> None:
        urls = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if not urls:
            return
        # One file per drop: importing several would need one key-range prompt pair EACH.
        path = urls[0]
        ext = os.path.splitext(path)[1].lower()
        if ext == ".ksc":
            self.open_collection_path(path)
        elif ext == ".kmp":
            self.open_kmp_path(path)
        elif ext in DROPPABLE_AUDIO:
            keys = self._prompt_zone_keys()
            if keys is not None:
                self._after_keep_multisample(lambda: self._model.import_audio_as_new_zone(path, *keys))
        else:
            self._status.setText(f"Don't know what to do with '{os.path.basename(path)}' - drop a .KSC, .KMP, or an audio file.")

    def closeEvent(self, e) -> None:
        if self._model.has_unsaved_changes and not confirm(
                self, "Unsaved Changes", "There are unsaved changes in the Sample Editor. Close anyway and discard them?"):
            e.ignore()
            return
        QApplication.instance().removeEventFilter(self)
        self._vu_timer.stop()
        self._playhead_timer.stop()
        self._model.stop_playback()
        self._model.dispose()
        super().closeEvent(e)
