"""Storage, audio backend, FTP closure and import-regression safety checks."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["QT_QPA_PLATFORM"] = "offscreen"

import numpy as np
from PySide6.QtWidgets import QApplication

import Core.sample_ftp as ftp
import Core.sample_playback as playback
from Data.ksc_collection import KscCollection
from Data.ksf_sample import KsfSample
import Models.storage as storage
from Rendering.vu_meter import AudioCapture


class PortabilitySafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_legacy_data_uses_application_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "app"
            (root / "Models").mkdir(parents=True)
            (root / "settings.json").write_text("{}", encoding="utf-8")
            user = Path(tmp) / "user"
            with patch.object(storage, "__file__", str(root / "Models" / "storage.py")), \
                    patch.object(storage, "_user_data_dir", return_value=user), \
                    patch.object(storage, "_data_dir_override", None), \
                    patch.dict(os.environ, {"KRONOS_DATA_DIR": ""}):
                resolved, notice = storage._resolve_data_dir()
                self.assertEqual(resolved, root)
                self.assertIsNone(notice)
                with patch.object(storage, "_write_probe", side_effect=lambda p: p != root):
                    resolved, notice = storage._resolve_data_dir()
                    self.assertEqual(resolved, user)
                    self.assertIsNotNone(notice)
                    with patch.object(storage, "_data_dir", return_value=user):
                        self.assertEqual(storage.legacy_data_dir(), root)

    def test_concurrent_atomic_writes_use_separate_staging_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "index.json"
            barrier = threading.Barrier(2)
            replace_lock = threading.Lock()
            errors = []
            fsync = os.fsync
            replace = os.replace

            def synchronized_fsync(fd):
                fsync(fd)
                barrier.wait(timeout=5)

            def write(data):
                try:
                    storage.atomic_write_bytes(target, data)
                except Exception as exc:
                    errors.append(exc)

            def serialized_replace(source, destination):
                with replace_lock:
                    return replace(source, destination)

            with patch.object(storage.os, "fsync", side_effect=synchronized_fsync), \
                    patch.object(storage.os, "replace", side_effect=serialized_replace):
                threads = [threading.Thread(target=write, args=(data,))
                           for data in (b"first" * 1000, b"second" * 1000)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(10)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertIn(target.read_bytes(), (b"first" * 1000, b"second" * 1000))
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

    def test_non_wasapi_outputs_can_be_selected(self):
        for api_name in ("ALSA", "Core Audio"):
            with self.subTest(api=api_name):
                devices = [
                    {"name": "Speakers", "hostapi": 0, "max_output_channels": 2},
                    {"name": "USB", "hostapi": 0, "max_output_channels": 2},
                ]
                sd = SimpleNamespace(
                    query_hostapis=lambda: [{"name": api_name}],
                    query_devices=lambda i=None: devices if i is None else devices[i],
                )
                with patch.dict(sys.modules, sounddevice=sd):
                    self.assertEqual(playback.list_playback_devices(),
                                     [("Speakers", "Speakers"), ("USB", "USB")])
                    p = playback.SamplePlayback()
                    p.output_device_id = "USB"
                    self.assertEqual(p._resolve_device(), (1, None))
                    p.output_device_id = "unplugged"
                    self.assertEqual(p._resolve_device(), (None, None))

    def test_wasapi_routing_is_preserved(self):
        apis = [{"name": "MME"}, {"name": "Windows WASAPI", "default_output_device": 1}]
        devices = [
            {"name": "Speakers", "hostapi": 0, "max_output_channels": 2},
            {"name": "Speakers", "hostapi": 1, "max_output_channels": 2},
        ]
        extra = object()
        sd = SimpleNamespace(
            query_hostapis=lambda: apis,
            query_devices=lambda i=None: devices if i is None else devices[i],
            WasapiSettings=lambda **kw: extra,
        )
        with patch.dict(sys.modules, sounddevice=sd):
            self.assertEqual(playback.list_playback_devices(), [("Speakers", "Speakers")])
            p = playback.SamplePlayback()
            p.output_device_id = "Speakers"
            self.assertEqual(p._resolve_device(), (1, extra))
            p.output_device_id = "unplugged"
            self.assertEqual(p._resolve_device(), (1, extra))

    def test_mono_capture_uses_one_channel(self):
        capture = AudioCapture("3")
        levels = []
        capture.levels_updated.connect(lambda l, r: levels.append((l, r)))
        requests = []

        class Stream:
            def __init__(self, **kwargs):
                requests.append(kwargs)

            def __enter__(self):
                requests[-1]["callback"](np.full((8, 1), 0.5), 8, None, None)
                capture._running = False
                return self

            def __exit__(self, *args):
                return False

        sd = SimpleNamespace(
            query_devices=lambda *args, **kwargs: {"max_input_channels": 1},
            InputStream=Stream,
        )
        with patch.dict(sys.modules, sounddevice=sd):
            capture.run()
        self.assertEqual(requests[0]["channels"], 1)
        self.assertEqual(requests[0]["device"], 3)
        self.assertEqual(len(levels), 1)
        self.assertAlmostEqual(levels[0][0], levels[0][1])
        self.assertFalse(capture._running)

    def test_capture_failure_is_reported(self):
        capture = AudioCapture("3")
        errors = []
        capture.capture_failed.connect(errors.append)
        sd = SimpleNamespace(
            query_devices=lambda *args, **kwargs: {"max_input_channels": 2},
            InputStream=lambda **kw: (_ for _ in ()).throw(RuntimeError("device unavailable")),
        )
        with patch.dict(sys.modules, sounddevice=sd), self.assertLogs("Rendering.vu_meter", "ERROR"):
            capture.run()
        self.assertFalse(capture._running)
        self.assertEqual(len(errors), 1)
        self.assertIn("device unavailable", errors[0])

    def test_bare_samples_transfer_and_honor_edit_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            collection_path = root / "Bank.KSC"
            content = root / "Bank"
            content.mkdir()
            collection = KscCollection(entries=["Loose.KSF"])
            collection.save(str(collection_path))
            sample = KsfSample(name="Loose", sample_rate=44100)
            sample.set_samples(np.array([1, 2, 3], dtype=np.int16))
            sample_path = content / "Loose.KSF"
            sample.save(str(sample_path))
            uploads = []
            with patch.object(ftp, "_upload_one",
                              side_effect=lambda f, l, r, p, errors: uploads.append(r)):
                self.assertEqual(ftp.push_closure(object(), str(collection_path), collection,
                                                 "/SSD2/Test"), [])
                self.assertIn("/SSD2/Test/Bank/Loose.KSF", uploads)
                uploads.clear()
                self.assertEqual(ftp.push_closure(object(), str(collection_path), collection,
                                                 "/SSD2/Test", only_ksf_paths=set()), [])
                self.assertEqual(uploads, ["/SSD2/Test/Bank.KSC"])
                uploads.clear()
                ftp.push_closure(object(), str(collection_path), collection, "/SSD2/Test",
                                 only_ksf_paths={str(sample_path)})
                self.assertIn("/SSD2/Test/Bank/Loose.KSF", uploads)

            requested = []

            def download(f, remote, local, mapping, progress):
                requested.append(remote)
                return (root / remote.removeprefix("/SSD2/Test/")).read_bytes()

            with patch.object(ftp, "_download_one", side_effect=download):
                _, _, failures = ftp.pull(object(), "/SSD2/Test/Bank.KSC", str(root / "pulled"))
            self.assertEqual(failures, [])
            self.assertIn("/SSD2/Test/Bank/Loose.KSF", requested)
            sample_path.unlink()
            with patch.object(ftp, "_upload_one"):
                failures = ftp.push_closure(object(), str(collection_path), collection, "/SSD2/Test")
            self.assertTrue(any("Loose.KSF" in error for error in failures))
            with patch.object(ftp, "_download_one", side_effect=download):
                _, _, failures = ftp.pull(object(), "/SSD2/Test/Bank.KSC", str(root / "pulled"))
            self.assertTrue(any("Loose.KSF" in error for error in failures))

    def test_real_collection_ftp_round_trip_includes_bare_samples(self):
        from fixture_paths import SAMPLE_FIXTURES
        fixture = Path(SAMPLE_FIXTURES) / "ANDRE_K2_73" / "samplesfeb28_25.KSC"
        collection = KscCollection.open(fixture.read_bytes())
        collection.path = str(fixture)
        bare = [entry for entry in collection.entries if entry.upper().endswith(".KSF")]
        self.assertTrue(bare, "hardware fixture must include standalone samples")

        class MemoryFtp:
            def __init__(self):
                self.files = {}
                self.dirs = set()

            def file_exists(self, path):
                return path in self.files or path in self.dirs

            def mkdir(self, path):
                self.dirs.add(path)

            def upload(self, local, remote):
                self.files[remote] = Path(local).read_bytes()

            def rename(self, old, new):
                self.files[new] = self.files.pop(old)

            def delete_file(self, path):
                del self.files[path]

            def download(self, remote, local):
                Path(local).write_bytes(self.files[remote])

        backend = MemoryFtp()
        self.assertEqual(ftp.push_closure(backend, str(fixture), collection, "/SSD2/Test"), [])
        with tempfile.TemporaryDirectory() as tmp:
            _, mapping, failures = ftp.pull(backend, "/SSD2/Test/" + fixture.name, tmp)
            self.assertEqual(failures, [])
            for entry in bare:
                remote = f"/SSD2/Test/{fixture.stem}/{entry}"
                local = ftp.local_path_for(tmp, remote)
                self.assertEqual(mapping[local], remote)
                self.assertEqual(Path(local).read_bytes(),
                                 (fixture.parent / fixture.stem / entry).read_bytes())

    def test_regress_fails_for_top_level_and_nested_import_errors(self):
        for name in ("Core.audio_engine", "Core.sample_editor_model.markers"):
            with self.subTest(module=name), tempfile.TemporaryDirectory() as tmp:
                code = (
                    "import importlib,runpy; from unittest.mock import patch\n"
                    "real=importlib.import_module\n"
                    "def load(name,*a,**k):\n"
                    f" if name=={name!r}: raise ImportError('regression sentinel')\n"
                    " return real(name,*a,**k)\n"
                    "with patch.object(importlib,'import_module',side_effect=load):\n"
                    f" runpy.run_path({str(ROOT / 'tests' / 'regress.py')!r},run_name='__main__')\n"
                )
                result = subprocess.run(
                    [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                    env=dict(os.environ, KRONOS_DATA_DIR=tmp), timeout=30)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("regression sentinel", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
