"""Standalone Effect Preview Panel — test effects against real hardware.

Usage:
    python tests/manual_effect_preview.py              # load a WAV file
    python tests/manual_effect_preview.py <file.wav>    # specific file
    python tests/manual_effect_preview.py --list-devices

Requires: PySide6, sounddevice, numpy.
"""
from __future__ import annotations
import sys
import os
import wave
import struct
import numpy as np

# ── Qt bootstrap ────────────────────────────────────────────────────────────
os.environ["QT_QPA_PLATFORM"] = "offscreen"  # allow headless import

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QSlider, QCheckBox, QFileDialog, QGroupBox,
    QMessageBox, QComboBox
)
from PySide6.QtCore import Qt, Signal, QThread, QTimer, QObject
from PySide6.QtMultimedia import QAudioOutput

# ── Project imports ─────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from Views.effect_preview_panel import EffectPreviewPanel, EffectControlWidget
from Core.audio_effects import get_audio_effects, AudioEffectChain
from Core.sample_playback import list_playback_devices


# ── Audio provider thread ───────────────────────────────────────────────────
class AudioPlaybackThread(QThread):
    """Plays raw int16 numpy arrays through sounddevice."""

    finished = Signal()

    def __init__(self, samples: np.ndarray, sample_rate: int,
                 channels: int, device_index: int | None = None):
        super().__init__()
        self.samples = samples
        self.sample_rate = sample_rate
        self.channels = channels
        self.device_index = device_index
        self._stop_flag = False

    def run(self):
        import sounddevice as sd
        try:
            stream = sd.OutputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype='int16',
                device=self.device_index,
                blocksize=512,
            )
            chunk_size = 4096
            i = 0
            while i < len(self.samples) and not self._stop_flag:
                end = min(i + chunk_size, len(self.samples))
                chunk = self.samples[i:end]
                stream.write(chunk)
                i = end
            stream.stop()
            stream.close()
        except Exception as e:
            print(f"Playback error: {e}")
        finally:
            self.finished.emit()

    def stop(self):
        self._stop_flag = True
        self.wait()


# ── Main window ─────────────────────────────────────────────────────────────
class EffectTestWindow(QMainWindow):
    """Full effect preview test window."""

    def __init__(self, wav_path: str | None = None):
        super().__init__()
        self.setWindowTitle("Effect Preview Test")
        self.resize(700, 600)
        self._provider: RawAudioProvider | None = None
        self._thread: AudioPlaybackThread | None = None
        self._samples: np.ndarray | None = None
        self._sample_rate: int = 48000
        self._channels: int = 1
        self._device_index: int | None = None

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(10)

        # ── Top row: file + device ────────────────────────────────────────
        top = QHBoxLayout()
        self.lbl_file = QLabel("No file loaded")
        self.lbl_file.setStyleSheet("font-weight:bold")
        top.addWidget(self.lbl_file)
        top.addStretch()

        top.addWidget(QLabel("Output device:"))
        self.cmb_device = QComboBox()
        self._refresh_devices()
        self.cmb_device.currentIndexChanged.connect(self._on_device_changed)
        top.addWidget(self.cmb_device)
        top.addStretch()
        btn_refresh = QPushButton("Refresh")
        btn_refresh.clicked.connect(self._refresh_devices)
        top.addWidget(btn_refresh)
        main_layout.addLayout(top)

        # ── Transport controls ────────────────────────────────────────────
        transport = QHBoxLayout()
        self.btn_load = QPushButton("Load WAV / KSF")
        self.btn_load.clicked.connect(self._load_file)
        transport.addWidget(self.btn_load)

        self.btn_play = QPushButton("Play")
        self.btn_play.clicked.connect(self._play)
        transport.addWidget(self.btn_play)

        self.btn_stop = QPushButton("Stop")
        self.btn_stop.clicked.connect(self._stop)
        transport.addWidget(self.btn_stop)
        main_layout.addLayout(transport)

        # ── Volume slider ─────────────────────────────────────────────────
        vol_layout = QHBoxLayout()
        vol_layout.addWidget(QLabel("Volume:"))
        self.sld_volume = QSlider(Qt.Orientation.Horizontal)
        self.sld_volume.setMinimum(0)
        self.sld_volume.setMaximum(100)
        self.sld_volume.setValue(80)
        self.sld_volume.valueChanged.connect(self._on_volume)
        vol_layout.addWidget(self.sld_volume)
        self.lbl_volume = QLabel("80")
        self.lbl_volume.setMaximumWidth(40)
        vol_layout.addWidget(self.lbl_volume)
        vol_layout.addStretch()
        main_layout.addLayout(vol_layout)

        # ── Effect preview panel ──────────────────────────────────────────
        self.effect_panel = EffectPreviewPanel()
        self.effect_panel.preview_enabled_changed.connect(self._on_preview_toggled)
        self.effect_panel.effects_changed.connect(self._reprocess)
        main_layout.addWidget(self.effect_panel)

        # ── Status ────────────────────────────────────────────────────────
        self.lbl_status = QLabel("Ready")
        self.lbl_status.setStyleSheet("color:gray")
        main_layout.addWidget(self.lbl_status)

        # Load file if requested on cmdline
        if wav_path:
            self._load_path(wav_path)

    # ── Device management ─────────────────────────────────────────────────
    def _refresh_devices(self):
        self.cmb_device.clear()
        devices = list_playback_devices()
        if not devices:
            self.cmb_device.addItem("(no devices)")
            return
        self.cmb_device.addItem("(default)")
        for name, _ in devices:
            short = name[:60] + ("…" if len(name) > 60 else "")
            self.cmb_device.addItem(short)
        # Remember selection
        idx = self.cmb_device.currentIndex()
        if 0 < idx < len(devices) + 1:
            self._device_index = idx - 1  # skip "(default)"

    def _on_device_changed(self, idx: int):
        self._device_index = idx - 1 if idx > 0 else None

    # ── File loading ──────────────────────────────────────────────────────
    def _load_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load audio file", "",
            "WAV files (*.wav);;KSF files (*.ksf);;All files (*.*)"
        )
        if not path:
            return
        self._load_path(path)

    def _load_path(self, path: str):
        self._stop()
        try:
            if path.lower().endswith('.ksf'):
                self._load_ksf(path)
            else:
                self._load_wav(path)
        except Exception as e:
            QMessageBox.critical(self, "Load error", str(e))
            return

        self.lbl_file.setText(f"Loaded: {os.path.basename(path)}")
        self.lbl_status.setText(
            f"{self._channels}ch, {self._sample_rate}Hz, "
            f"{len(self._samples)} samples ({len(self._samples)/self._sample_rate:.1f}s)"
        )

    def _load_wav(self, path: str):
        with wave.open(path, 'rb') as wf:
            self._sample_rate = wf.getframerate()
            self._channels = wf.getnchannels()
            data = wf.readframes(wf.getnframes())
        self._samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)
        if self._channels > 1:
            self._samples = self._samples.reshape(-1, self._channels)

    def _load_ksf(self, path: str):
        """Minimal KSF loader: reads RIFF header, extracts PCM data."""
        from Data.ksf_sample import KsfSample
        ks = KsfSample.from_file(path)
        pcm = ks.pcm
        self._sample_rate = pcm.sample_rate
        self._channels = pcm.channels
        self._samples = pcm.samples.astype(np.float32)
        # Convert to float32 range [-1, 1]
        if pcm.bits_per_sample == 16:
            self._samples = self._samples / 32768.0
        elif pcm.bits_per_sample == 24:
            self._samples = self._samples / 8388608.0
        elif pcm.bits_per_sample == 32:
            self._samples = self._samples.astype(np.float32) / 2147483648.0

    # ── Playback ──────────────────────────────────────────────────────────
    def _play(self):
        if self._samples is None:
            QMessageBox.warning(self, "No file", "Load an audio file first.")
            return
        self._stop()

        # Apply volume
        vol = self.sld_volume.value() / 100.0
        samples = self._samples * vol

        # Apply effects if preview enabled
        if self.effect_panel.is_preview_enabled():
            chain = self.effect_panel.get_effect_chain()
            if chain and chain.enabled:
                data = samples.astype(np.int16).tobytes()
                data = chain.process(data, self._sample_rate, self._channels)
                samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            else:
                samples = samples * 32768.0
        else:
            samples = samples * 32768.0

        # Ensure int16 range
        samples = np.clip(samples, -32768, 32767).astype(np.int16)

        self._thread = AudioPlaybackThread(
            samples, self._sample_rate, self._channels, self._device_index
        )
        self._thread.finished.connect(self._on_playback_finished)
        self._thread.start()
        self.btn_play.setEnabled(False)
        self.lbl_status.setText("Playing...")

    def _stop(self):
        if self._thread:
            self._thread.stop()
            self._thread = None
        self.btn_play.setEnabled(True)

    def _on_playback_finished(self):
        self.btn_play.setEnabled(True)
        self.lbl_status.setText("Playback finished — load another file or adjust effects")

    def _on_volume(self, val: int):
        self.lbl_volume.setText(str(val))

    def _on_preview_toggled(self, enabled: bool):
        if enabled:
            self.lbl_status.setText("Effect preview ON — adjust effects and press Play")
        else:
            self.lbl_status.setText("Effect preview OFF — dry signal")

    def _reprocess(self):
        """Reprocess currently loaded samples with updated effect params."""
        if self._samples is not None and self.effect_panel.is_preview_enabled():
            chain = self.effect_panel.get_effect_chain()
            if chain and chain.enabled:
                data = (self._samples * 32768.0).astype(np.int16).tobytes()
                data = chain.process(data, self._sample_rate, self._channels)
                self._samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


# ── Entry point ─────────────────────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser(description="Effect Preview Panel — test effects against real hardware")
    parser.add_argument("file", nargs="?", help="WAV or KSF file to load")
    parser.add_argument("--list-devices", action="store_true", help="List audio devices and exit")
    args = parser.parse_args()

    if args.list_devices:
        devices = list_playback_devices()
        if not devices:
            print("(no devices)")
        else:
            print("Audio playback devices:")
            for i, (name, _id) in enumerate(devices):
                print(f"  [{i}] {name}")
            print("  [default] system default")
        return

    app = QApplication(sys.argv)
    window = EffectTestWindow(args.file)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
