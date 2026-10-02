"""Sample Editor model package — a UI-independent port of ViewModels/SampleEditorViewModel.cs.

`core.py`     state, tree, open/unload/revert, selection, stereo partner, dirty registries, rename
`edit.py`     markers, fields, waveform effects, clipboard, undo/redo
`zones.py`    zone add/delete/move/reorder, import, link, assign, remove sample, multisample create/delete
`io.py`       save, export, Save As, remote pull/push
`play.py`     transport + piano-key playback
`model.py`    SampleEditorModel — the assembled class the window uses
"""

from Core.sample_editor_model.model import SampleEditorModel  # noqa: E402,F401
