"""SampleEditorModel — the assembled Sample Editor view-model the window binds to."""
from Core.sample_editor_model.core import ModelCore
from Core.sample_editor_model.edit import EditMixin
from Core.sample_editor_model.io import IoMixin
from Core.sample_editor_model.markers import MarkerMixin, SampleMarkerKind  # noqa: F401
from Core.sample_editor_model.play import PlayMixin
from Core.sample_editor_model.zones import ZoneMixin


class SampleEditorModel(ModelCore, EditMixin, MarkerMixin, ZoneMixin, IoMixin, PlayMixin):
    def __init__(self, settings=None, save_settings=None):
        super().__init__(settings, save_settings)
        self._init_play()
