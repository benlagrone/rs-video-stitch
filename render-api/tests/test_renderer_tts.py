import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import renderer


def _setup_project(tmpdir: Path, api_value: str, voice: str = "custom-voice") -> Path:
    storage_root = tmpdir
    pid = "pid123"
    project_root = storage_root / "projects" / pid
    input_dir = project_root / "input"
    images_dir = input_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    (images_dir / "img001.png").write_bytes(b"fake")

    scenes = {
        "vid": {
            "voice": voice,
            "lang": "en",
            "api": api_value,
        },
        "scenes": [
            {
                "title": "",
                "images": ["img001.png"],
                "VO": "Hello world",
            }
        ],
    }
    scenes_path = input_dir / "scenes.json"
    scenes_path.write_text(json.dumps(scenes), encoding="utf-8")
    return storage_root


class RendererTTSTest(TestCase):
    def _common_patches(self):
        fake_run_outputs = []

        def fake_run(cmd, log=None):
            if cmd:
                target = Path(cmd[-1])
                if target.suffix:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(b"")
                    fake_run_outputs.append(target)

        patches = [
            mock.patch.object(renderer, "run", side_effect=fake_run),
            mock.patch.object(renderer, "ffprobe_duration", return_value=5.0),
            mock.patch.object(renderer, "DEFAULT_TTS_VOICE", "fallback-voice"),
            mock.patch.object(renderer, "DEFAULT_TTS_LANGUAGE", "en"),
            mock.patch.object(renderer, "DEFAULT_TTS_API", "xtts"),
        ]
        return patches

    def test_xtts_is_used_when_api_xtts(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage_root = Path(tmp)
            storage_root = _setup_project(storage_root, "xtts")
            xtts_calls = []
            azure_calls = []

            def fake_xtts(text, destination, *, voice=None, language=None, log=None):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"xtts")
                xtts_calls.append((text, destination, voice, language))
                return destination

            def fake_azure(text, destination, *, voice=None, language=None, log=None):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"azure")
                azure_calls.append((text, destination, voice, language))
                return destination

            patches = self._common_patches() + [
                mock.patch.object(renderer, "synthesize_xtts", side_effect=fake_xtts),
                mock.patch.object(renderer, "synthesize_azure", side_effect=fake_azure),
            ]

            for patch in patches:
                patch.start()
            try:
                result = renderer.render_project(
                    "pid123",
                    storage_root,
                    {},
                    "output.mp4",
                )
            finally:
                for patch in reversed(patches):
                    patch.stop()

            self.assertTrue(result.exists())
            self.assertEqual(result.name, "output.mp4")
            self.assertEqual(len(xtts_calls), 1)
            self.assertEqual(len(azure_calls), 0)

            text, destination, voice, language = xtts_calls[0]
            self.assertEqual(text, "Hello world")
            self.assertEqual(voice, "custom-voice")
            self.assertEqual(language, "en")
            self.assertTrue(destination.exists())

    def test_azure_is_used_when_api_azure(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage_root = Path(tmp)
            storage_root = _setup_project(
                storage_root,
                "azure",
                voice="en-US-AdamMultilingualNeural",
            )
            xtts_calls = []
            azure_calls = []

            def fake_xtts(text, destination, *, voice=None, language=None, log=None):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"xtts")
                xtts_calls.append((text, destination, voice, language))
                return destination

            def fake_azure(text, destination, *, voice=None, language=None, log=None):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"azure")
                azure_calls.append((text, destination, voice, language))
                return destination

            patches = self._common_patches() + [
                mock.patch.object(renderer, "synthesize_xtts", side_effect=fake_xtts),
                mock.patch.object(renderer, "synthesize_azure", side_effect=fake_azure),
                mock.patch.dict(
                    os.environ,
                    {"AZURE_TTS_VOICE": "en-US-AriaNeural"},
                    clear=False,
                ),
            ]

            for patch in patches:
                patch.start()
            try:
                result = renderer.render_project(
                    "pid123",
                    storage_root,
                    {},
                    "output.mp4",
                )
            finally:
                for patch in reversed(patches):
                    patch.stop()

            self.assertTrue(result.exists())
            self.assertEqual(result.name, "output.mp4")
            self.assertEqual(len(azure_calls), 1)
            self.assertEqual(len(xtts_calls), 0)

            text, destination, voice, language = azure_calls[0]
            self.assertEqual(text, "Hello world")
            self.assertEqual(voice, "en-US-AdamMultilingualNeural")
            self.assertEqual(language, "en")
            self.assertTrue(destination.exists())
